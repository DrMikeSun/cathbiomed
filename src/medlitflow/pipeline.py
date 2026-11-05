"""pipeline.py — 主调度模块。

职责：6 阶段顺序执行（collect → dedup → retrieval → fallback → enrich → output）
+ 3 个 post_hooks（merge → move → record）+ 3 个启动钩子（enrichment → archive → screening）
+ 断点续传 + 中断恢复 + output 导出 + Interactor 注入。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Protocol

import pandas as pd

from medlitflow import (
    annotator,
    archive,
    collectors,
    core,
    dedup,
    enrichment,
    fallback,
    query_translator,
    retrieval,
    robustness,
    screening,
    state,
)

__all__ = [
    "run_pipeline",
    "move_run_papers_to_library",
    "set_interactor",
    "get_interactor",
    "abandon_run",
]

_HEALTH_WAIT_TIMEOUT_SECONDS = 10.0

_DEFAULT_MODE = "intensive"
_VALID_MODES = frozenset({"intensive", "scan"})

_DEFAULT_MAX_HITS_THRESHOLD = 1000
_DEFAULT_SCAN_MAX_HITS = 100
_DEFAULT_FORCE_ROLLBACK = True
_DEFAULT_FORCE_ROLLBACK_TO_SCAN = False

_SAFE_RUN_NAME_RE = re.compile(r"[^0-9A-Za-z._-]+")
_RUN_NAME_COLLISION_LIMIT = 100
_RUN_NAME_TIMESTAMP_FMT = "%Y%m%dT%H%M%S"

_STAGE_ORDER = ("collect", "dedup", "retrieval", "fallback", "enrich", "output")
_STAGE_DEPS = {
    "collect":   (),
    "dedup":     ("collect",),
    "retrieval": ("dedup",),
    "fallback":  ("retrieval",),
    "enrich":    ("fallback",),
    "output":    ("collect", "dedup", "retrieval", "fallback", "enrich"),
}
_STAGE_OUTPUT_PATH = {
    "collect":   "interim/records_collected.parquet",
    "dedup":     "interim/records_deduplicated.parquet",
    "retrieval": "interim/records_retrieved.parquet",
    "fallback":  "interim/records_finalized.parquet",
    "enrich":    "interim/enrich/entity_papers.parquet",
    "output":    "output/records_final.parquet",
}
_NON_PIPELINE_STAGES = frozenset({"screening", "archive", "zotero-sync"})

_VALIDATION_FIELD = {
    "collect":   "collected_df",
    "dedup":     "dedup_df",
    "retrieval": "retrieval_df",
    "fallback":  "fallback_df",
    "enrich":    "entity_papers",
    "output":    "final_df",
}

_ENRICH_TABLE_NAMES = (
    "entity_papers",
    "entity_authors",
    "entity_institutions",
    "entity_journals",
    "relation_cites",
    "relation_authored",
    "relation_affiliated",
    "relation_published_in",
)

_OUTPUT_EXPORT_MODES = frozenset({"copy", "symlink", "none"})
_EXPORT_FILES = (
    "records_final.parquet",
    "summary_report.md",
    "run_metadata.json",
    "query_execution.json",
)

_FILE_INDEX_COLUMNS = [
    "canonical_id",
    "downloaded_file_path",
    "current_file_path",
    "file_last_moved_at",
    "file_retention_level",
    "trash_path",
    "deleted_at",
    "expire_at",
]

_PIPELINE_AUDIT_FILENAME = "pipeline_audit.jsonl"

_LIBRARY_FILES = ("records_final.parquet", "index.parquet", "file_index.parquet")

_PARTIAL_SUPPORTED_STAGES = frozenset({"retrieval"})

_HOOK_ORDER = ("merge", "move", "record")

class Interactor(Protocol):
    def prompt(self, text: str, choices: list[str]) -> str: ...
    def notify(self, level: str, message: str) -> None: ...
    def progress(self, stage: str, current: int, total: int) -> None: ...
    def log(self, message: str) -> None: ...


class NullInteractor:
    def prompt(self, text: str, choices: list[str]) -> str:
        return choices[0] if choices else ""

    def notify(self, level: str, message: str) -> None:
        pass

    def progress(self, stage: str, current: int, total: int) -> None:
        pass

    def log(self, message: str) -> None:
        pass

_run_started_at: str = ""
_health_check_future = None
_interactor: "Interactor | None" = None

def _now_iso() -> str:
    return core.now_iso()

def _atomic_write_parquet(df: pd.DataFrame, out_path: Path) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    try:
        df.to_parquet(tmp, index=False)
        os.replace(tmp, out_path)
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise

def _resolve_archive_root(config: dict) -> Path:
    archive_cfg = config.get("archive", {}) or {}

    strategies_root = str(
        archive_cfg.get("strategies_root", "") or ""
    ).strip()
    if strategies_root:
        return Path(strategies_root).parent

    consolidated_root = str(
        archive_cfg.get("consolidated_root", "") or ""
    ).strip()
    if consolidated_root:
        return Path(consolidated_root).parent

    return Path(config["project"]["output_root"]) / "archive"

def _normalize_query(query: str) -> str:
    """归一化检索式。保留圆括号作为语义边界：
    "(a AND b) OR c" 与 "a AND (b OR c)" 归一后必须不同。"""
    if not query:
        return ""
    text = str(query)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = text.replace('"', " ").replace("'", " ")
    text = text.replace("{", "(").replace("}", ")")
    text = text.lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text

def _derive_strategy_name(query: str, max_len: int = 60) -> str:
    normalized = _normalize_query(query)
    if not normalized:
        h = hashlib.sha256(str(query).encode("utf-8")).hexdigest()[:8]
        return f"query-{h}"

    sanitized = re.sub(r"[^\w\-()]", "-", normalized, flags=re.UNICODE)
    sanitized = re.sub(r"-+", "-", sanitized).strip("-")

    if not sanitized:
        h = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
        return f"query-{h}"

    if len(sanitized) > max_len:
        h = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
        sanitized = sanitized[:max_len].rstrip("-") + f"-{h}"

    return sanitized

def _resolve_queries(config: dict) -> list[dict]:
    injected = config.get("queries")
    if isinstance(injected, list) and injected:
        return injected
    return []

def _find_query_in_list(queries: list[dict], name: str) -> dict | None:
    if not name:
        return None
    for q in queries:
        if isinstance(q, dict) and q.get("name") == name:
            return q
    return None

def _collect_meta_path(run_root: Path) -> Path:
    return Path(run_root) / ".stage_done" / "collect.meta.json"


def _read_collect_meta(run_root: Path) -> dict | None:
    path = _collect_meta_path(run_root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return data

def _write_collect_meta(
    run_root: Path,
    effective_mode: str,
    original_mode: str,
) -> None:
    path = _collect_meta_path(run_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "stage": "collect",
        "effective_mode": str(effective_mode),
        "original_mode": str(original_mode),
        "downgraded": str(effective_mode) != str(original_mode),
        "written_at": _now_iso(),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)

def _init_interactor() -> None:
    global _interactor
    if _interactor is None:
        _interactor = NullInteractor()

def set_interactor(interactor: "Interactor | None") -> None:
    global _interactor
    _interactor = interactor

def get_interactor() -> "Interactor":
    _init_interactor()
    return _interactor

def _append_pipeline_audit(run_root: Path, event: str, **fields) -> None:
    audit_dir = Path(run_root) / "audit"
    entry = {"event": event, "timestamp": _now_iso(), **fields}
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        with (audit_dir / _PIPELINE_AUDIT_FILENAME).open(
            "a", encoding="utf-8",
        ) as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass

def _resolve_stages(stage: str | None) -> tuple[str, ...]:
    if stage is None:
        return _STAGE_ORDER
    if stage not in _STAGE_ORDER:
        raise ValueError(f"未知 stage：{stage}")
    return (stage,)

def _build_last_run_dates_for_collect(
    strategy_name: str,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for source in ("pubmed", "pmc", "europepmc", "crossref", "openalex"):
        d = archive.get_last_run_date(strategy_name, source)
        if d:
            result[source] = d
    return result

def _ensure_dep_available(
    dep_stage: str, run_root: Path, stage_state: dict,
) -> None:
    if dep_stage == "collect" and "collected_df" not in stage_state:
        _rehydrate_state("collect", run_root, stage_state)
    elif dep_stage == "dedup" and "dedup_df" not in stage_state:
        _rehydrate_state("dedup", run_root, stage_state)
    elif dep_stage == "retrieval" and "retrieval_df" not in stage_state:
        _rehydrate_state("retrieval", run_root, stage_state)
    elif dep_stage == "fallback" and "fallback_df" not in stage_state:
        _rehydrate_state("fallback", run_root, stage_state)
    elif dep_stage == "enrich" and "enrich_tables" not in stage_state:
        _rehydrate_state("enrich", run_root, stage_state)

def _get_validation_df(stage: str, stage_state: dict):
    key = _VALIDATION_FIELD.get(stage)
    if key is None:
        return None
    if stage == "enrich":
        return (stage_state.get("enrich_tables") or {}).get("entity_papers")
    return stage_state.get(key)

def _compute_input_count(stage: str, stage_state: dict) -> int:
    if stage == "collect":
        return 0
    df = _get_validation_df(_STAGE_DEPS[stage][0], stage_state)
    return len(df) if df is not None else 0

def _validate_done(
    run_root: Path, stage: str, done: dict | None,
) -> bool:
    if done is None:
        return False
    if done["format"] == "legacy":
        return True
    out_path = Path(done.get("output_path", ""))
    if not out_path.exists():
        return False
    expected_rows = int(done.get("output_rows", 0))
    if expected_rows > 0 and out_path.suffix == ".parquet":
        try:
            actual = len(pd.read_parquet(out_path))
            if actual != expected_rows:
                return False
        except Exception:  # noqa: BLE001
            return False
    return True

def _can_skip_stage(run_root: Path, stage: str) -> bool:
    return _validate_done(
        run_root, stage, robustness.read_stage_done(run_root, stage),
    )

def _resolve_stage_state(run_root: Path, stage: str) -> str:
    done = robustness.read_stage_done(run_root, stage)
    if _validate_done(run_root, stage, done):
        return "done"
    if stage in _PARTIAL_SUPPORTED_STAGES:
        pq = run_root / "interim" / f"{stage}.partial.parquet"
        if pq.exists():
            try:
                df = pd.read_parquet(pq)
                if len(df) > 0:
                    return "partial"
            except Exception:  # noqa: BLE001
                pass
    return "none"

def _mark_stage_done(
    run_root: Path, stage: str, out_path: Path, output_rows: int,
) -> None:
    checksum = "sha256:" + hashlib.sha256(out_path.read_bytes()).hexdigest()
    robustness.write_stage_done(
        run_root, stage, out_path, output_rows, checksum,
    )

def _rehydrate_state(
    stage: str, run_root: Path, stage_state: dict,
) -> None:
    if stage == "collect":
        p = run_root / _STAGE_OUTPUT_PATH["collect"]
        if not p.exists():
            raise RuntimeError(f"缺少 {stage} 输出：{p}")
        stage_state["collected_df"] = pd.read_parquet(p)
        meta = _read_collect_meta(run_root)
        if meta:
            saved = str(meta.get("effective_mode", "") or "")
            if saved in _VALID_MODES:
                stage_state["_effective_mode"] = saved
    elif stage == "dedup":
        p = run_root / _STAGE_OUTPUT_PATH["dedup"]
        if not p.exists():
            raise RuntimeError(f"缺少 {stage} 输出：{p}")
        stage_state["dedup_df"] = pd.read_parquet(p)
    elif stage == "retrieval":
        p = run_root / _STAGE_OUTPUT_PATH["retrieval"]
        if not p.exists():
            raise RuntimeError(f"缺少 {stage} 输出：{p}")
        stage_state["retrieval_df"] = pd.read_parquet(p)
    elif stage == "fallback":
        p = run_root / _STAGE_OUTPUT_PATH["fallback"]
        if not p.exists():
            raise RuntimeError(f"缺少 {stage} 输出：{p}")
        stage_state["fallback_df"] = pd.read_parquet(p)
    elif stage == "enrich":
        enrich_dir = run_root / "interim" / "enrich"
        tables: dict = {}
        for name in _ENRICH_TABLE_NAMES:
            p = enrich_dir / f"{name}.parquet"
            tables[name] = pd.read_parquet(p) if p.exists() else pd.DataFrame()
        stage_state["enrich_tables"] = tables
    elif stage == "output":
        p = run_root / _STAGE_OUTPUT_PATH["output"]
        if not p.exists():
            raise RuntimeError(f"缺少 {stage} 输出：{p}")
        stage_state["final_df"] = pd.read_parquet(p)

def _write_run_state(run_root: Path, state_dict: dict) -> None:
    path = Path(run_root) / ".run_state.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(state_dict, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)

def _read_run_state(run_root: Path) -> dict | None:
    path = Path(run_root) / ".run_state.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return data

def _update_run_state(run_root: Path, updates: dict) -> None:
    state = _read_run_state(run_root)
    if state is None:
        return
    state.update(updates)
    _write_run_state(run_root, state)

def _update_run_state_if_first_interrupt(
    run_root: Path, updates: dict,
) -> None:
    try:
        state = _read_run_state(run_root)
        if state is None:
            return
        if state.get("interrupt_at"):
            return
        state.update(updates)
        _write_run_state(run_root, state)
    except Exception:
        pass

def _ensure_health_check(config: dict, run_root: Path) -> None:
    global _health_check_future

    if _health_check_future is not None:
        try:
            result = _health_check_future.result(
                timeout=_HEALTH_WAIT_TIMEOUT_SECONDS
            )
        except TimeoutError:
            _append_pipeline_audit(
                run_root, "health_check_timeout",
                wait_seconds=_HEALTH_WAIT_TIMEOUT_SECONDS,
            )
            return
        except Exception as exc:  # noqa: BLE001
            _append_pipeline_audit(
                run_root, "health_check_future_error", error=str(exc),
            )
            result = None
        finally:
            _health_check_future = None
    else:
        result = robustness.startup_health_check(config)

    if result is None:
        return

    blockers = result.get("blockers") or []
    if blockers:
        raise RuntimeError(f"启动体检失败：{blockers}")

    warnings = result.get("warnings") or []
    if warnings:
        _append_pipeline_audit(
            run_root, "health_check_warning", count=len(warnings),
        )

def _set_health_check_future(future) -> None:
    global _health_check_future
    _health_check_future = future

def _resolve_mode(config: dict) -> str:
    raw = str(
        (config.get("pipeline", {}) or {}).get("mode", _DEFAULT_MODE)
        or _DEFAULT_MODE
    ).lower()
    return raw if raw in _VALID_MODES else _DEFAULT_MODE

def _apply_hit_threshold(
    df: pd.DataFrame, mode: str, config: dict, run_root: Path,
) -> tuple[pd.DataFrame, str]:
    pipeline_cfg = config.get("pipeline", {}) or {}

    if mode == "scan":
        limit = int(pipeline_cfg.get("scan_max_hits", _DEFAULT_SCAN_MAX_HITS))
        if len(df) > limit:
            _append_pipeline_audit(
                run_root, "scan_truncated",
                original=len(df), kept=limit,
                truncation_criterion="source_order",
            )
            return df.head(limit).reset_index(drop=True), "scan"
        return df, "scan"

    threshold = int(pipeline_cfg.get(
        "max_hits_threshold", _DEFAULT_MAX_HITS_THRESHOLD,
    ))
    if len(df) <= threshold:
        return df, "intensive"

    force_rollback = bool(pipeline_cfg.get(
        "force_rollback", _DEFAULT_FORCE_ROLLBACK,
    ))
    if not force_rollback:
        _append_pipeline_audit(
            run_root, "intensive_over_threshold",
            hits=len(df), threshold=threshold,
        )
        _interactor.notify(
            "warning",
            f"4 源合并后共 {len(df)} 条记录，"
            f"超过总检索上限 {threshold}；"
            f"force_rollback=false，继续执行（不截断）",
        )
        return df, "intensive"

    rollback_to_scan = bool(pipeline_cfg.get(
        "force_rollback_to_scan", _DEFAULT_FORCE_ROLLBACK_TO_SCAN,
    ))
    if rollback_to_scan:
        limit = int(pipeline_cfg.get("scan_max_hits", _DEFAULT_SCAN_MAX_HITS))
        _append_pipeline_audit(
            run_root, "intensive_rollback_to_scan",
            hits=len(df), threshold=threshold, kept=limit,
        )
        _interactor.notify(
            "warning",
            f"4 源合并后共 {len(df)} 条记录，"
            f"超过总检索上限 {threshold}；"
            f"自动降级为 scan（保留前 {limit} 条）",
        )
        return df.head(limit).reset_index(drop=True), "scan"

    _append_pipeline_audit(
        run_root, "intensive_rollback",
        hits=len(df), threshold=threshold,
    )
    raise RuntimeError(
        f"总检索上限检查失败：4 源合并后共 {len(df)} 条记录，"
        f"超过阈值 {threshold}"
        f"（参数：pipeline.max_hits_threshold）。\n"
        f"处理建议：\n"
        f"  1. 保留更多文献：在「设置 → 数据源」调高「总检索上限」\n"
        f"  2. 精炼检索式：增加关键词限定、时间范围或出版类型过滤\n"
        f"  3. 强制继续：将 config.pipeline.force_rollback 设为 false"
    )

_MAX_RUN_NAME_LEN = 100

def _sanitize_run_name(name: str) -> str:
    if not name:
        raise ValueError("run_name 不能为空")
    safe = _SAFE_RUN_NAME_RE.sub("_", name).strip("._")
    if not safe:
        raise ValueError(f"run_name 无有效字符：{name!r}")
    if len(safe) > _MAX_RUN_NAME_LEN:
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
        safe = f"{safe[:_MAX_RUN_NAME_LEN - 9]}_{digest}"
    return safe

def _resolve_run_name(
    config: dict, strategy_name: str, resume: bool = False,
) -> str:
    explicit = str(
        (config.get("project", {}) or {}).get("run_name", "") or ""
    ).strip()
    output_root = Path(config["project"]["output_root"])
    runs_dir = output_root / "runs"

    if explicit:
        safe = _sanitize_run_name(explicit)
        exists = (runs_dir / safe).exists()
        if resume:
            if not exists:
                raise core.ConfigError(
                    f"--resume 指定的 run_name 不存在：{safe}"
                )
            return safe
        if exists:
            raise core.ConfigError(f"config.project.run_name 冲突：{safe}")
        return safe

    if resume:
        prefix = _sanitize_run_name(strategy_name) + "_"
        candidates: list[os.DirEntry] = []
        if runs_dir.exists():
            with os.scandir(runs_dir) as it:
                for entry in it:
                    if not entry.is_dir():
                        continue
                    if entry.name.startswith(prefix):
                        candidates.append(entry)
        if not candidates:
            raise RuntimeError(
                f"--resume 未找到策略 {strategy_name} 的历史 run"
            )
        candidates.sort(key=lambda e: e.name, reverse=True)
        return candidates[0].name

    ts = datetime.fromisoformat(core.now_iso()).strftime(_RUN_NAME_TIMESTAMP_FMT)
    base = _sanitize_run_name(f"{strategy_name}_{ts}")
    if not (runs_dir / base).exists():
        return base
    for i in range(1, _RUN_NAME_COLLISION_LIMIT + 1):
        candidate = f"{base}_{i:02d}"
        if not (runs_dir / candidate).exists():
            return candidate
    raise RuntimeError(
        f"run 名冲突超过 {_RUN_NAME_COLLISION_LIMIT} 次：{base}"
    )

def _resolve_strategy_name(config: dict) -> str:
    name = str(
        (config.get("project", {}) or {}).get("strategy_name", "") or ""
    ).strip()
    if name:
        return name
    raise core.ConfigError(
        "config.project.strategy_name 未注入；"
        "cli 必须显式注入（单策略或批量循环）"
    )

def _is_non_interactive(config: dict) -> bool:
    return bool(
        (config.get("project", {}) or {}).get("non_interactive", False)
    )

def _quality_level(unsupported_count: int) -> str:
    if unsupported_count == 0:
        return "high"
    if unsupported_count <= 2:
        return "medium"
    return "low"

def _handle_q2_ask(unsupported: list[dict], run_root: Path) -> None:
    lines = ["检测到 PubMed 检索式包含以下不支持的语法：", ""]
    for i, item in enumerate(unsupported, start=1):
        lines.append(
            f"  {i}. 字段 [{item.get('field','')}]："
            f"术语 {item.get('term','')}（位置 {item.get('position',0)}）"
        )
        lines.append(f"     降级方案：{item.get('downgrade_to','')}")
    lines += ["", "是否降级执行？(y/n)"]

    choice = _interactor.prompt("\n".join(lines), ["y", "n"])
    if choice == "y":
        _append_pipeline_audit(
            run_root, "q2_ask_downgrade",
            unsupported_count=len(unsupported), non_interactive=False,
        )
        return
    if choice == "n":
        raise KeyboardInterrupt("用户拒绝降级执行")
    raise KeyboardInterrupt("用户取消降级询问（未响应或关闭对话框）")

def _prepare_query_audit_and_q2(
    config: dict, strategy_name: str, run_root: Path, mode: str,
) -> None:
    if mode == "scan":
        return

    strategy = _find_query_in_list(
        _resolve_queries(config), strategy_name
    ) or {}
    pubmed_query = str(strategy.get("pubmed", "") or "")
    if not pubmed_query:
        return

    unsupported = query_translator.detect_unsupported_syntax(pubmed_query)
    non_interactive = _is_non_interactive(config)

    if unsupported and not non_interactive:
        _handle_q2_ask(unsupported, run_root)
    elif unsupported:
        _append_pipeline_audit(
            run_root, "q2_ask_downgrade",
            unsupported_count=len(unsupported), non_interactive=True,
        )

    try:
        translated = query_translator.translate_pubmed_query(pubmed_query)
    except query_translator.DnfClauseLimitError as exc:
        raise RuntimeError(
            f"检索式无法翻译为 Crossref/OpenAlex 查询：{exc}"
        ) from exc

    quality = _quality_level(len(unsupported))
    rows = [
        {"source": "pubmed", "pubmed_query": pubmed_query,
         "translated_queries": translated.get("pubmed", ""),
         "quality_level": quality},
        {"source": "pmc", "pubmed_query": pubmed_query,
         "translated_queries": translated.get("pubmed", ""),
         "quality_level": quality},
        {"source": "europepmc", "pubmed_query": pubmed_query,
         "translated_queries": translated.get("europepmc", ""),
         "quality_level": quality},
        {"source": "crossref", "pubmed_query": pubmed_query,
         "translated_queries": json.dumps(
             translated.get("crossref", []), ensure_ascii=False),
         "quality_level": quality},
        {"source": "openalex", "pubmed_query": pubmed_query,
         "translated_queries": json.dumps(
             translated.get("openalex", []), ensure_ascii=False),
         "quality_level": quality},
    ]
    try:
        pd.DataFrame(rows).to_csv(
            run_root / "output" / "query_translation_audit.csv",
            index=False, encoding="utf-8-sig",
        )
    except OSError:
        pass

def _should_export(config: dict) -> bool:
    out_cfg = config.get("output", {}) or {}
    if not bool(out_cfg.get("auto_export", False)):
        return False
    if not str(out_cfg.get("user_dir", "") or "").strip():
        return False
    mode = str(out_cfg.get("export_mode", "symlink") or "symlink").lower()
    return mode in _OUTPUT_EXPORT_MODES and mode != "none"

def _export_output(
    run_root: Path, config: dict, strategy_name: str,
) -> None:
    out_cfg = config.get("output", {}) or {}
    user_dir_raw = str(out_cfg.get("user_dir", "") or "").strip()
    if not user_dir_raw:
        _append_pipeline_audit(
            run_root, "export_skipped", reason="empty_user_dir",
        )
        return
    user_dir = Path(user_dir_raw)
    mode = str(out_cfg.get("export_mode", "symlink") or "symlink").lower()
    if mode == "symlink" and os.name == "nt":
        mode = "copy"
    if mode not in _OUTPUT_EXPORT_MODES or mode == "none":
        _append_pipeline_audit(
            run_root, "export_skipped", reason=f"invalid_mode:{mode}",
        )
        return

    tpl = str(out_cfg.get("subdir_template", "{strategy}_{date}")
              or "{strategy}_{date}")
    run_date = _run_started_at[:10]
    subdir = tpl.replace("{strategy}", strategy_name).replace("{date}", run_date)
    target_dir = user_dir / subdir

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        src_dir = run_root / "output"
        copied = 0
        for name in _EXPORT_FILES:
            src = src_dir / name
            if not src.exists():
                continue
            dst = target_dir / name
            if dst.exists():
                _append_pipeline_audit(
                    run_root, "export_skip_existing",
                    mode=mode, target=str(dst), name=name,
                )
                continue
            if mode == "copy":
                shutil.copy2(src, dst)
            else:
                os.symlink(src.resolve(), dst)
            copied += 1
        _append_pipeline_audit(
            run_root, "export_done",
            mode=mode, target_dir=str(target_dir), files=copied,
        )
    except Exception as exc:  # noqa: BLE001
        _append_pipeline_audit(
            run_root, "export_failed", mode=mode, error=str(exc),
        )

def _load_library(library_root: Path) -> pd.DataFrame:
    path = Path(library_root) / "records_final.parquet"
    if not path.exists():
        return pd.DataFrame()
    return pd.read_parquet(path)

def _build_last_run_dates_for_record(
    config: dict, snapshot: pd.DataFrame,
) -> dict[str, str]:
    today = core.today_iso()
    sources_cfg = (config.get("retrieval", {}) or {}).get("sources", {}) or {}
    enabled = [
        src for src in ("pubmed", "pmc", "europepmc", "crossref", "openalex")
        if bool((sources_cfg.get(src, {}) or {}).get("enabled", True))
    ]

    if snapshot is None or len(snapshot) == 0:
        return {}
    if "source_databases" not in snapshot.columns:
        return {}

    observed: set[str] = set()
    for s in snapshot["source_databases"].astype(str):
        for part in s.split(";"):
            part = part.strip().lower()
            if part:
                observed.add(part)

    return {src: today for src in enabled if src in observed}

def _write_run_metadata_and_query_execution(
    run_root: Path, config: dict,
    strategy_name: str, run_name: str,
) -> None:
    output_dir = run_root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    strategy = _find_query_in_list(
        _resolve_queries(config), strategy_name
    ) or {}
    pubmed_query = str(strategy.get("pubmed", "") or "")

    last_pubmed = archive.get_last_run_date(strategy_name, "pubmed")

    metadata_path = output_dir / "run_metadata.json"
    existing_started_at = ""
    if metadata_path.exists():
        try:
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            existing_started_at = str(existing.get("started_at", "") or "")
        except (json.JSONDecodeError, OSError):
            existing_started_at = ""

    run_metadata = {
        "run_name": run_name,
        "started_at": existing_started_at or _run_started_at,
        "language": str(strategy.get("language", "") or ""),
        "date_range": str(strategy.get("date_range", "") or ""),
        "is_first_run": last_pubmed is None,
        "from_date": str(strategy.get("from_date", "") or ""),
        "to_date": str(strategy.get("to_date", "") or ""),
        "effective_max_results_per_source": (
            collectors.resolve_effective_limits(config)
        ),
    }
    (output_dir / "run_metadata.json").write_text(
        json.dumps(run_metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    translated = (
        query_translator.translate_pubmed_query(pubmed_query)
        if pubmed_query else {}
    )
    _pubmed_query_str = translated.get("pubmed", "")
    _raw_counts_by_source: dict[str, int] = {}
    _collected_path = run_root / "interim" / "records_collected.parquet"
    if _collected_path.exists():
        try:
            _collected_df = pd.read_parquet(
                _collected_path, columns=["source_database"],
            )
            if "source_database" in _collected_df.columns:
                _vc = (
                    _collected_df["source_database"]
                    .astype(str).value_counts()
                )
                _raw_counts_by_source = {
                    str(k): int(v) for k, v in _vc.to_dict().items()
                }
        except Exception:  # noqa: BLE001
            _raw_counts_by_source = {}

    query_execution = {
        "pubmed":    {"query": _pubmed_query_str, "filters": ""},
        "pmc":       {"query": _pubmed_query_str, "filters": ""},
        "europepmc": {"query": translated.get("europepmc", ""), "filters": ""},
        "crossref":  translated.get("crossref", []),
        "openalex":  translated.get("openalex", []),
        "raw_counts_by_source": _raw_counts_by_source,
    }
    (output_dir / "query_execution.json").write_text(
        json.dumps(query_execution, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def _run_post_hooks(
    run_root: Path, config: dict,
    strategy_name: str, run_name: str, library_root: Path,
    *, resume_from: str = "none",
) -> None:
    records_final_path = run_root / "output" / "records_final.parquet"
    txn_dir = library_root / ".txn" / run_name

    existing_state = _read_run_state(run_root) or {}
    existing_txn = existing_state.get("txn", {}) or {}

    def _is_txn_valid(txn_meta: dict) -> bool:
        backup_dir_raw = txn_meta.get("library_backup_dir", "")
        if not backup_dir_raw:
            return False
        backup_dir = Path(backup_dir_raw)
        if not backup_dir.exists():
            return False
        restore_files = txn_meta.get("restore_files", []) or []
        for fname in restore_files:
            if not (backup_dir / fname).exists():
                return False
        return True

    if _is_txn_valid(existing_txn):
        pass
    elif existing_txn.get("library_backup_dir"):
        raise RuntimeError(
            f"事务备份已损坏或缺失："
            f"{existing_txn.get('library_backup_dir')}；"
            f"请使用 --abandon 清理该 run，"
            f"或手动删除 runs/{run_name}/ 后重试"
        )
    else:
        txn_dir.mkdir(parents=True, exist_ok=True)
        restore_files_actual: list[str] = []
        for fname in _LIBRARY_FILES:
            src = library_root / fname
            if src.exists():
                shutil.copy2(src, txn_dir / fname)
                restore_files_actual.append(fname)

        archive_root = _resolve_archive_root(config)
        strategy_dir = archive_root / "strategies" / strategy_name
        run_metadata = json.loads(
            (run_root / "output" / "run_metadata.json").read_text("utf-8")
        )
        date = archive._parse_run_date(run_metadata, run_name)
        archive_run_dir = strategy_dir / "runs" / date / run_name
        lrd_path = strategy_dir / "last_run_dates.json"
        lrd_snapshot = {}
        if lrd_path.exists():
            try:
                lrd_snapshot = json.loads(lrd_path.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                lrd_snapshot = {}

        _update_run_state(run_root, {
            "post_hook_stage": "none",
            "txn": {
                "library_backup_dir": str(txn_dir),
                "restore_files": restore_files_actual,
                "moved_log": str(run_root / ".move_log.jsonl"),
                "archive_strategy_dir": str(strategy_dir),
                "archive_run_dir": str(archive_run_dir),
                "last_run_dates_snapshot": lrd_snapshot,
            },
        })

    start_idx = 0
    if resume_from in _HOOK_ORDER:
        start_idx = _HOOK_ORDER.index(resume_from)

    for hook_name in _HOOK_ORDER[start_idx:]:
        _update_run_state(run_root, {"post_hook_stage": hook_name})

        if hook_name == "merge":
            _interactor.notify("info", "post_hook 1: merge_into_library")
            try:
                _interactor.log(
                    f"post_hook 1: 准备合并 "
                    f"{len(pd.read_parquet(records_final_path))} 条记录"
                )
                run_df = pd.read_parquet(records_final_path)
                library_df = _load_library(library_root)
                merged = annotator.merge_into_library(
                    run_df, library_df, config=config,
                )
                library_root.mkdir(parents=True, exist_ok=True)
                tmp_records = library_root / "records_final.parquet.tmp"
                merged.to_parquet(tmp_records, index=False)
                os.replace(tmp_records, library_root / "records_final.parquet")
                annotator.update_library_index(library_root)
                annotator.write_summary_report(
                    run_df, run_root, library_root=library_root,
                )
                _interactor.log(
                    f"post_hook 1: library 现有 {len(merged)} 条记录"
                )
            except robustness.InterruptRequested:
                _update_run_state_if_first_interrupt(run_root, {
                    "interrupt_at": _now_iso(),
                    "interrupt_detail": (
                        robustness.get_shutdown_detail()
                        or "merge 阶段收到中断信号"
                    ),
                })
                raise
            except Exception as exc:  # noqa: BLE001
                _append_pipeline_audit(
                    run_root, "post_hook_1_failed", error=str(exc),
                )
                _update_run_state_if_first_interrupt(run_root, {
                    "interrupt_at": _now_iso(),
                    "interrupt_detail": (
                        f"merge 失败：{type(exc).__name__}: {exc}"
                    ),
                })
                raise

        elif hook_name == "move":
            _interactor.notify("info", "post_hook 2: move_run_papers_to_library")
            try:
                _interactor.log(
                    "post_hook 2: 开始移动 papers/inbox/ 到 library/inbox/"
                )
                move_run_papers_to_library(
                    run_root, library_root,
                    log_path=run_root / ".move_log.jsonl",
                )
                _interactor.log("post_hook 2: 文件移动完成")
            except robustness.InterruptRequested:
                _update_run_state_if_first_interrupt(run_root, {
                    "interrupt_at": _now_iso(),
                    "interrupt_detail": (
                        robustness.get_shutdown_detail()
                        or "move 阶段收到中断信号"
                    ),
                })
                raise
            except Exception as exc:  # noqa: BLE001
                _append_pipeline_audit(
                    run_root, "post_hook_2_failed", error=str(exc),
                )
                _update_run_state_if_first_interrupt(run_root, {
                    "interrupt_at": _now_iso(),
                    "interrupt_detail": (
                        f"move 失败：{type(exc).__name__}: {exc}"
                    ),
                })
                raise

        elif hook_name == "record":
            _interactor.notify("info", "post_hook 3: archive.record_run")
            _interactor.log("post_hook 3: 开始写入 archive 快照")

            max_record_retries = 3
            last_record_exc: Exception | None = None
            for record_attempt in range(1, max_record_retries + 1):
                try:
                    run_metadata = json.loads(
                        (run_root / "output" / "run_metadata.json")
                        .read_text("utf-8")
                    )
                    snapshot = pd.read_parquet(records_final_path)
                    last_run_dates = _build_last_run_dates_for_record(
                        config, snapshot,
                    )
                    archive.record_run(
                        strategy_name=strategy_name,
                        run_name=run_name,
                        run_metadata=run_metadata,
                        records_final_snapshot=snapshot,
                        last_run_dates=last_run_dates,
                    )
                    _interactor.log(
                        f"post_hook 3: archive 已记录 run {run_name}"
                        f"（尝试 {record_attempt}/{max_record_retries}）"
                    )
                    last_record_exc = None
                    break
                except robustness.InterruptRequested:
                    _update_run_state_if_first_interrupt(run_root, {
                        "interrupt_at": _now_iso(),
                        "interrupt_detail": (
                            robustness.get_shutdown_detail()
                            or "record 阶段收到中断信号"
                        ),
                    })
                    raise
                except Exception as exc:  # noqa: BLE001
                    last_record_exc = exc
                    if record_attempt < max_record_retries:
                        wait = 2 ** record_attempt
                        _interactor.notify(
                            "warning",
                            f"record 尝试 {record_attempt}/"
                            f"{max_record_retries} 失败："
                            f"{type(exc).__name__}: {exc}；"
                            f"{wait} 秒后重试",
                        )
                        core.interruptible_sleep(wait)
                        continue

            if last_record_exc is not None:
                _append_pipeline_audit(
                    run_root, "post_hook_3_failed",
                    error=str(last_record_exc),
                    attempts=max_record_retries,
                )
                _update_run_state_if_first_interrupt(run_root, {
                    "interrupt_at": _now_iso(),
                    "interrupt_detail": (
                        f"record 重试 {max_record_retries} 次后失败："
                        f"{type(last_record_exc).__name__}: {last_record_exc}"
                    ),
                })
                _interactor.notify(
                    "error",
                    f"archive.record_run 连续 {max_record_retries} 次失败："
                    f"{type(last_record_exc).__name__}: {last_record_exc}\n"
                    f"  · library 与 papers 已成功更新（数据完整）\n"
                    f"  · archive 快照缺失，下次增量检索可能重复抓取\n"
                    f"  · 事务备份（txn）已保留\n"
                    f"  · 处理选项：\n"
                    f"      --resume  从 record 断点重试（修复问题后）\n"
                    f"      --abandon 完整回滚 library + papers + archive\n"
                    f"  · 常见原因：磁盘空间不足 / archive 目录权限 / "
                    f"网络文件系统不可写"
                )
                raise

    _update_run_state(run_root, {"state": "committed"})
    (run_root / ".run_state.json").unlink(missing_ok=True)
    (run_root / ".move_log.jsonl").unlink(missing_ok=True)
    shutil.rmtree(txn_dir, ignore_errors=True)

def _run_startup_hooks(
    config: dict, library_root: Path,
    archive_root: Path, run_root: Path,
) -> None:
    """3 个启动钩子，顺序固定：enrichment → archive → screening """
    # -------- Hook A: enrichment --------
    try:
        enrichment.trigger_citation_enrichment_for_confirmed_strategies(
            library_root, config,
        )
        _append_pipeline_audit(run_root, "startup_hook_done", hook="A")
    except robustness.InterruptRequested:
        _append_pipeline_audit(run_root, "startup_hook_interrupted", hook="A")
        raise
    except Exception as exc:  # noqa: BLE001
        _append_pipeline_audit(
            run_root, "startup_hook_failed", hook="A", error=str(exc),
        )
        _interactor.notify("warning", f"启动钩子 A 失败：{exc}")

    # -------- Hook B: archive --------
    try:
        messages = archive.check_concurrent_strategies(archive_root)
        strong_count = 0
        for msg in messages:
            level = str(msg.get("level", "info"))
            if level == "strong_warning":
                level = "warning"
                strong_count += 1
            _interactor.notify(level, str(msg.get("message", "")))
        _append_pipeline_audit(
            run_root, "startup_hook_done", hook="B",
            strong_warning_count=strong_count,
        )
    except robustness.InterruptRequested:
        _append_pipeline_audit(run_root, "startup_hook_interrupted", hook="B")
        raise
    except Exception as exc:  # noqa: BLE001
        _append_pipeline_audit(
            run_root, "startup_hook_failed", hook="B", error=str(exc),
        )
        _interactor.notify("warning", f"启动钩子 B 失败：{exc}")

    # -------- Hook C: screening（条件）--------
    screening_cfg = config.get("screening", {}) or {}
    auto_cleanup = bool(screening_cfg.get(
        "auto_cleanup_trash_on_startup", True,
    ))
    if not auto_cleanup:
        _append_pipeline_audit(run_root, "startup_hook_skipped", hook="C")
        return
    try:
        result = screening.cleanup_trash(library_root, config)
        _append_pipeline_audit(
            run_root, "startup_hook_done", hook="C",
            deleted_count=int(result.get("deleted_count", 0)),
            freed_bytes=int(result.get("freed_bytes", 0)),
            deleted_ids_count=len(result.get("deleted_ids", [])),
        )
    except robustness.InterruptRequested:
        _append_pipeline_audit(run_root, "startup_hook_interrupted", hook="C")
        raise
    except Exception as exc:  # noqa: BLE001
        _append_pipeline_audit(
            run_root, "startup_hook_failed", hook="C", error=str(exc),
        )
        _interactor.notify("warning", f"启动钩子 C 失败：{exc}")

def _notify_incomplete_runs(config: dict) -> None:
    runs_root = Path(config["project"]["output_root"]) / "runs"
    if not runs_root.exists():
        return

    incomplete: list[dict] = []
    import time as _time
    for run_dir in sorted(runs_root.iterdir()):
        if not run_dir.is_dir():
            continue
        state = _read_run_state(run_dir)
        if state is None or state.get("state") != "running":
            continue
        if not state.get("interrupt_at"):
            try:
                _m = (run_dir / ".run_state.json").stat().st_mtime
                if _time.time() - _m < 300.0:
                    continue
            except OSError:
                pass
        incomplete.append({
            "run_name": state.get("run_name", run_dir.name),
            "strategy": state.get("strategy_name", ""),
            "stage": state.get("post_hook_stage", "none"),
            "at": state.get("interrupt_at", ""),
            "detail": state.get("interrupt_detail", ""),
        })

    if not incomplete:
        return

    lines = [f"存在 {len(incomplete)} 个未完成的 run："]
    for item in incomplete:
        lines.append(
            f"  · {item['run_name']} | 策略 {item['strategy']} | "
            f"断点 {item['stage']} | {item['at']}"
        )
        lines.append(f"      原因: {item['detail']}")
    lines.append("")
    lines.append("处理方式（不阻断本次运行）：")
    lines.append("  medlitflow --config X --list-incomplete")
    lines.append('  medlitflow --config X --resume --query "<query>"')
    lines.append("  medlitflow --config X --abandon <run_name>")

    _interactor.notify("info", "\n".join(lines))

def _execute_retrieval_stage(
    run_root: Path, config: dict, stage_state: dict, resume: bool,
) -> Path:
    partial = robustness.load_partial(run_root, "retrieval") if resume else None
    if partial and partial["data"].get("completed_cids"):
        pq_path = run_root / partial["data"]["checkpoint_parquet"]
        if not pq_path.exists():
            raise RuntimeError(f"partial parquet 缺失：{pq_path}")

        completed_df = pd.read_parquet(pq_path)
        if "canonical_id" not in completed_df.columns:
            raise RuntimeError("checkpoint parquet 缺少 canonical_id 列")

        completed = set(completed_df["canonical_id"].astype(str))
        all_cids = set(stage_state["dedup_df"]["canonical_id"].astype(str))
        pending = all_cids - completed

        if not pending:
            df = completed_df
        else:
            pending_df = stage_state["dedup_df"][
                stage_state["dedup_df"]["canonical_id"].astype(str).isin(pending)
            ]
            df = retrieval.retrieve_fulltexts(
                pending_df, config, run_root, initial_df=completed_df,
            )

        final_cids = set(df["canonical_id"].astype(str))
        if final_cids != all_cids:
            raise RuntimeError("恢复后 cid 不一致")
    else:
        df = retrieval.retrieve_fulltexts(
            stage_state["dedup_df"], config, run_root,
        )

    if "library_special_status" in df.columns:
        lib_sp = (
            df["library_special_status"].fillna("").astype(str).str.strip()
        )
        if "special_status" not in df.columns:
            df["special_status"] = lib_sp
        else:
            cur_sp = (
                df["special_status"].fillna("").astype(str).str.strip()
            )
            df["special_status"] = cur_sp.where(cur_sp.ne(""), lib_sp)

    stage_state["retrieval_df"] = df
    out_path = run_root / _STAGE_OUTPUT_PATH["retrieval"]
    _atomic_write_parquet(df, out_path)

    pq_path = run_root / "interim" / "retrieval.partial.parquet"
    if pq_path.exists():
        pq_path.unlink(missing_ok=True)
    (run_root / ".stage_done" / "retrieval.partial.json").unlink(missing_ok=True)
    return out_path

def _execute_stage(
    stage, run_root, config, strategy_name, run_name,
    stage_state, mode, resume,
) -> Path:
    if stage == "collect":
        last_run_dates = _build_last_run_dates_for_collect(strategy_name)
        df = collectors.collect_all(
            config, strategy_name, run_name, last_run_dates or None,
        )
        df, effective_mode = _apply_hit_threshold(
            df, mode, config, run_root,
        )
        stage_state["collected_df"] = df
        stage_state["_effective_mode"] = effective_mode
        _write_collect_meta(
            run_root,
            effective_mode=effective_mode,
            original_mode=mode,
        )
        out_path = run_root / _STAGE_OUTPUT_PATH["collect"]
        _atomic_write_parquet(df, out_path)
        return out_path

    if stage == "dedup":
        df, _stats = dedup.deduplicate(stage_state["collected_df"], config)
        stage_state["dedup_df"] = df
        out_path = run_root / _STAGE_OUTPUT_PATH["dedup"]
        _atomic_write_parquet(df, out_path)
        return out_path

    if stage == "retrieval":
        return _execute_retrieval_stage(
            run_root, config, stage_state, resume,
        )

    if stage == "fallback":
        df = fallback.fallback_abstracts(stage_state["retrieval_df"], config)
        stage_state["fallback_df"] = df
        out_path = run_root / _STAGE_OUTPUT_PATH["fallback"]
        _atomic_write_parquet(df, out_path)
        return out_path

    if stage == "enrich":
        tables = enrichment.enrich_all(
            stage_state["fallback_df"], config, run_root,
        )
        stage_state["enrich_tables"] = tables
        enrich_dir = run_root / "interim" / "enrich"
        for name in _ENRICH_TABLE_NAMES:
            t = tables.get(name)
            if t is not None:
                _atomic_write_parquet(t, enrich_dir / f"{name}.parquet")
        return enrich_dir / "entity_papers.parquet"

    if stage == "output":
        final_df = annotator.build_final_table(
            stage_state["dedup_df"], stage_state["retrieval_df"],
            stage_state["fallback_df"], stage_state["enrich_tables"],
        )
        final_df["first_seen_run"] = run_name
        final_df["last_seen_run"] = run_name
        stage_state["final_df"] = final_df
        out_path = run_root / _STAGE_OUTPUT_PATH["output"]
        _atomic_write_parquet(final_df, out_path)
        return out_path

    raise ValueError(f"未知 stage：{stage}")

def _run_stage(
    stage: str, run_root: Path, config: dict,
    strategy_name: str, run_name: str,
    stage_state: dict, mode: str, resume: bool,
) -> None:
    for dep in _STAGE_DEPS.get(stage, ()):
        _ensure_dep_available(dep, run_root, stage_state)

    _interactor.notify("info", f"开始: {stage}")
    started = time.monotonic()
    input_count = _compute_input_count(stage, stage_state)

    _update_run_state(run_root, {
        "last_stage": stage,
        "last_stage_state": "running",
    })

    try:
        out_path = _execute_stage(
            stage, run_root, config, strategy_name, run_name,
            stage_state, mode, resume,
        )
        df_for_validation = _get_validation_df(stage, stage_state)
        output_rows = (
            len(df_for_validation) if df_for_validation is not None else 0
        )
    except robustness.InterruptRequested:
        try:
            _update_run_state(run_root, {
                "last_stage": stage,
                "last_stage_state": "partial",
            })
        except Exception:  # noqa: BLE001
            pass
        _update_run_state_if_first_interrupt(run_root, {
            "interrupt_at": _now_iso(),
            "interrupt_detail": (
                robustness.get_shutdown_detail()
                or f"stage {stage} 收到中断信号"
            ),
        })
        elapsed = time.monotonic() - started
        state.write_stage_audit(run_root, stage, input_count, 0, elapsed, 1)
        _interactor.notify(
            "warning",
            f"中断: {stage}（进度已保存；"
            f"若有网络请求正在进行，需等其完成后生效）",
        )
        raise
    except KeyboardInterrupt as exc:
        try:
            _update_run_state(run_root, {
                "last_stage": stage,
                "last_stage_state": "interrupted",
            })
        except Exception:  # noqa: BLE001
            pass
        _append_pipeline_audit(run_root, "stage_keyboard_interrupt", stage=stage)
        _update_run_state_if_first_interrupt(run_root, {
            "interrupt_at": _now_iso(),
            "interrupt_detail": (
                str(exc) or "用户中断（KeyboardInterrupt）"
            ),
        })
        raise
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - started
        state.write_stage_audit(run_root, stage, input_count, 0, elapsed, 1)
        _interactor.notify("error", f"失败: {stage}")
        _append_pipeline_audit(
            run_root, "stage_failed", stage=stage, error=str(exc),
        )
        _update_run_state(run_root, {
            "last_stage": stage,
            "last_stage_state": "failed",
        })
        _update_run_state_if_first_interrupt(run_root, {
            "interrupt_at": _now_iso(),
            "interrupt_detail": (
                f"stage {stage} 失败：{type(exc).__name__}: {exc}"
            ),
        })
        raise

    elapsed = time.monotonic() - started
    validation = robustness.validate_stage_output(stage, df_for_validation)
    if not validation["ok"]:
        state.write_stage_audit(
            run_root, stage, input_count, output_rows, elapsed,
            len(validation["errors"]),
        )
        _append_pipeline_audit(
            run_root, "validation_failed",
            stage=stage, errors=validation["errors"],
        )
        _update_run_state_if_first_interrupt(run_root, {
            "interrupt_at": _now_iso(),
            "interrupt_detail": (
                f"stage {stage} 数据校验失败：{validation['errors']}"
            ),
        })
        raise RuntimeError(f"{stage} 数据校验失败：{validation['errors']}")

    _mark_stage_done(run_root, stage, out_path, output_rows)

    state.write_stage_audit(
        run_root, stage, input_count, output_rows, elapsed, 0,
    )
    _interactor.notify("info", f"完成: {stage}")
    _update_run_state(run_root, {
        "last_stage": stage,
        "last_stage_state": "done",
    })

def move_run_papers_to_library(
    run_root: Path, library_root: Path,
    *, log_path: Path | None = None,
) -> None:
    run_root = Path(run_root)
    library_root = Path(library_root)

    records_path = run_root / "output" / "records_final.parquet"
    if not records_path.exists():
        raise FileNotFoundError(
            f"records_final.parquet 不存在：{records_path}"
        )
    run_df = pd.read_parquet(records_path)

    dirname_to_cid: dict[str, str] = {}
    for cid in run_df["canonical_id"].astype(str):
        cid = cid.strip()
        if cid:
            dirname_to_cid[core.canonical_id_to_dirname(cid)] = cid

    src_inbox = run_root / "papers" / "inbox"
    dst_inbox = library_root / "inbox"
    dst_inbox.mkdir(parents=True, exist_ok=True)

    moved_map: dict[str, str] = {}
    if src_inbox.exists():
        entries = sorted(src_inbox.iterdir())
        for safe_dir in entries:
            if not safe_dir.is_dir():
                continue
            cid = dirname_to_cid.get(safe_dir.name)
            if not cid:
                continue
            robustness.check_shutdown()
            dst_dir = dst_inbox / safe_dir.name
            if dst_dir.exists():
                _append_pipeline_audit(run_root, "move_conflict", cid=cid)
                continue
            shutil.move(str(safe_dir), str(dst_dir))
            for fname in (
                "article.pdf", "article.xml", "article.md",
                "article.html", "article_secondary.pdf",
            ):
                fp = dst_dir / fname
                if fp.exists():
                    moved_map[cid] = str(fp)
                    break

            if log_path is not None:
                try:
                    with Path(log_path).open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps({
                            "cid": cid,
                            "src": str(safe_dir),
                            "dst": str(dst_dir),
                            "at": _now_iso(),
                        }, ensure_ascii=False) + "\n")
                        fh.flush()
                        os.fsync(fh.fileno())
                except OSError:
                    pass

    if dst_inbox.exists():
        for safe_dir in dst_inbox.iterdir():
            if not safe_dir.is_dir():
                continue
            cid = dirname_to_cid.get(safe_dir.name)
            if not cid:
                continue
            if cid in moved_map:
                continue
            for fname in (
                "article.pdf", "article.xml", "article.md",
                "article.html", "article_secondary.pdf",
            ):
                fp = safe_dir / fname
                if fp.exists():
                    moved_map[cid] = str(fp)
                    break

    lib_records_path = library_root / "records_final.parquet"
    if lib_records_path.exists() and moved_map:
        lib_df = pd.read_parquet(lib_records_path)
        has_temp_col = "file_download_temp_path" in lib_df.columns
        for idx, row in lib_df.iterrows():
            cid = str(row.get("canonical_id", ""))
            if cid in moved_map:
                lib_df.at[idx, "downloaded_file_path"] = moved_map[cid]
                if has_temp_col:
                    lib_df.at[idx, "file_download_temp_path"] = ""
        tmp_lib = lib_records_path.with_name(lib_records_path.name + ".tmp")
        lib_df.to_parquet(tmp_lib, index=False)
        os.replace(tmp_lib, lib_records_path)

    file_index_path = library_root / "file_index.parquet"
    if file_index_path.exists():
        fi = pd.read_parquet(file_index_path)
    else:
        fi = pd.DataFrame(columns=_FILE_INDEX_COLUMNS)
    for c in _FILE_INDEX_COLUMNS:
        if c not in fi.columns:
            fi[c] = ""

    now = _now_iso()
    for cid, new_path in moved_map.items():
        mask = fi["canonical_id"] == cid
        if mask.any():
            ridx = fi.index[mask][0]
            fi.at[ridx, "downloaded_file_path"] = new_path
            fi.at[ridx, "current_file_path"] = new_path
            fi.at[ridx, "file_last_moved_at"] = now
            if not fi.at[ridx, "file_retention_level"]:
                fi.at[ridx, "file_retention_level"] = "EMPTY"
        else:
            new_row = {c: "" for c in _FILE_INDEX_COLUMNS}
            new_row.update({
                "canonical_id": cid,
                "downloaded_file_path": new_path,
                "current_file_path": new_path,
                "file_last_moved_at": now,
                "file_retention_level": "EMPTY",
            })
            fi = pd.concat(
                [fi, pd.DataFrame([new_row])], ignore_index=True,
            )
    tmp_fi = file_index_path.with_name(file_index_path.name + ".tmp")
    fi.to_parquet(tmp_fi, index=False)
    os.replace(tmp_fi, file_index_path)

    annotator.update_library_index(library_root, list(moved_map.keys()))

    if src_inbox.exists() and not any(src_inbox.iterdir()):
        try:
            src_inbox.rmdir()
        except OSError:
            pass

    _append_pipeline_audit(run_root, "move_done", moved=len(moved_map))

def abandon_run(config_path: str | None, run_name: str) -> None:
    config = core.load_config(config_path)
    output_root = Path(config["project"]["output_root"])
    library_root = Path(config["library"]["root"])

    lock_path = output_root / ".pipeline.lock"
    if lock_path.exists():
        if robustness.is_lock_stale(lock_path):
            lock_path.unlink(missing_ok=True)
        else:
            raise RuntimeError("pipeline 正在运行，请先停止再放弃")

    run_root = output_root / "runs" / run_name
    state = _read_run_state(run_root) if run_root.exists() else None

    if state is None:
        residuals, inferred_strategy = _find_abandon_residuals(
            run_name, config, output_root,
        )
        if not residuals:
            _interactor.notify(
                "info",
                f"run {run_name} 不存在，且无残留；视为已清理",
            )
            return

        _interactor.notify(
            "warning",
            f"run {run_name} 状态缺失，但检测到 "
            f"{len(residuals)} 处残留；执行清理",
        )
        for p in residuals:
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
            except OSError:
                pass

        archive._set_config(config)
        idempotent_cleanup_result: dict = {}
        try:
            if inferred_strategy:
                screening.update_strategy_status(
                    inferred_strategy, library_root,
                )
                idempotent_cleanup_result = archive.cleanup_orphan_strategy(
                    inferred_strategy, library_root, config,
                )
            archive.maintain_index()
        finally:
            archive._set_config(None)

        try:
            log_path = output_root / "abandon_log.jsonl"
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "run_name": run_name,
                    "strategy_name": inferred_strategy or "",
                    "abandoned_at": _now_iso(),
                    "post_hook_stage": "unknown",
                    "rollback_actions": ["residual_cleanup"],
                    "cleanup_result": idempotent_cleanup_result,
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return

    if state.get("state") == "committed":
        (run_root / ".run_state.json").unlink(missing_ok=True)
        raise RuntimeError("run 已提交，无法放弃；可手动删除 run 目录")

    strategy_name = state.get("strategy_name", "")
    if not strategy_name:
        raise RuntimeError("run_state 缺少 strategy_name")

    post_hook_stage = state.get("post_hook_stage", "none")
    txn = state.get("txn", {}) or {}
    rollback_actions: list[str] = []

    if post_hook_stage in ("merge", "move", "record"):
        if txn.get("library_backup_dir"):
            _rollback_library(run_root, state)
            rollback_actions.append("library")

    if post_hook_stage in ("move", "record"):
        if txn.get("moved_log"):
            _rollback_move(run_root, state)
            rollback_actions.append("move")

    if post_hook_stage == "record":
        if txn.get("archive_run_dir") or txn.get("archive_strategy_dir"):
            _rollback_archive(run_root, config, state)
            rollback_actions.append("archive")

    archive._set_config(config)
    cleanup_result: dict = {}
    try:
        screening.update_strategy_status(strategy_name, library_root)
        cleanup_result = archive.cleanup_orphan_strategy(
            strategy_name, library_root, config,
        )
        archive.maintain_index()
    finally:
        archive._set_config(None)

    shutil.rmtree(run_root, ignore_errors=True)

    try:
        log_path = output_root / "abandon_log.jsonl"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "run_name": run_name,
                "strategy_name": strategy_name,
                "abandoned_at": _now_iso(),
                "post_hook_stage": post_hook_stage,
                "rollback_actions": rollback_actions,
                "cleanup_result": cleanup_result,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass

def _rollback_library(run_root: Path, state: dict) -> None:
    txn = state["txn"]
    backup_dir = Path(txn["library_backup_dir"])
    library_root = backup_dir.parent.parent
    restore_files = txn.get("restore_files", []) or []

    for fname in restore_files:
        src = backup_dir / fname
        if src.exists():
            tmp = library_root / f"{fname}.tmp"
            shutil.copy2(src, tmp)
            os.replace(tmp, library_root / fname)

    for fname in _LIBRARY_FILES:
        if fname in restore_files:
            continue
        target = library_root / fname
        if target.exists():
            target.unlink()

    shutil.rmtree(backup_dir, ignore_errors=True)

def _rollback_move(run_root: Path, state: dict) -> None:
    move_log = Path(state["txn"]["moved_log"])
    if not move_log.exists():
        return
    for line in move_log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        src, dst = Path(rec["dst"]), Path(rec["src"])
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
    move_log.unlink(missing_ok=True)

def _rollback_archive(run_root: Path, config: dict, state: dict) -> None:
    from medlitflow import archive  # 局部导入避免顶部循环
    txn = state["txn"]
    archive_run_dir = txn.get("archive_run_dir")
    if archive_run_dir and Path(archive_run_dir).exists():
        shutil.rmtree(archive_run_dir, ignore_errors=True)

    strategy_dir_raw = txn.get("archive_strategy_dir")
    snapshot = txn.get("last_run_dates_snapshot") or {}
    if strategy_dir_raw and snapshot:
        strategy_dir = Path(strategy_dir_raw)
        strategy_dir.mkdir(parents=True, exist_ok=True)
        lrd_path = strategy_dir / "last_run_dates.json"
        tmp = lrd_path.with_name(lrd_path.name + ".tmp")
        tmp.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, lrd_path)

def _find_abandon_residuals(
    run_name: str, config: dict, output_root: Path,
) -> tuple[list[Path], str]:
    residuals: list[Path] = []
    inferred_strategy = ""

    library_root = Path(config["library"]["root"])
    archive_root = _resolve_archive_root(config)

    txn_dir = library_root / ".txn" / run_name
    if txn_dir.exists():
        residuals.append(txn_dir)

    strategies_root = archive_root / "strategies"
    if strategies_root.exists():
        for strategy_dir in strategies_root.iterdir():
            if not strategy_dir.is_dir():
                continue
            runs_root = strategy_dir / "runs"
            if not runs_root.exists():
                continue
            for date_dir in runs_root.iterdir():
                if not date_dir.is_dir():
                    continue
                candidate = date_dir / run_name
                if candidate.exists():
                    residuals.append(candidate)
                    if not inferred_strategy:
                        inferred_strategy = strategy_dir.name

    return residuals, inferred_strategy

def run_pipeline(
    config_path: "str | None",
    stage: str | None = None,
    resume: bool = False,
) -> None:
    global _run_started_at, _interactor
    _init_interactor()
    robustness.reset_shutdown()

    config = core.load_config(config_path)
    archive._set_config(config)  # noqa: frozen

    mode = _resolve_mode(config)

    if stage in _NON_PIPELINE_STAGES:
        raise ValueError(
            f"stage={stage} 不由 run_pipeline 处理；请通过 cli 分发"
        )

    strategy_name = _resolve_strategy_name(config)
    run_name = _resolve_run_name(config, strategy_name, resume=resume)

    output_root = Path(config["project"]["output_root"])
    library_root = Path(config["library"]["root"])
    archive_root = _resolve_archive_root(config)

    if not robustness.acquire_lock(output_root):
        raise RuntimeError(
            f"已有 pipeline 实例运行（锁：{output_root}/.pipeline.lock）"
        )

    run_root: Path | None = None

    try:
        _run_started_at = _now_iso()
        run_root = state.init_run(run_name, config)
        robustness.register_signal_handler()
        existing_state = _read_run_state(run_root)

        if (
            existing_state is not None
            and existing_state.get("state") == "committed"
        ):
            (run_root / ".run_state.json").unlink(missing_ok=True)
            old_run_name = run_name
            config_no_run_name = dict(config)
            config_no_run_name["project"] = dict(config.get("project", {}) or {})
            config_no_run_name["project"]["run_name"] = ""
            run_name = _resolve_run_name(
                config_no_run_name, strategy_name, resume=False,
            )
            run_root = state.init_run(run_name, config)
            existing_state = None
            _interactor.notify(
                "info",
                f"检测到 run {old_run_name} 已提交（残留 .run_state.json），"
                f"已自动切换到新 run：{run_name}",
            )

        if (
            existing_state is not None
            and existing_state.get("state") == "running"
            and existing_state.get("post_hook_stage", "none") != "none"
        ):
            pass

        else:
            _strategy_entry = _find_query_in_list(
                _resolve_queries(config), strategy_name,
            ) or {}
            _pubmed_query = str(_strategy_entry.get("pubmed", "") or "")
            _previous_interrupts: list[dict] = []
            if existing_state is not None:
                _existing_history = existing_state.get("previous_interrupts")
                if isinstance(_existing_history, list):
                    _previous_interrupts = list(_existing_history)
                _cur_int_at = str(
                    existing_state.get("interrupt_at", "") or ""
                )
                if _cur_int_at:
                    _previous_interrupts.append({
                        "interrupt_at": _cur_int_at,
                        "interrupt_detail": str(
                            existing_state.get("interrupt_detail", "") or ""
                        ),
                    })

            _new_state = {
                "schema_version": 1,
                "run_name": run_name,
                "strategy_name": strategy_name,
                "pubmed_query": _pubmed_query,
                "state": "running",
                "started_at": _run_started_at,
                "last_stage": "",
                "last_stage_state": "",
                "post_hook_stage": "none",
                "txn": {},
            }
            if _previous_interrupts:
                _new_state["previous_interrupts"] = _previous_interrupts

            _write_run_state(run_root, _new_state)

        try:
            _ensure_health_check(config, run_root)
        except robustness.InterruptRequested:
            raise
        except Exception:
            if not resume:
                shutil.rmtree(run_root, ignore_errors=True)
            raise

        _run_startup_hooks(config, library_root, archive_root, run_root)

        if stage is None:
            _notify_incomplete_runs(config)

        existing_run_state = _read_run_state(run_root)
        resume_from_post_hook = (
            resume
            and existing_run_state is not None
            and existing_run_state.get("state") == "running"
            and existing_run_state.get("post_hook_stage", "none") != "none"
        )

        if resume_from_post_hook:
            _interactor.notify(
                "info",
                f"检测到未完成 post_hooks（断点："
                f"{existing_run_state.get('post_hook_stage')}），"
                f"跳过阶段执行",
            )
            records_final_path = run_root / _STAGE_OUTPUT_PATH["output"]
            if not records_final_path.exists():
                raise RuntimeError(
                    f"post_hooks 断点检测异常："
                    f"{records_final_path} 不存在"
                )
            metadata_path = run_root / "output" / "run_metadata.json"
            if not metadata_path.exists():
                raise RuntimeError(
                    f"post_hooks 断点检测异常："
                    f"{metadata_path} 不存在"
                )
            _run_post_hooks(
                run_root, config, strategy_name, run_name, library_root,
                resume_from=existing_run_state.get("post_hook_stage", "none"),
            )
            return

        stage_state: dict = {}
        stages = _resolve_stages(stage)
        output_actually_executed = False
        collect_will_run = (
            "collect" in stages
            and not (resume and _can_skip_stage(run_root, "collect"))
        )
        if collect_will_run:
            _prepare_query_audit_and_q2(
                config, strategy_name, run_root, mode,
            )

        for st in stages:
            if resume and _resolve_stage_state(run_root, st) == "done":
                _interactor.notify("info", f"跳过（已完成）: {st}")
                _rehydrate_state(st, run_root, stage_state)
                continue
            _run_stage(
                st, run_root, config, strategy_name, run_name,
                stage_state, mode, resume,
            )
            if st == "output":
                output_actually_executed = True

        effective_mode = stage_state.get("_effective_mode", mode)

        if output_actually_executed:
            _write_run_metadata_and_query_execution(
                run_root, config, strategy_name, run_name,
            )

        if effective_mode == "scan" and output_actually_executed:
            _append_pipeline_audit(
                run_root, "scan_mode_skip_hooks",
                skipped=["hook1", "hook2", "hook3"],
                downgraded=(mode == "intensive"),
            )
        elif output_actually_executed:
            _run_post_hooks(
                run_root, config, strategy_name, run_name, library_root,
            )

        if output_actually_executed and _should_export(config):
            _export_output(run_root, config, strategy_name)

    except robustness.InterruptRequested:
        if run_root is not None:
            _update_run_state_if_first_interrupt(run_root, {
                "interrupt_at": _now_iso(),
                "interrupt_detail": (
                    robustness.get_shutdown_detail()
                    or "pipeline 中断"
                ),
            })
        raise
    finally:
        robustness.release_lock(output_root)
        archive._set_config(None)