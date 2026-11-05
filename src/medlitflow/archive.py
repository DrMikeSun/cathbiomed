"""medlitflow archive.py — 策略中心归档层。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from medlitflow import core, record_normalize

__all__ = [
    "get_last_run_date",
    "record_run",
    "consolidate",
    "preview_archive",
    "cleanup_old_runs",
    "maintain_index",
    "check_concurrent_strategies",
    "archive_strategy_as_unprocessed",
    "delete_strategy",
    "unmark_archived_unprocessed",
    "cleanup_orphan_strategy",
]

SNAPSHOT_POLICY_METADATA_ONLY = "metadata_only"
SNAPSHOT_POLICY_PER_RUN       = "per_run"
SNAPSHOT_POLICY_FULL          = "full"
VALID_SNAPSHOT_POLICIES = frozenset({
    SNAPSHOT_POLICY_METADATA_ONLY,
    SNAPSHOT_POLICY_PER_RUN,
    SNAPSHOT_POLICY_FULL,
})
DEFAULT_SNAPSHOT_POLICY = SNAPSHOT_POLICY_PER_RUN
DEFAULT_KEEP_RECENT_RUNS = 3
MIN_KEEP_RECENT_RUNS = 1
DEFAULT_KEEP_RECENT_VERSIONS = 5
DEFAULT_KEEP_RECENT_PREVIEWS = 3
DEFAULT_CONSOLIDATION_THRESHOLD_RUNS = 5
DEFAULT_CONCURRENT_THRESHOLDS = {
    "info":           5,
    "warning":        10,
    "strong_warning": 15,
}

ARCHIVE_AUDIT_FILENAME = "archive_audit.csv"
ARCHIVE_AUDIT_COLUMNS = [
    "timestamp",
    "strategy_name",
    "run_name",
    "event",
    "snapshot_policy",
    "files_copied",
    "bytes_copied",
    "status",
    "message",
]

AUDIT_EVENTS = frozenset({
    "record_run_start",
    "record_run_done",
    "record_run_skip_idempotent",
    "record_run_overwrite",
    "record_run_degraded",
    "record_run_recovered_from_old",
    "consolidate_start",
    "consolidate_done",
    "consolidate_skip",
    "preview_archive",
    "cleanup_old_runs",
    "maintain_index",
    "archive_as_unprocessed",
    "version_bump",
    "delete_strategy",
})

ARCHIVE_INDEX_FILENAME        = "index.json"
ARCHIVE_MANIFEST_FILENAME     = "archive_manifest.json"
LAST_RUN_DATES_FILENAME       = "last_run_dates.json"
CONSOLIDATE_MANIFEST_FILENAME = "consolidate_manifest.json"
ARCHIVED_UNPROCESSED_MARKER   = ".archived_unprocessed.json"
KEEP_MARKER_FILENAME          = ".keep"
PREVIEW_DIRNAME               = "preview"
VERSIONS_DIRNAME              = "versions"
LATEST_VERSION_FILENAME       = "latest.txt"
RUNS_DIRNAME                  = "runs"
CONSOLIDATED_SUBDIR           = "consolidated"

STRATEGY_STATUS_COLLECTING           = "collecting"
STRATEGY_STATUS_UNSCREENED           = "unscreened"
STRATEGY_STATUS_PARTIALLY_SCREENED   = "partially_screened"
STRATEGY_STATUS_CONFIRMED            = "confirmed"
STRATEGY_STATUS_ARCHIVED_UNPROCESSED = "archived_unprocessed"
ACTIVE_STRATEGY_STATUSES = frozenset({
    STRATEGY_STATUS_COLLECTING,
    STRATEGY_STATUS_UNSCREENED,
    STRATEGY_STATUS_PARTIALLY_SCREENED,
})


DEFAULT_MIN_FREE_DISK_MB_FOR_FULL = 512
ISO_DATE_LEN = 10
_FULL_COPY_DIRS = ("interim", "papers/inbox", "audit")
_FULL_COPY_OUTPUT_PATTERN = "*.csv"
_STRATEGY_REGISTRY_COLUMNS = [
    "strategy_name", "strategy_status", "created_at",
    "last_activity_at", "unscreened_count", "maybe_count",
    "trashed_count", "days_since_last_activity",
]

_config: dict | None = None

def _set_config(config: dict | None) -> None:
    global _config
    _config = config

def _get_config() -> dict:
    global _config
    if _config is None:
        _config = core.load_config()
    return _config

def _now_iso() -> str:
    return core.now_iso()

def _today_iso() -> str:
    return core.today_iso()

_is_empty = record_normalize.is_empty
_safe_str = record_normalize.safe_str

def _safe_int(v: Any) -> int | None:
    if _is_empty(v):
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None

def _to_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

def _load_json(path: Path) -> dict:
    if not Path(path).exists():
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

def _write_json_atomic(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        os.replace(tmp, path)
    except OSError:
        sibling = path.with_name(path.name + ".sibling")
        try:
            shutil.copy2(tmp, sibling)
            os.replace(sibling, path)
        except OSError:
            try:
                if sibling.exists():
                    sibling.unlink()
            except OSError:
                pass
            raise
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

def _compute_dataframe_checksum(df: pd.DataFrame) -> str:
    if df is None or len(df) == 0:
        payload = b""
    else:
        try:
            payload = df.to_csv(index=False).encode("utf-8")
        except Exception:
            payload = b""
    digest = hashlib.sha256(payload).hexdigest()
    return f"sha256:{digest}"

def _compute_file_size(path: Path) -> int:
    try:
        return int(Path(path).stat().st_size)
    except (OSError, AttributeError):
        return 0

def _compute_dir_size(path: Path) -> int:
    total = 0
    p = Path(path)
    if not p.exists():
        return 0
    for fp in p.rglob("*"):
        if fp.is_file():
            try:
                total += fp.stat().st_size
            except OSError:
                continue
    return total

def _copy_file_preserve(src: Path, dst: Path) -> tuple[int, int]:
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return 1, int(src.stat().st_size)
    except OSError:
        return 0, 0

def _resolve_archive_root(config: dict) -> Path:
    archive_cfg = config.get("archive", {}) or {}
    strategies_root = _safe_str(archive_cfg.get("strategies_root", ""))
    if strategies_root:
        return Path(strategies_root).parent
    consolidated_root = _safe_str(archive_cfg.get("consolidated_root", ""))
    if consolidated_root:
        return Path(consolidated_root).parent
    project_cfg = config.get("project", {}) or {}
    output_root = _safe_str(project_cfg.get("output_root", "")) or "./data"
    return Path(output_root) / "archive"

def _resolve_library_root(config: dict) -> Path:
    library_cfg = config.get("library", {}) or {}
    root = _safe_str(library_cfg.get("root", ""))
    if root:
        return Path(root)
    project_cfg = config.get("project", {}) or {}
    output_root = _safe_str(project_cfg.get("output_root", "")) or "./data"
    return Path(output_root) / "library"

def _resolve_run_root_for_record(config: dict, run_name: str) -> Path:
    project_cfg = config.get("project", {}) or {}
    output_root = _safe_str(project_cfg.get("output_root", "")) or "./data"
    return Path(output_root) / "runs" / run_name

def _validate_path_component(value: str, label: str) -> str:
    text = _safe_str(value)
    if not text:
        raise ValueError(f"{label} 不能为空")
    if any(c in text for c in ("/", "\\", "..")):
        raise ValueError(f"{label} 非法（含路径分隔符或 ..）：{text}")
    return text

def _resolve_strategy_dir(strategy_name: str, archive_root: Path) -> Path:
    return (
        Path(archive_root) / "strategies"
        / _validate_path_component(strategy_name, "strategy_name")
    )

def _resolve_run_dir(
    strategy_name: str,
    run_name: str,
    date: str,
    archive_root: Path,
) -> Path:
    return (
        Path(archive_root) / "strategies"
        / _validate_path_component(strategy_name, "strategy_name")
        / RUNS_DIRNAME
        / _validate_path_component(date, "date")
        / _validate_path_component(run_name, "run_name")
    )

def _is_iso_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False

def _parse_run_date(run_metadata: dict, run_name: str) -> str:
    started_at = _safe_str(run_metadata.get("started_at", "")) if run_metadata else ""
    if len(started_at) >= ISO_DATE_LEN:
        cand = started_at[:ISO_DATE_LEN]
        if _is_iso_date(cand):
            return cand
    m = re.search(r"\d{4}-\d{2}-\d{2}", run_name or "")
    if m and _is_iso_date(m.group(0)):
        return m.group(0)
    return _today_iso()

def _extract_query_execution(run_root: Path) -> dict | None:
    p = Path(run_root) / "output" / "query_execution.json"
    if not p.exists():
        return None
    data = _load_json(p)
    return data if data else None

def _build_summary_counts(df: pd.DataFrame) -> dict:
    if df is None or len(df) == 0:
        return {
            "rows_total": 0,
            "columns": [],
            "has_canonical_id": False,
        }
    return {
        "rows_total": int(len(df)),
        "columns": list(df.columns),
        "has_canonical_id": "canonical_id" in df.columns,
    }

def _collect_full_files(run_root: Path, dest: Path) -> tuple[int, int, int]:
    run_root = Path(run_root)
    dest = Path(dest)
    files = 0
    bytes_copied = 0
    failed = 0

    for sub in _FULL_COPY_DIRS:
        src_base = run_root / sub
        if not src_base.exists():
            continue
        for src_fp in src_base.rglob("*"):
            if not src_fp.is_file():
                continue
            rel = src_fp.relative_to(run_root)
            dst_fp = dest / rel
            n, b = _copy_file_preserve(src_fp, dst_fp)
            if n == 0:
                failed += 1
            else:
                files += n
                bytes_copied += b

    out_src = run_root / "output"
    if out_src.exists():
        for src_fp in out_src.glob(_FULL_COPY_OUTPUT_PATTERN):
            if not src_fp.is_file():
                continue
            rel = src_fp.relative_to(run_root)
            dst_fp = dest / rel
            n, b = _copy_file_preserve(src_fp, dst_fp)
            if n == 0:
                failed += 1
            else:
                files += n
                bytes_copied += b

    if files == 0 and failed > 0:
        raise OSError(
            f"full 档复制全部失败：{failed} 个文件均未成功"
        )
    return files, bytes_copied, failed

def _append_audit(archive_root: Path, rows: list[dict]) -> None:
    if not rows:
        return
    try:
        path = Path(archive_root) / ARCHIVE_AUDIT_FILENAME
        new_df = pd.DataFrame(rows, columns=ARCHIVE_AUDIT_COLUMNS).astype(str)
        if path.exists():
            try:
                existing = pd.read_csv(path, dtype=str).fillna("")
                combined = pd.concat([existing, new_df], ignore_index=True)
            except Exception:
                combined = new_df
        else:
            combined = new_df
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        try:
            combined.to_csv(tmp, index=False, encoding="utf-8-sig")
            os.replace(tmp, path)
        except BaseException:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise
    except Exception:
        pass

def _make_audit_row(
    event: str,
    *,
    strategy_name: str = "",
    run_name: str = "",
    snapshot_policy: str = "",
    files_copied: int = 0,
    bytes_copied: int = 0,
    status: str = "ok",
    message: str = "",
) -> dict:
    if event not in AUDIT_EVENTS:
        event = f"unknown:{event}"
    return {
        "timestamp": _now_iso(),
        "strategy_name": strategy_name,
        "run_name": run_name,
        "event": event,
        "snapshot_policy": snapshot_policy,
        "files_copied": int(files_copied),
        "bytes_copied": int(bytes_copied),
        "status": status,
        "message": message,
    }

def _load_strategy_registry(archive_root: Path) -> pd.DataFrame:
    path = Path(archive_root) / "strategy_registry.parquet"
    if not path.exists():
        return pd.DataFrame(columns=_STRATEGY_REGISTRY_COLUMNS)
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        backup_msg = ""
        try:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            corrupt_path = path.with_name(
                f"{path.name}.corrupt_{ts}"
            )
            shutil.copy2(path, corrupt_path)
            backup_msg = f"（已备份为 {corrupt_path.name}）"
        except Exception:
            backup_msg = "（备份失败，请手动备份）"
        raise RuntimeError(
            f"strategy_registry parquet 读取失败：{path.name}"
            f"（{type(exc).__name__}: {exc}）{backup_msg}。"
            f"为避免用空表覆盖损坏文件，本次读取已中止。"
            f"请修复或从备份恢复后重试。"
        ) from exc
    for c in _STRATEGY_REGISTRY_COLUMNS:
        if c not in df.columns:
            df[c] = ""
    return df[_STRATEGY_REGISTRY_COLUMNS].copy()

def _save_strategy_registry(archive_root: Path, df: pd.DataFrame) -> None:
    path = Path(archive_root) / "strategy_registry.parquet"
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    except Exception as exc:
        if tmp is not None:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
        raise RuntimeError(
            f"strategy_registry 写入失败：{path}"
            f"（{type(exc).__name__}: {exc}）"
        ) from exc

def _has_archived_marker(strategy_dir: Path) -> bool:
    return (Path(strategy_dir) / ARCHIVED_UNPROCESSED_MARKER).exists()

_CONSOLIDATE_FIELD_STRATEGIES = record_normalize.FIELD_STRATEGIES
_CROSS_RUN_CONFLICT_FIELDS = (
    "title", "journal", "year", "doi", "authors", "abstract_final",
)
_CONFLICT_LOG_EXTRA_FIELDS = ("pmid", "pmcid")

_parse_json_list = record_normalize._parse_json_list
_increment_count = record_normalize._increment_count
_max_int = record_normalize._max_int

def _classify_variant_status(variants: list) -> str:
    if len(variants) <= 1:
        return "single"
    hashes = {
        v.get("content_hash", "")
        for v in variants if isinstance(v, dict)
    }
    hashes.discard("")
    if len(hashes) == 1:
        return "identical"
    lengths = [
        int(v.get("length", 0))
        for v in variants if isinstance(v, dict)
    ]
    if not lengths:
        return "single"
    max_len = max(lengths)
    min_len = min(lengths)
    if max_len > 0 and (max_len - min_len) / max_len < 0.05:
        return "near_identical"
    return "divergent"

def _compute_matched_strategy_count(strategy_names: str) -> int:
    if not strategy_names:
        return 0
    parts = [p.strip() for p in strategy_names.split(";") if p.strip()]
    return len(parts)

def _normalize_for_conflict(value: Any, field: str) -> str:
    if _is_empty(value):
        return ""
    text = _safe_str(value)
    if not text:
        return ""
    if field in ("title", "journal", "abstract_final"):
        return core.normalize_title(text)
    if field == "doi":
        return core.normalize_doi(text)
    if field == "pmid":
        return core.normalize_pmid(text)
    if field == "pmcid":
        return core.normalize_pmcid(text)
    if field == "year":
        try:
            return str(int(float(text)))
        except (ValueError, TypeError):
            return text
    if field == "authors":
        first_author = text.split(";")[0].strip()
        return core.normalize_title(first_author)
    return text

_split_conflict_fields = record_normalize.split_conflict_fields

def _merge_field_by_strategy(field: str, old_val: Any, new_val: Any) -> Any:
    return record_normalize.merge_field_by_strategy(field, old_val, new_val)

def _recompute_consolidated_fields(merged: dict) -> None:
    variants = _parse_json_list(merged.get("abstract_variants_json", "[]"))
    merged["abstract_variant_count"] = len(variants)
    merged["abstract_variant_status"] = _classify_variant_status(variants)

    status = _safe_str(merged.get("abstract_variant_status", ""))
    if status == "divergent":
        prev = _safe_str(merged.get("abstract_divergence_type", ""))
        merged["abstract_divergence_type"] = prev or "unknown"
    else:
        merged["abstract_divergence_type"] = ""

    merged["abstract_missing"] = not bool(
        _safe_str(merged.get("abstract_final", ""))
    )
    merged["matched_strategy_count"] = _compute_matched_strategy_count(
        _safe_str(merged.get("strategy_names", ""))
    )
    merged["has_conflict"] = bool(merged.get("has_conflict", False))

def _merge_rows_sequentially(rows: list[pd.Series]) -> dict:
    all_cols: set[str] = set()
    for r in rows:
        all_cols.update(r.index)

    merged: dict = {}
    for col in all_cols:
        merged[col] = rows[0].get(col)
    merged["appearance_count"] = 1

    cross_conflict_set: set[str] = set()

    for new_row in rows[1:]:
        for field in _CROSS_RUN_CONFLICT_FIELDS:
            if field == "abstract_final":
                old_val = _normalize_for_conflict(merged.get(field), field)
                new_val = _normalize_for_conflict(new_row.get(field), field)
            else:
                old_val = record_normalize.make_key(
                    merged.get(field), field,
                )
                new_val = record_normalize.make_key(
                    new_row.get(field), field,
                )
            if old_val and new_val and old_val != new_val:
                cross_conflict_set.add(field)
        for field in _CONFLICT_LOG_EXTRA_FIELDS:
            old_val = _normalize_for_conflict(merged.get(field), field)
            new_val = _normalize_for_conflict(new_row.get(field), field)
            if old_val and new_val and old_val != new_val:
                cross_conflict_set.add(field)

        for col in all_cols:
            if col == "appearance_count":
                continue
            old_val = merged.get(col)
            new_val = new_row.get(col)
            merged[col] = _merge_field_by_strategy(col, old_val, new_val)
        merged["appearance_count"] = _increment_count(
            merged.get("appearance_count")
        )

    if cross_conflict_set:
        existing = _split_conflict_fields(
            _safe_str(merged.get("conflict_fields", ""))
        )
        all_cf = existing | cross_conflict_set
        merged["conflict_fields"] = ";".join(sorted(all_cf))
        merged["has_conflict"] = True

    _recompute_consolidated_fields(merged)
    return merged

def _consolidate_merge(dfs: pd.DataFrame) -> pd.DataFrame:
    if dfs is None or len(dfs) == 0:
        return pd.DataFrame()

    groups: dict[str, list[pd.Series]] = {}
    order: list[str] = []
    for _, row in dfs.iterrows():
        cid = _safe_str(row.get("canonical_id", ""))
        if not cid:
            continue
        if cid not in groups:
            groups[cid] = []
            order.append(cid)
        groups[cid].append(row)

    merged_rows: list[dict] = []
    for cid in order:
        rows = groups[cid]
        d = _merge_rows_sequentially(rows)
        d["canonical_id"] = cid
        merged_rows.append(d)

    return pd.DataFrame(merged_rows)

def _copy_policy_files(
    policy: str,
    run_root: Path,
    dest: Path,
    records_final_snapshot: pd.DataFrame,
) -> tuple[int, int, list[str], bool]:
    run_root = Path(run_root)
    dest = Path(dest)
    files = 0
    bytes_copied = 0
    copied: list[str] = []
    failed = 0

    for fname in ("run_metadata.json", "query_execution.json"):
        src = run_root / "output" / fname
        if src.exists():
            n, b = _copy_file_preserve(src, dest / fname)
            if n:
                files += n
                bytes_copied += b
                copied.append(fname)
            else:
                failed += 1

    sc_src = run_root / "output" / "summary_counts.json"
    if sc_src.exists():
        n, b = _copy_file_preserve(sc_src, dest / "summary_counts.json")
        if n:
            files += n
            bytes_copied += b
            copied.append("summary_counts.json")
        else:
            failed += 1
    else:
        try:
            (dest / "summary_counts.json").write_text(
                json.dumps(
                    _build_summary_counts(records_final_snapshot),
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
            files += 1
            copied.append("summary_counts.json")
        except OSError:
            failed += 1

    if policy in (SNAPSHOT_POLICY_PER_RUN, SNAPSHOT_POLICY_FULL):
        for fname in ("records_final.parquet", "summary_report.md"):
            src = run_root / "output" / fname
            if src.exists():
                n, b = _copy_file_preserve(src, dest / fname)
                if n:
                    files += n
                    bytes_copied += b
                    copied.append(fname)
                else:
                    failed += 1

    if policy == SNAPSHOT_POLICY_FULL:
        n, b, f = _collect_full_files(run_root, dest)
        files += n
        bytes_copied += b
        failed += f

    partial = failed > 0
    return files, bytes_copied, copied, partial

def _cleanup_stale_siblings(
    parent: Path,
    run_name: str,
    keep_tmp_name: str | None = None,
) -> None:
    for pattern in (f"{run_name}.tmp-*", f"{run_name}.old-*"):
        for stale in parent.glob(pattern):
            if keep_tmp_name and stale.name == keep_tmp_name:
                continue
            try:
                shutil.rmtree(stale, ignore_errors=True)
            except OSError:
                pass

def _recover_from_old(parent: Path, run_dir: Path) -> bool:
    if run_dir.exists():
        return False
    candidates = sorted(
        parent.glob(f"{run_dir.name}.old-*"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    for old_dir in candidates:
        if not old_dir.is_dir():
            continue
        try:
            os.rename(old_dir, run_dir)
            return True
        except OSError:
            continue
    return False

def _update_last_run_dates(
    strategy_dir: Path,
    strategy_name: str,
    run_name: str,
    last_run_dates: dict[str, str],
) -> None:
    path = Path(strategy_dir) / LAST_RUN_DATES_FILENAME
    data = _load_json(path) or {}
    sources = data.get("sources", {}) or {}
    for src, date_val in (last_run_dates or {}).items():
        sources[src] = {
            "last_run_date": _safe_str(date_val),
            "last_run_name": run_name,
        }
    data["strategy_name"] = strategy_name
    data["sources"] = sources
    data["updated_at"] = _now_iso()
    _write_json_atomic(path, data)

def _bump_version_if_needed(
    strategy_dir: Path,
    run_name: str,
    run_metadata: dict,
    query_execution: dict | None,
) -> str | None:
    versions_root = Path(strategy_dir) / VERSIONS_DIRNAME
    latest_path = versions_root / LATEST_VERSION_FILENAME
    current_version = ""
    if latest_path.exists():
        try:
            current_version = latest_path.read_text(encoding="utf-8").strip()
        except OSError:
            current_version = ""

    current_date = _parse_run_date(run_metadata or {}, run_name)

    prev_meta: dict = {}
    prev_query: dict = {}
    runs_root = Path(strategy_dir) / RUNS_DIRNAME
    if runs_root.exists():
        candidates: list[tuple[str, str, Path]] = []
        for date_dir in runs_root.iterdir():
            if not date_dir.is_dir():
                continue
            for rdir in date_dir.iterdir():
                if not rdir.is_dir():
                    continue
                if date_dir.name == current_date and rdir.name == run_name:
                    continue
                candidates.append((date_dir.name, rdir.name, rdir))
        candidates.sort(key=lambda t: (t[0], t[1]), reverse=True)
        if candidates:
            prev_run_dir = candidates[0][2]
            prev_meta = _load_json(prev_run_dir / "run_metadata.json")
            prev_query = _load_json(prev_run_dir / "query_execution.json")

    reasons: list[str] = []
    if not current_version:
        reasons.append("initial")
    else:
        if (
            _safe_str(run_metadata.get("from_date", ""))
            != _safe_str(prev_meta.get("from_date", ""))
            or _safe_str(run_metadata.get("to_date", ""))
            != _safe_str(prev_meta.get("to_date", ""))
        ):
            reasons.append("date_range_changed")
        cur_pm = _safe_str(
            ((query_execution or {}).get("pubmed") or {}).get("query", "")
        )
        old_pm = _safe_str(
            ((prev_query or {}).get("pubmed") or {}).get("query", "")
        )
        if cur_pm != old_pm:
            reasons.append("query_changed")
        cur_cr = (query_execution or {}).get("crossref") or []
        old_cr = (prev_query or {}).get("crossref") or []
        if len(cur_cr) != len(old_cr):
            reasons.append("query_changed")
        for src in ("pubmed", "europepmc"):
            cur_f = _safe_str(
                ((query_execution or {}).get(src) or {}).get("filters", "")
            )
            old_f = _safe_str(
                ((prev_query or {}).get(src) or {}).get("filters", "")
            )
            if cur_f != old_f:
                reasons.append("filter_changed")
                break

    if not reasons:
        return None

    if len(reasons) > 1:
        change_reason = "mixed"
    else:
        change_reason = reasons[0]

    if current_version.startswith("v"):
        try:
            n = int(current_version[1:]) + 1
        except (ValueError, IndexError):
            n = 2
    else:
        n = 1
    new_version = f"v{n}"

    version_dir = versions_root / new_version
    tmp_version_dir = versions_root / f"{new_version}.tmp-{uuid.uuid4().hex[:8]}"
    try:
        tmp_version_dir.mkdir(parents=True, exist_ok=True)
        (tmp_version_dir / "run_metadata.json").write_text(
            json.dumps(run_metadata or {}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if query_execution:
            (tmp_version_dir / "query_execution.json").write_text(
                json.dumps(query_execution, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        version_manifest = {
            "version": new_version,
            "created_at": _now_iso(),
            "based_on_run": run_name,
            "from_date": _safe_str(run_metadata.get("from_date", "")),
            "to_date": _safe_str(run_metadata.get("to_date", "")),
            "query_execution_checksum": _compute_dataframe_checksum(
                pd.DataFrame([query_execution or {}])
            ),
            "changed_from": current_version or None,
            "change_reason": change_reason,
        }
        (tmp_version_dir / "version_manifest.json").write_text(
            json.dumps(version_manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if version_dir.exists():
            shutil.rmtree(version_dir, ignore_errors=True)
        os.rename(tmp_version_dir, version_dir)
    except OSError:

        try:
            if tmp_version_dir.exists():
                shutil.rmtree(tmp_version_dir, ignore_errors=True)
        except OSError:
            pass
        return None

    try:
        latest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_latest = latest_path.with_name(latest_path.name + ".tmp")
        tmp_latest.write_text(new_version, encoding="utf-8")
        os.replace(tmp_latest, latest_path)
    except OSError:
        pass

    try:
        vdirs = sorted(
            [d for d in versions_root.iterdir()
             if d.is_dir() and re.fullmatch(r"v\d+", d.name)],
            key=lambda d: int(d.name[1:]),
            reverse=True,
        )
        for old in vdirs[DEFAULT_KEEP_RECENT_VERSIONS:]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass

    return new_version

def record_run(
    strategy_name: str,
    run_name: str,
    run_metadata: dict,
    records_final_snapshot: pd.DataFrame,
    last_run_dates: dict[str, str],
) -> None:
    if not _safe_str(strategy_name):
        raise ValueError("strategy_name 不能为空")
    if not _safe_str(run_name):
        raise ValueError("run_name 不能为空")
    if not isinstance(records_final_snapshot, pd.DataFrame):
        raise TypeError("records_final_snapshot 必须是 pd.DataFrame")
    if "canonical_id" not in records_final_snapshot.columns:
        raise ValueError("records_final_snapshot 缺 canonical_id 列")

    config = _get_config()
    archive_root = _resolve_archive_root(config)
    library_root = _resolve_library_root(config)

    _append_audit(archive_root, [_make_audit_row(
        "record_run_start",
        strategy_name=strategy_name,
        run_name=run_name,
        status="ok",
    )])

    archive_cfg = config.get("archive", {}) or {}
    raw_policy = _safe_str(archive_cfg.get("snapshot_policy", ""))
    if raw_policy and raw_policy not in VALID_SNAPSHOT_POLICIES:
        policy = DEFAULT_SNAPSHOT_POLICY
        _append_audit(archive_root, [_make_audit_row(
            "record_run_start",
            strategy_name=strategy_name,
            run_name=run_name,
            snapshot_policy=raw_policy,
            status="error",
            message=(
                f"非法 snapshot_policy={raw_policy}；"
                f"回退 {DEFAULT_SNAPSHOT_POLICY}"
            ),
        )])
    else:
        policy = raw_policy or DEFAULT_SNAPSHOT_POLICY

    date = _parse_run_date(run_metadata or {}, run_name)

    run_dir = _resolve_run_dir(strategy_name, run_name, date, archive_root)
    run_dir.parent.mkdir(parents=True, exist_ok=True)

    snapshot_checksum = _compute_dataframe_checksum(records_final_snapshot)

    manifest_path = run_dir / ARCHIVE_MANIFEST_FILENAME
    if manifest_path.exists():
        old_manifest = _load_json(manifest_path)
        old_checksum = _safe_str(
            old_manifest.get("records_final_checksum", "")
        )
        if old_checksum == snapshot_checksum:
            _append_audit(archive_root, [_make_audit_row(
                "record_run_skip_idempotent",
                strategy_name=strategy_name,
                run_name=run_name,
                snapshot_policy=policy,
                status="skip",
                message="checksum 相同，跳过",
            )])
            return
        else:
            _append_audit(archive_root, [_make_audit_row(
                "record_run_overwrite",
                strategy_name=strategy_name,
                run_name=run_name,
                snapshot_policy=policy,
                status="ok",
                message="checksum 不同，将原子覆盖",
            )])

    tmp_uuid = uuid.uuid4().hex[:12]
    tmp_dir = run_dir.parent / f"{run_dir.name}.tmp-{tmp_uuid}"

    if _recover_from_old(run_dir.parent, run_dir):
        _append_audit(archive_root, [_make_audit_row(
            "record_run_recovered_from_old",
            strategy_name=strategy_name,
            run_name=run_name,
            snapshot_policy=policy,
            status="ok",
            message="从 old-* 中转目录恢复 run_dir",
        )])

    _cleanup_stale_siblings(
        run_dir.parent, run_dir.name,
        keep_tmp_name=tmp_dir.name,
    )

    effective_policy = policy
    degraded_from: str | None = None
    if policy == SNAPSHOT_POLICY_FULL:
        try:
            free_mb = shutil.disk_usage(archive_root).free / 1024 / 1024
            if free_mb < DEFAULT_MIN_FREE_DISK_MB_FOR_FULL:
                effective_policy = SNAPSHOT_POLICY_PER_RUN
                degraded_from = SNAPSHOT_POLICY_FULL
                _append_audit(archive_root, [_make_audit_row(
                    "record_run_degraded",
                    strategy_name=strategy_name,
                    run_name=run_name,
                    snapshot_policy=SNAPSHOT_POLICY_FULL,
                    status="ok",
                    message=(
                        f"磁盘不足（free={free_mb:.1f}MB），"
                        f"降级 per_run"
                    ),
                )])
        except FileNotFoundError:
            pass

    tmp_dir.mkdir(parents=True, exist_ok=True)

    run_root = _resolve_run_root_for_record(config, run_name)
    try:
        files_copied, bytes_copied, copied_files, partial = _copy_policy_files(
            effective_policy, run_root, tmp_dir, records_final_snapshot,
        )
    except OSError:
        try:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except OSError:
            pass
        raise

    manifest = {
        "schema_version": 1,
        "strategy_name": strategy_name,
        "run_name": run_name,
        "run_date": date,
        "recorded_at": _now_iso(),
        "snapshot_policy": effective_policy,
        "status": "degraded" if degraded_from else (
            "partial" if partial else "complete"
        ),
        "records_final_checksum": snapshot_checksum,
        "records_final_rows": int(len(records_final_snapshot)),
        "records_final_path": (
            "records_final.parquet"
            if effective_policy in (
                SNAPSHOT_POLICY_PER_RUN,
                SNAPSHOT_POLICY_FULL,
            )
            else ""
        ),
        "copied_files": copied_files,
        "total_files": files_copied,
        "total_bytes": bytes_copied,
        "degraded_from": degraded_from,
        "strategy_version": "",
        "last_run_dates_snapshot": dict(last_run_dates or {}),
    }

    try:
        query_exec = _extract_query_execution(run_root)
        new_version = _bump_version_if_needed(
            _resolve_strategy_dir(strategy_name, archive_root),
            run_name, run_metadata or {}, query_exec,
        )
        if new_version:
            manifest["strategy_version"] = new_version
            _append_audit(archive_root, [_make_audit_row(
                "version_bump",
                strategy_name=strategy_name,
                run_name=run_name,
                status="ok",
                message=f"新版本 {new_version}",
            )])
    except Exception:
        pass

    (tmp_dir / ARCHIVE_MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    old_dir = run_dir.parent / f"{run_dir.name}.old-{tmp_uuid}"
    try:
        if run_dir.exists():
            os.rename(run_dir, old_dir)
        os.rename(tmp_dir, run_dir)
    except OSError:
        if old_dir.exists() and not run_dir.exists():
            try:
                os.rename(old_dir, run_dir)
            except OSError:
                pass
        try:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except OSError:
            pass
        raise

    if old_dir.exists():
        shutil.rmtree(old_dir, ignore_errors=True)

    strategy_dir = _resolve_strategy_dir(strategy_name, archive_root)
    try:
        _update_last_run_dates(
            strategy_dir, strategy_name, run_name, last_run_dates or {},
        )
    except Exception:
        pass

    try:
        from medlitflow import screening  # 延迟导入避免循环
        screening.update_strategy_status(strategy_name, library_root)
    except Exception:
        pass

    try:
        maintain_index()
    except Exception:
        pass

    _append_audit(archive_root, [_make_audit_row(
        "record_run_done",
        strategy_name=strategy_name,
        run_name=run_name,
        snapshot_policy=effective_policy,
        files_copied=files_copied,
        bytes_copied=bytes_copied,
        status="ok",
    )])

def get_last_run_date(strategy_name: str, source: str) -> str | None:
    if not _safe_str(strategy_name):
        raise ValueError("strategy_name 不能为空")
    if not _safe_str(source):
        return None
    config = _get_config()
    archive_root = _resolve_archive_root(config)
    path = (
        _resolve_strategy_dir(strategy_name, archive_root)
        / LAST_RUN_DATES_FILENAME
    )
    if not path.exists():
        return None
    data = _load_json(path)
    sources = data.get("sources", {}) or {}
    entry = sources.get(source, {}) or {}
    v = _safe_str(entry.get("last_run_date", ""))
    return v or None

def _iter_runs(
    strategy_dir: Path, sort_desc: bool = False,
) -> list[tuple[str, str, Path]]:
    runs_root = Path(strategy_dir) / RUNS_DIRNAME
    items: list[tuple[str, str, Path]] = []
    if not runs_root.exists():
        return items
    for date_dir in runs_root.iterdir():
        if not date_dir.is_dir():
            continue
        for rdir in date_dir.iterdir():
            if rdir.is_dir():
                items.append((date_dir.name, rdir.name, rdir))
    items.sort(key=lambda t: (t[0], t[1]), reverse=sort_desc)
    return items

def consolidate(strategy_name: str) -> dict:
    if not _safe_str(strategy_name):
        raise ValueError("strategy_name 不能为空")
    config = _get_config()
    archive_root = _resolve_archive_root(config)
    strategy_dir = _resolve_strategy_dir(strategy_name, archive_root)

    _append_audit(archive_root, [_make_audit_row(
        "consolidate_start",
        strategy_name=strategy_name,
        status="ok",
    )])

    runs = _iter_runs(strategy_dir, sort_desc=False)
    eligible: list[tuple[str, str, Path, pd.DataFrame]] = []
    skipped_runs: list[dict] = []
    for date, rname, rdir in runs:
        parquet_path = rdir / "records_final.parquet"
        if not parquet_path.exists():
            skipped_runs.append({"run": f"{date}/{rname}", "reason": "no_records_final"})
            continue
        try:
            df = pd.read_parquet(parquet_path)
        except Exception:
            skipped_runs.append({"run": f"{date}/{rname}", "reason": "parquet_read_failed"})
            continue
        if "canonical_id" not in df.columns:
            skipped_runs.append({"run": f"{date}/{rname}", "reason": "missing_canonical_id"})
            continue
        if ("first_seen_run" not in df.columns
                or "last_seen_run" not in df.columns):
            skipped_runs.append({"run": f"{date}/{rname}", "reason": "missing_run_tracking"})
            continue
        df = df.copy()
        df["_archive_date"] = date
        df["_archive_run"] = rname
        eligible.append((date, rname, rdir, df))

    if not eligible:
        _append_audit(archive_root, [_make_audit_row(
            "consolidate_skip",
            strategy_name=strategy_name,
            status="skip",
            message="无 eligible run",
        )])
        return {
            "status": "skip",
            "consolidated_path": "",
            "manifest_path": "",
            "rows_total": 0,
            "source_runs": [],
            "skipped_runs": skipped_runs,
            "message": "无 eligible run（全部 metadata_only）",
        }

    all_df = pd.concat(
        [e[3] for e in eligible], ignore_index=True, sort=False,
    )
    rows_by_run = {f"{d}/{r}": int(len(df)) for d, r, _, df in eligible}

    merged = _consolidate_merge(all_df)

    for col in ("_archive_date", "_archive_run"):
        if col in merged.columns:
            merged = merged.drop(columns=[col])

    out_dir = Path(archive_root) / CONSOLIDATED_SUBDIR / strategy_name
    out_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = out_dir / "consolidated.parquet"
    manifest_path = out_dir / CONSOLIDATE_MANIFEST_FILENAME

    try:
        tmp_parquet = parquet_path.with_name(parquet_path.name + ".tmp")
        merged.to_parquet(tmp_parquet, index=False)
        os.replace(tmp_parquet, parquet_path)

        checksum = _compute_dataframe_checksum(merged)
        consolidate_manifest = {
            "generated_at": _now_iso(),
            "strategy_name": strategy_name,
            "source_runs": [f"{d}/{r}" for d, r, _, _ in eligible],
            "skipped_runs": skipped_runs,
            "rows_total": int(len(merged)),
            "rows_by_run": rows_by_run,
            "columns": list(merged.columns),
            "checksum": checksum,
        }
        _write_json_atomic(manifest_path, consolidate_manifest)
    except OSError as exc:
        _append_audit(archive_root, [_make_audit_row(
            "consolidate_done",
            strategy_name=strategy_name,
            status="conflict",
            message=f"{type(exc).__name__}: {exc}",
        )])
        return {
            "status": "conflict",
            "consolidated_path": str(parquet_path),
            "manifest_path": str(manifest_path),
            "rows_total": int(len(merged)),
            "source_runs": [f"{d}/{r}" for d, r, _, _ in eligible],
            "skipped_runs": skipped_runs,
            "message": f"写入失败：{exc}",
        }

    _append_audit(archive_root, [_make_audit_row(
        "consolidate_done",
        strategy_name=strategy_name,
        files_copied=2,
        bytes_copied=_compute_dir_size(out_dir),
        status="ok",
    )])

    return {
        "status": "ok",
        "consolidated_path": str(parquet_path),
        "manifest_path": str(manifest_path),
        "rows_total": int(len(merged)),
        "source_runs": [f"{d}/{r}" for d, r, _, _ in eligible],
        "skipped_runs": skipped_runs,
        "message": "合并成功",
    }

def preview_archive(strategy_name: str) -> Path:
    if not _safe_str(strategy_name):
        raise ValueError("strategy_name 不能为空")
    config = _get_config()
    archive_root = _resolve_archive_root(config)
    strategy_dir = _resolve_strategy_dir(strategy_name, archive_root)

    runs = _iter_runs(strategy_dir, sort_desc=False)
    run_infos: list[dict] = []
    skipped_infos: list[dict] = []
    estimated_bytes = 0
    cid_set: set[str] = set()

    for date, rname, rdir in runs:
        parquet_path = rdir / "records_final.parquet"
        manifest = _load_json(rdir / ARCHIVE_MANIFEST_FILENAME)
        policy = _safe_str(manifest.get("snapshot_policy", "")) or "unknown"
        if parquet_path.exists():
            rows = int(manifest.get("records_final_rows", 0) or 0)
            b = int(manifest.get("total_bytes", 0) or 0)
            estimated_bytes += b
            run_infos.append({
                "date": date,
                "run_name": rname,
                "snapshot_policy": policy,
                "rows": rows,
                "bytes": b,
            })
            try:
                col = pd.read_parquet(parquet_path, columns=["canonical_id"])
                cid_set.update(
                    col["canonical_id"].astype(str).tolist()
                )
            except Exception:
                pass
        else:
            skipped_infos.append({
                "date": date,
                "run_name": rname,
                "snapshot_policy": policy,
                "reason": "no records_final.parquet",
            })

    run_count = len(runs)
    eligible_run_count = len(run_infos)
    estimated_rows = len(cid_set)

    keep_recent = DEFAULT_KEEP_RECENT_RUNS
    has_metadata_only = any(
        s["snapshot_policy"] == SNAPSHOT_POLICY_METADATA_ONLY
        for s in skipped_infos
    )
    if run_count > 2 * keep_recent and has_metadata_only:
        recommendation = "cleanup_first"
    elif eligible_run_count >= 2 and estimated_rows > 0:
        recommendation = "consolidate"
    else:
        recommendation = "wait"

    message = (
        f"{run_count} 个 run，其中 {eligible_run_count} 个可合并"
        f"；建议 {recommendation}。"
    )

    payload = {
        "generated_at": _now_iso(),
        "strategy_name": strategy_name,
        "run_count": run_count,
        "eligible_run_count": eligible_run_count,
        "skipped_run_count": len(skipped_infos),
        "estimated_rows": estimated_rows,
        "estimated_bytes": estimated_bytes,
        "runs": run_infos,
        "skipped": skipped_infos,
        "recommendation": recommendation,
        "message": message,
    }

    preview_dir = strategy_dir / PREVIEW_DIRNAME
    preview_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = preview_dir / f"preview_{ts}.json"
    _write_json_atomic(path, payload)

    try:
        previews = sorted(
            preview_dir.glob("preview_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in previews[DEFAULT_KEEP_RECENT_PREVIEWS:]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass

    _append_audit(archive_root, [_make_audit_row(
        "preview_archive",
        strategy_name=strategy_name,
        files_copied=1,
        status="ok",
    )])

    return path

def cleanup_old_runs(strategy_name: str, keep_recent: int = 3) -> dict:
    if not _safe_str(strategy_name):
        raise ValueError("strategy_name 不能为空")
    config = _get_config()
    archive_root = _resolve_archive_root(config)
    strategy_dir = _resolve_strategy_dir(strategy_name, archive_root)

    if keep_recent is None:
        keep = DEFAULT_KEEP_RECENT_RUNS
    else:
        keep = int(keep_recent)
    keep = max(MIN_KEEP_RECENT_RUNS, keep)

    runs = _iter_runs(strategy_dir, sort_desc=True)
    total = len(runs)
    if total == 0:
        _append_audit(archive_root, [_make_audit_row(
            "cleanup_old_runs",
            strategy_name=strategy_name,
            status="skip",
            message="无 run",
        )])
        return {
            "status": "skip",
            "deleted_count": 0,
            "freed_bytes": 0,
            "deleted_runs": [],
            "kept_runs": [],
            "message": "无 run",
        }

    consolidate_manifest = _load_json(
        Path(archive_root) / CONSOLIDATED_SUBDIR / strategy_name
        / CONSOLIDATE_MANIFEST_FILENAME
    )
    protected_runs: set[str] = set(
        _safe_str(x)
        for x in (consolidate_manifest.get("source_runs", []) or [])
    )

    kept: list[str] = []
    deleted: list[str] = []
    freed_bytes = 0

    for idx, (date, rname, rdir) in enumerate(runs):
        key = f"{date}/{rname}"
        if idx < keep:
            kept.append(key)
            continue
        if (rdir / KEEP_MARKER_FILENAME).exists():
            kept.append(key)
            continue
        if key in protected_runs:
            kept.append(key)
            continue
        size = _compute_dir_size(rdir)
        try:
            shutil.rmtree(rdir)
            freed_bytes += size
            deleted.append(key)
        except OSError:
            kept.append(key)

    status = "ok" if deleted else "skip"
    _append_audit(archive_root, [_make_audit_row(
        "cleanup_old_runs",
        strategy_name=strategy_name,
        files_copied=0,
        bytes_copied=freed_bytes,
        status=status,
        message=f"deleted_count={len(deleted)}",
    )])

    return {
        "status": status,
        "deleted_count": len(deleted),
        "freed_bytes": freed_bytes,
        "deleted_runs": deleted,
        "kept_runs": kept,
        "message": f"删除 {len(deleted)} 个 run",
    }

def maintain_index() -> None:
    config = _get_config()
    archive_root = _resolve_archive_root(config)
    strategies_root = archive_root / "strategies"
    registry = _load_strategy_registry(archive_root)
    registry_map: dict[str, str] = {}
    if len(registry):
        for _, r in registry.iterrows():
            name = _safe_str(r.get("strategy_name", ""))
            if name:
                registry_map[name] = _safe_str(r.get("strategy_status", ""))

    strategies: dict[str, dict] = {}
    total_runs = 0
    total_size = 0
    if strategies_root.exists():
        for sdir in strategies_root.iterdir():
            if not sdir.is_dir():
                continue
            sname = sdir.name
            if _has_archived_marker(sdir):
                status = STRATEGY_STATUS_ARCHIVED_UNPROCESSED
                archived = True
            elif sname in registry_map:
                status = registry_map[sname]
                archived = False
            else:
                status = STRATEGY_STATUS_COLLECTING
                archived = False

            runs = _iter_runs(sdir, sort_desc=False)
            run_count = len(runs)
            eligible_run_count = sum(
                1 for _, _, rdir in runs
                if (rdir / "records_final.parquet").exists()
            )

            lrd_data = _load_json(sdir / LAST_RUN_DATES_FILENAME)
            lrd_sources = lrd_data.get("sources", {}) or {}
            last_run_dates = {
                k: _safe_str(v.get("last_run_date", ""))
                for k, v in lrd_sources.items()
                if isinstance(v, dict)
            }

            consolidated_path = ""
            consolidated_generated_at = ""
            c_manifest = _load_json(
                archive_root / CONSOLIDATED_SUBDIR / sname
                / CONSOLIDATE_MANIFEST_FILENAME
            )
            if c_manifest:
                rel = (
                    Path(CONSOLIDATED_SUBDIR) / sname
                    / "consolidated.parquet"
                )
                consolidated_path = rel.as_posix()
                consolidated_generated_at = _safe_str(
                    c_manifest.get("generated_at", "")
                )

            size = _compute_dir_size(sdir)
            latest_run = ""
            if runs:
                d, r, _ = runs[-1]
                latest_run = f"{d}/{r}"

            strategies[sname] = {
                "strategy_status": status,
                "run_count": run_count,
                "eligible_run_count": eligible_run_count,
                "last_run_dates": last_run_dates,
                "archived_unprocessed": archived,
                "consolidated_path": consolidated_path,
                "consolidated_generated_at": consolidated_generated_at,
                "total_size_bytes": size,
                "latest_run": latest_run,
            }
            total_runs += run_count
            total_size += size

    index_payload = {
        "generated_at": _now_iso(),
        "total_strategies": len(strategies),
        "total_runs": total_runs,
        "total_size_bytes": total_size,
        "strategies": strategies,
    }
    index_path = archive_root / ARCHIVE_INDEX_FILENAME
    write_status = "ok"
    try:
        _write_json_atomic(index_path, index_payload)
    except OSError:
        write_status = "error"

    _append_audit(archive_root, [_make_audit_row(
        "maintain_index",
        files_copied=1,
        bytes_copied=_compute_file_size(index_path),
        status=write_status,
    )])

def archive_strategy_as_unprocessed(strategy_name: str) -> None:
    if not _safe_str(strategy_name):
        raise ValueError("strategy_name 不能为空")
    config = _get_config()
    archive_root = _resolve_archive_root(config)
    strategy_dir = _resolve_strategy_dir(strategy_name, archive_root)
    strategy_dir.mkdir(parents=True, exist_ok=True)

    registry = _load_strategy_registry(archive_root)
    if len(registry):
        mask = registry["strategy_name"] == strategy_name
        if mask.any():
            registry.loc[mask, "strategy_status"] = (
                STRATEGY_STATUS_ARCHIVED_UNPROCESSED
            )
            _save_strategy_registry(archive_root, registry)

    marker = {
        "strategy_name": strategy_name,
        "archived_at": _now_iso(),
        "operator": "cli",
        "note": "",
    }
    _write_json_atomic(strategy_dir / ARCHIVED_UNPROCESSED_MARKER, marker)

    try:
        maintain_index()
    except Exception:
        pass

    _append_audit(archive_root, [_make_audit_row(
        "archive_as_unprocessed",
        strategy_name=strategy_name,
        status="ok",
    )])

def unmark_archived_unprocessed(strategy_name: str) -> bool:
    name = _safe_str(strategy_name)
    if not name:
        return True
    try:
        config = _get_config()
        archive_root = _resolve_archive_root(config)
    except Exception:
        return True
    marker = (
        _resolve_strategy_dir(name, archive_root)
        / ARCHIVED_UNPROCESSED_MARKER
    )
    if not marker.exists():
        return True
    try:
        marker.unlink()
        try:
            maintain_index()
        except Exception:
            pass
        return True
    except OSError:
        return False

def delete_strategy(strategy_name: str) -> dict:
    name = _safe_str(strategy_name)
    if not name:
        return {
            "status": "error",
            "strategy_name": "",
            "run_count": 0,
            "freed_bytes": 0,
            "removed_registry": False,
            "message": "strategy_name 不能为空",
        }

    if any(c in name for c in ("/", "\\", "..")):
        return {
            "status": "error",
            "strategy_name": name,
            "run_count": 0,
            "freed_bytes": 0,
            "removed_registry": False,
            "message": f"策略名非法（含路径分隔符或 ..）：{name}",
        }

    try:
        config = _get_config()
        archive_root = _resolve_archive_root(config)
    except Exception as exc:
        return {
            "status": "error",
            "strategy_name": name,
            "run_count": 0,
            "freed_bytes": 0,
            "removed_registry": False,
            "message": f"配置加载失败：{exc}",
        }

    try:
        from medlitflow import robustness as _rb
        _output_root_raw = _safe_str(
            (config.get("project", {}) or {}).get("output_root", "")
        )
        if _output_root_raw:
            _lock_path = Path(_output_root_raw) / ".pipeline.lock"
            if _lock_path.exists() and not _rb.is_lock_stale(_lock_path):
                return {
                    "status": "error",
                    "strategy_name": name,
                    "run_count": 0,
                    "freed_bytes": 0,
                    "removed_registry": False,
                    "message": (
                        "pipeline 正在运行，删除操作已拒绝。"
                        "请停止 pipeline 后重试。"
                    ),
                }
    except Exception:
        pass

    strategies_dir = _resolve_strategy_dir(name, archive_root)
    consolidated_dir = (
        Path(archive_root) / CONSOLIDATED_SUBDIR / name
    )
    registry_path = archive_root / "strategy_registry.parquet"

    has_strategies = strategies_dir.exists()
    has_consolidated = consolidated_dir.exists()

    registry = _load_strategy_registry(archive_root)
    has_registry_row = False
    if len(registry):
        mask = registry["strategy_name"].astype(str).str.strip() == name
        has_registry_row = bool(mask.any())

    if not (has_strategies or has_consolidated or has_registry_row):
        return {
            "status": "not_found",
            "strategy_name": name,
            "run_count": 0,
            "freed_bytes": 0,
            "removed_registry": False,
            "message": (
                f"策略 {name} 不存在于 archive"
                f"（目录 / consolidated / registry 均无）"
            ),
        }

    run_count = 0
    if has_strategies:
        try:
            runs = _iter_runs(strategies_dir, sort_desc=False)
            run_count = len(runs)
        except Exception:
            run_count = 0

    freed_bytes = 0
    for d in (strategies_dir, consolidated_dir):
        if d.exists():
            try:
                freed_bytes += _compute_dir_size(d)
            except Exception:
                pass

    try:
        if has_strategies:
            shutil.rmtree(strategies_dir, ignore_errors=True)
        if has_consolidated:
            shutil.rmtree(consolidated_dir, ignore_errors=True)
    except OSError as exc:
        return {
            "status": "error",
            "strategy_name": name,
            "run_count": run_count,
            "freed_bytes": 0,
            "removed_registry": False,
            "message": f"目录删除失败：{type(exc).__name__}: {exc}",
        }

    removed_registry = False
    if has_registry_row and registry_path.exists():
        try:
            mask = registry["strategy_name"].astype(str).str.strip() == name
            if mask.any():
                registry_kept = registry[~mask].reset_index(drop=True)
                tmp = registry_path.with_name(
                    registry_path.name + ".tmp"
                )
                registry_kept.to_parquet(tmp, index=False)
                os.replace(tmp, registry_path)
                removed_registry = True
        except Exception:
            removed_registry = False

    try:
        maintain_index()
    except Exception:
        pass

    _append_audit(archive_root, [_make_audit_row(
        "delete_strategy",
        strategy_name=name,
        bytes_copied=freed_bytes,
        status="ok",
        message=(
            f"run_count={run_count}, "
            f"removed_registry={removed_registry}"
        ),
    )])

    return {
        "status": "ok",
        "strategy_name": name,
        "run_count": run_count,
        "freed_bytes": freed_bytes,
        "removed_registry": removed_registry,
        "message": (
            f"已删除策略 {name}（{run_count} 个 run，"
            f"释放 {freed_bytes} 字节）；library 数据未受影响"
        ),
    }

def cleanup_orphan_strategy(
    strategy_name: str,
    library_root: Path,
    config: dict | None = None,
) -> dict:
    name = _safe_str(strategy_name)
    if not name:
        return {
            "removed_registry": False,
            "removed_dir": False,
            "skipped_reason": "empty_strategy_name",
        }

    if config is None:
        config = _get_config()
    archive_root = _resolve_archive_root(config)
    library_root = Path(library_root)
    strategy_dir = _resolve_strategy_dir(name, archive_root)

    records_path = library_root / "records_final.parquet"
    if records_path.exists():
        try:
            records = pd.read_parquet(
                records_path, columns=["strategy_names"],
            )
            if "strategy_names" in records.columns and len(records) > 0:
                pattern = record_normalize.strategy_regex(name)
                has_cid = (
                    records["strategy_names"]
                    .fillna("").astype(str)
                    .str.contains(pattern, regex=True, na=False)
                    .any()
                )
                if has_cid:
                    return {
                        "removed_registry": False,
                        "removed_dir": False,
                        "skipped_reason": "records_exist",
                    }
        except Exception:
            return {
                "removed_registry": False,
                "removed_dir": False,
                "skipped_reason": "records_read_failed",
            }

    if strategy_dir.exists():
        runs = _iter_runs(strategy_dir, sort_desc=False)
        for _, _, rdir in runs:
            if (rdir / ARCHIVE_MANIFEST_FILENAME).exists():
                return {
                    "removed_registry": False,
                    "removed_dir": False,
                    "skipped_reason": "archive_runs_exist",
                }

    consolidated_dir = archive_root / CONSOLIDATED_SUBDIR / name
    if consolidated_dir.exists():
        return {
            "removed_registry": False,
            "removed_dir": False,
            "skipped_reason": "consolidated_exists",
        }

    removed_registry = False
    registry_path = archive_root / "strategy_registry.parquet"
    if registry_path.exists():
        try:
            registry = pd.read_parquet(registry_path)
            if "strategy_name" in registry.columns and len(registry) > 0:
                mask = (
                    registry["strategy_name"].astype(str).str.strip()
                    == name
                )
                if mask.any():
                    registry_kept = registry[~mask].reset_index(drop=True)
                    tmp = registry_path.with_name(
                        registry_path.name + ".tmp"
                    )
                    registry_kept.to_parquet(tmp, index=False)
                    os.replace(tmp, registry_path)
                    removed_registry = True
        except Exception:
            removed_registry = False

    removed_dir = False
    if strategy_dir.exists():
        try:
            shutil.rmtree(strategy_dir, ignore_errors=True)
            removed_dir = True
        except OSError:
            removed_dir = False

    return {
        "removed_registry": removed_registry,
        "removed_dir": removed_dir,
        "skipped_reason": "",
    }

def _parse_concurrent_thresholds(config: dict) -> dict[str, int]:
    archive_cfg = config.get("archive", {}) or {}
    raw = archive_cfg.get("concurrent_thresholds") or {}
    result = dict(DEFAULT_CONCURRENT_THRESHOLDS)
    if not isinstance(raw, dict):
        return result
    for key in ("info", "warning", "strong_warning"):
        if key in raw:
            try:
                val = int(raw[key])
            except (ValueError, TypeError):
                continue
            if val > 0:
                result[key] = val
    if not (
        result["info"] < result["warning"] < result["strong_warning"]
    ):
        return dict(DEFAULT_CONCURRENT_THRESHOLDS)
    return result

def check_concurrent_strategies(archive_root: Path) -> list[dict]:
    config = _get_config()
    archive_root = Path(archive_root)

    thresholds = _parse_concurrent_thresholds(config)
    archive_cfg = config.get("archive", {}) or {}
    consolidation_threshold = _to_int(
        archive_cfg.get(
            "consolidation_threshold_runs",
            DEFAULT_CONSOLIDATION_THRESHOLD_RUNS,
        ),
        DEFAULT_CONSOLIDATION_THRESHOLD_RUNS,
    )

    registry = _load_strategy_registry(archive_root)

    active_names: list[str] = []
    if len(registry):
        for _, row in registry.iterrows():
            sname = _safe_str(row.get("strategy_name", ""))
            if not sname:
                continue
            status = _safe_str(row.get("strategy_status", ""))
            if status not in ACTIVE_STRATEGY_STATUSES:
                continue
            sdir = archive_root / "strategies" / sname
            if _has_archived_marker(sdir):
                continue
            active_names.append(sname)

    results: list[dict] = []
    active_count = len(active_names)
    if active_count >= thresholds["strong_warning"]:
        results.append({
            "level": "strong_warning",
            "message": (
                f"{active_count} 个策略同时处于活跃状态，"
                f"建议先归档或确认部分策略。"
            ),
            "count": active_count,
        })
    elif active_count >= thresholds["warning"]:
        results.append({
            "level": "warning",
            "message": (
                f"{active_count} 个策略同时处于活跃状态，"
                f"注意 library 冲突风险。"
            ),
            "count": active_count,
        })
    elif active_count >= thresholds["info"]:
        results.append({
            "level": "info",
            "message": f"{active_count} 个策略处于活跃状态。",
            "count": active_count,
        })

    all_names: list[str] = []
    if len(registry):
        for _, row in registry.iterrows():
            sname = _safe_str(row.get("strategy_name", ""))
            if not sname:
                continue
            sdir = archive_root / "strategies" / sname
            if _has_archived_marker(sdir):
                continue
            all_names.append(sname)

    for sname in all_names:
        sdir = archive_root / "strategies" / sname
        eligible = 0
        for _, _, rdir in _iter_runs(sdir, sort_desc=False):
            if (rdir / "records_final.parquet").exists():
                eligible += 1
        if eligible > consolidation_threshold:
            results.append({
                "level": "warning",
                "message": (
                    f"策略 {sname} 已有 {eligible} 个 run，"
                    f"建议 consolidate。"
                ),
                "count": eligible,
            })

    return results