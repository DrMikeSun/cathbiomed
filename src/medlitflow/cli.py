"""medlitflow cli.py — CLI 入口与交互层。

公开接口：main(argv: list[str] | None = None) -> int
退出码：0 成功 / 1 参数错误 / 2 运行错误 / 3 用户取消"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from medlitflow.cli_interactor import CLIInteractor
from medlitflow import (
    archive,
    core,
    pipeline,
    robustness,
    screening,
    zotero_bridge,
)

__all__ = ["main"]

EXIT_SUCCESS = 0
EXIT_ARG_ERROR = 1
EXIT_RUNTIME_ERROR = 2
EXIT_USER_CANCELLED = 3

MAIN_ARGS_CORE = (
    "--config", "--query", "--query-file", "--stage", "--resume", "--mode",
    "--parallel", "--force-redownload", "--yes", "--export",
)
MAIN_ARG_VERBOSE = "--verbose"

PIPELINE_STAGES = frozenset({
    "collect", "dedup", "retrieval", "fallback", "enrich", "output",
})
NON_PIPELINE_STAGES = frozenset({"screening", "archive", "zotero-sync"})
VALID_STAGES = PIPELINE_STAGES | NON_PIPELINE_STAGES
VALID_MODES = frozenset({"intensive", "scan"})

SCREENING_SUBCOMMANDS = frozenset({
    "batch_confirm",
    "set_maybe", "restore", "cleanup_trash",
    "preview_trash_cleanup", "list_maybe", "refresh_status",
    "migrate_schema",
})

_SUB_ARG_SPECIAL = "_special"

SCREENING_SUBCOMMAND_ARG_KEYS: dict[str, str | None] = {
    "batch_confirm":         "prescreen_label",
    "set_maybe":             _SUB_ARG_SPECIAL,
    "restore":               "canonical_id",
    "cleanup_trash":         None,
    "preview_trash_cleanup": None,
    "list_maybe":            None,
    "refresh_status":        "strategy_name",
    "migrate_schema":        None,
}

ARCHIVE_SUBCOMMANDS = frozenset({
    "consolidate", "preview", "cleanup_runs",
    "archive_strategy", "check_concurrent", "maintain_index",
    "delete_strategy",
    "purge_strategy",
    "cleanup_orphans",
})

ARCHIVE_SUBCOMMAND_ARG_KEYS: dict[str, str | None] = {
    "consolidate":      "strategy_name",
    "preview":          "strategy_name",
    "cleanup_runs":     "strategy_name",
    "archive_strategy": "strategy_name",
    "check_concurrent": None,
    "maintain_index":   None,
    "delete_strategy":  "strategy_name",
    "purge_strategy":   "strategy_name",
    "cleanup_orphans":  None,
}

_SUB_ARG_ATTRS: tuple[str, ...] = (
    "batch_confirm",
    "set_maybe", "restore", "cleanup_trash",
    "preview_trash_cleanup", "list_maybe", "refresh_status",
    "migrate_schema",
    "consolidate", "preview", "cleanup_runs", "archive_strategy",
    "check_concurrent", "maintain_index",
    "delete_strategy",
    "purge_strategy", "cleanup_orphans",
    "keep_recent",
)

_TMP_CONFIG_DIR_PREFIX = "medlitflow_cli_"
_TMP_CONFIG_FILENAME_FMT = "{uuid}_{strategy}.yaml"
_SAFE_STRATEGY_RE = re.compile(r"[^0-9A-Za-z._-]+")

_PROMPT_CONFIRM_SUFFIX = "此操作不可恢复，确认？(y/n) "

_BATCH_SUMMARY_FILENAME_FMT = "batch_summary_{ts}.json"
_BATCH_TIMESTAMP_FMT = "%Y%m%dT%H%M%S"

def _now_iso() -> str:
    return core.now_iso()

def _sanitize_strategy(name: str) -> str:
    safe = _SAFE_STRATEGY_RE.sub("_", str(name)).strip("._")
    return safe or "strategy"

@dataclass
class ParsedArgs:
    config_path: str | None = None
    stage: str | None = None
    resume: bool = False
    mode: str | None = None
    parallel: bool | None = None
    force_redownload: bool = False
    yes: bool = False
    export_dir: str | None = None
    export_mode: str | None = None
    verbose: bool = False
    subcommand: str | None = None
    subcommand_args: dict[str, Any] = field(default_factory=dict)
    zotero_verify: bool = False
    zotero_prune: bool = False
    abandon_run_name: str | None = None
    list_incomplete: bool = False
    query: str | None = None
    query_file: str | None = None
    derived_queries: list[dict] = field(default_factory=list)
    screening_strategy: str | None = None

class _CLIArgumentError(Exception):
    """CLI 参数错误，映射退出码 1。"""


class _CLIArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        raise _CLIArgumentError(message)

def _build_parser() -> argparse.ArgumentParser:
    parser = _CLIArgumentParser(
        prog="medlitflow",
        description="medlitflow 医学文献采集与知识图谱构建管线",
    )
    parser.add_argument("--config", default=None)
    parser.add_argument(
        "--stage", default=None,
        choices=sorted(VALID_STAGES),
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--mode", default=None, choices=["intensive", "scan"],
    )

    query_group = parser.add_mutually_exclusive_group()
    query_group.add_argument(
        "--query", default=None, dest="query",
        help=(
            "检索式（分号 / 换行分隔多策略）；"
            "strategy_name 由 pipeline._derive_strategy_name 派生"
        ),
    )
    query_group.add_argument(
        "--query-file", default=None, dest="query_file",
        help=(
            "检索式文件路径；每行一个检索式；"
            "# 开头为注释；空行跳过"
        ),
    )

    parallel_group = parser.add_mutually_exclusive_group()
    parallel_group.add_argument(
        "--parallel", dest="parallel", action="store_true", default=None,
    )
    parallel_group.add_argument(
        "--no-parallel", dest="parallel", action="store_false",
    )

    parser.add_argument("--force-redownload", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--export", default=None, dest="export_dir")
    parser.add_argument(
        "--export-mode", default=None,
        choices=["copy", "symlink", "none"],
        dest="export_mode",
        help="导出模式（覆盖 config 中的 export_mode）",
    )
    parser.add_argument("--verbose", action="store_true")

    parser.add_argument(
        "--zotero-verify", action="store_true", dest="zotero_verify",
        help="校验 zotero_links 与 Zotero 实际状态（只读）",
    )
    parser.add_argument(
        "--zotero-prune", action="store_true", dest="zotero_prune",
        help="清理已从 Zotero 删除的失效记录（隐含 verify）",
    )

    parser.add_argument("--batch-confirm", default=None, dest="batch_confirm")
    parser.add_argument("--set-maybe", default=None, dest="set_maybe")
    parser.add_argument("--restore", default=None, dest="restore")
    parser.add_argument("--cleanup-trash", action="store_true")
    parser.add_argument("--preview-trash-cleanup", action="store_true")
    parser.add_argument("--list-maybe", action="store_true")
    parser.add_argument("--refresh-status", default=None, dest="refresh_status")
    parser.add_argument(
        "--migrate-schema", action="store_true", dest="migrate_schema",
        help=(
            "迁移 screening_decisions / file_index 至 v1.2 schema"
            "（B16 + B18 一次性执行，幂等）。需 --stage screening。"
        ),
    )
    parser.add_argument(
        "--strategy", default=None, dest="screening_strategy",
        help=(
            "v1.2 二元组筛选：指定策略名。"
            "仅对 --stage screening 的 --set-maybe 有效。"
        ),
    )

    parser.add_argument("--consolidate", default=None)
    parser.add_argument("--preview", default=None)
    parser.add_argument("--cleanup-runs", default=None, dest="cleanup_runs")
    parser.add_argument(
        "--keep-recent", type=int, default=None, dest="keep_recent",
    )
    parser.add_argument("--archive-strategy", default=None, dest="archive_strategy")
    parser.add_argument("--check-concurrent", action="store_true")
    parser.add_argument("--maintain-index", action="store_true")
    parser.add_argument(
        "--delete-strategy", default=None, dest="delete_strategy",
        help=(
            "移除策略（模式R）：删 archive 快照 + registry 行；"
            "library 文献完整保留（含 strategy_names 字段）。"
            "文献的「检索策略」列仍显示策略名；可用 --cleanup-orphans "
            "后续彻底清理。需要 --stage archive。"
            "与 --purge-strategy 互斥。"
        ),
    )
    parser.add_argument(
        "--purge-strategy", default=None, dest="purge_strategy",
        help=(
            "彻底删除策略（模式P）：独占文献硬删 + 共享文献重算 + "
            "archive 快照删 + registry 行删。不可恢复。"
            "需要 --stage archive。与 --delete-strategy 互斥。"
        ),
    )
    parser.add_argument(
        "--cleanup-orphans", action="store_true",
        help=(
            "清理孤儿策略文献（模式R → 模式P 桥接）："
            "删除所有「已移除策略」残留的文献（含文件）。"
            "需要 --stage archive。"
        ),
    )
    parser.add_argument(
        "--abandon", default=None, dest="abandon_run_name",
        help="放弃指定 run（回滚 library 与 archive 副作用）",
    )
    parser.add_argument(
        "--list-incomplete", action="store_true",
        dest="list_incomplete",
        help="列出所有未完成 run",
    )

    return parser

def _normalize_args(args: argparse.Namespace) -> ParsedArgs:
    if args.stage is None or args.stage in PIPELINE_STAGES:
        for attr in _SUB_ARG_ATTRS:
            if attr == "keep_recent":
                continue
            val = getattr(args, attr, None)
            if val is None or val is False:
                continue
            raise _CLIArgumentError(
                f"--{attr.replace('_', '-')} 需要 "
                f"--stage screening 或 --stage archive"
            )

    if (
        getattr(args, "keep_recent", None) is not None
        and getattr(args, "cleanup_runs", None) is None
    ):
        raise _CLIArgumentError("--keep-recent 需要 --cleanup-runs")

    derived_queries = _parse_query_inputs(args)

    subcommand, subcommand_args = _detect_subcommand(args)

    return ParsedArgs(
        config_path=args.config,
        stage=args.stage,
        resume=bool(args.resume),
        mode=args.mode,
        parallel=args.parallel,
        force_redownload=bool(args.force_redownload),
        yes=bool(args.yes),
        export_dir=args.export_dir,
        export_mode=getattr(args, "export_mode", None), 
        verbose=bool(args.verbose),
        subcommand=subcommand,
        subcommand_args=subcommand_args,
        zotero_verify=bool(getattr(args, "zotero_verify", False)),
        zotero_prune=bool(getattr(args, "zotero_prune", False)),
        abandon_run_name=getattr(args, "abandon_run_name", None),
        list_incomplete=bool(getattr(args, "list_incomplete", False)),
        query=getattr(args, "query", None),
        query_file=getattr(args, "query_file", None),
        derived_queries=derived_queries,
        screening_strategy=getattr(args, "screening_strategy", None),
    )

def _parse_query_inputs(args: argparse.Namespace) -> list[dict]:
    results: list[dict] = []

    raw_query = getattr(args, "query", None)
    if raw_query is not None:
        text = str(raw_query).replace("；", ";")
        parts = [
            p.strip()
            for p in re.split(r"[;\n]", text)
            if p.strip()
        ]
        for q in parts:
            results.append({
                "name": pipeline._derive_strategy_name(q),
                "pubmed": q,
            })
        return results

    raw_file = getattr(args, "query_file", None)
    if raw_file is not None:
        path = Path(str(raw_file))
        if not path.exists():
            raise _CLIArgumentError(
                f"--query-file 文件不存在：{path}"
            )
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise _CLIArgumentError(
                f"--query-file 读取失败：{path}（{exc}）"
            ) from exc
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            results.append({
                "name": pipeline._derive_strategy_name(stripped),
                "pubmed": stripped,
            })
        return results

    return []

def _detect_subcommand(
    args: argparse.Namespace,
) -> tuple[str | None, dict[str, Any]]:
    stage = args.stage

    if stage == "screening":
        mapping: dict[str, str | None] = dict(SCREENING_SUBCOMMAND_ARG_KEYS)
    elif stage == "archive":
        mapping = dict(ARCHIVE_SUBCOMMAND_ARG_KEYS)
    else:
        return None, {}

    provided: list[tuple[str, Any]] = []
    for attr, key in mapping.items():
        val = getattr(args, attr, None)
        if key is None:
            if val is True:
                provided.append((attr, None))
        else:
            if val is not None:
                if isinstance(val, str) and val.strip() == "":
                    raise _CLIArgumentError(
                        f"--{attr.replace('_', '-')} 需要非空值"
                    )
                provided.append((attr, val))

    if len(provided) == 0:
        return None, {}
    if len(provided) > 1:
        names = ", ".join(f"--{a.replace('_', '-')}" for a, _ in provided)
        raise _CLIArgumentError(
            f"--stage {stage} 只能指定一个子命令，收到：{names}"
        )

    sub, val = provided[0]
    sub_args: dict[str, Any] = {}

    if stage == "screening" and sub == "set_maybe":
        try:
            cid, decision = _split_kv(str(val))
        except ValueError as exc:
            raise _CLIArgumentError(str(exc)) from exc
        sub_args["canonical_id"] = cid
        sub_args["decision"] = decision
        return sub, sub_args

    key = mapping[sub]
    if key == _SUB_ARG_SPECIAL:
        raise _CLIArgumentError(
            f"_SUB_ARG_SPECIAL 子命令 {sub} 缺少特殊分支处理"
        )
    if key is not None:
        sub_args[key] = val

    if stage == "archive" and sub == "cleanup_runs":
        kr = getattr(args, "keep_recent", None)
        sub_args["keep_recent"] = int(kr) if kr is not None else 3

    return sub, sub_args

def _apply_cli_overrides(config: dict, parsed: ParsedArgs) -> None:
    if parsed.mode is not None:
        config.setdefault("pipeline", {})["mode"] = parsed.mode

    if parsed.parallel is not None:
        config.setdefault("retrieval", {})["parallel"] = parsed.parallel

    if parsed.force_redownload:
        rc = config.setdefault("retrieval", {}).setdefault(
            "library_cross_check", {},
        )
        rc["enabled"] = False

    if parsed.yes:
        config.setdefault("project", {})["non_interactive"] = True

    if parsed.export_dir is not None:
        out = config.setdefault("output", {})
        out["user_dir"] = parsed.export_dir
        out["auto_export"] = True
        if parsed.export_mode is not None:
            out["export_mode"] = parsed.export_mode
        elif str(out.get("export_mode", "")).lower() == "none":
            # 用户给了 --export 但没给 --export-mode，隐式切换为 copy
            out["export_mode"] = "copy"

def _validate_combinations(parsed: ParsedArgs) -> None:
    if parsed.list_incomplete:
        if (parsed.stage or parsed.subcommand
                or parsed.abandon_run_name):
            raise ValueError("--list-incomplete 不能与其他操作组合")
        if not parsed.config_path:
            raise ValueError("--list-incomplete 需要 --config")
        return

    if parsed.abandon_run_name:
        if parsed.stage or parsed.subcommand:
            raise ValueError("--abandon 不能与其他操作组合")
        if not parsed.config_path:
            raise ValueError("--abandon 需要 --config")
        return

    if parsed.stage in ("screening", "archive") and not parsed.subcommand:
        raise ValueError(f"--stage {parsed.stage} 需要指定子命令")

    if parsed.mode is not None and parsed.stage in NON_PIPELINE_STAGES:
        raise ValueError(f"--mode 对 --stage {parsed.stage} 无效")

    overrides_non_pipeline = any((
        parsed.export_dir is not None,
        parsed.export_mode is not None,
        parsed.force_redownload,
        parsed.parallel is not None,
    ))
    if overrides_non_pipeline and parsed.stage in NON_PIPELINE_STAGES:
        raise ValueError(
            "--export / --export-mode / --force-redownload / --parallel "
            f"对 --stage {parsed.stage} 无效"
        )

    if (
        (parsed.zotero_verify or parsed.zotero_prune)
        and parsed.stage != "zotero-sync"
    ):
        raise ValueError(
            "--zotero-verify / --zotero-prune 需要 --stage zotero-sync"
        )

    if parsed.zotero_verify and parsed.zotero_prune:
        raise ValueError(
            "--zotero-verify 与 --zotero-prune 互斥"
            "（prune 已包含 verify）"
        )

    if parsed.export_mode is not None and parsed.export_dir is None:
        raise ValueError("--export-mode 需要 --export")

    has_query_arg = (
        parsed.query is not None or parsed.query_file is not None
    )

    if has_query_arg and parsed.stage in NON_PIPELINE_STAGES:
        raise ValueError(
            f"--query / --query-file 对 --stage {parsed.stage} 无效"
        )

    if has_query_arg and parsed.subcommand:
        raise ValueError(
            "--query / --query-file 不能与子命令组合"
        )

    if has_query_arg and not parsed.derived_queries:
        if parsed.query is not None:
            raise ValueError("--query 不能为空")
        raise ValueError(
            f"--query-file 未读到有效检索式：{parsed.query_file}"
        )

    if (
        parsed.screening_strategy is not None
        and parsed.stage != "screening"
    ):
        raise ValueError("--strategy 仅对 --stage screening 有效")

def _dispatch(parsed: ParsedArgs, config: dict) -> int:
    if parsed.list_incomplete:
        return _dispatch_list_incomplete(config)

    if parsed.abandon_run_name:
        return _dispatch_abandon(parsed, config)

    if parsed.stage is None or parsed.stage in PIPELINE_STAGES:
        if parsed.derived_queries:
            return _dispatch_query(parsed, config)
        return _dispatch_pipeline_single(parsed, config)

    if parsed.stage == "screening":
        return _dispatch_screening(parsed, config)

    if parsed.stage == "archive":
        return _dispatch_archive(parsed, config)

    if parsed.stage == "zotero-sync":
        return _dispatch_zotero(parsed, config)

    raise ValueError(f"未知 stage：{parsed.stage}")

def _dispatch_abandon(parsed: ParsedArgs, config: dict) -> int:
    pipeline.abandon_run(parsed.config_path, parsed.abandon_run_name)
    print(f"已放弃并删除 run：{parsed.abandon_run_name}")
    return EXIT_SUCCESS

def _dispatch_list_incomplete(config: dict) -> int:
    runs_root = Path(config["project"]["output_root"]) / "runs"
    if not runs_root.exists():
        print("无未完成 run。")
        return EXIT_SUCCESS

    count = 0
    for run_dir in sorted(runs_root.iterdir()):
        if not run_dir.is_dir():
            continue
        state_path = run_dir / ".run_state.json"
        if not state_path.exists():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if state.get("state") != "running":
            continue

        interrupt_at = str(state.get("interrupt_at", "") or "")
        interrupt_detail = str(state.get("interrupt_detail", "") or "")
        if not interrupt_at:
            try:
                import time as _time
                _m = state_path.stat().st_mtime
                if _time.time() - _m < 300.0:
                    continue
                from datetime import datetime as _dt, timezone as _tz
                interrupt_at = _dt.fromtimestamp(
                    _m, _tz.utc
                ).isoformat()
                if not interrupt_detail:
                    interrupt_detail = (
                        "运行异常终止（无中断记录，可能被强杀）"
                    )
            except OSError:
                pass
        count += 1
        print(f"  {state.get('run_name', run_dir.name)}")
        print(f"    策略: {state.get('strategy_name', '')}")
        print(f"    断点: {state.get('post_hook_stage', 'none')}")
        print(f"    中断时间: {interrupt_at}")
        print(f"    中断原因: {interrupt_detail}")
        print(
            f"    [继续]: medlitflow --config X --resume "
            f"--query \"<与中断时相同的检索式>\""
        )
        print(
            f"    [放弃]: medlitflow --config X --abandon "
            f"{state.get('run_name')}"
        )
        print()

    if count == 0:
        print("无未完成 run。")
    else:
        print(f"共 {count} 个未完成 run。")
    return EXIT_SUCCESS

def _dispatch_pipeline_single(parsed: ParsedArgs, config: dict) -> int:
    if _needs_tmp_config(parsed):
        tmpdir = tempfile.mkdtemp(prefix=_TMP_CONFIG_DIR_PREFIX)
        try:
            cfg_path = _materialize_config(
                config, tmpdir, strategy="single",
            )
            pipeline.run_pipeline(cfg_path, parsed.stage, parsed.resume)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        pipeline.run_pipeline(
            parsed.config_path, parsed.stage, parsed.resume,
        )
    return EXIT_SUCCESS

def _dispatch_query(parsed: ParsedArgs, config: dict) -> int:
    items = parsed.derived_queries
    if not items:
        return EXIT_SUCCESS

    if len(items) == 1:
        return _dispatch_query_single(parsed, config, items[0])
    return _dispatch_query_batch(parsed, config, items)


def _dispatch_query_single(
    parsed: ParsedArgs, config: dict, item: dict,
) -> int:
    config_i = copy.deepcopy(config)
    config_i["queries"] = [{
        "name": item["name"],
        "pubmed": item["pubmed"],
    }]
    project = config_i.setdefault("project", {})
    project["strategy_name"] = item["name"]
    project["run_name"] = ""

    tmpdir = tempfile.mkdtemp(prefix=_TMP_CONFIG_DIR_PREFIX)
    try:
        cfg_path = _materialize_config(
            config_i, tmpdir, strategy=item["name"],
        )
        pipeline.run_pipeline(cfg_path, parsed.stage, parsed.resume)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return EXIT_SUCCESS

def _dispatch_query_batch(
    parsed: ParsedArgs, config: dict, items: list[dict],
) -> int:
    if not items:
        return EXIT_SUCCESS

    continue_on_failure = bool(
        ((config.get("pipeline", {}) or {}).get("batch", {}) or {})
        .get("continue_on_failure", True)
    )
    output_root = Path(config["project"]["output_root"])
    tmpdir = tempfile.mkdtemp(prefix=_TMP_CONFIG_DIR_PREFIX)

    started_at = _now_iso()
    results: list[dict] = []
    n = len(items)
    print(f"批量队列：共 {n} 个检索式，串行执行。")
    try:
        for i, item in enumerate(items, start=1):
            name = str(item.get("name", "") or "")
            print(f"  [{i}/{n}] {name} 开始...")

            config_i = copy.deepcopy(config)
            config_i["queries"] = [{
                "name": item["name"],
                "pubmed": item["pubmed"],
            }]
            project = config_i.setdefault("project", {})
            project["strategy_name"] = item["name"]
            project["run_name"] = ""

            cfg_path = _materialize_config(
                config_i, tmpdir, strategy=item["name"],
            )

            try:
                pipeline.run_pipeline(
                    cfg_path, stage=None, resume=parsed.resume,
                )
                results.append({"strategy": name, "status": "ok"})
                print(f"  [{i}/{n}] {name} 完成。")
            except KeyboardInterrupt:
                results.append({"strategy": name, "status": "cancelled"})
                print(
                    f"  [{i}/{n}] {name} 用户取消。",
                    file=sys.stderr,
                )
                break
            except robustness.InterruptRequested:
                results.append({"strategy": name, "status": "cancelled"})
                print(
                    f"  [{i}/{n}] {name} 已中断，停止队列。",
                    file=sys.stderr,
                )
                break
            except Exception as exc:
                results.append({
                    "strategy": name,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                })
                print(
                    f"  [{i}/{n}] {name} 失败：{exc}",
                    file=sys.stderr,
                )
                if not continue_on_failure:
                    break
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    summary_path = _write_batch_summary(
        output_root, started_at, results, continue_on_failure,
    )
    ok = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] == "failed")
    cancelled = sum(1 for r in results if r["status"] == "cancelled")
    print(f"批量完成：成功 {ok}，失败 {failed}，取消 {cancelled}。")
    if summary_path is not None:
        print(f"汇总文件：{summary_path}")
    return _batch_exit_code(results)

def _needs_tmp_config(parsed: ParsedArgs) -> bool:
    return any((
        parsed.mode is not None,
        parsed.parallel is not None,
        parsed.force_redownload,
        parsed.yes,
        parsed.export_dir is not None,
    ))

def _materialize_config(config: dict, tmpdir: str, strategy: str) -> str:
    uid = uuid.uuid4().hex[:8]
    safe = _sanitize_strategy(strategy)
    name = _TMP_CONFIG_FILENAME_FMT.format(uuid=uid, strategy=safe)
    path = Path(tmpdir) / name
    path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return str(path)

def _write_batch_summary(
    output_root: Path, started_at: str,
    results: list[dict], continue_on_failure: bool,
) -> Path | None:
    finished_at = _now_iso()
    summary = {"ok": 0, "failed": 0, "cancelled": 0, "partial": 0}
    for r in results:
        st = r.get("status", "")
        if st in summary:
            summary[st] += 1
    payload = {
        "started_at": started_at,
        "finished_at": finished_at,
        "continue_on_failure": continue_on_failure,
        "strategies": results,
        "summary": summary,
    }
    ts = datetime.fromisoformat(finished_at).strftime(_BATCH_TIMESTAMP_FMT)
    path = output_root / _BATCH_SUMMARY_FILENAME_FMT.format(ts=ts)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path
    except OSError as exc:
        print(
            f"警告：批量汇总文件写入失败：{exc}",
            file=sys.stderr,
        )
        return None

def _batch_exit_code(results: list[dict]) -> int:
    if any(r.get("status") == "cancelled" for r in results):
        return EXIT_USER_CANCELLED
    if any(r.get("status") == "failed" for r in results):
        return EXIT_RUNTIME_ERROR
    return EXIT_SUCCESS

def _dispatch_screening(parsed: ParsedArgs, config: dict) -> int:
    library_root = Path(config["library"]["root"])

    sub = parsed.subcommand
    sa = parsed.subcommand_args
    strategy = getattr(parsed, "screening_strategy", None) or ""

    if sub == "migrate_schema":
        return _dispatch_migrate_schema(library_root)

    if sub == "batch_confirm":
        n = screening.batch_confirm(
            sa["prescreen_label"], library_root,
            strategy_name=strategy,
        )
        print(f"已批量确认 {n} 条。")
        return EXIT_SUCCESS

    if sub == "set_maybe":
        if not strategy:
            print(
                "参数错误：--set-maybe 需要 --strategy <name>",
                file=sys.stderr,
            )
            return EXIT_ARG_ERROR
        screening.set_maybe_decision(
            sa["canonical_id"], sa["decision"], library_root, config,
            strategy_name=strategy,
        )
        print(
            f"已为 {sa['canonical_id']} 设置二次决策：{sa['decision']}"
            f"（策略 {strategy}）"
        )
        return EXIT_SUCCESS

    if sub == "restore":
        screening.restore_from_trash(sa["canonical_id"], library_root)
        print(f"已从回收站恢复 {sa['canonical_id']}。")
        return EXIT_SUCCESS

    if sub == "cleanup_trash":
        if not _confirm_destructive(parsed, "清空回收站"):
            return EXIT_USER_CANCELLED
        result = screening.cleanup_trash(library_root, config)
        print(
            f"已删除 {result['deleted_count']} 条，"
            f"释放 {result['freed_bytes']} 字节。"
        )
        return EXIT_SUCCESS

    if sub == "preview_trash_cleanup":
        path = screening.preview_trash_cleanup(library_root)
        print(f"预览文件：{path}")
        return EXIT_SUCCESS

    if sub == "list_maybe":
        return _list_maybe(library_root)

    if sub == "refresh_status":
        return _refresh_status(sa["strategy_name"], library_root)

    raise ValueError(f"未处理的 screening 子命令：{sub}")

def _dispatch_migrate_schema(library_root: Path) -> int:
    print(f"迁移 library: {library_root}")

    r_dec = screening.migrate_decisions_schema(library_root)
    status_d = r_dec.get("status", "error")
    if status_d == "migrated":
        print(
            f"  · decisions：迁移成功 "
            f"（{r_dec.get('original_rows', 0)} → "
            f"{r_dec.get('migrated_rows', 0)} 行，"
            f"孤儿 {r_dec.get('orphan_rows', 0)} 条，"
            f"备份 {r_dec.get('backup_path', '')}）"
        )
    elif status_d == "already_migrated":
        print("  · decisions：已迁移（跳过）")
    elif status_d == "no_file":
        print("  · decisions：文件不存在（跳过）")
    else:
        print(
            f"  · decisions：迁移失败 - {r_dec.get('reason', '')}",
            file=sys.stderr,
        )

    r_fi = screening.migrate_file_index_schema(library_root)
    status_f = r_fi.get("status", "error")
    if status_f == "migrated":
        cols = ", ".join(r_fi.get("columns_added", []))
        print(
            f"  · file_index：迁移成功"
            f"（加列 {cols}，{r_fi.get('rows_total', 0)} 行）"
        )
    elif status_f == "already_migrated":
        print("  · file_index：已迁移（跳过）")
    elif status_f == "no_file":
        print("  · file_index：文件不存在（跳过）")
    else:
        print(
            f"  · file_index：迁移失败 - {r_fi.get('reason', '')}",
            file=sys.stderr,
        )

    all_ok = (
        status_d in ("migrated", "already_migrated", "no_file")
        and status_f in ("migrated", "already_migrated", "no_file")
    )
    if all_ok:
        print("迁移完成。可安全使用 v1.2 CLI screening 命令。")
        return EXIT_SUCCESS
    return EXIT_RUNTIME_ERROR

def _split_kv(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise ValueError(
            f"--set-maybe 需要 cid=decision 格式，收到：{value}"
        )
    cid, decision = value.split("=", 1)
    cid = cid.strip()
    decision = decision.strip()
    if not cid or not decision:
        raise ValueError(
            f"--set-maybe 需要 cid=decision 格式，收到：{value}"
        )
    return cid, decision

def _list_maybe(library_root: Path) -> int:
    path = library_root / "screening_decisions.parquet"
    if not path.exists():
        print("无 maybe 记录。")
        return EXIT_SUCCESS
    df = pd.read_parquet(path)
    if "user_decision" not in df.columns:
        print("无 maybe 记录。")
        return EXIT_SUCCESS
    maybe = df[df["user_decision"] == "maybe"]
    if len(maybe) == 0:
        print("无 maybe 记录。")
        return EXIT_SUCCESS
    has_notes = "user_notes" in maybe.columns
    for _, row in maybe.iterrows():
        notes = str(row["user_notes"]) if has_notes else ""
        print(f"  {row['canonical_id']}\t{notes}")
    print(f"共 {len(maybe)} 条 maybe 记录。")
    return EXIT_SUCCESS

def _refresh_status(strategy_name: str, library_root: Path) -> int:
    screening.update_strategy_status(strategy_name, library_root)
    print(f"策略 {strategy_name} 状态已刷新。")
    return EXIT_SUCCESS

def _dispatch_archive(parsed: ParsedArgs, config: dict) -> int:
    archive._set_config(config)  # noqa: frozen
    archive_root = _resolve_archive_root_for_cli(config)

    sub = parsed.subcommand
    sa = parsed.subcommand_args

    if sub == "consolidate":
        result = archive.consolidate(sa["strategy_name"])
        print(
            f"合并状态：{result.get('status', '')}，"
            f"行数：{result.get('rows_total', 0)}"
        )
        return EXIT_SUCCESS

    if sub == "preview":
        path = archive.preview_archive(sa["strategy_name"])
        print(f"预览文件：{path}")
        return EXIT_SUCCESS

    if sub == "cleanup_runs":
        keep_recent = int(sa.get("keep_recent", 3))
        if not _confirm_destructive(
            parsed,
            f"删除策略 {sa['strategy_name']} 的旧 run"
            f"（保留最近 {keep_recent} 个）",
        ):
            return EXIT_USER_CANCELLED
        result = archive.cleanup_old_runs(sa["strategy_name"], keep_recent)
        print(
            f"已删除 {result.get('deleted_count', 0)} 个 run，"
            f"释放 {result.get('freed_bytes', 0)} 字节。"
        )
        return EXIT_SUCCESS

    if sub == "archive_strategy":
        archive.archive_strategy_as_unprocessed(sa["strategy_name"])
        print(f"策略 {sa['strategy_name']} 已归档为未处理。")
        return EXIT_SUCCESS

    if sub == "check_concurrent":
        messages = archive.check_concurrent_strategies(archive_root)
        if not messages:
            print("无并发策略提示。")
            return EXIT_SUCCESS
        for msg in messages:
            print(f"[{msg['level']}] {msg['message']} (count={msg['count']})")
        return EXIT_SUCCESS

    if sub == "maintain_index":
        archive.maintain_index()
        print("archive/index.json 已刷新。")
        return EXIT_SUCCESS

    if sub == "delete_strategy":
        return _dispatch_delete_strategy(
            parsed, sa["strategy_name"], archive_root, config,
        )

    if sub == "purge_strategy":
        return _dispatch_purge_strategy(
            parsed, sa["strategy_name"], archive_root, config,
        )

    if sub == "cleanup_orphans":
        return _dispatch_cleanup_orphans(
            parsed, config, archive_root,
        )

    raise ValueError(f"未处理的 archive 子命令：{sub}")

def _dispatch_delete_strategy(
    parsed: ParsedArgs,
    strategy_name: str,
    archive_root: Path,
    config: dict,
) -> int:
    name = str(strategy_name or "").strip()
    if not name:
        print("参数错误：策略名不能为空", file=sys.stderr)
        return EXIT_ARG_ERROR
    if any(c in name for c in ("/", "\\", "..")):
        print(
            f"参数错误：策略名非法（含路径分隔符或 ..）：{name}",
            file=sys.stderr,
        )
        return EXIT_ARG_ERROR

    try:
        from medlitflow import robustness as _rb
        _output_root_raw = str(
            (config.get("project", {}) or {}).get(
                "output_root", "",
            ) or ""
        )
        if _output_root_raw:
            _lock_path = Path(_output_root_raw) / ".pipeline.lock"
            if _lock_path.exists() and not _rb.is_lock_stale(_lock_path):
                print(
                    "错误：pipeline 正在运行，删除操作已拒绝。"
                    "请停止 pipeline 后重试。",
                    file=sys.stderr,
                )
                return EXIT_RUNTIME_ERROR
    except Exception:
        pass

    strategies_dir = archive_root / "strategies" / name
    consolidated_dir = archive_root / "consolidated" / name

    run_count = 0
    if strategies_dir.exists():
        runs_root = strategies_dir / "runs"
        if runs_root.exists():
            try:
                for date_dir in runs_root.iterdir():
                    if not date_dir.is_dir():
                        continue
                    for rdir in date_dir.iterdir():
                        if rdir.is_dir():
                            run_count += 1
            except OSError:
                pass

    size_bytes = 0
    for d in (strategies_dir, consolidated_dir):
        if d.exists():
            try:
                for fp in d.rglob("*"):
                    if fp.is_file():
                        try:
                            size_bytes += fp.stat().st_size
                        except OSError:
                            pass
            except OSError:
                pass
    size_mb = size_bytes / 1024 / 1024

    exclusive_n = -1
    shared_n = -1
    prescan_ok = False
    try:
        _library_root = Path(config["library"]["root"])
        if _library_root.exists():
            _pre = screening.prescan_delete_strategy(name, _library_root)
            if _pre.get("ok"):
                exclusive_n = int(_pre.get("exclusive_count", 0) or 0)
                shared_n = int(_pre.get("shared_count", 0) or 0)
                prescan_ok = True
    except Exception:
        pass

    if prescan_ok:
        library_line = (
            f"  · library {exclusive_n} 篇独占 + {shared_n} 篇共享 → 全部保留\n"
        )
    else:
        library_line = (
            "  · library 数据量未知（预扫描失败，仅 archive 层会被移除）\n"
        )
    action_desc = (
        f"仅移除策略 {name}（模式R，library 数据完整保留）：\n"
        f"  · archive/strategies/{name}/"
        f"（{run_count} 个 run，约 {size_mb:.1f} MB）\n"
        f"  · archive/consolidated/{name}/\n"
        f"  · archive/strategy_registry.parquet 中的策略行\n"
        + library_line
        + f"  · 如需彻底删除文献 → 请用 --purge-strategy\n"
        f"  · 如需后续清理残留文献 → 请用 --cleanup-orphans"
    )
    if not _confirm_destructive(parsed, action_desc):
        return EXIT_USER_CANCELLED

    try:
        result = archive.delete_strategy(name)
    except Exception as exc:
        print(f"删除失败：{exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    status = str(result.get("status", ""))
    if status == "not_found":
        print(f"策略 {name} 不存在于 archive，无需删除。")
        return EXIT_SUCCESS

    if status == "error":
        print(
            f"删除失败：{result.get('message', '')}",
            file=sys.stderr,
        )
        return EXIT_RUNTIME_ERROR

    print(result.get("message", f"已删除策略 {name}"))
    print(
        f"  run 数：{result.get('run_count', 0)}，"
        f"释放 {result.get('freed_bytes', 0)} 字节"
    )
    print(
        f"  registry 行已移除：{result.get('removed_registry', False)}"
    )
    return EXIT_SUCCESS

def _dispatch_purge_strategy(
    parsed: ParsedArgs,
    strategy_name: str,
    archive_root: Path,
    config: dict,
) -> int:
    # 模式P：独占硬删 + 共享重算 + archive 删 + registry 删（不可恢复）
    name = str(strategy_name or "").strip()
    if not name:
        print("参数错误：策略名不能为空", file=sys.stderr)
        return EXIT_ARG_ERROR
    if any(c in name for c in ("/", "\\", "..")):
        print(
            f"参数错误：策略名非法（含路径分隔符或 ..）：{name}",
            file=sys.stderr,
        )
        return EXIT_ARG_ERROR

    try:
        from medlitflow import robustness as _rb
        _output_root_raw = str(
            (config.get("project", {}) or {}).get(
                "output_root", "",
            ) or ""
        )
        if _output_root_raw:
            _lock_path = Path(_output_root_raw) / ".pipeline.lock"
            if _lock_path.exists() and not _rb.is_lock_stale(_lock_path):
                print(
                    "错误：pipeline 正在运行，删除操作已拒绝。"
                    "请停止 pipeline 后重试。",
                    file=sys.stderr,
                )
                return EXIT_RUNTIME_ERROR
    except Exception:
        pass

    library_root = Path(config["library"]["root"])
    try:
        pre = screening.prescan_delete_strategy(name, library_root)
    except Exception as exc:
        print(f"预扫描失败：{exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    if not pre.get("ok"):
        errs = pre.get("errors", [])
        err_text = "; ".join(str(e) for e in errs) if errs else "未知原因"
        print(f"预扫描失败：{err_text}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    exclusive_n = int(pre.get("exclusive_count", 0) or 0)
    shared_n = int(pre.get("shared_count", 0) or 0)
    files_move_n = int(pre.get("files_to_move", 0) or 0)
    size_mb_pre = int(pre.get("size_bytes", 0) or 0) / 1024 / 1024
    decisions_n = int(pre.get("decisions_to_delete", 0) or 0)

    action_desc = (
        f"彻底删除策略 {name}（模式P，不可恢复）：\n"
        f"  · 独占文献 {exclusive_n} 篇 → 硬删除"
        f"（含文件，约 {size_mb_pre:.1f} MB）\n"
        f"  · 共享文献 {shared_n} 篇 → 保留 + 移除该策略 + 重算位置\n"
        f"  · decisions 该策略 {decisions_n} 行 → 删除\n"
        f"  · archive/strategies/{name}/ + consolidated/{name}/ → 删除\n"
        f"  · 预计重排文件位置 {files_move_n} 篇"
    )
    if not _confirm_destructive(parsed, action_desc):
        return EXIT_USER_CANCELLED

    try:
        result = screening.delete_strategy_cascade(
            name, library_root, config,
            delete_archive=True,
            prescan_result=pre,
        )
    except Exception as exc:
        print(f"删除失败：{exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    if not result.get("ok"):
        errs = result.get("errors", [])
        err_text = "; ".join(str(e) for e in errs) if errs else "未知原因"
        partial = bool(result.get("partial", False))
        txn_dir = str(result.get("txn_retained_at", "") or "")
        if partial:
            print(
                f"级联删除部分失败（数据已部分落盘）：\n"
                f"  错误：{err_text}\n"
                f"  事务备份：{txn_dir}",
                file=sys.stderr,
            )
        else:
            print(f"删除失败：{err_text}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    exclusive_deleted = int(result.get("exclusive_deleted", 0) or 0)
    shared_updated = int(result.get("shared_updated", 0) or 0)
    files_moved = int(result.get("files_moved", 0) or 0)
    freed_mb = int(result.get("freed_bytes", 0) or 0) / 1024 / 1024
    decisions_deleted = int(result.get("decisions_deleted", 0) or 0)

    print(f"已彻底删除策略 {name}：")
    print(f"  · 独占文献删除 {exclusive_deleted} 篇")
    print(f"  · 共享文献更新 {shared_updated} 篇")
    print(f"  · 重排文件位置 {files_moved} 篇")
    print(f"  · 删除决策 {decisions_deleted} 行")
    print(f"  · 释放空间 {freed_mb:.2f} MB")
    print("  · archive 快照已删除（下次同策略检索从零开始）")
    return EXIT_SUCCESS

def _dispatch_cleanup_orphans(
    parsed: ParsedArgs,
    config: dict,
    archive_root: Path,
) -> int:
    # 计算孤儿 → 二次确认 → 逐个 delete_strategy_cascade(delete_archive=True)
    library_root = Path(config["library"]["root"])
    if not library_root.exists():
        print(f"library 不存在：{library_root}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    try:
        alive = screening.get_alive_strategies(archive_root)
        if alive is None:
            print(
                "strategy_registry 不可用（可能首次使用或损坏），"
                "视为无孤儿策略。"
            )
            return EXIT_SUCCESS
        records_path = library_root / "records_final.parquet"
        records = (
            pd.read_parquet(records_path)
            if records_path.exists() else None
        )
        orphans = screening.compute_orphan_strategies(records, alive)
    except Exception as exc:
        print(f"计算孤儿失败：{exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    if not orphans:
        print("无孤儿策略文献。")
        return EXIT_SUCCESS

    total = sum(o["record_count"] for o in orphans)
    print(f"检测到 {len(orphans)} 个孤儿策略，共 {total} 篇文献：")
    for o in orphans:
        print(f"  · {o['name']}  —  {o['record_count']} 篇")

    action_desc = (
        f"清理上述 {len(orphans)} 个孤儿策略的文献"
        f"（共 {total} 篇，含已下载文件）"
    )
    if not _confirm_destructive(parsed, action_desc):
        return EXIT_USER_CANCELLED

    ok_count = 0
    failed_names: list[str] = []
    for o in orphans:
        name = o["name"]
        print(f"  正在清理 {name} ...")
        try:
            result = screening.delete_strategy_cascade(
                name, library_root, config,
                delete_archive=True,
            )
            if result.get("ok"):
                ok_count += 1
                print(
                    f"    ✓ 独占 {result.get('exclusive_deleted', 0)}，"
                    f"共享 {result.get('shared_updated', 0)}"
                )
            else:
                errs = result.get("errors", [])
                failed_names.append(f"{name}: {errs}")
                print(f"    ✗ 失败：{errs}", file=sys.stderr)
        except Exception as exc:
            failed_names.append(f"{name}: {exc}")
            print(f"    ✗ 失败：{exc}", file=sys.stderr)

    print()
    print(f"清理完成：成功 {ok_count}/{len(orphans)}")
    if failed_names:
        print("失败项：", file=sys.stderr)
        for f in failed_names:
            print(f"  · {f}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    return EXIT_SUCCESS

def _resolve_archive_root_for_cli(config: dict) -> Path:
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

def _dispatch_zotero_prune(parsed, config, library_root):
    try:
        verify = zotero_bridge.verify_zotero_links(library_root, config)
    except RuntimeError as exc:
        print(f"校验失败：{exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    n_confirmed = len(verify["confirmed_missing"])
    n_unverified = len(verify["unverified"])
    n_invalid = len(verify["invalid"])

    if n_confirmed == 0:
        print(
            f"无确认失效记录"
            f"（未验证 {n_unverified} 条 + 非法 {n_invalid} 条均保留）。"
        )
        return EXIT_SUCCESS

    force = bool(parsed.yes)
    if n_unverified > 0 and not force:
        print(
            f"已拒绝：存在 {n_unverified} 条未验证记录（网络/超时）。"
            f"为防误删，请确认 Zotero 稳定后重试；"
            f"或用 --yes 强制继续（只删确认失效的 {n_confirmed} 条，"
            f"未验证的 {n_unverified} 条将保留）。",
            file=sys.stderr,
        )
        return EXIT_RUNTIME_ERROR

    lines = [
        f"即将清理 {n_confirmed} 条确认失效记录（已从 Zotero 删除）",
    ]
    if n_unverified > 0:
        lines.append(f"  · 注意：{n_unverified} 条暂无法验证，将保留")
    if n_invalid > 0:
        lines.append(f"  · 注意：{n_invalid} 条 key 为空/非法，将保留")
    action_desc = "\n".join(lines)

    if not _confirm_destructive(parsed, action_desc):
        return EXIT_USER_CANCELLED

    try:
        result = zotero_bridge.prune_zotero_links(
            library_root, config, force=force,
        )
    except RuntimeError as exc:
        print(f"清理失败：{exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    print(
        f"已清理 {result['removed']} 条确认失效记录，"
        f"剩余 {result['after']} 条。"
    )
    if result.get("unverified_count"):
        print(f"  · {result['unverified_count']} 条未验证记录已保留")
    if result.get("invalid_count"):
        print(
            f"  · {result['invalid_count']} 条非法记录已保留"
            f"（请手动处理 library/zotero_links.parquet）"
        )
    return EXIT_SUCCESS

def _dispatch_zotero(parsed, config):
    library_root = Path(config["library"]["root"])

    if parsed.zotero_prune:
        return _dispatch_zotero_prune(parsed, config, library_root)

    if parsed.zotero_verify:
        try:
            result = zotero_bridge.verify_zotero_links(library_root, config)
        except RuntimeError as exc:
            print(f"校验失败：{exc}", file=sys.stderr)
            return EXIT_RUNTIME_ERROR
        print(f"zotero_links 总记录：{result['total']}")
        print(f"  Zotero 中仍存在：{result['alive']}")
        print(f"  已从 Zotero 删除（确认）：{len(result['confirmed_missing'])}")
        print(f"  暂无法验证（网络/超时）：{len(result['unverified'])}")
        print(f"  记录 key 为空/非法：{len(result['invalid'])}")
        print(f"  超过 90 天未同步：{len(result['stale'])}")
        if result["confirmed_missing_report"]:
            print(f"  确认失效清单：{result['confirmed_missing_report']}")
        if result["unverified_report"]:
            print(f"  未验证清单：{result['unverified_report']}")
        if result["stale_report"]:
            print(f"  超期清单：{result['stale_report']}")
        return EXIT_SUCCESS

    included_df = _build_included_df(library_root)
    if included_df is None or len(included_df) == 0:
        print("无 include 记录，跳过 Zotero 同步。")
        return EXIT_SUCCESS

    zotero_bridge.sync_to_zotero(included_df, config)
    print(f"已完成 {len(included_df)} 条记录的 Zotero 同步尝试。")
    print(f"若存在失败条目，见 {library_root}/zotero_audit.csv")
    return EXIT_SUCCESS

def _build_included_df(library_root: Path) -> pd.DataFrame:
    decisions_path = library_root / "screening_decisions.parquet"
    records_path = library_root / "records_final.parquet"
    if not decisions_path.exists() or not records_path.exists():
        return pd.DataFrame()

    decisions = pd.read_parquet(decisions_path)
    if (
        "user_decision" not in decisions.columns
        or "canonical_id" not in decisions.columns
    ):
        return pd.DataFrame()

    included_cids = set(
        decisions.loc[
            decisions["user_decision"] == "include", "canonical_id",
        ].astype(str).tolist()
    )
    if not included_cids:
        return pd.DataFrame()

    records = pd.read_parquet(records_path)
    if "canonical_id" not in records.columns:
        return pd.DataFrame()
    records = records[
        records["canonical_id"].astype(str).isin(included_cids)
    ]

    keep = ["canonical_id", "title", "authors", "doi", "journal", "year"]
    for c in keep:
        if c not in records.columns:
            records[c] = ""
    return records[keep].reset_index(drop=True)

def _confirm_destructive(parsed: ParsedArgs, action: str) -> bool:
    if parsed.yes:
        print(f"[非交互] --yes 已启用：自动确认 {action}")
        return True

    if not sys.stdin.isatty():
        print(
            "非交互环境且未提供 --yes，已取消。",
            file=sys.stderr,
        )
        return False

    try:
        reply = input(f"即将 {action}，{_PROMPT_CONFIRM_SUFFIX}")
    except EOFError:
        print("读取输入失败，已取消。", file=sys.stderr)
        return False

    return reply.strip().lower() in ("y", "yes")

def main(argv: list[str] | None = None) -> int:
    # 退出码：0 成功 / 1 参数错误 / 2 运行错误 / 3 用户取消
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _CLIArgumentError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return EXIT_ARG_ERROR
    except SystemExit as exc:
        # --help / --version 走 argparse 内部 sys.exit
        return int(exc.code) if exc.code is not None else EXIT_SUCCESS

    try:
        parsed = _normalize_args(args)
        _validate_combinations(parsed)
    except (_CLIArgumentError, ValueError) as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return EXIT_ARG_ERROR

    try:
        config = core.load_config(parsed.config_path)
        _apply_cli_overrides(config, parsed)
    except core.ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    except Exception as exc:
        if parsed.verbose:
            import traceback
            traceback.print_exc()
        print(f"覆盖注入失败：{exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    pipeline.set_interactor(CLIInteractor(verbose=parsed.verbose))

    try:
        return _dispatch(parsed, config)
    
    except KeyboardInterrupt:
        print("用户取消。", file=sys.stderr)
        return EXIT_USER_CANCELLED
    except robustness.InterruptRequested:
        print("已中断，进度已保存。", file=sys.stderr)
        return EXIT_USER_CANCELLED
    except core.ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    except (RuntimeError, OSError) as exc:
        if parsed.verbose:
            import traceback
            traceback.print_exc()
        print(f"运行错误：{exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    except Exception as exc:
        if parsed.verbose:
            import traceback
            traceback.print_exc()
        print(f"未预期错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    finally:
        archive._set_config(None)

if __name__ == "__main__":
    sys.exit(main())