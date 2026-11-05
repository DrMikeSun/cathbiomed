"""medlitflow screening_io.py — 筛选 IO 基础设施。"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from medlitflow import annotator, core, record_normalize


_DEFAULT_TRASH_RETENTION_DAYS = 30
_DEFAULT_RETENTION_MODE = "release_space"

_VALID_DECISIONS = frozenset({
    "include", "exclude", "maybe", "unscreened",
})

_DECISION_TARGETS: dict[str, tuple[str, str]] = {
    "include": ("included", "L3"),
    "exclude": ("excluded", "L4"),
    "maybe": ("maybe", "L5_MAYBE"),
    "unscreened": ("unscreened", "L5"),
}

_PDF_PLUS_XML_PLUS_MD = ("article.pdf", "article.xml", "article.md", "article.html", "article_secondary.pdf")
_XML_PLUS_MD_ONLY = ("article.xml", "article.md")

_SCREENING_DECISIONS_COLUMNS = [
    "canonical_id", "user_decision", "user_decision_date",
    "user_notes", "user_confirmed_at", "prescreen_label_snapshot",
    "reviewer_id", "decision_1", "decision_2",
    "conflict_status", "exclusion_reason",
]

_FILE_INDEX_COLUMNS = [
    "canonical_id", "downloaded_file_path", "current_file_path",
    "file_last_moved_at", "file_retention_level",
    "trash_path", "deleted_at", "expire_at",
]

_STRATEGY_REGISTRY_COLUMNS = [
    "strategy_name", "strategy_status", "created_at",
    "last_activity_at", "unscreened_count", "maybe_count",
    "trashed_count", "days_since_last_activity",
]

_SCREENING_AUDIT_COLUMNS = [
    "canonical_id", "action", "from_path", "to_path",
    "timestamp", "operator",
]

_EXPORT_COLS: list[str] = [
    "canonical_id", "title", "authors", "journal", "year",
    "doi", "pmid", "pmcid",
    "publication_type_category",
    "user_decision", "user_decision_date", "user_notes",
]

_RECORD_EXPORT_COLS: list[str] = [
    "canonical_id", "title", "authors", "journal", "year",
    "doi", "pmid", "pmcid", "publication_type_category",
]

_DECISION_PRIORITY: dict[str, int] = {
    "include":    4,
    "maybe":      3,
    "exclude":    2,
    "unscreened": 1,
}

_OPEN_LITERATURE_SUBDIRS: tuple[str, ...] = (
    "inbox", "maybe", "included", "excluded", "unscreened",
)
_OPEN_LITERATURE_FILENAMES: tuple[str, ...] = (
    "article.pdf", "article.html", "article.xml", "metadata.json",
)
_OPEN_LITERATURE_TRASH_FILENAMES: tuple[str, ...] = (
    "article.pdf", "article.html",
)

_SUBDIR_TO_LEVEL: dict[str, str] = {
    "inbox":      "EMPTY",
    "included":   "L3",
    "excluded":   "L4",
    "maybe":      "L5_MAYBE",
    "unscreened": "L5",
}

_DRAFT_COLUMNS: list[str] = [
    "canonical_id", "draft_decision", "draft_updated_at",
]

_LOAD_SCREENING_TITLE_MAX_LEN: int = 500

_REPORT_DIRNAME: str = "reports"
_REPORT_DATE_FMT: str = "%Y%m%d"
_REPORT_DIRNAME_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_MIGRATE_BACKUP_SUFFIX: str = ".bak_v1"

_FILE_INDEX_V2_NEW_COLUMNS: list[str] = [
    "highest_priority_strategy",
    "highest_priority_decision",
    "file_missing",
    "file_location_updated_at",
]

_FILE_INDEX_V2_DEFAULTS: dict = {
    "highest_priority_strategy": "",
    "highest_priority_decision": "",
    "file_missing": False,
    "file_location_updated_at": "",
}

_DECISIONS_STRATEGY_COL: str = "strategy_name"

_FILE_INDEX_COLUMNS_V2: list[str] = _FILE_INDEX_COLUMNS + _FILE_INDEX_V2_NEW_COLUMNS


def _now_iso() -> str:
    return core.now_iso()


def _today_iso() -> str:
    return core.today_iso()


def _days_since(iso_str: str) -> int:
    if not iso_str:
        return 0
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return max(0, delta.days)
    except (ValueError, TypeError):
        return 0


def _load_table(path: Path, columns: list[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=columns)
    try:
        df = pd.read_parquet(path)
        for c in columns:
            if c not in df.columns:
                df[c] = ""
        return df[columns].copy()
    except Exception as exc:
        raise RuntimeError(
            f"parquet 读取失败：{path}（{type(exc).__name__}: {exc}）"
        ) from exc


def _save_table(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def _read_decisions_full_df(decisions_path: Path) -> pd.DataFrame:
    if not decisions_path.exists():
        df = pd.DataFrame(columns=_SCREENING_DECISIONS_COLUMNS)
        for c in _SCREENING_DECISIONS_COLUMNS:
            if c not in df.columns:
                df[c] = ""
        df[_DECISIONS_STRATEGY_COL] = ""
        return df

    try:
        df = pd.read_parquet(decisions_path)
    except Exception as exc:
        corrupt_msg = ""
        try:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            corrupt_path = decisions_path.with_name(
                f"{decisions_path.name}.corrupt_{ts}"
            )
            shutil.copy2(decisions_path, corrupt_path)
            corrupt_msg = f"（已备份为 {corrupt_path.name}）"
        except Exception:
            corrupt_msg = "（备份失败，请手动备份）"
        raise RuntimeError(
            f"decisions parquet 读取失败：{decisions_path.name}"
            f"（{type(exc).__name__}: {exc}）{corrupt_msg}。"
            f"为避免用空表覆盖损坏文件，本次读取已中止。"
            f"请修复 decisions 或从备份恢复后重试。"
        ) from exc

    for c in _SCREENING_DECISIONS_COLUMNS:
        if c not in df.columns:
            df[c] = ""

    if _DECISIONS_STRATEGY_COL in df.columns:
        df[_DECISIONS_STRATEGY_COL] = (
            df[_DECISIONS_STRATEGY_COL].fillna("").astype(str)
        )

    return df


def _read_file_index_v2(file_index_path: Path) -> pd.DataFrame:
    if file_index_path.exists():
        try:
            df = pd.read_parquet(file_index_path)
        except Exception as exc:
            corrupt_msg = ""
            try:
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                corrupt_path = file_index_path.with_name(
                    f"{file_index_path.name}.corrupt_{ts}"
                )
                shutil.copy2(file_index_path, corrupt_path)
                corrupt_msg = f"（已备份为 {corrupt_path.name}）"
            except Exception:
                corrupt_msg = "（备份失败，请手动备份）"
            raise RuntimeError(
                f"file_index parquet 读取失败：{file_index_path.name}"
                f"（{type(exc).__name__}: {exc}）{corrupt_msg}。"
                f"为避免用空表覆盖损坏文件，本次读取已中止。"
                f"请修复 file_index 或从备份恢复后重试。"
            ) from exc
    else:
        df = pd.DataFrame(columns=_FILE_INDEX_COLUMNS_V2)
    for c in _FILE_INDEX_COLUMNS_V2:
        if c not in df.columns:
            df[c] = _FILE_INDEX_V2_DEFAULTS.get(c, "")
    if "file_missing" in df.columns:
        df["file_missing"] = record_normalize.to_bool_series(
            df["file_missing"]
        )
    return df


def _migrate_backup(path: Path) -> Path:
    path = Path(path)
    if not path.exists():
        return Path("")

    backup = path.with_name(path.name + _MIGRATE_BACKUP_SUFFIX)
    if backup.exists():
        return backup

    try:
        shutil.copy2(path, backup)
    except OSError:
        return Path("")

    return backup


def _check_pipeline_lock(
    library_root: Path,
    config: dict | None,
) -> str:
    try:
        from medlitflow import robustness
    except ImportError:
        return ""

    if not config:
        try:
            config = core.load_config()
        except Exception:
            config = None

    output_root_raw = ""
    if config:
        output_root_raw = str(
            (config.get("project") or {}).get("output_root", "") or ""
        )
    if output_root_raw:
        lock_dir = Path(output_root_raw)
    else:
        lock_dir = Path(library_root).parent

    lock_path = lock_dir / ".pipeline.lock"
    if not lock_path.exists():
        return ""

    try:
        is_stale = robustness.is_lock_stale(lock_path)
    except Exception:
        return ""
    if is_stale:
        try:
            lock_path.unlink()
        except OSError:
            pass
        return ""
    return "pipeline 正在运行，请停止后重试"


def _append_screening_audit(library_root: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path = library_root / "screening_audit.csv"
    df = pd.DataFrame(rows, columns=_SCREENING_AUDIT_COLUMNS)
    if path.exists():
        try:
            existing = pd.read_csv(path, dtype=str).fillna("")
            df = pd.concat([existing, df], ignore_index=True)
        except Exception:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        df.to_csv(tmp, index=False, encoding="utf-8-sig")
        os.replace(tmp, path)
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def _is_iso_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def _sanitize_report_dirname(name: str) -> str:
    s = str(name or "").strip()
    if not s:
        return "unnamed"
    s = _REPORT_DIRNAME_INVALID_CHARS.sub("_", s)
    s = s.strip(" .")
    if not s:
        return "unnamed"
    return s[:80]


def _cleanup_old_strategy_reports(
    reports_root: Path,
    safe_strategy: str,
    keep_name: str = "",
) -> None:
    if not safe_strategy:
        return
    if not reports_root.exists() or not reports_root.is_dir():
        return

    prefix = f"{safe_strategy}_"
    try:
        for d in reports_root.iterdir():
            if not d.is_dir():
                continue
            name = d.name
            if not name.startswith(prefix):
                continue
            if keep_name and name == keep_name:
                continue
            suffix = name[len(prefix):]
            if len(suffix) == 8:
                if not suffix.isdigit():
                    continue
            elif len(suffix) == 15:
                if not (suffix[:8].isdigit()
                        and suffix[8] == "_"
                        and suffix[9:].isdigit()):
                    continue
            else:
                continue
            try:
                shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue
    except OSError:
        pass


def _touch_activity(strategy_name: str, library_root: Path) -> None:
    archive_root = library_root.parent / "archive"
    registry_path = archive_root / "strategy_registry.parquet"
    registry = _load_table(registry_path, _STRATEGY_REGISTRY_COLUMNS)
    rmask = registry["strategy_name"] == strategy_name
    if rmask.any():
        registry.loc[rmask, "last_activity_at"] = _now_iso()
        _save_table(registry, registry_path)


def _refresh_strategy_status_for_cids(
    cids: set[str],
    library_root: Path,
    touch_activity: bool = True,
) -> None:
    records_path = library_root / "records_final.parquet"
    if not records_path.exists():
        return
    try:
        records = pd.read_parquet(records_path)
    except Exception:
        return
    if "strategy_names" not in records.columns:
        return
    mask = records["canonical_id"].astype(str).isin(cids)
    strategies: set[str] = set()
    for s in records.loc[mask, "strategy_names"].dropna():
        for name in str(s).split(";"):
            if name.strip():
                strategies.add(name.strip())
    from medlitflow.screening import update_strategy_status
    for name in sorted(strategies):
        if touch_activity:
            _touch_activity(name, library_root)
        update_strategy_status(name, library_root)


def _refresh_after_special_change(
    library_root: Path,
    affected_cids: set[str],
    errors: list[str],
) -> None:
    try:
        annotator.update_library_index(library_root)
    except Exception as exc:
        errors.append(f"index_refresh_failed: {exc}")

    try:
        if affected_cids:
            _refresh_strategy_status_for_cids(
                affected_cids, library_root, touch_activity=False,
            )
    except Exception as exc:
        errors.append(f"registry_refresh_failed: {exc}")