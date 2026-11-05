"""medlitflow screening.py — 筛选流程与文件生命周期管理。"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from medlitflow import annotator, core, record_normalize, robustness
from medlitflow.screening_io import (
    _DEFAULT_TRASH_RETENTION_DAYS, _DEFAULT_RETENTION_MODE,
    _VALID_DECISIONS, _DECISION_TARGETS,
    _PDF_PLUS_XML_PLUS_MD, _XML_PLUS_MD_ONLY,
    _SCREENING_DECISIONS_COLUMNS, _FILE_INDEX_COLUMNS,
    _STRATEGY_REGISTRY_COLUMNS, _SCREENING_AUDIT_COLUMNS,
    _EXPORT_COLS, _RECORD_EXPORT_COLS,
    _DECISION_PRIORITY,
    _OPEN_LITERATURE_SUBDIRS, _OPEN_LITERATURE_FILENAMES,
    _OPEN_LITERATURE_TRASH_FILENAMES,
    _SUBDIR_TO_LEVEL, _LOAD_SCREENING_TITLE_MAX_LEN,
    _REPORT_DIRNAME, _REPORT_DATE_FMT,
    _FILE_INDEX_V2_NEW_COLUMNS, _FILE_INDEX_V2_DEFAULTS,
    _DECISIONS_STRATEGY_COL, _FILE_INDEX_COLUMNS_V2,
    _now_iso, _today_iso, _days_since,
    _load_table, _save_table, _read_decisions_full_df, _read_file_index_v2,
    _migrate_backup, _check_pipeline_lock, _append_screening_audit,
    _sanitize_report_dirname, _cleanup_old_strategy_reports, _is_iso_date,
    _touch_activity, _refresh_strategy_status_for_cids, _refresh_after_special_change,
)

__all__ = [
    "apply_screening",
    "move_to_library",
    "soft_delete",
    "restore_from_trash",
    "cleanup_trash",
    "preview_trash_cleanup",
    "set_maybe_decision",
    "batch_confirm",
    "update_strategy_status",
    "open_inbox_pdf",
    "open_literature_file",
    "compute_progress_stats",
    "compute_storage_stats",
    "compute_file_missing_status",
    "redownload_from_urls",
    "save_draft_to_decisions",
    "load_screening_rows",
    "export_screening_report",
    "estimate_archive_space_release",
    "reopen_strategy",
    "migrate_decisions_schema",
    "migrate_file_index_schema",
    "mark_as_retrieval_noise",
    "mark_as_deleted_from_source",
    "restore_special_status",
    "hard_delete_literature",
    "prescan_delete_strategy",
    "delete_strategy_cascade",
    "get_alive_strategies",
    "compute_orphan_strategies",
]

class ScreenedConditionError(Exception):
    """策略未达 confirmed 状态时试图进入图谱构建。"""

_FILE_MISSING_CACHE_SIG: tuple[str, float, int] | None = None
_FILE_MISSING_CACHE_DATA: dict[str, bool] | None = None

def _get_strategy_names_for_cid(
    canonical_id: str,
    cid_strategies: dict[str, list[str]],
) -> list[str]:
    # 返回该 cid 的 strategy_names 列表（迁移时用）。
    strategies = cid_strategies.get(str(canonical_id))
    if not strategies:
        return [""]
    seen: set[str] = set()
    unique: list[str] = []
    for s in strategies:
        s_clean = core.clean_text(s)
        if s_clean and s_clean not in seen:
            seen.add(s_clean)
            unique.append(s_clean)
    return unique if unique else [""]

def _move_named_files(src_dir: Path, dst_dir: Path, names: tuple[str, ...]) -> list[tuple[str, str]]:
    moved: list[tuple[str, str]] = []
    if not src_dir.exists():
        return moved
    dst_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        src = src_dir / name
        if src.exists() and src.is_file():
            dst = dst_dir / name
            if dst.exists():
                continue
            shutil.move(str(src), str(dst))
            moved.append((str(src), str(dst)))
    return moved

def _restore_pdf_from_trash(
    canonical_id: str,
    library_root: Path,
    target_subdir: str,
    needed_pdf: bool = True,
) -> dict:
    library_root = Path(library_root)
    safe = core.canonical_id_to_dirname(canonical_id)
    trash_dir = library_root / "trash" / safe
    if not trash_dir.exists() or not trash_dir.is_dir():
        return {"restored": False, "clear_trash_meta": False}

    dst_dir = library_root / target_subdir / safe
    dst_dir.mkdir(parents=True, exist_ok=True)

    restored_any = False
    already_present = True
    filenames_to_restore = (
        ("article.pdf", "article.html", "article_secondary.pdf")
        if needed_pdf else ()
    )
    for fname in filenames_to_restore:
        src = trash_dir / fname
        dst = dst_dir / fname
        if dst.is_file():
            continue
        already_present = False
        if not src.is_file():
            continue
        try:
            shutil.move(str(src), str(dst))
            restored_any = True
        except OSError:
            continue

    if already_present:
        clear_meta = False
        try:
            if not any(trash_dir.iterdir()):
                trash_dir.rmdir()
                clear_meta = True
        except OSError:
            pass
        return {"restored": True, "clear_trash_meta": clear_meta}

    if not restored_any:
        return {"restored": False, "clear_trash_meta": False}

    clear_meta = False
    try:
        if not any(trash_dir.iterdir()):
            trash_dir.rmdir()
            clear_meta = True
    except OSError:
        pass

    return {"restored": True, "clear_trash_meta": clear_meta}

def _ensure_file_index_row(
    file_index: pd.DataFrame,
    canonical_id: str,
) -> int:
    for c in _FILE_INDEX_COLUMNS_V2:
        if c not in file_index.columns:
            file_index[c] = _FILE_INDEX_V2_DEFAULTS.get(c, "")
    if "file_missing" in file_index.columns:
        file_index["file_missing"] = record_normalize.to_bool_series(
            file_index["file_missing"]
        )

    mask = file_index["canonical_id"] == canonical_id
    if mask.any():
        return int(file_index.index[mask][0])
    new_row = {c: "" for c in _FILE_INDEX_COLUMNS_V2}
    new_row.update({
        "canonical_id": canonical_id,
        "file_retention_level": "EMPTY",
        "file_missing": False,
    })
    new_idx = int(file_index.index.max()) + 1 if len(file_index) > 0 else 0
    file_index.loc[new_idx] = new_row
    return new_idx

def _decision_target(decision: str) -> tuple[str, str]:
    return _DECISION_TARGETS.get(decision, ("unscreened", "L5"))

def _screening_cfg(config: dict) -> dict:
    return config.get("screening", {}) or {}

def _list_strategy_runs(
    archive_root: Path,
    strategy_name: str,
) -> list[dict]:
    runs_root = (
        Path(archive_root) / "strategies" / strategy_name / "runs"
    )
    if not runs_root.exists():
        return []

    items: list[dict] = []
    try:
        for date_dir in runs_root.iterdir():
            if not date_dir.is_dir():
                continue
            if not _is_iso_date(date_dir.name):
                continue
            for run_dir in date_dir.iterdir():
                if not run_dir.is_dir():
                    continue

                q_path = run_dir / "query_execution.json"
                if not q_path.exists():
                    q_path = run_dir / "output" / "query_execution.json"

                records_count = 0
                manifest_path = run_dir / "archive_manifest.json"
                if manifest_path.exists():
                    try:
                        m = json.loads(
                            manifest_path.read_text(encoding="utf-8")
                        )
                        records_count = int(
                            m.get("records_final_rows", 0) or 0
                        )
                    except (json.JSONDecodeError, OSError, ValueError):
                        pass
                if records_count == 0:
                    records_path = run_dir / "records_final.parquet"
                    if records_path.exists():
                        try:
                            records_count = int(len(pd.read_parquet(
                                records_path,
                                columns=["canonical_id"],
                            )))
                        except Exception:
                            pass

                items.append({
                    "date": date_dir.name,
                    "run_name": run_dir.name,
                    "run_dir": run_dir,
                    "query_execution_path": (
                        q_path if q_path.exists() else None
                    ),
                    "records_count": records_count,
                })
    except OSError:
        pass

    items.sort(key=lambda x: (x["date"], x["run_name"]), reverse=True)
    return items

def _resolve_queries(runs: list[dict]) -> dict:
    # 从多个 run 的 query_execution.json 合并检索式（去重后 " || " 拼接）。
    # 多个 run 的检索式可能不同（增量 run 通常加了日期过滤）。
    merged = {
        "pubmed": [],
        "europepmc": [],
        "crossref": [],
        "openalex": [],
    }

    for run in runs:
        q_path = run.get("query_execution_path")
        if q_path is None:
            continue
        try:
            q = json.loads(Path(q_path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(q, dict):
            continue

        # pubmed / europepmc：单查询
        for src in ("pubmed", "europepmc"):
            entry = q.get(src) or {}
            if isinstance(entry, dict):
                query = core.clean_text(entry.get("query", ""))
                if query:
                    merged[src].append(query)

        # crossref / openalex：多查询（list[dict]）
        cr_list = q.get("crossref") or []
        if isinstance(cr_list, list):
            for entry in cr_list:
                if not isinstance(entry, dict):
                    continue
                query = core.clean_text(
                    entry.get("query.bibliographic", "")
                )
                if query:
                    merged["crossref"].append(query)

        oa_list = q.get("openalex") or []
        if isinstance(oa_list, list):
            for entry in oa_list:
                if not isinstance(entry, dict):
                    continue
                query = core.clean_text(entry.get("search", ""))
                if query:
                    merged["openalex"].append(query)

    # 去重 + 拼接
    def _join_unique(values: list[str]) -> str:
        seen: set[str] = set()
        unique: list[str] = []
        for v in values:
            if v and v not in seen:
                seen.add(v)
                unique.append(v)
        return " || ".join(unique)

    return {
        "pubmed": _join_unique(merged["pubmed"]),
        "europepmc": _join_unique(merged["europepmc"]),
        "crossref": _join_unique(merged["crossref"]),
        "openalex": _join_unique(merged["openalex"]),
    }

def _resolve_source_counts(runs: list[dict]) -> dict[str, int]:
    # 从多个 run 的 query_execution.json 累加分源原始命中数（去重前）。
    # 跨 run 累加 = 该策略历史上各源总命中次数（每个 run 独立一次识别）。
    # 历史 run 无 raw_counts_by_source 字段 → 跳过；全部缺失 → 返回 {}。
    merged: dict[str, int] = {}
    for run in runs:
        q_path = run.get("query_execution_path")
        if q_path is None:
            continue
        try:
            q = json.loads(Path(q_path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(q, dict):
            continue
        raw = q.get("raw_counts_by_source")
        if not isinstance(raw, dict):
            continue
        for src, n in raw.items():
            try:
                src_key = str(src)
                merged[src_key] = merged.get(src_key, 0) + int(n)
            except (ValueError, TypeError):
                continue
    return merged

def _resolve_reviewer_id(config: dict | None) -> str:
    if not config:
        return ""
    project = config.get("project", {}) or {}
    rid = core.clean_text(project.get("reviewer_id", ""))
    if rid:
        return rid
    return core.clean_text(config.get("reviewer_id", ""))

def open_literature_file(
    canonical_id: str,
    library_root: Path,
) -> bool:
    library_root = Path(library_root)
    safe = core.canonical_id_to_dirname(canonical_id)

    file_index_path = library_root / "file_index.parquet"
    if file_index_path.exists():
        try:
            fi = _load_table(file_index_path, _FILE_INDEX_COLUMNS)
            mask = fi["canonical_id"].astype(str) == str(canonical_id)
            if mask.any():
                current = core.clean_text(
                    fi.loc[mask, "current_file_path"].iloc[0]
                )
                if current:
                    p = Path(current)
                    if p.is_file():
                        return core.open_file_with_default_app(p)
        except Exception:
            pass

    for subdir in _OPEN_LITERATURE_SUBDIRS:
        base = library_root / subdir / safe
        if not base.exists():
            continue
        for fname in _OPEN_LITERATURE_FILENAMES:
            p = base / fname
            if p.is_file():
                return core.open_file_with_default_app(p)

    trash_base = library_root / "trash" / safe
    if trash_base.exists():
        for fname in _OPEN_LITERATURE_TRASH_FILENAMES:
            p = trash_base / fname
            if p.is_file():
                return core.open_file_with_default_app(p)

    return False

def open_inbox_pdf(canonical_id: str, library_root: Path) -> bool:
    return open_literature_file(canonical_id, library_root)

def _draft_path(library_root: Path, strategy_name: str) -> Path:
    return Path(library_root) / "screening_drafts" / f"{strategy_name}.parquet"

_BACKFILL_MARKER_FILENAME = ".decisions_backfill.marker"
_BACKFILL_SCHEMA_VERSION = 1

def _backfill_marker_path(library_root: Path) -> Path:
    return Path(library_root) / _BACKFILL_MARKER_FILENAME

def _is_backfill_done(library_root: Path) -> bool:
    marker = _backfill_marker_path(library_root)
    if not marker.exists():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    try:
        ver = int(data.get("schema_version", 0))
    except (ValueError, TypeError):
        return False
    return ver == _BACKFILL_SCHEMA_VERSION

def _write_backfill_marker(library_root: Path) -> None:
    marker = _backfill_marker_path(library_root)
    payload = {
        "done_at": _now_iso(),
        "schema_version": _BACKFILL_SCHEMA_VERSION,
    }
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        tmp = marker.with_name(marker.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, marker)
    except OSError:
        pass

def apply_decisions_incremental(
    strategy_name: str,
    decisions: dict[str, str],
    library_root: Path,
) -> dict:
    if not strategy_name or not decisions:
        return {"updated": 0, "errors": []}

    library_root = Path(library_root)
    decisions_path = library_root / "screening_decisions.parquet"
    file_index_path = library_root / "file_index.parquet"
    errors: list[str] = []

    try:
        decisions_all = _read_decisions_full_df(decisions_path)
    except RuntimeError as exc:
        return {"updated": 0, "errors": [f"decisions_read_failed: {exc}"]}

    if _DECISIONS_STRATEGY_COL not in decisions_all.columns:
        return {"updated": 0, "errors": ["legacy_schema_no_strategy_col"]}

    now = _now_iso()
    today = _today_iso()
    updated_cids: set[str] = set()

    for cid, decision in decisions.items():
        cid_str = str(cid or "").strip()
        if not cid_str:
            continue
        decision_str = str(decision or "unscreened").strip().lower()
        if decision_str not in _VALID_DECISIONS:
            continue

        mask = (
            (decisions_all["canonical_id"].astype(str) == cid_str)
            & (decisions_all[_DECISIONS_STRATEGY_COL].astype(str)
               == strategy_name)
        )
        if mask.any():
            decisions_all.loc[mask, "user_decision"] = decision_str
            decisions_all.loc[mask, "user_decision_date"] = today
            decisions_all.loc[mask, "user_confirmed_at"] = now
        else:
            new_row = {c: "" for c in _SCREENING_DECISIONS_COLUMNS}
            new_row.update({
                "canonical_id": cid_str,
                _DECISIONS_STRATEGY_COL: strategy_name,
                "user_decision": decision_str,
                "user_decision_date": today,
                "user_confirmed_at": now,
            })
            decisions_all = pd.concat(
                [decisions_all, pd.DataFrame([new_row])],
                ignore_index=True,
            )
        updated_cids.add(cid_str)

    if not updated_cids:
        return {"updated": 0, "errors": []}

    try:
        _save_table(decisions_all, decisions_path)
    except OSError as exc:
        return {
            "updated": 0,
            "errors": [f"decisions_write_failed: {exc}"],
        }

    try:
        fi = _read_file_index_v2(file_index_path)
        if "highest_priority_decision" in fi.columns:
            now_iso = _now_iso()
            fi_cid_to_idx: dict[str, int] = {}
            for i in fi.index:
                cid_val = str(fi.at[i, "canonical_id"] or "")
                if cid_val:
                    fi_cid_to_idx[cid_val] = int(i)
            next_new_idx = (
                int(fi.index.max()) + 1 if len(fi) > 0 else 0
            )
            for cid_str in updated_cids:
                try:
                    hd, hs = _compute_highest_priority_decision(
                        cid_str, decisions_all,
                    )
                except Exception:
                    continue
                ridx = fi_cid_to_idx.get(cid_str)
                if ridx is None:
                    new_row = {c: "" for c in _FILE_INDEX_COLUMNS_V2}
                    new_row.update({
                        "canonical_id": cid_str,
                        "highest_priority_strategy": hs,
                        "highest_priority_decision": hd,
                        "file_location_updated_at": now_iso,
                        "file_missing": False,
                        "file_retention_level": "EMPTY",
                    })
                    fi.loc[next_new_idx] = new_row
                    fi_cid_to_idx[cid_str] = next_new_idx
                    next_new_idx += 1
                    continue
                fi.at[ridx, "highest_priority_strategy"] = hs
                fi.at[ridx, "highest_priority_decision"] = hd
                fi.at[ridx, "file_location_updated_at"] = now_iso
            _save_table(fi, file_index_path)
    except Exception as exc:
        errors.append(f"file_index_update_failed: {exc}")

    return {"updated": len(updated_cids), "errors": errors}

def backfill_file_index_decisions(
    library_root: Path,
) -> dict:
    library_root = Path(library_root)
    decisions_path = library_root / "screening_decisions.parquet"
    file_index_path = library_root / "file_index.parquet"

    if _is_backfill_done(library_root):
        return {
            "backfilled": 0,
            "errors": [],
            "skipped_by_marker": True,
        }

    if not decisions_path.exists():
        _write_backfill_marker(library_root)
        return {"backfilled": 0, "errors": []}

    try:
        decisions_all = _read_decisions_full_df(decisions_path)
    except RuntimeError as exc:
        return {"backfilled": 0, "errors": [f"decisions_read_failed: {exc}"]}

    if len(decisions_all) == 0:
        return {"backfilled": 0, "errors": []}
    if "canonical_id" not in decisions_all.columns:
        return {"backfilled": 0, "errors": ["decisions_missing_cid"]}
    if _DECISIONS_STRATEGY_COL not in decisions_all.columns:
        return {"backfilled": 0, "errors": ["legacy_schema_skip"]}

    try:
        decision_cids = set(
            decisions_all["canonical_id"]
            .fillna("").astype(str).str.strip()
            .loc[lambda s: s.ne("")]
            .tolist()
        )
    except Exception as exc:
        return {"backfilled": 0, "errors": [f"cid_extract_failed: {exc}"]}
    if not decision_cids:
        return {"backfilled": 0, "errors": []}

    errors: list[str] = []
    try:
        fi = _read_file_index_v2(file_index_path)
    except Exception as exc: 
        return {"backfilled": 0, "errors": [f"file_index_read_failed: {exc}"]}

    if "highest_priority_decision" not in fi.columns:
        return {"backfilled": 0, "errors": ["file_index_not_v2"]}

    fi_cid_to_idx: dict[str, int] = {}
    fi_existing_hd: dict[str, str] = {}
    for i in fi.index:
        cid_val = str(fi.at[i, "canonical_id"] or "").strip()
        if cid_val:
            fi_cid_to_idx[cid_val] = int(i)
            fi_existing_hd[cid_val] = str(
                fi.at[i, "highest_priority_decision"] or ""
            ).strip().lower()

    now_iso = _now_iso()
    backfilled = 0
    next_new_idx = (
        int(fi.index.max()) + 1 if len(fi) > 0 else 0
    )

    for cid_str in decision_cids:
        try:
            hd, hs = _compute_highest_priority_decision(
                cid_str, decisions_all,
            )
        except Exception:
            continue

        if hd not in _VALID_DECISIONS:
            continue
        existing_hd = fi_existing_hd.get(cid_str, "")
        existing_idx = fi_cid_to_idx.get(cid_str)

        if existing_idx is None:
            new_row = {c: "" for c in _FILE_INDEX_COLUMNS_V2}
            new_row.update({
                "canonical_id": cid_str,
                "highest_priority_strategy": hs,
                "highest_priority_decision": hd,
                "file_location_updated_at": now_iso,
                "file_missing": False,
                "file_retention_level": "EMPTY",
            })
            fi.loc[next_new_idx] = new_row
            fi_cid_to_idx[cid_str] = next_new_idx
            fi_existing_hd[cid_str] = hd
            next_new_idx += 1
            backfilled += 1
        elif not existing_hd:
            fi.at[existing_idx, "highest_priority_strategy"] = hs
            fi.at[existing_idx, "highest_priority_decision"] = hd
            fi.at[existing_idx, "file_location_updated_at"] = now_iso
            fi_existing_hd[cid_str] = hd
            backfilled += 1

    if backfilled > 0:
        try:
            _save_table(fi, file_index_path)
        except OSError as exc:
            return {
                "backfilled": 0,
                "errors": [f"file_index_write_failed: {exc}"],
            }

    _write_backfill_marker(library_root)

    return {"backfilled": backfilled, "errors": errors}

def save_draft_to_decisions(
    strategy_name: str,
    library_root: Path,
    config: dict | None = None,
) -> dict:
    library_root = Path(library_root)
    result = {"merged": 0, "skipped": 0}
    if not strategy_name:
        return result

    drafts_path = _draft_path(library_root, strategy_name)
    if not drafts_path.exists():
        return result

    try:
        drafts = pd.read_parquet(drafts_path)
    except Exception:
        return result

    if (
        len(drafts) == 0
        or "canonical_id" not in drafts.columns
        or "draft_decision" not in drafts.columns
    ):
        return result

    decisions_path = library_root / "screening_decisions.parquet"

    try:
        decisions = _read_decisions_full_df(decisions_path)
    except RuntimeError as exc:
        return {
            "merged": 0,
            "skipped": 0,
            "error": "decisions_read_failed",
            "detail": str(exc),
        }

    now = _now_iso()
    today = _today_iso()
    reviewer_id = ""
    if config:
        reviewer_id = core.clean_text(
            (config.get("project", {}) or {}).get("reviewer_id", "")
        )

    has_strategy_col = "strategy_name" in decisions.columns

    merged = 0
    skipped = 0
    skipped_reasons: list[dict] = []
    special_cids: set[str] = set()
    try:
        rec_path = library_root / "records_final.parquet"
        if rec_path.exists():
            rec_df = pd.read_parquet(
                rec_path, columns=["canonical_id", "special_status"],
            )
            if "special_status" in rec_df.columns:
                sp_series = (
                    rec_df["special_status"]
                    .fillna("").astype(str).str.strip()
                )
                special_cids = set(
                    rec_df.loc[sp_series.ne(""), "canonical_id"]
                    .astype(str).tolist()
                )
    except Exception:
        special_cids = set()

    for _, row in drafts.iterrows():
        cid = core.clean_text(row.get("canonical_id", ""))
        if not cid:
            skipped += 1
            skipped_reasons.append({"cid": "", "reason": "empty_cid"})
            continue

        if cid in special_cids:
            skipped += 1
            skipped_reasons.append(
                {"cid": cid, "reason": "special_status"}
            )
            continue

        decision = core.clean_text(row.get("draft_decision", "")).strip().lower()
        if not decision:
            decision = "unscreened"
        if decision not in _VALID_DECISIONS:
            skipped += 1
            skipped_reasons.append(
                {"cid": cid, "reason": "invalid_decision"}
            )
            continue

        new_row = {c: "" for c in _SCREENING_DECISIONS_COLUMNS}
        new_row.update({
            "canonical_id": cid,
            "user_decision": decision,
            "user_decision_date": today,
            "user_confirmed_at": now,
            "reviewer_id": reviewer_id,
        })
        if has_strategy_col:
            new_row["strategy_name"] = strategy_name

        if has_strategy_col:
            mask = (
                (decisions["canonical_id"].astype(str) == cid)
                & (decisions["strategy_name"].astype(str) == strategy_name)
            )
        else:
            mask = decisions["canonical_id"].astype(str) == cid

        if mask.any():
            for k, v in new_row.items():
                decisions.loc[mask, k] = v
        else:
            decisions = pd.concat(
                [decisions, pd.DataFrame([new_row])],
                ignore_index=True,
            )
        merged += 1

    try:
        _save_table(decisions, decisions_path)
    except OSError as exc:
        return {
            "merged": 0,
            "skipped": merged + skipped,
            "error": "write_failed",
            "detail": str(exc),
        }

    try:
        drafts_path.unlink()
    except OSError:
        pass

    try:
        _sync_file_index_priority_metadata(
            set(drafts["canonical_id"].astype(str).tolist()),
            library_root,
        )
    except Exception:
        pass

    try:
        _refresh_strategy_status_for_cids(
            set(drafts["canonical_id"].astype(str).tolist()),
            library_root,
        )
    except Exception:
        pass

    result["merged"] = merged
    result["skipped"] = skipped
    result["skipped_reasons"] = skipped_reasons
    return result

def _sync_file_index_priority_metadata(
    cids: set[str], library_root: Path,
) -> None:
    decisions_path = library_root / "screening_decisions.parquet"
    file_index_path = library_root / "file_index.parquet"
    if not decisions_path.exists() or not file_index_path.exists():
        return
    try:
        fi = pd.read_parquet(file_index_path)
    except Exception:
        return
    if "highest_priority_strategy" not in fi.columns:
        return
    decisions_all = _read_decisions_full_df(decisions_path)
    now = _now_iso()
    for cid in cids:
        fmask = fi["canonical_id"].astype(str) == cid
        if not fmask.any():
            continue
        try:
            hd, hs = _compute_highest_priority_decision(cid, decisions_all)
        except Exception:
            continue
        ridx = int(fi.index[fmask][0])
        fi.at[ridx, "highest_priority_strategy"] = hs
        fi.at[ridx, "highest_priority_decision"] = hd
        fi.at[ridx, "file_location_updated_at"] = now
    try:
        _save_table(fi, file_index_path)
    except OSError:
        pass

def move_to_library(
    canonical_id: str,
    source_dir: Path,
    target_dir: Path,
) -> None:
    source_dir = Path(source_dir)
    target_dir = Path(target_dir)
    safe = core.canonical_id_to_dirname(canonical_id)
    src = source_dir / safe
    dst = target_dir / safe
    if not src.exists():
        return
    _move_named_files(src, dst, _PDF_PLUS_XML_PLUS_MD)
    try:
        if src.exists() and not any(src.iterdir()):
            src.rmdir()
    except OSError:
        pass

def _compute_highest_priority_decision(
    canonical_id: str,
    all_decisions: pd.DataFrame,
) -> tuple[str, str]:
    # 优先级：include(4) > maybe(3) > exclude(2) > unscreened(1)。
    if all_decisions is None or len(all_decisions) == 0:
        return "unscreened", ""
    if "canonical_id" not in all_decisions.columns:
        return "unscreened", ""

    mask = all_decisions["canonical_id"].astype(str) == str(canonical_id)
    rows = all_decisions[mask]
    if len(rows) == 0:
        return "unscreened", ""

    has_strategy_col = "strategy_name" in rows.columns

    best_decision: str = ""
    best_strategy: str = ""
    best_priority: int = 0

    for _, r in rows.iterrows():
        decision = core.clean_text(r.get("user_decision", "")).lower()
        priority = _DECISION_PRIORITY.get(decision, 0)
        strategy = (
            core.clean_text(r.get("strategy_name", ""))
            if has_strategy_col else ""
        )

        if priority > best_priority:
            best_decision = decision
            best_strategy = strategy
            best_priority = priority
        elif priority == best_priority and priority > 0:
            if not best_strategy or strategy < best_strategy:
                best_decision = decision
                best_strategy = strategy

    if best_priority == 0:
        return "unscreened", ""
    return best_decision, best_strategy

def apply_screening(
    strategy_name: str,
    library_root: Path,
    config: dict,
) -> dict:
    library_root = Path(library_root)
    library_root.mkdir(parents=True, exist_ok=True)
    result = {
        "moved": 0, "skipped": 0, "restored": 0, "redownloaded": 0,
        "missing": 0, "errors": [], "space_freed_bytes": 0,
        "missing_cids": [], "_missing_cids_set": set(),
    }
    if not strategy_name:
        result["errors"].append("empty_strategy")
        return result

    decisions_all = _read_decisions_full_df(
        library_root / "screening_decisions.parquet"
    )
    records_path = library_root / "records_final.parquet"
    if not records_path.exists():
        result["errors"].append("records_final_not_found")
        return result
    try:
        records = pd.read_parquet(records_path)
    except Exception as exc:
        result["errors"].append(f"records_read_failed: {exc}")
        return result
    if "strategy_names" not in records.columns:
        result["errors"].append("records_missing_strategy_names")
        return result

    pattern = record_normalize.strategy_regex(strategy_name)
    mask = (
        records["strategy_names"].fillna("").astype(str)
        .str.contains(pattern, regex=True, na=False)
    )
    strategy_cids = set(records.loc[mask, "canonical_id"].astype(str).tolist())
    if not strategy_cids:
        result["errors"].append("no_records_for_strategy")
        return result

    excluded_special_cids: list[tuple[str, str]] = []
    if "special_status" in records.columns:
        sp_series = (
            records["special_status"].fillna("").astype(str).str.strip()
        )
        cid_series = records["canonical_id"].astype(str)
        kept_cids: set[str] = set()
        for cid in strategy_cids:
            cmask = cid_series == cid
            if not cmask.any():
                kept_cids.add(cid)
                continue
            cur_status = sp_series[cmask].iloc[0]
            if cur_status == "":
                kept_cids.add(cid)
            else:
                excluded_special_cids.append((cid, cur_status))
        strategy_cids = kept_cids
        if excluded_special_cids:
            now_skip = _now_iso()
            _append_screening_audit(
                library_root,
                [
                    {"canonical_id": cid,
                     "action": "skip_special",
                     "from_path": "",
                     "to_path": f"status={st}",
                     "timestamp": now_skip,
                     "operator": "system"}
                    for cid, st in excluded_special_cids
                ],
            )

    cfg = _screening_cfg(config)
    retention_mode = core.clean_text(
        cfg.get("retention_mode", _DEFAULT_RETENTION_MODE)
    ) or _DEFAULT_RETENTION_MODE
    if retention_mode not in ("release_space", "complete_retention"):
        retention_mode = _DEFAULT_RETENTION_MODE

    file_index_path = library_root / "file_index.parquet"
    try:
        file_index = _read_file_index_v2(file_index_path)
    except RuntimeError as exc:
        result["errors"].append(f"file_index_read_failed: {exc}")
        result["missing_cids"] = sorted(
            result.pop("_missing_cids_set", set())
        )
        return result

    audit_rows: list[dict] = []
    now = _now_iso()

    for cid in strategy_cids:
        _settle_one_cid(
            cid, decisions_all, file_index, library_root,
            retention_mode, now, audit_rows, result, config,
        )

    result["missing_cids"] = sorted(
        result.pop("_missing_cids_set", set())
    )

    try:
        _save_table(file_index, file_index_path)
    except OSError as exc:
        result["errors"].append(f"file_index_write_failed: {exc}")

    _append_screening_audit(library_root, audit_rows)

    try:
        update_strategy_status(strategy_name, library_root)
    except Exception as exc:
        result["errors"].append(f"update_status_failed: {exc}")

    return result

def _settle_one_cid(
    cid: str,
    decisions_all: pd.DataFrame,
    file_index: pd.DataFrame,
    library_root: Path,
    retention_mode: str,
    now: str,
    audit_rows: list[dict],
    result: dict,
    config: dict,
) -> None:
    try:
        highest_decision, locked_strategy = _compute_highest_priority_decision(
            cid, decisions_all,
        )
    except Exception as exc:
        result["errors"].append(f"{cid}: priority_error: {exc}")
        return

    target_subdir, target_level = _decision_target(highest_decision)

    fmask = file_index["canonical_id"].astype(str) == cid
    fridx: int | None = None
    if fmask.any():
        fridx = int(file_index.index[fmask][0])

    safe = core.canonical_id_to_dirname(cid)
    target_dir = library_root / target_subdir / safe

    def _is_in_target() -> bool:
        if target_subdir == "unscreened":
            return (target_dir / "metadata.json").is_file()
        if (target_subdir == "excluded"
                and retention_mode == "release_space"):
            return (
                (target_dir / "article.xml").is_file()
                or (target_dir / "article.md").is_file()
            )
        return (
            (target_dir / "article.pdf").is_file()
            or (target_dir / "article.xml").is_file()
            or (target_dir / "article.html").is_file()
        )

    already = _is_in_target()

    trash_dir_str = ""
    trash_bytes = 0
    move_result: dict = {}

    if already:
        result["skipped"] += 1
    else:
        move_result = _execute_cid_move(
            cid, target_dir, target_subdir,
            library_root, retention_mode,
            now, audit_rows, result, config,
            file_index,
        )
        kind = str(move_result.get("kind", "failed") or "failed")
        trash_dir_str = str(move_result.get("trash_dir") or "")
        trash_bytes = int(move_result.get("trash_bytes") or 0)
        result["space_freed_bytes"] += trash_bytes
        if kind == "move":
            result["moved"] += 1
        elif kind == "restore":
            result["restored"] += 1
        elif kind == "redownload":
            result["redownloaded"] += 1
            # redownload 可能通过内存 file_index 新增行，重新定位 fridx
            fmask_new = file_index["canonical_id"].astype(str) == cid
            if fmask_new.any():
                fridx = int(file_index.index[fmask_new][0])
        else:
            result["missing"] += 1
            result["_missing_cids_set"].add(cid)

    if fridx is None:
        new_row = {c: "" for c in _FILE_INDEX_COLUMNS_V2}
        new_row["canonical_id"] = cid
        new_row["file_missing"] = False
        new_idx = (
            int(file_index.index.max()) + 1
            if len(file_index) > 0
            else 0
        )
        file_index.loc[new_idx] = new_row
        fridx = new_idx

    file_index.at[fridx, "highest_priority_strategy"] = locked_strategy
    file_index.at[fridx, "highest_priority_decision"] = highest_decision
    file_index.at[fridx, "file_location_updated_at"] = now
    file_index.at[fridx, "file_retention_level"] = target_level
    file_index.at[fridx, "file_missing"] = (cid in result["_missing_cids_set"])

    if move_result.get("clear_trash_meta"):
        file_index.at[fridx, "trash_path"] = ""
        file_index.at[fridx, "deleted_at"] = ""
        file_index.at[fridx, "expire_at"] = ""

    if trash_dir_str:
        retention_days = _get_trash_retention_days(config)
        try:
            now_dt = datetime.fromisoformat(now)
        except ValueError:
            now_dt = datetime.now(timezone.utc)
        expire = (now_dt + timedelta(days=retention_days)).isoformat()
        file_index.at[fridx, "trash_path"] = trash_dir_str
        file_index.at[fridx, "deleted_at"] = now
        file_index.at[fridx, "expire_at"] = expire

    current = ""
    for cand in _OPEN_LITERATURE_FILENAMES:
        p = target_dir / cand
        if p.is_file():
            current = str(p)
            break
    if current:
        file_index.at[fridx, "current_file_path"] = current
    elif cid in result["_missing_cids_set"]:
        file_index.at[fridx, "current_file_path"] = ""

def _resolve_target_files(
    target_subdir: str,
    retention_mode: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if target_subdir in ("included", "maybe"):
        return _PDF_PLUS_XML_PLUS_MD, ()
    if target_subdir == "excluded":
        if retention_mode == "complete_retention":
            return _PDF_PLUS_XML_PLUS_MD, ()
        return _XML_PLUS_MD_ONLY, (
            "article.pdf", "article.html", "article_secondary.pdf",
        )
    return (), _PDF_PLUS_XML_PLUS_MD

def _execute_cid_move(
    cid: str,
    target_dir: Path,
    target_subdir: str,
    library_root: Path,
    retention_mode: str,
    now: str,
    audit_rows: list[dict],
    result: dict,
    config: dict,
    file_index: pd.DataFrame,
) -> dict:
    safe = core.canonical_id_to_dirname(cid)
    trash_dir_str = ""
    trash_bytes = 0

    src_subdir = ""
    for s in _OPEN_LITERATURE_SUBDIRS:
        sd = library_root / s / safe
        if sd.exists() and any(sd.iterdir()):
            src_subdir = s
            break

    moved_files, trash_files = _resolve_target_files(
        target_subdir, retention_mode,
    )

    moved_any = False
    if src_subdir:
        src_dir = library_root / src_subdir / safe

        if moved_files:
            moved = _move_named_files(src_dir, target_dir, moved_files)
            for s_path, d_path in moved:
                audit_rows.append({
                    "canonical_id": cid,
                    "action": "move",
                    "from_path": s_path,
                    "to_path": d_path,
                    "timestamp": now,
                    "operator": "ui",
                })
            if moved:
                moved_any = True

        if trash_files:
            trash_dir = library_root / "trash" / safe
            trash_dir.mkdir(parents=True, exist_ok=True)
            for fname in trash_files:
                fp = src_dir / fname
                if not fp.is_file():
                    continue
                dst_trash = trash_dir / fname
                if dst_trash.exists():
                    continue
                try:
                    file_size = int(fp.stat().st_size)
                    shutil.move(str(fp), str(dst_trash))
                    trash_bytes += file_size
                    trash_dir_str = str(trash_dir)
                    audit_rows.append({
                        "canonical_id": cid,
                        "action": "soft_delete",
                        "from_path": str(fp),
                        "to_path": str(dst_trash),
                        "timestamp": now,
                        "operator": "ui",
                    })
                    moved_any = True
                except OSError:
                    continue

        if target_subdir == "unscreened":
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "metadata.json").write_text(
                json.dumps(
                    {"canonical_id": cid, "retention_level": "L5"},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )

        try:
            if src_dir.exists() and not any(src_dir.iterdir()):
                src_dir.rmdir()
        except OSError:
            pass

        if moved_any:
            return {
                "moved_any": True,
                "kind": "move",
                "trash_dir": trash_dir_str,
                "trash_bytes": trash_bytes,
                "clear_trash_meta": False,
            }

    needed_pdf = not (
        target_subdir == "excluded" and retention_mode == "release_space"
    )
    restore_result = _restore_pdf_from_trash(
        cid, library_root, target_subdir, needed_pdf=needed_pdf,
    )
    if restore_result["restored"]:
        return {
            "moved_any": True,
            "kind": "restore",
            "trash_dir": "",
            "trash_bytes": 0,
            "clear_trash_meta": restore_result["clear_trash_meta"],
        }

    try:
        if redownload_from_urls(
            cid, library_root, target_subdir, config, file_index=file_index,
        ):
            return {
                "moved_any": True,
                "kind": "redownload",
                "trash_dir": "",
                "trash_bytes": 0,
                "clear_trash_meta": False,
            }
    except robustness.InterruptRequested:
        raise
    except Exception as exc:
        result["errors"].append(f"{cid}: redownload_failed: {exc}")

    return {
        "moved_any": False,
        "kind": "failed",
        "trash_dir": "",
        "trash_bytes": 0,
        "clear_trash_meta": False,
    }

def _get_trash_retention_days(config: dict | None) -> int:
    if config is None:
        return _DEFAULT_TRASH_RETENTION_DAYS
    try:
        return int(
            config.get("screening", {}).get(
                "trash_retention_days",
                _DEFAULT_TRASH_RETENTION_DAYS,
            )
        )
    except (ValueError, TypeError):
        return _DEFAULT_TRASH_RETENTION_DAYS

def soft_delete(
    canonical_id: str,
    file_path: Path,
    library_root: Path,
    config: dict | None = None, 
) -> None:
    file_path = Path(file_path)
    library_root = Path(library_root)
    if not file_path.exists():
        return

    safe = core.canonical_id_to_dirname(canonical_id)
    trash_dir = library_root / "trash" / safe
    trash_dir.mkdir(parents=True, exist_ok=True)
    dst = trash_dir / file_path.name
    if not dst.exists():
        shutil.move(str(file_path), str(dst))

    file_index_path = library_root / "file_index.parquet"
    file_index = _read_file_index_v2(file_index_path)
    ridx = _ensure_file_index_row(file_index, canonical_id)

    now = _now_iso()
    retention_days = _get_trash_retention_days(config)
    expire = (
        datetime.now(timezone.utc) + timedelta(days=retention_days)
    ).isoformat()

    file_index.at[ridx, "file_retention_level"] = "TRASH"
    file_index.at[ridx, "trash_path"] = str(trash_dir)
    file_index.at[ridx, "deleted_at"] = now
    file_index.at[ridx, "expire_at"] = expire
    file_index.at[ridx, "current_file_path"] = str(dst)
    file_index.at[ridx, "file_last_moved_at"] = now

    _save_table(file_index, file_index_path)
    _refresh_strategy_status_for_cids({canonical_id}, library_root, touch_activity=False)

def restore_from_trash(
    canonical_id: str,
    library_root: Path,
    target_level: str | None = None,
    strategy_name: str = "",
) -> None:
    library_root = Path(library_root)
    file_index_path = library_root / "file_index.parquet"
    decisions_path = library_root / "screening_decisions.parquet"

    file_index = _read_file_index_v2(file_index_path)
    decisions = _read_decisions_full_df(decisions_path)
    has_strategy_col = _DECISIONS_STRATEGY_COL in decisions.columns

    mask = file_index["canonical_id"].astype(str) == str(canonical_id)
    if not mask.any():
        raise FileNotFoundError(f"file_index 中无 {canonical_id} 记录")
    ridx = int(file_index.index[mask][0])

    trash_path = core.clean_text(file_index.at[ridx, "trash_path"])
    if not trash_path:
        raise ValueError(f"{canonical_id} 不在 trash 中（trash_path 为空）")

    if target_level is None:
        decision = ""
        if has_strategy_col and strategy_name:
            smask = (
                (decisions["canonical_id"].astype(str) == str(canonical_id))
                & (decisions[_DECISIONS_STRATEGY_COL].astype(str)
                   == strategy_name)
            )
            if smask.any():
                decision = core.clean_text(
                    decisions.loc[smask, "user_decision"].iloc[0]
                ).lower()
        elif has_strategy_col:
            hd, _hs = _compute_highest_priority_decision(
                canonical_id, decisions,
            )
            decision = hd
        else:
            dmask = decisions["canonical_id"].astype(str) == str(canonical_id)
            if dmask.any():
                decision = core.clean_text(
                    decisions.loc[dmask, "user_decision"].iloc[0]
                ).lower()

        if decision == "exclude":
            target_level = "L4"
        elif decision == "maybe":
            target_level = "L5_MAYBE"
        elif decision == "include":
            target_level = "L3"
        else:
            target_level = "L5"

    safe_for_check = core.canonical_id_to_dirname(canonical_id)
    trash_dir_check = library_root / "trash" / safe_for_check
    has_pdf_in_trash = False
    if trash_dir_check.exists():
        for fp in trash_dir_check.iterdir():
            if fp.is_file() and fp.name in (
                "article.pdf", "article_secondary.pdf",
            ):
                has_pdf_in_trash = True
                break
    if has_pdf_in_trash and target_level in ("L4", "L5"):
        target_level = "L3"

    level_to_subdir = {
        "L3": "included", "L4": "excluded",
        "L5_MAYBE": "maybe", "L5": "unscreened",
    }
    target_subdir = level_to_subdir.get(target_level, "unscreened")

    safe = core.canonical_id_to_dirname(canonical_id)
    trash_dir = library_root / "trash" / safe
    dst_dir = library_root / target_subdir / safe
    dst_dir.mkdir(parents=True, exist_ok=True)

    now = _now_iso()
    audit_rows: list[dict] = []
    if trash_dir.exists():
        files_to_restore = [fp for fp in trash_dir.iterdir() if fp.is_file()]
        for fp in files_to_restore:
            dst = dst_dir / fp.name
            if dst.exists():
                raise FileExistsError(f"目标位置已有同名文件：{dst}")
        moved_pairs: list[tuple[Path, Path]] = []
        try:
            for fp in files_to_restore:
                dst = dst_dir / fp.name
                shutil.move(str(fp), str(dst))
                moved_pairs.append((fp, dst))
                audit_rows.append({
                    "canonical_id": canonical_id,
                    "action": "restore_from_trash",
                    "from_path": str(fp),
                    "to_path": str(dst),
                    "timestamp": now,
                    "operator": "cli",
                })
        except OSError:
            for src, dst in reversed(moved_pairs):
                if dst.exists() and not src.exists():
                    try:
                        shutil.move(str(dst), str(src))
                    except OSError:
                        pass
            raise

    file_index.at[ridx, "file_last_moved_at"] = now
    file_index.at[ridx, "file_retention_level"] = target_level
    current = ""
    for cand in ("article.pdf", "article.xml", "article.md", "metadata.json"):
        p = dst_dir / cand
        if p.exists():
            current = str(p)
            break
    file_index.at[ridx, "current_file_path"] = current
    file_index.at[ridx, "trash_path"] = ""
    file_index.at[ridx, "deleted_at"] = ""
    file_index.at[ridx, "expire_at"] = ""
    if "file_missing" in file_index.columns:
        file_index.at[ridx, "file_missing"] = False
    if "file_location_updated_at" in file_index.columns:
        file_index.at[ridx, "file_location_updated_at"] = now

    _save_table(file_index, file_index_path)

    try:
        if trash_dir.exists() and not any(trash_dir.iterdir()):
            trash_dir.rmdir()
    except OSError:
        pass

    new_decision = {
        "L3": "include", "L4": "exclude",
        "L5_MAYBE": "maybe", "L5": "unscreened",
    }.get(target_level, "unscreened")

    if not has_strategy_col:
        dmask = decisions["canonical_id"].astype(str) == str(canonical_id)
        if dmask.any():
            decisions.loc[dmask, "user_decision"] = new_decision
            _save_table(decisions, decisions_path)
    elif strategy_name:
        dmask = (
            (decisions["canonical_id"].astype(str) == str(canonical_id))
            & (decisions[_DECISIONS_STRATEGY_COL].astype(str)
               == strategy_name)
        )
        if dmask.any():
            decisions.loc[dmask, "user_decision"] = new_decision
            _save_table(decisions, decisions_path)

    _append_screening_audit(library_root, audit_rows)
    _refresh_strategy_status_for_cids({canonical_id}, library_root)

def _expired_trash_rows(
    file_index: pd.DataFrame,
    now: datetime,
) -> list[tuple[int, str]]:
    expired: list[tuple[int, str]] = []
    for idx in file_index.index:
        trash_path = core.clean_text(file_index.at[idx, "trash_path"])
        if not trash_path:
            continue
        expire_str = core.clean_text(file_index.at[idx, "expire_at"])
        if not expire_str:
            continue
        try:
            expire_dt = datetime.fromisoformat(expire_str)
        except ValueError:
            continue
        if expire_dt <= now:
            expired.append((int(idx), core.clean_text(file_index.at[idx, "canonical_id"])))
    return expired

def preview_trash_cleanup(library_root: Path) -> Path:
    library_root = Path(library_root)
    file_index = _load_table(library_root / "file_index.parquet", _FILE_INDEX_COLUMNS)
    trash_dir = library_root / "trash"

    now = datetime.now(timezone.utc)
    expired = _expired_trash_rows(file_index, now)

    files: list[str] = []
    freed_bytes = 0
    for _, cid in expired:
        safe = core.canonical_id_to_dirname(cid)
        cdir = trash_dir / safe
        if cdir.exists():
            for fp in cdir.rglob("*"):
                if fp.is_file():
                    files.append(str(fp))
                    try:
                        freed_bytes += fp.stat().st_size
                    except OSError:
                        pass

    preview = {
        "generated_at": now.isoformat(),
        "trash_dir": str(trash_dir),
        "to_delete_count": len(expired),
        "files_count": len(files),
        "freed_bytes": freed_bytes,
        "canonical_ids": [cid for _, cid in expired],
        "files": files,
    }
    path = library_root / "trash_cleanup_preview.json"
    path.write_text(json.dumps(preview, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

def cleanup_trash(
    library_root: Path,
    config: dict,
    dry_run: bool = False,
) -> dict:
    library_root = Path(library_root)
    file_index_path = library_root / "file_index.parquet"
    file_index = _read_file_index_v2(file_index_path)
    trash_dir = library_root / "trash"

    now = datetime.now(timezone.utc)
    expired = _expired_trash_rows(file_index, now)

    deleted_ids: list[str] = []
    freed_bytes = 0

    for ridx, cid in expired:
        safe = core.canonical_id_to_dirname(cid)
        cdir = trash_dir / safe
        if cdir.exists():
            for fp in cdir.rglob("*"):
                if fp.is_file():
                    try:
                        freed_bytes += fp.stat().st_size
                    except OSError:
                        pass
            if not dry_run:
                shutil.rmtree(cdir, ignore_errors=True)

        if not dry_run:
            old_level = core.clean_text(file_index.at[ridx, "file_retention_level"])
            if old_level == "TRASH":
                file_index.at[ridx, "file_retention_level"] = "EMPTY"
                file_index.at[ridx, "current_file_path"] = ""
            file_index.at[ridx, "trash_path"] = ""
            file_index.at[ridx, "deleted_at"] = ""
            file_index.at[ridx, "expire_at"] = ""

        deleted_ids.append(cid)

    if not dry_run and len(deleted_ids) > 0:
        _save_table(file_index, file_index_path)

    _refresh_strategy_status_for_cids(set(deleted_ids), library_root, touch_activity=False)
    return {
        "deleted_count": len(deleted_ids),
        "freed_bytes": freed_bytes,
        "deleted_ids": deleted_ids,
    }

def set_maybe_decision(
    canonical_id: str,
    decision: str,
    library_root: Path,
    config: dict | None = None,
    strategy_name: str = "",
) -> None:
    if decision not in ("include", "exclude"):
        raise ValueError(
            f"maybe 二次决策只能为 include / exclude，"
            f"收到 {decision}"
        )
    library_root = Path(library_root)
    decisions_path = library_root / "screening_decisions.parquet"
    decisions = _read_decisions_full_df(decisions_path)
    has_strategy_col = _DECISIONS_STRATEGY_COL in decisions.columns

    new_row = {c: "" for c in _SCREENING_DECISIONS_COLUMNS}
    new_row.update({
        "canonical_id": canonical_id,
        "user_decision": decision,
        "user_decision_date": _today_iso(),
        "user_confirmed_at": _now_iso(),
    })
    if has_strategy_col:
        new_row[_DECISIONS_STRATEGY_COL] = strategy_name

    if has_strategy_col:
        mask = (
            (decisions["canonical_id"].astype(str) == canonical_id)
            & (decisions[_DECISIONS_STRATEGY_COL].astype(str)
               == strategy_name)
        )
    else:
        mask = decisions["canonical_id"].astype(str) == canonical_id

    if mask.any():
        for k, v in new_row.items():
            if k in decisions.columns:
                decisions.loc[mask, k] = v
    else:
        decisions = pd.concat(
            [decisions, pd.DataFrame([new_row])],
            ignore_index=True,
        )
    _save_table(decisions, decisions_path)

    try:
        _sync_file_index_priority_metadata({canonical_id}, library_root)
    except Exception:
        pass
    _refresh_strategy_status_for_cids({canonical_id}, library_root)

def batch_confirm(
    prescreen_label: str,
    library_root: Path,
    strategy_name: str = "",
) -> int:
    library_root = Path(library_root)
    prescreen_path = library_root / "prescreen" / "screening_prescreen_labels.parquet"
    if not prescreen_path.exists():
        return 0

    labels = pd.read_parquet(prescreen_path)
    if "prescreen_label" not in labels.columns:
        return 0

    subset = labels[labels["prescreen_label"] == prescreen_label]
    if len(subset) == 0:
        return 0

    mapping = {"auto_include": "include", "auto_exclude": "exclude"}
    user_decision = mapping.get(prescreen_label, "")

    decisions_path = library_root / "screening_decisions.parquet"
    decisions = _read_decisions_full_df(decisions_path)
    has_strategy_col = _DECISIONS_STRATEGY_COL in decisions.columns

    cid_strategies: dict[str, list[str]] = {}
    if has_strategy_col and not strategy_name:
        records_path = library_root / "records_final.parquet"
        if records_path.exists():
            try:
                records = pd.read_parquet(
                    records_path,
                    columns=["canonical_id", "strategy_names"],
                )
                for _, r in records.iterrows():
                    cid = core.clean_text(r.get("canonical_id", ""))
                    if not cid:
                        continue
                    for s in str(r.get("strategy_names", "")).split(";"):
                        s_clean = core.clean_text(s)
                        if s_clean:
                            cid_strategies.setdefault(cid, []).append(s_clean)
            except Exception:
                cid_strategies = {}

    now = _now_iso()
    today = _today_iso()
    count = 0

    for cid_raw in subset["canonical_id"].astype(str).tolist():
        cid = core.clean_text(cid_raw)
        if not cid:
            continue

        if has_strategy_col:
            if strategy_name:
                targets: list[str] = [strategy_name]
            else:
                strategies_for_cid = cid_strategies.get(cid)
                if strategies_for_cid:
                    seen: set[str] = set()
                    targets = []
                    for s in strategies_for_cid:
                        if s not in seen:
                            seen.add(s)
                            targets.append(s)
                else:
                    targets = [""]
        else:
            targets = [""]

        for target_strategy in targets:
            if has_strategy_col:
                dmask = (
                    (decisions["canonical_id"].astype(str) == cid)
                    & (decisions[_DECISIONS_STRATEGY_COL].astype(str)
                       == target_strategy)
                )
            else:
                dmask = decisions["canonical_id"].astype(str) == cid

            if dmask.any():
                if user_decision:
                    decisions.loc[dmask, "user_decision"] = user_decision
                decisions.loc[dmask, "user_decision_date"] = today
                decisions.loc[dmask, "user_confirmed_at"] = now
                decisions.loc[dmask, "prescreen_label_snapshot"] = prescreen_label
            else:
                new_row = {c: "" for c in _SCREENING_DECISIONS_COLUMNS}
                new_row.update({
                    "canonical_id": cid,
                    "user_decision": user_decision or "unscreened",
                    "user_decision_date": today,
                    "user_confirmed_at": now,
                    "prescreen_label_snapshot": prescreen_label,
                })
                if has_strategy_col:
                    new_row[_DECISIONS_STRATEGY_COL] = target_strategy
                decisions = pd.concat(
                    [decisions, pd.DataFrame([new_row])],
                    ignore_index=True,
                )
        count += 1

    _save_table(decisions, decisions_path)
    affected_cids = set(subset["canonical_id"].astype(str))
    try:
        _sync_file_index_priority_metadata(affected_cids, library_root)
    except Exception:
        pass
    _refresh_strategy_status_for_cids(affected_cids, library_root)
    return count

def update_strategy_status(strategy_name: str, library_root: Path) -> None:
    library_root = Path(library_root)
    records_path = library_root / "records_final.parquet"
    decisions_path = library_root / "screening_decisions.parquet"
    file_index_path = library_root / "file_index.parquet"
    archive_root = library_root.parent / "archive"
    registry_path = archive_root / "strategy_registry.parquet"

    if not records_path.exists():
        return

    records = pd.read_parquet(records_path)
    if "strategy_names" not in records.columns:
        return

    try:
        decisions = _read_decisions_full_df(decisions_path)
    except RuntimeError as exc:
        print(
            f"[screening] update_strategy_status 跳过"
            f"（decisions 异常）：{exc}",
            file=sys.stderr,
        )
        return
    file_index = _load_table(file_index_path, _FILE_INDEX_COLUMNS)

    _pattern = re.compile(
        record_normalize.strategy_regex(strategy_name)
    )
    col = records["strategy_names"].fillna("").astype(str)
    mask = col.str.contains(_pattern, regex=True, na=False)

    if "special_status" in records.columns:
        sp_series = (
            records["special_status"].fillna("").astype(str).str.strip()
        )
        mask = mask & sp_series.eq("")

    cids = records.loc[mask, "canonical_id"].astype(str).tolist()

    unscreened_count = 0
    maybe_count = 0
    trashed_count = 0

    has_strategy_col = _DECISIONS_STRATEGY_COL in decisions.columns
    if has_strategy_col:
        decisions_for_strategy = decisions[
            decisions[_DECISIONS_STRATEGY_COL].astype(str)
            == strategy_name
        ]
    else:
        decisions_for_strategy = decisions

    decision_map = dict(zip(
        decisions_for_strategy["canonical_id"].astype(str),
        decisions_for_strategy["user_decision"].astype(str),
    ))
    level_map = dict(zip(file_index["canonical_id"].astype(str),
                         file_index["file_retention_level"].astype(str)))

    for cid in cids:
        d = decision_map.get(cid, "")
        level = level_map.get(cid, "")
        if level == "TRASH":
            trashed_count += 1
        if d in ("", "unscreened"):
            unscreened_count += 1
        elif d == "maybe":
            maybe_count += 1

    if not cids:
        status = "unscreened"
    elif unscreened_count == len(cids):
        status = "unscreened"
    elif unscreened_count > 0 or maybe_count > 0:
        status = "partially_screened"
    else:
        status = "confirmed"

    registry = _load_table(registry_path, _STRATEGY_REGISTRY_COLUMNS)
    now = _now_iso()
    rmask = registry["strategy_name"] == strategy_name

    prev_activity = ""
    if rmask.any():
        prev_activity = core.clean_text(
            registry.loc[rmask, "last_activity_at"].iloc[0]
        )
    days = _days_since(prev_activity)

    if rmask.any():
        registry.loc[rmask, "strategy_status"] = status  
        registry.loc[rmask, "unscreened_count"] = unscreened_count
        registry.loc[rmask, "maybe_count"] = maybe_count
        registry.loc[rmask, "trashed_count"] = trashed_count
        registry.loc[rmask, "days_since_last_activity"] = days
    else:
        new_row = {
            "strategy_name": strategy_name,
            "strategy_status": status,
            "created_at": now,
            "last_activity_at": now,
            "unscreened_count": unscreened_count,
            "maybe_count": maybe_count,
            "trashed_count": trashed_count,
            "days_since_last_activity": days,
        }
        registry = pd.concat([registry, pd.DataFrame([new_row])], ignore_index=True)

    _save_table(registry, registry_path)

def reopen_strategy(
    strategy_name: str,
    library_root: Path,
    config: dict | None = None,
) -> dict:
    if not strategy_name:
        return {"ok": False, "reason": "empty_strategy"}

    library_root = Path(library_root)
    archive_root = library_root.parent / "archive"
    registry_path = archive_root / "strategy_registry.parquet"

    if not registry_path.exists():
        return {"ok": False, "reason": "registry_not_found"}

    try:
        registry = _load_table(registry_path, _STRATEGY_REGISTRY_COLUMNS)
    except RuntimeError:
        return {"ok": False, "reason": "registry_read_failed"}

    if len(registry) == 0:
        return {"ok": False, "reason": "strategy_not_found"}

    mask = registry["strategy_name"].astype(str) == str(strategy_name)
    if not mask.any():
        return {"ok": False, "reason": "strategy_not_found"}

    current_status = core.clean_text(
        registry.loc[mask, "strategy_status"].iloc[0]
    )

    if current_status == "confirmed":
        registry.loc[mask, "strategy_status"] = "partially_screened"
        registry.loc[mask, "last_activity_at"] = _now_iso()
        try:
            _save_table(registry, registry_path)
        except OSError:
            return {"ok": False, "reason": "registry_write_failed"}
        return {
            "ok": True,
            "from": "confirmed",
            "to": "partially_screened",
        }

    if current_status == "archived_unprocessed":
        try:
            from medlitflow import archive as _arc
            if config is not None:
                _arc._set_config(config)
            try:
                marker_deleted = _arc.unmark_archived_unprocessed(
                    strategy_name
                )
            finally:
                if config is not None:
                    _arc._set_config(None)
            if not marker_deleted:
                return {"ok": False, "reason": "marker_remove_failed"}
        except Exception:
            return {"ok": False, "reason": "archive_unavailable"}

        try:
            update_strategy_status(strategy_name, library_root)
        except Exception:
            pass

        new_status = ""
        try:
            r2 = _load_table(registry_path, _STRATEGY_REGISTRY_COLUMNS)
            m2 = r2["strategy_name"].astype(str) == str(strategy_name)
            if m2.any():
                new_status = core.clean_text(
                    r2.loc[m2, "strategy_status"].iloc[0]
                )
        except Exception:
            pass

        if new_status in ("", "archived_unprocessed"):
            try:
                r3 = _load_table(registry_path, _STRATEGY_REGISTRY_COLUMNS)
                m3 = r3["strategy_name"].astype(str) == str(strategy_name)
                if m3.any():
                    r3.loc[m3, "strategy_status"] = "partially_screened"
                    r3.loc[m3, "last_activity_at"] = _now_iso()
                    _save_table(r3, registry_path)
                    new_status = "partially_screened"
            except OSError:
                return {"ok": False, "reason": "fallback_write_failed"}

        try:
            _touch_activity(strategy_name, library_root)
        except Exception:
            pass

        return {
            "ok": True,
            "from": "archived_unprocessed",
            "to": new_status or "partially_screened",
        }

    return {
        "ok": False,
        "reason": f"status_is_{current_status or 'empty'}",
    }

def compute_progress_stats(
    strategy_name: str,
    library_root: Path,
) -> dict:
    library_root = Path(library_root)
    records_path = library_root / "records_final.parquet"
    decisions_path = library_root / "screening_decisions.parquet"

    stats = {
        "include": 0, "exclude": 0, "maybe": 0,
        "unscreened": 0, "total": 0,
    }

    if not records_path.exists():
        return stats

    try:
        records = pd.read_parquet(records_path)
    except Exception:
        return stats

    if strategy_name and "strategy_names" in records.columns:
        pattern = record_normalize.strategy_regex(strategy_name)
        mask = (
            records["strategy_names"].fillna("").astype(str)
            .str.contains(pattern, regex=True, na=False)
        )
        sub = records[mask]
    else:
        sub = records

    if "special_status" in sub.columns:
        sub = sub[
            sub["special_status"].fillna("").astype(str)
            .str.strip().eq("")
        ]

    total = int(len(sub))
    stats["total"] = total
    if total == 0:
        return stats
    cids = set(sub["canonical_id"].astype(str).tolist())

    if not decisions_path.exists():
        stats["unscreened"] = total
        return stats

    try:
        decisions = pd.read_parquet(decisions_path)
    except Exception:
        stats["unscreened"] = total
        return stats

    if len(decisions) == 0 or "canonical_id" not in decisions.columns:
        stats["unscreened"] = total
        return stats

    if strategy_name and "strategy_name" in decisions.columns:
        decisions = decisions[
            decisions["strategy_name"].astype(str) == strategy_name
        ]

    cid_series = decisions["canonical_id"].astype(str)
    decisions = decisions[cid_series.isin(cids)]

    if len(decisions) == 0:
        stats["unscreened"] = total
        return stats

    decisions = decisions.drop_duplicates(
        subset=["canonical_id"], keep="last",
    )

    if "user_decision" not in decisions.columns:
        stats["unscreened"] = total
        return stats

    decision_norm = (
        decisions["user_decision"].fillna("").astype(str)
        .str.strip().str.lower()
    )
    stats["include"] = int((decision_norm == "include").sum())
    stats["exclude"] = int((decision_norm == "exclude").sum())
    stats["maybe"] = int((decision_norm == "maybe").sum())

    decided = (
        stats["include"] + stats["exclude"]
        + stats["maybe"]
    )
    stats["unscreened"] = max(0, total - decided)
    return stats

def compute_storage_stats(library_root: Path) -> dict:
    library_root = Path(library_root)
    result = {
        "library_bytes": 0,
        "library_gb": 0.0,
        "trash_bytes": 0,
        "trash_gb": 0.0,
    }
    if not library_root.exists():
        return result

    def _dir_size(path: Path) -> int:
        total = 0
        if not path.exists():
            return 0
        for fp in path.rglob("*"):
            if fp.is_file():
                try:
                    total += fp.stat().st_size
                except OSError:
                    continue
        return total

    trash_dir = library_root / "trash"
    trash_size = _dir_size(trash_dir)
    total_size = _dir_size(library_root)
    library_size = max(0, total_size - trash_size)

    result["library_bytes"] = int(library_size)
    result["library_gb"] = round(library_size / (1024 ** 3), 2)
    result["trash_bytes"] = int(trash_size)
    result["trash_gb"] = round(trash_size / (1024 ** 3), 2)
    return result

def _classify_downloaded_content(
    payload: bytes,
    content_type: str,
    url: str,
) -> str:
    if not payload:
        return "other"

    if payload.startswith(b"%PDF"):
        return "pdf"
    sample = payload[:2000].lstrip().lower()
    if sample.startswith(b"<?xml") or sample.startswith(b"<tei"):
        return "xml"
    if (sample.startswith(b"<!doctype html")
            or sample.startswith(b"<html")):
        return "html"

    ct = (content_type or "").lower()
    if "pdf" in ct:
        return "pdf"
    if "xml" in ct:
        return "xml"
    if "html" in ct:
        return "html"

    url_l = (url or "").lower()
    if url_l.endswith(".pdf"):
        return "pdf"
    if url_l.endswith(".xml"):
        return "xml"
    if url_l.endswith((".html", ".htm")):
        return "html"

    return "other"

def compute_file_missing_status(
    library_root: Path,
    persist: bool = True,
) -> dict[str, bool]:
    global _FILE_MISSING_CACHE_SIG, _FILE_MISSING_CACHE_DATA
    library_root = Path(library_root)
    result: dict[str, bool] = {}
    file_index_path = library_root / "file_index.parquet"
    if not file_index_path.exists():
        return result

    try:
        st = file_index_path.stat()
        sig = (
            str(file_index_path.resolve()),
            float(st.st_mtime),
            int(st.st_size),
        )
    except OSError:
        return result

    if (
        _FILE_MISSING_CACHE_SIG == sig
        and _FILE_MISSING_CACHE_DATA is not None
    ):
        return dict(_FILE_MISSING_CACHE_DATA)

    try:
        fi = pd.read_parquet(file_index_path)
    except Exception:
        return result
    if len(fi) == 0:
        _FILE_MISSING_CACHE_SIG = sig
        _FILE_MISSING_CACHE_DATA = {}
        return result
    if "current_file_path" not in fi.columns:
        _FILE_MISSING_CACHE_SIG = sig
        _FILE_MISSING_CACHE_DATA = {}
        return result

    for idx in fi.index:
        cid = core.clean_text(fi.at[idx, "canonical_id"]) if "canonical_id" in fi.columns else ""
        if not cid:
            continue
        current = core.clean_text(fi.at[idx, "current_file_path"])
        if not current:
            continue
        try:
            missing = not Path(current).is_file()
        except OSError:
            missing = True
        if missing:
            result[cid] = True

    _FILE_MISSING_CACHE_SIG = sig
    _FILE_MISSING_CACHE_DATA = dict(result)

    if persist and "file_missing" in fi.columns:
        changed = False
        for idx in fi.index:
            cid = core.clean_text(fi.at[idx, "canonical_id"]) if "canonical_id" in fi.columns else ""
            new_val = cid in result
            old_val = bool(fi.at[idx, "file_missing"]) if pd.notna(fi.at[idx, "file_missing"]) else False
            if new_val != old_val:
                fi.at[idx, "file_missing"] = new_val
                changed = True
        if changed:
            try:
                _save_table(fi, file_index_path)
            except OSError:
                pass

    return result

def redownload_from_urls(
    canonical_id: str,
    library_root: Path,
    target_subdir: str,
    config: dict | None = None,
    file_index: pd.DataFrame | None = None,
) -> bool:
    library_root = Path(library_root)
    safe = core.canonical_id_to_dirname(canonical_id)
    dst_dir = library_root / target_subdir / safe

    for fname in ("article.pdf", "article.xml", "article.html"):
        if (dst_dir / fname).is_file():
            return True

    records_path = library_root / "records_final.parquet"
    if not records_path.exists():
        return False
    try:
        records = pd.read_parquet(records_path)
    except Exception:
        return False
    if "canonical_id" not in records.columns:
        return False

    mask = records["canonical_id"].astype(str) == str(canonical_id)
    if not mask.any():
        return False
    row = records[mask].iloc[0]

    urls: list[tuple[str, int]] = []
    seen: set[str] = set()

    def _add(url: Any, priority: int) -> None:
        text = core.clean_text(url)
        if not text or not text.startswith(("http://", "https://")):
            return
        if text in seen:
            return
        seen.add(text)
        urls.append((text, priority))

    _add(row.get("pdf_url", ""), 1)

    raw_json = core.clean_text(row.get("candidate_urls_json", ""))
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, list):
                for entry in parsed:
                    if not isinstance(entry, dict):
                        continue
                    kind = core.clean_text(
                        entry.get("kind_hint", "")
                    ).lower()
                    pri = {"pdf": 1, "xml": 2, "html": 3}.get(kind, 4)
                    _add(entry.get("url", ""), pri)
        except (json.JSONDecodeError, TypeError):
            pass

    _add(row.get("fulltext_url", ""), 3)
    _add(row.get("landing_page_url", ""), 4)

    if not urls:
        return False

    urls.sort(key=lambda t: t[1])

    retrieval_cfg = (config or {}).get("retrieval", {}) or {}
    timeout = int(retrieval_cfg.get("timeout_seconds", 45))
    rate_cfg = (config or {}).get("rate_limit", {}) or {}
    delay = float(rate_cfg.get("download", 0.25))

    dst_dir.mkdir(parents=True, exist_ok=True)

    for url, _pri in urls:
        try:
            response = core.http_download(url, timeout)
        except robustness.InterruptRequested:
            raise
        except Exception:
            continue

        status = int(response.get("status_code", 0) or 0)
        if status < 200 or status >= 300:
            continue

        payload = response.get("payload", b"") or b""
        content_type = response.get("content_type", "") or ""
        final_url = core.clean_text(response.get("final_url", url)) or url

        kind = _classify_downloaded_content(payload, content_type, final_url)
        if kind == "other":
            continue

        ext_map = {"pdf": ".pdf", "xml": ".xml", "html": ".html"}
        fname = f"article{ext_map[kind]}"
        dst = dst_dir / fname
        try:
            dst.write_bytes(payload)
        except OSError:
            continue

        if file_index is not None:
            # 内存模式：直接更新调用方的 DataFrame（由 apply_screening 统一写盘）
            try:
                ridx = _ensure_file_index_row(file_index, canonical_id)
                file_index.at[ridx, "current_file_path"] = str(dst)
                file_index.at[ridx, "downloaded_file_path"] = str(dst)
                file_index.at[ridx, "file_last_moved_at"] = _now_iso()
                file_index.at[ridx, "file_retention_level"] = (
                    _SUBDIR_TO_LEVEL.get(target_subdir, "EMPTY")
                )
            except (OSError, RuntimeError):
                pass
        else:
            try:
                file_index_path = library_root / "file_index.parquet"
                fi = _read_file_index_v2(file_index_path)
                ridx = _ensure_file_index_row(fi, canonical_id)
                fi.at[ridx, "current_file_path"] = str(dst)
                fi.at[ridx, "downloaded_file_path"] = str(dst)
                fi.at[ridx, "file_last_moved_at"] = _now_iso()
                level = _SUBDIR_TO_LEVEL.get(target_subdir, "EMPTY")
                fi.at[ridx, "file_retention_level"] = level
                _save_table(fi, file_index_path)
            except (OSError, RuntimeError):
                pass

        if delay > 0:
            core.interruptible_sleep(delay)

        return True

    return False

def load_screening_rows(
    strategy_name: str,
    library_root: Path,
) -> list[dict]:
    library_root = Path(library_root)
    if not strategy_name:
        return []

    records_path = library_root / "records_final.parquet"
    if not records_path.exists():
        return []

    try:
        records = pd.read_parquet(records_path)
    except Exception:
        return []
    if "strategy_names" not in records.columns:
        return []

    pattern = record_normalize.strategy_regex(strategy_name)
    mask = (
        records["strategy_names"].fillna("").astype(str)
        .str.contains(pattern, regex=True, na=False)
    )
    sub = records[mask]

    if "special_status" in sub.columns:
        sub = sub[
            sub["special_status"].fillna("").astype(str)
            .str.strip().eq("")
        ]

    if len(sub) == 0:
        return []

    decisions_path = library_root / "screening_decisions.parquet"
    all_decisions_df = pd.DataFrame()
    current_decisions: dict[str, str] = {}

    if decisions_path.exists():
        try:
            all_decisions_df = pd.read_parquet(decisions_path)
        except Exception:
            all_decisions_df = pd.DataFrame()

        if (
            len(all_decisions_df) > 0
            and "canonical_id" in all_decisions_df.columns
            and "user_decision" in all_decisions_df.columns
        ):
            has_strategy_col = "strategy_name" in all_decisions_df.columns
            if has_strategy_col:
                filtered = all_decisions_df[
                    all_decisions_df["strategy_name"].astype(str)
                    == strategy_name
                ]
            else:
                filtered = all_decisions_df
            current_decisions = dict(zip(
                filtered["canonical_id"].astype(str),
                filtered["user_decision"]
                .fillna("").astype(str).str.strip().str.lower(),
            ))

    drafts_path = _draft_path(library_root, strategy_name)
    draft_map: dict[str, str] = {}
    if drafts_path.exists():
        try:
            drafts = pd.read_parquet(drafts_path)
            if (
                len(drafts) > 0
                and "canonical_id" in drafts.columns
                and "draft_decision" in drafts.columns
            ):
                draft_map = dict(zip(
                    drafts["canonical_id"].astype(str),
                    drafts["draft_decision"]
                    .fillna("").astype(str).str.strip().str.lower(),
                ))
        except Exception:
            pass

    try:
        missing_map = compute_file_missing_status(library_root)
    except Exception:
        missing_map = {}

    cid_decisions: dict[str, list[tuple[str, str]]] = {}
    if len(all_decisions_df) > 0 and "canonical_id" in all_decisions_df.columns:
        has_strategy_col = "strategy_name" in all_decisions_df.columns
        for _, dr in all_decisions_df.iterrows():
            dcid = core.clean_text(dr.get("canonical_id", ""))
            if not dcid:
                continue
            d = core.clean_text(
                dr.get("user_decision", "")
            ).strip().lower()
            if d not in _VALID_DECISIONS:
                continue
            s = (
                core.clean_text(dr.get("strategy_name", ""))
                if has_strategy_col else ""
            )
            cid_decisions.setdefault(dcid, []).append((d, s))

    fi_v2_map: dict[str, tuple[str, str]] = {}
    try:
        fi_path = library_root / "file_index.parquet"
        if fi_path.exists():
            fi_raw = pd.read_parquet(fi_path)
            if "highest_priority_strategy" in fi_raw.columns:
                for _, fr in fi_raw.iterrows():
                    dcid = core.clean_text(fr.get("canonical_id", ""))
                    if not dcid:
                        continue
                    hd = core.clean_text(
                        fr.get("highest_priority_decision", "")
                    ).lower()
                    hs = core.clean_text(
                        fr.get("highest_priority_strategy", "")
                    )
                    if hd in _VALID_DECISIONS:
                        fi_v2_map[dcid] = (hd, hs)
    except Exception:
        fi_v2_map = {}

    def _highest_for(cid: str) -> tuple[str, str]:
        if cid in fi_v2_map:
            return fi_v2_map[cid]
        entries = cid_decisions.get(cid, [])
        if not entries:
            return "unscreened", ""
        best_d, best_s, best_p = "", "", 0
        for d, s in entries:
            p = _DECISION_PRIORITY.get(d, 0)
            if p > best_p:
                best_d, best_s, best_p = d, s, p
            elif p == best_p and p > 0:
                if not best_s or s < best_s:
                    best_d, best_s = d, s
        if best_p == 0:
            return "unscreened", ""
        return best_d, best_s

    rows: list[dict] = []
    for _, r in sub.iterrows():
        cid = core.clean_text(r.get("canonical_id", ""))
        if not cid:
            continue
        if cid in draft_map and draft_map[cid]:
            decision = draft_map[cid]
        else:
            decision = current_decisions.get(cid, "")

        file_locked_decision, file_locked_strategy = _highest_for(cid)

        title = core.clean_text(r.get("title", ""))
        if len(title) > _LOAD_SCREENING_TITLE_MAX_LEN:
            title = title[: _LOAD_SCREENING_TITLE_MAX_LEN - 1] + "…"

        authors = core.clean_text(r.get("authors", ""))
        first_author = ""
        if authors:
            first_author = authors.split(";")[0].strip()
            if ";" in authors:
                first_author = f"{first_author} 等"

        jif = r.get("journal_impact_factor")
        if jif is not None:
            try:
                jif_f = float(jif)
                if jif_f != jif_f:
                    jif = None
                else:
                    jif = jif_f
            except (ValueError, TypeError):
                jif = None

        pt_cat = core.clean_text(
            r.get("publication_type_category", "")
        )

        _doi = core.clean_text(r.get("doi", ""))
        _journal = core.clean_text(r.get("journal", ""))
        _year_raw = r.get("year")
        _year: int | None = None
        if pd.notna(_year_raw):
            try:
                _year = int(_year_raw)
            except (ValueError, TypeError):
                _year = None

        rows.append({
            "canonical_id": cid,
            "decision": decision,
            "title": title,
            "first_author": first_author,
            "journal_if": jif,
            "publication_type_category": pt_cat,
            "file_locked_by_strategy": file_locked_strategy,
            "file_locked_decision": file_locked_decision,
            "file_missing": bool(missing_map.get(cid, False)),
            "doi": _doi,
            "journal": _journal,
            "year": _year,
            "special_status": core.clean_text(
                r.get("special_status", "")
            ).strip().lower(),
        })

    return rows

def export_screening_report(
    strategy_name: str,
    library_root: Path,
    output_dir: Path | None = None,
    config: dict | None = None,
) -> dict:
    # 输出：prisma_report.md（筛选汇总）+ included/excluded/all_decisions CSV
    # 默认输出：library/reports/{strategy}_{YYYYMMDD}/（同策略旧目录先清理）
    # reviewer_id：config.project.reviewer_id，未配置显示"（未设置）"
    library_root = Path(library_root)
    if not strategy_name:
        return {
            "ok": False, "reason": "empty_strategy",
            "output_dir": "", "prisma_path": "",
            "included_csv": "", "excluded_csv": "",
            "all_decisions_csv": "",
        }

    if output_dir is None:
        date_str = datetime.now(timezone.utc).strftime(_REPORT_DATE_FMT)
        safe_strategy = _sanitize_report_dirname(strategy_name)
        reports_root = library_root / _REPORT_DIRNAME
        reports_root.mkdir(parents=True, exist_ok=True)
        target_name = f"{safe_strategy}_{date_str}"
        _cleanup_old_strategy_reports(
            reports_root, safe_strategy, keep_name=target_name,
        )
        base_dir = reports_root / target_name
    else:
        base_dir = Path(output_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    records_path = library_root / "records_final.parquet"
    if not records_path.exists():
        return {
            "ok": False, "reason": "records_final_not_found",
            "output_dir": str(base_dir), "prisma_path": "",
            "included_csv": "", "excluded_csv": "",
            "all_decisions_csv": "",
        }
    try:
        records = pd.read_parquet(records_path)
    except Exception:
        return {
            "ok": False, "reason": "records_read_failed",
            "output_dir": str(base_dir), "prisma_path": "",
            "included_csv": "", "excluded_csv": "",
            "all_decisions_csv": "",
        }
    if "strategy_names" not in records.columns:
        return {
            "ok": False, "reason": "missing_strategy_names",
            "output_dir": str(base_dir), "prisma_path": "",
            "included_csv": "", "excluded_csv": "",
            "all_decisions_csv": "",
        }

    pattern = record_normalize.strategy_regex(strategy_name)
    mask = (
        records["strategy_names"].fillna("").astype(str)
        .str.contains(pattern, regex=True, na=False)
    )
    sub = records[mask]
    cids = set(sub["canonical_id"].astype(str).tolist())

    decisions_path = library_root / "screening_decisions.parquet"
    decisions_sub = pd.DataFrame()
    if decisions_path.exists():
        try:
            decisions_all = pd.read_parquet(decisions_path)
        except Exception:
            decisions_all = pd.DataFrame()
        if len(decisions_all) > 0 and "canonical_id" in decisions_all.columns:
            has_strategy_col = "strategy_name" in decisions_all.columns
            if has_strategy_col:
                decisions_sub = decisions_all[
                    decisions_all["strategy_name"].astype(str)
                    == strategy_name
                ].copy()
            else:
                decisions_sub = decisions_all.copy()
            cid_series = decisions_sub["canonical_id"].astype(str)
            decisions_sub = decisions_sub[cid_series.isin(cids)].copy()

    stats = compute_progress_stats(strategy_name, library_root)

    archive_root = library_root.parent / "archive"
    runs = _list_strategy_runs(archive_root, strategy_name)
    queries = _resolve_queries(runs)
    reviewer_id = _resolve_reviewer_id(config)

    lines: list[str] = []
    lines.append(f"# {strategy_name} 筛选报告")
    lines.append("")
    lines.append(f"**筛选人**：{reviewer_id or '（未设置）'}")
    lines.append(f"**生成时间**：{_now_iso()}")
    lines.append("")
    lines.append("## 数据来源")
    lines.append("")

    source_counts = _resolve_source_counts(runs)
    _SOURCE_LABELS = {
        "pubmed": "PubMed", "pmc": "PMC",
        "europepmc": "Europe PMC", "crossref": "Crossref",
        "openalex": "OpenAlex",
    }
    if source_counts:
        lines.append("**原始命中数（去重前）**：")
        lines.append("")
        lines.append("| 数据源 | 原始命中数 |")
        lines.append("|---|---|")
        _total_raw = 0
        for _src_key in ("pubmed", "pmc", "europepmc",
                         "crossref", "openalex"):
            _n = int(source_counts.get(_src_key, 0))
            _total_raw += _n
            lines.append(
                f"| {_SOURCE_LABELS[_src_key]} | {_n} |"
            )
        lines.append(f"| **合计（去重前）** | **{_total_raw}** |")
        lines.append("")
    else:
        lines.append("（无分源原始命中数记录）")
        lines.append("")

    if runs:
        lines.append(
            f"本报告基于策略 `{strategy_name}` 的以下采集 run 累积结果："
        )
        lines.append("")
        lines.append("| Run | 采集日期 | 文献数 |")
        lines.append("|---|---|---|")
        for r in runs:
            lines.append(
                f"| {r['run_name']} | {r['date']} | {r['records_count']} |"
            )
        lines.append("")
        lines.append(
            "采集细节（检索式、去重率、全文获取）请参见各 run 的 "
            "`summary_report.md`。"
        )
    else:
        lines.append("（尚无采集 run 记录）")
    lines.append("")

    if any(queries.values()):
        lines.append("## 检索式")
        lines.append("")
        if queries.get("pubmed"):
            lines.append("**PubMed**：")
            lines.append("```")
            lines.append(queries["pubmed"])
            lines.append("```")
            lines.append("")
        if queries.get("europepmc"):
            lines.append("**Europe PMC**：")
            lines.append("```")
            lines.append(queries["europepmc"])
            lines.append("```")
            lines.append("")
        if queries.get("crossref"):
            lines.append("**Crossref**：")
            lines.append("```")
            lines.append(queries["crossref"])
            lines.append("```")
            lines.append("")
        if queries.get("openalex"):
            lines.append("**OpenAlex**：")
            lines.append("```")
            lines.append(queries["openalex"])
            lines.append("```")
            lines.append("")

    total = stats["total"]
    decided = total - stats["unscreened"]

    n_noise = 0
    n_deleted = 0
    if "special_status" in sub.columns and len(sub) > 0:
        _sp = (
            sub["special_status"].fillna("")
            .astype(str).str.strip().str.lower()
        )
        n_noise = int(_sp.eq("retrieval_noise").sum())
        n_deleted = int(_sp.eq("deleted_from_source").sum())
    special_total = n_noise + n_deleted
    raw_total = total + special_total

    lines.append("## 1. 筛选流程")
    lines.append("")
    lines.append("| 阶段 | 数量 | 说明 |")
    lines.append("|---|---|---|")
    lines.append(
        f"| 采集记录数（累积，去重后） | {raw_total} | — |"
    )
    if special_total > 0:
        lines.append(
            f"| ├ 🗑 特殊标记·检索噪声 | {n_noise} | 不进入筛选，元数据保留 |"
        )
        lines.append(
            f"| ├ ⚠ 特殊标记·数据源删除 | {n_deleted} | 不进入筛选，待处理 |"
        )
        lines.append(
            f"| └ 进入筛选的文献 | {total} | — |"
        )
    lines.append(f"| 已筛选 | {decided} | 含 include/exclude/maybe |")
    lines.append(f"| 未筛选 | {stats['unscreened']} | |")
    lines.append("")
    lines.append("## 2. 决策分布")
    lines.append("")
    lines.append("| 决策 | 数量 | 占比 |")
    lines.append("|---|---|---|")
    if total > 0:
        for label, key in (
            ("纳入（include）", "include"),
            ("排除（exclude）", "exclude"),
            ("待定（maybe）", "maybe"),
            ("未筛选（unscreened）", "unscreened"),
        ):
            n = int(stats.get(key, 0))
            pct = f"{n / total * 100:.1f}%"
            lines.append(f"| {label} | {n} | {pct} |")
    else:
        lines.append("| （无数据） | 0 | 0.0% |")
    lines.append("")
    lines.append("## 3. 排除原因分布")
    lines.append("")
    lines.append("| 原因 | 数量 |")
    lines.append("|---|---|")
    lines.append(f"| 未分类 | {stats.get('exclude', 0)} |")
    lines.append("")

    prisma_path = base_dir / "prisma_report.md"
    try:
        prisma_path.write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        return {
            "ok": False, "reason": "prisma_write_failed",
            "output_dir": str(base_dir), "prisma_path": "",
            "included_csv": "", "excluded_csv": "",
            "all_decisions_csv": "",
        }

    _records_export = sub[_RECORD_EXPORT_COLS].copy() \
        if all(c in sub.columns for c in _RECORD_EXPORT_COLS) \
        else pd.DataFrame()
    if len(_records_export) == 0:
        _records_export = pd.DataFrame(columns=_RECORD_EXPORT_COLS)
        for _c in _RECORD_EXPORT_COLS:
            if _c not in _records_export.columns:
                _records_export[_c] = ""

    if len(decisions_sub) > 0 and "canonical_id" in decisions_sub.columns:
        _records_dedup = _records_export.drop_duplicates(
            subset=["canonical_id"], keep="first",
        )
        _export_df = decisions_sub.merge(
            _records_dedup, on="canonical_id", how="left",
        )
    else:
        _export_df = pd.DataFrame(columns=_EXPORT_COLS)

    def _export_csv(
        df: pd.DataFrame, path: Path, filter_decision: str | None = None,
    ) -> bool:
        if filter_decision is not None:
            if "user_decision" not in df.columns:
                filtered = df.iloc[0:0]
            else:
                m = (
                    df["user_decision"].fillna("").astype(str)
                    .str.strip().str.lower() == filter_decision
                )
                filtered = df[m]
        else:
            filtered = df

        try:
            out = filtered.reindex(columns=_EXPORT_COLS, fill_value="")
            out.to_csv(path, index=False, encoding="utf-8-sig")
            return True
        except OSError:
            return False

    included_csv = base_dir / "included.csv"
    excluded_csv = base_dir / "excluded.csv"
    all_csv = base_dir / "all_decisions.csv"

    _export_csv(_export_df, included_csv, filter_decision="include")
    _export_csv(_export_df, excluded_csv, filter_decision="exclude")
    _export_csv(_export_df, all_csv, filter_decision=None)

    return {
        "ok": True,
        "reason": "",
        "output_dir": str(base_dir),
        "prisma_path": str(prisma_path),
        "included_csv": str(included_csv),
        "excluded_csv": str(excluded_csv),
        "all_decisions_csv": str(all_csv),
    }

def estimate_archive_space_release(
    strategy_name: str,
    library_root: Path,
    config: dict | None = None,
) -> dict:
    library_root = Path(library_root)
    result = {
        "freed_bytes": 0,
        "freed_mb": 0.0,
        "freed_gb": 0.0,
        "pdf_count": 0,
        "cid_count": 0,
        "ok": True,
        "reason": "",
    }

    if not strategy_name:
        result["ok"] = False
        result["reason"] = "empty_strategy"
        return result

    records_path = library_root / "records_final.parquet"
    decisions_path = library_root / "screening_decisions.parquet"

    if not records_path.exists():
        result["ok"] = False
        result["reason"] = "records_final_not_found"
        return result

    try:
        records = pd.read_parquet(records_path)
    except Exception:
        result["ok"] = False
        result["reason"] = "records_read_failed"
        return result

    if "strategy_names" not in records.columns:
        result["ok"] = False
        result["reason"] = "missing_strategy_names"
        return result

    pattern = record_normalize.strategy_regex(strategy_name)
    mask = (
        records["strategy_names"].fillna("").astype(str)
        .str.contains(pattern, regex=True, na=False)
    )
    sub = records[mask]
    if len(sub) == 0:
        result["reason"] = "no_records_for_strategy"
        return result

    decisions_all = pd.DataFrame()
    cid_has_decision_set: set[str] = set()
    if decisions_path.exists():
        try:
            decisions_all = pd.read_parquet(decisions_path)
            if (
                len(decisions_all) > 0
                and "canonical_id" in decisions_all.columns
            ):
                cid_has_decision_set = set(
                    decisions_all["canonical_id"].astype(str).tolist()
                )
        except Exception:
            decisions_all = pd.DataFrame()

    pdf_to_trash_decisions = {"exclude"}
    all_to_trash_decisions = {"unscreened"}
    source_subdirs = ("included", "maybe", "inbox")
    pdf_filenames = (
        "article.pdf", "article.html", "article_secondary.pdf",
    )
    all_filenames = pdf_filenames + ("article.xml", "article.md")

    for _, r in sub.iterrows():
        cid = core.clean_text(r.get("canonical_id", ""))
        if not cid:
            continue

        if cid not in cid_has_decision_set:
            continue

        try:
            highest_decision, _locked_strategy = _compute_highest_priority_decision(
                cid, decisions_all,
            )
        except Exception:
            continue

        if highest_decision in pdf_to_trash_decisions:
            source_filenames = pdf_filenames
        elif highest_decision in all_to_trash_decisions:
            source_filenames = all_filenames
        else:
            continue

        safe = core.canonical_id_to_dirname(cid)
        cid_contributed = False
        for src_subdir in source_subdirs:
            src_dir = library_root / src_subdir / safe
            if not src_dir.exists():
                continue
            for fname in source_filenames:
                fp = src_dir / fname
                if not fp.is_file():
                    continue
                try:
                    size = int(fp.stat().st_size)
                except OSError:
                    continue
                result["freed_bytes"] += size
                result["pdf_count"] += 1
                cid_contributed = True

        if cid_contributed:
            result["cid_count"] += 1

    result["freed_mb"] = round(result["freed_bytes"] / (1024 ** 2), 2)
    result["freed_gb"] = round(result["freed_bytes"] / (1024 ** 3), 2)
    return result

def migrate_decisions_schema(
    library_root: Path,
    config: dict | None = None,
) -> dict:
    result = {
        "ok": False,
        "status": "error",
        "reason": "",
        "backup_path": "",
        "original_rows": 0,
        "migrated_rows": 0,
        "orphan_rows": 0,
        "expand_factor": 0.0,
        "warning": "",
    }

    library_root = Path(library_root)
    decisions_path = library_root / "screening_decisions.parquet"

    if not decisions_path.exists():
        result["ok"] = True
        result["status"] = "no_file"
        return result

    try:
        decisions = pd.read_parquet(decisions_path)
    except Exception as exc:
        result["reason"] = f"decisions_read_failed: {exc}"
        return result

    if len(decisions) == 0:
        result["ok"] = True
        result["status"] = "no_file"
        return result

    if "canonical_id" not in decisions.columns:
        result["reason"] = "missing_canonical_id"
        return result

    has_strategy_col = _DECISIONS_STRATEGY_COL in decisions.columns
    if has_strategy_col:
        non_empty = (
            decisions[_DECISIONS_STRATEGY_COL]
            .fillna("").astype(str).str.strip().ne("")
        )
        if non_empty.any():
            result["ok"] = True
            result["status"] = "already_migrated"
            result["original_rows"] = len(decisions)
            result["migrated_rows"] = len(decisions)
            return result

    backup = _migrate_backup(decisions_path)
    if not backup:
        result["reason"] = "backup_failed"
        return result
    result["backup_path"] = str(backup)
    result["original_rows"] = int(len(decisions))

    cid_strategies: dict[str, list[str]] = {}
    records_path = library_root / "records_final.parquet"
    if records_path.exists():
        try:
            records = pd.read_parquet(
                records_path, columns=["canonical_id", "strategy_names"],
            )
            for _, rr in records.iterrows():
                rcid = core.clean_text(rr.get("canonical_id", ""))
                if not rcid:
                    continue
                r_strats = core.clean_text(rr.get("strategy_names", ""))
                for s in str(r_strats).split(";"):
                    s_clean = core.clean_text(s)
                    if s_clean:
                        cid_strategies.setdefault(rcid, []).append(s_clean)
        except Exception:
            pass

    expanded_rows: list[dict] = []
    orphan_count = 0

    for _, old_row in decisions.iterrows():
        old_dict = old_row.to_dict()
        cid = core.clean_text(old_dict.get("canonical_id", ""))
        if not cid:
            continue

        strategies = _get_strategy_names_for_cid(cid, cid_strategies)
        if strategies == [""]:
            orphan_count += 1

        for strat in strategies:
            new_dict = dict(old_dict)
            new_dict[_DECISIONS_STRATEGY_COL] = strat
            expanded_rows.append(new_dict)

    if not expanded_rows:
        result["reason"] = "no_valid_rows_to_migrate"
        return result

    expanded_df = pd.DataFrame(expanded_rows)

    if "canonical_id" in expanded_df.columns:
        expanded_df = expanded_df.drop_duplicates(
            subset=["canonical_id", _DECISIONS_STRATEGY_COL],
            keep="last",
        ).reset_index(drop=True)

    try:
        _save_table(expanded_df, decisions_path)
    except OSError as exc:
        result["reason"] = f"decisions_write_failed: {exc}"
        return result

    result["ok"] = True
    result["status"] = "migrated"
    result["migrated_rows"] = int(len(expanded_df))
    result["orphan_rows"] = orphan_count
    result["expand_factor"] = round(
        len(expanded_df) / max(len(decisions), 1), 2,
    )
    return result

def migrate_file_index_schema(
    library_root: Path,
) -> dict:
    result = {
        "ok": False,
        "status": "error",
        "reason": "",
        "backup_path": "",
        "columns_added": [],
        "rows_total": 0,
    }

    library_root = Path(library_root)
    file_index_path = library_root / "file_index.parquet"

    if not file_index_path.exists():
        result["ok"] = True
        result["status"] = "no_file"
        return result

    try:
        fi = pd.read_parquet(file_index_path)
    except Exception as exc:
        result["reason"] = f"file_index_read_failed: {exc}"
        return result

    if len(fi) == 0:
        pass

    result["rows_total"] = int(len(fi))

    existing_cols = set(fi.columns)
    missing_cols = [
        c for c in _FILE_INDEX_V2_NEW_COLUMNS if c not in existing_cols
    ]

    if not missing_cols:
        result["ok"] = True
        result["status"] = "already_migrated"
        return result

    backup = _migrate_backup(file_index_path)
    if not backup and len(fi) > 0:
        result["reason"] = "backup_failed"
        return result
    result["backup_path"] = str(backup) if backup else ""

    for col in missing_cols:
        fi[col] = _FILE_INDEX_V2_DEFAULTS.get(col, "")
        result["columns_added"].append(col)

    try:
        _save_table(fi, file_index_path)
    except OSError as exc:
        result["reason"] = f"file_index_write_failed: {exc}"
        return result

    result["ok"] = True
    result["status"] = "migrated"
    return result

def _extract_strategy_list(names_str: Any) -> list[str]:
    if not names_str:
        return []
    text = str(names_str).strip()
    if not text:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for part in text.split(";"):
        s = part.strip()
        if s and s not in seen:
            seen.add(s)
            result.append(s)
    return result

def get_alive_strategies(
    archive_root: Path,
) -> set[str] | None:
    try:
        registry_path = Path(archive_root) / "strategy_registry.parquet"
        if not registry_path.exists():
            return None
        df = pd.read_parquet(registry_path)
        if len(df) == 0:
            return None
        return set(df["strategy_name"].astype(str).tolist())
    except Exception:
        return None


def compute_orphan_strategies(
    records: pd.DataFrame,
    alive_strategies: set[str] | None,
    running_strategies: set[str] | None = None,
) -> list[dict]:
    if alive_strategies is None:
        return []
    if records is None or len(records) == 0:
        return []
    if "strategy_names" not in records.columns:
        return []

    running_set = running_strategies or set()

    try:
        s = records["strategy_names"].fillna("").astype(str)
        names = s.str.split(";").explode().str.strip()
        all_strategies = {n for n in names if n}
    except Exception:
        return []
    if not all_strategies:
        return []

    orphan_names = all_strategies - alive_strategies - running_set
    if not orphan_names:
        return []

    result: list[dict] = []
    col = records["strategy_names"].fillna("").astype(str)
    for name in sorted(orphan_names):
        try:
            pattern = record_normalize.strategy_regex(name)
            mask = col.str.contains(pattern, regex=True, na=False)
            count = int(mask.sum())
            if count > 0:
                result.append({
                    "name": str(name),
                    "record_count": count,
                })
        except Exception:
            continue
    return result

def _resolve_current_file_subdir(
    safe: str, library_root: Path,
) -> str:
    for subdir in ("inbox", "maybe", "included", "excluded", "unscreened"):
        d = library_root / subdir / safe
        if d.exists() and any(d.iterdir()):
            return subdir
    return ""

def prescan_delete_strategy(
    strategy_name: str,
    library_root: Path,
) -> dict:
    library_root = Path(library_root)
    result: dict = {
        "ok": False,
        "errors": [],
        "exclusive_count": 0,
        "shared_count": 0,
        "exclusive_cids": [],
        "shared_cids": [],
        "files_to_delete": 0,
        "files_to_move": 0,
        "size_bytes": 0,
        "decisions_to_delete": 0,
        "warnings": [],
    }

    if not strategy_name:
        result["errors"].append("empty_strategy")
        return result

    records_path = library_root / "records_final.parquet"
    if not records_path.exists():
        result["errors"].append("records_final_not_found")
        return result

    try:
        records = pd.read_parquet(records_path)
    except Exception as exc:
        result["errors"].append(f"records_read_failed: {exc}")
        return result

    if "strategy_names" not in records.columns:
        result["errors"].append("missing_strategy_names_column")
        return result

    pattern = record_normalize.strategy_regex(strategy_name)
    hit_mask = (
        records["strategy_names"].fillna("").astype(str)
        .str.contains(pattern, regex=True, na=False)
    )
    hit = records[hit_mask]

    exclusive_cids: list[str] = []
    shared_cids: list[str] = []
    for _, r in hit.iterrows():
        cid = core.clean_text(r.get("canonical_id", ""))
        if not cid:
            continue
        names = _extract_strategy_list(r.get("strategy_names", ""))
        remaining = [s for s in names if s != strategy_name]
        if remaining:
            shared_cids.append(cid)
        else:
            exclusive_cids.append(cid)

    result["exclusive_count"] = len(exclusive_cids)
    result["shared_count"] = len(shared_cids)
    result["exclusive_cids"] = exclusive_cids[:10]
    result["shared_cids"] = shared_cids[:10]

    files_to_delete = 0
    size_bytes = 0
    for cid in exclusive_cids:
        safe = core.canonical_id_to_dirname(cid)
        for subdir in ("included", "maybe", "excluded",
                       "unscreened", "inbox", "trash"):
            d = library_root / subdir / safe
            if not d.exists():
                continue
            for fp in d.rglob("*"):
                if fp.is_file():
                    files_to_delete += 1
                    try:
                        size_bytes += fp.stat().st_size
                    except OSError:
                        pass
    result["files_to_delete"] = files_to_delete
    result["size_bytes"] = size_bytes

    decisions_path = library_root / "screening_decisions.parquet"
    decisions_kept = pd.DataFrame()
    if decisions_path.exists():
        try:
            decisions_all = _read_decisions_full_df(decisions_path)
            has_strategy_col = _DECISIONS_STRATEGY_COL in decisions_all.columns
            if has_strategy_col:
                decisions_kept = decisions_all[
                    decisions_all[_DECISIONS_STRATEGY_COL].astype(str)
                    != strategy_name
                ].copy()
            else:
                decisions_kept = decisions_all.copy()
            if has_strategy_col:
                result["decisions_to_delete"] = int(
                    (
                        decisions_all[_DECISIONS_STRATEGY_COL].astype(str)
                        == strategy_name
                    ).sum()
                )
        except Exception as exc:
            result["warnings"].append(f"decisions_read_failed: {exc}")

    files_to_move = 0
    for cid in shared_cids:
        try:
            highest_decision, _locked_by = _compute_highest_priority_decision(
                cid, decisions_kept,
            )
        except Exception:
            continue
        target_subdir, _ = _decision_target(highest_decision)
        current_subdir = _resolve_current_file_subdir(
            core.canonical_id_to_dirname(cid), library_root,
        )
        if current_subdir and current_subdir != target_subdir:
            files_to_move += 1
    result["files_to_move"] = files_to_move

    if result["exclusive_count"] > 0:
        result["warnings"].append(
            f"⚠ {result['exclusive_count']} 篇独占文献将被彻底删除"
            f"（archive 快照保留）"
        )
    if result["files_to_move"] > 0:
        result["warnings"].append(
            f"ℹ {result['files_to_move']} 篇共享文献的文件位置将被重算"
            f"（按剩余策略的最高优先级）"
        )

    result["ok"] = True
    return result

def delete_strategy_cascade(
    strategy_name: str,
    library_root: Path,
    config: dict | None = None,
    *,
    dry_run: bool = False,
    delete_archive: bool = False,
    prescan_result: dict | None = None,
) -> dict:
    library_root = Path(library_root)
    result: dict = {
        "ok": False,
        "errors": [],
        "exclusive_deleted": 0,
        "shared_updated": 0,
        "files_moved": 0,
        "freed_bytes": 0,
        "archive_result": None,
        "decisions_deleted": 0,
        "prescan": None,
        "partial": False,
        "txn_retained_at": "",
        "failed_steps": [],
        "suggested_action": "",
    }

    if not strategy_name:
        result["errors"].append("empty_strategy")
        return result

    lock_err = _check_pipeline_lock(library_root, config)
    if lock_err:
        result["errors"].append(lock_err)
        return result

    if prescan_result is not None:
        pre = prescan_result
    else:
        pre = prescan_delete_strategy(strategy_name, library_root)
    if dry_run:
        result["prescan"] = pre
        result["ok"] = bool(pre.get("ok"))
        if not pre.get("ok"):
            result["errors"] = list(pre.get("errors", []))
        return result

    if not pre.get("ok"):
        result["errors"] = list(pre.get("errors", []))
        return result

    records_path = library_root / "records_final.parquet"
    decisions_path = library_root / "screening_decisions.parquet"
    file_index_path = library_root / "file_index.parquet"

    op_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    txn_dir = library_root / ".txn" / f"delete_strategy_{op_id}"
    txn_files: list[str] = []
    try:
        txn_dir.mkdir(parents=True, exist_ok=True)
        for src_path in (records_path, decisions_path, file_index_path):
            if src_path.exists():
                shutil.copy2(src_path, txn_dir / src_path.name)
                txn_files.append(src_path.name)
    except OSError as exc:
        result["errors"].append(f"txn_backup_failed: {exc}")
        return result

    def _rollback_txn() -> None:
        for fname in txn_files:
            src = txn_dir / fname
            dst = library_root / fname
            if not src.exists():
                continue
            try:
                tmp = dst.with_name(dst.name + ".restore_tmp")
                shutil.copy2(src, tmp)
                os.replace(tmp, dst)
            except OSError:
                pass

    try:
        records = pd.read_parquet(records_path)
    except Exception as exc:
        result["errors"].append(f"records_read_failed: {exc}")
        return result

    try:
        decisions_all = _read_decisions_full_df(decisions_path)
    except RuntimeError as exc:
        result["errors"].append(f"decisions_read_failed: {exc}")
        return result

    file_index = _read_file_index_v2(file_index_path)

    pattern = record_normalize.strategy_regex(strategy_name)
    hit_mask = (
        records["strategy_names"].fillna("").astype(str)
        .str.contains(pattern, regex=True, na=False)
    )

    exclusive_idx: list[int] = []
    shared_idx: list[int] = []
    for idx in records.index[hit_mask]:
        names = _extract_strategy_list(
            records.at[idx, "strategy_names"]
        )
        remaining = [s for s in names if s != strategy_name]
        if remaining:
            shared_idx.append(int(idx))
        else:
            exclusive_idx.append(int(idx))

    exclusive_cids: set[str] = set(
        core.clean_text(records.at[idx, "canonical_id"])
        for idx in exclusive_idx
    )
    shared_cids: set[str] = set(
        core.clean_text(records.at[idx, "canonical_id"])
        for idx in shared_idx
    )
    exclusive_cids.discard("")
    shared_cids.discard("")

    for idx in shared_idx:
        names = _extract_strategy_list(
            records.at[idx, "strategy_names"]
        )
        remaining = [s for s in names if s != strategy_name]
        records.at[idx, "strategy_names"] = "; ".join(remaining)
        records.at[idx, "matched_strategy_count"] = len(remaining)

    if exclusive_idx:
        records = records.drop(index=exclusive_idx).reset_index(drop=True)

    try:
        _save_table(records, records_path)
    except OSError as exc:
        result["errors"].append(f"records_write_failed: {exc}")
        _rollback_txn()
        result["txn_retained_at"] = str(txn_dir)
        result["suggested_action"] = (
            "数据已恢复，备份保留；确认数据正确后可手动删除 .txn 目录"
        )
        return result

    has_strategy_col = _DECISIONS_STRATEGY_COL in decisions_all.columns
    if has_strategy_col:
        mask_del = (
            decisions_all[_DECISIONS_STRATEGY_COL].astype(str)
            == strategy_name
        )
        result["decisions_deleted"] = int(mask_del.sum())
        decisions_kept = decisions_all[~mask_del].reset_index(drop=True)
    else:
        decisions_kept = decisions_all.copy()

    try:
        _save_table(decisions_kept, decisions_path)
    except OSError as exc:
        result["errors"].append(f"decisions_write_failed: {exc}")
        _rollback_restore_records_and_abort(
            library_root, txn_dir, txn_files, result,
        )
        result["txn_retained_at"] = str(txn_dir)
        result["suggested_action"] = (
            "records 已恢复，但 decisions 未删；请从 .txn 备份恢复后重试"
        )
        return result

    now = _now_iso()
    moved_count = 0
    retention_mode = _DEFAULT_RETENTION_MODE
    if config:
        cfg = _screening_cfg(config)
        _rm = core.clean_text(
            cfg.get("retention_mode", _DEFAULT_RETENTION_MODE)
        ) or _DEFAULT_RETENTION_MODE
        if _rm in ("release_space", "complete_retention"):
            retention_mode = _rm

    for cid in shared_cids:
        try:
            highest, locked_by = _compute_highest_priority_decision(
                cid, decisions_kept,
            )
        except Exception:
            continue

        target_subdir, target_level = _decision_target(highest)
        safe = core.canonical_id_to_dirname(cid)
        current_subdir = _resolve_current_file_subdir(safe, library_root)

        trash_dir_for_cid = ""
        if current_subdir and current_subdir != target_subdir:
            src_dir = library_root / current_subdir / safe
            dst_dir = library_root / target_subdir / safe
            shared_moved_files, shared_trash_files = _resolve_target_files(
                target_subdir, retention_mode,
            )
            try:
                if shared_moved_files:
                    _move_named_files(src_dir, dst_dir, shared_moved_files)
                if shared_trash_files:
                    trash_dir = library_root / "trash" / safe
                    trash_dir.mkdir(parents=True, exist_ok=True)
                    for fname in shared_trash_files:
                        fp = src_dir / fname
                        if not fp.is_file():
                            continue
                        dst_trash = trash_dir / fname
                        if dst_trash.exists():
                            continue
                        try:
                            shutil.move(str(fp), str(dst_trash))
                            trash_dir_for_cid = str(trash_dir)
                        except OSError:
                            continue
                if target_subdir == "unscreened":
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    (dst_dir / "metadata.json").write_text(
                        json.dumps(
                            {"canonical_id": cid, "retention_level": "L5"},
                            ensure_ascii=False, indent=2,
                        ),
                        encoding="utf-8",
                    )
                if src_dir.exists() and not any(src_dir.iterdir()):
                    src_dir.rmdir()
                moved_count += 1
            except OSError:
                continue

        fmask = file_index["canonical_id"].astype(str) == cid
        if fmask.any():
            ridx = int(file_index.index[fmask][0])
            current_path = ""
            target_dir = library_root / target_subdir / safe
            for fname in _OPEN_LITERATURE_FILENAMES:
                p = target_dir / fname
                if p.is_file():
                    current_path = str(p)
                    break
            file_index.at[ridx, "current_file_path"] = current_path
            file_index.at[ridx, "file_retention_level"] = target_level
            file_index.at[ridx, "highest_priority_strategy"] = locked_by
            file_index.at[ridx, "highest_priority_decision"] = highest
            file_index.at[ridx, "file_location_updated_at"] = now

            if trash_dir_for_cid:
                retention_days = _get_trash_retention_days(config)
                try:
                    now_dt = datetime.fromisoformat(now)
                except ValueError:
                    now_dt = datetime.now(timezone.utc)
                expire = (
                    now_dt + timedelta(days=retention_days)
                ).isoformat()
                file_index.at[ridx, "trash_path"] = trash_dir_for_cid
                file_index.at[ridx, "deleted_at"] = now
                file_index.at[ridx, "expire_at"] = expire

    if exclusive_cids:
        fmask = file_index["canonical_id"].astype(str).isin(exclusive_cids)
        file_index = file_index[~fmask].reset_index(drop=True)

    try:
        _save_table(file_index, file_index_path)
    except OSError as exc:
        result["errors"].append(f"file_index_write_failed: {exc}")
        _rollback_restore_records_and_abort(
            library_root, txn_dir, txn_files, result,
        )
        result["ok"] = False
        result["partial"] = bool(txn_files) and txn_dir.exists()
        result["txn_retained_at"] = (
            str(txn_dir) if result["partial"] else ""
        )
        result["suggested_action"] = (
            "records/decisions/file_index 已从 .txn 回滚；"
            "请检查磁盘空间与权限后重试"
        )
        return result

    freed_bytes = 0
    for cid in exclusive_cids:
        safe = core.canonical_id_to_dirname(cid)
        for subdir in ("included", "maybe", "excluded",
                       "unscreened", "inbox", "trash"):
            d = library_root / subdir / safe
            if not d.exists():
                continue
            try:
                for fp in d.rglob("*"):
                    if fp.is_file():
                        try:
                            freed_bytes += fp.stat().st_size
                        except OSError:
                            pass
                shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue

    if delete_archive:
        try:
            from medlitflow import archive as _archive
            if config is not None:
                _archive._set_config(config)
            try:
                archive_result = _archive.delete_strategy(strategy_name)
            finally:
                if config is not None:
                    _archive._set_config(None)
            result["archive_result"] = archive_result
        except Exception as exc:
            result["errors"].append(f"archive_delete_failed: {exc}")
    else:
        result["archive_result"] = {
            "status": "preserved",
            "strategy_name": strategy_name,
            "message": "archive 快照保留",
        }

    try:
        from medlitflow import annotator as _annotator
        _annotator.update_library_index(library_root)
    except Exception as exc:
        result["errors"].append(f"index_refresh_failed: {exc}")

    all_remaining: set[str] = set()
    for names_str in records["strategy_names"].astype(str):
        for s in _extract_strategy_list(names_str):
            all_remaining.add(s)
    for s in sorted(all_remaining):
        try:
            update_strategy_status(s, library_root)
        except Exception:
            pass

    try:
        audit_rows = []
        for cid in exclusive_cids:
            audit_rows.append({
                "canonical_id": cid,
                "action": "delete_strategy_cascade",
                "from_path": "",
                "to_path": f"exclusive:{strategy_name}",
                "timestamp": now,
                "operator": "ui",
            })
        for cid in shared_cids:
            audit_rows.append({
                "canonical_id": cid,
                "action": "delete_strategy_cascade",
                "from_path": f"shared:{strategy_name}",
                "to_path": "",
                "timestamp": now,
                "operator": "ui",
            })
        _append_screening_audit(library_root, audit_rows)
    except Exception:
        pass

    if not result["errors"]:
        try:
            shutil.rmtree(txn_dir, ignore_errors=True)
        except OSError:
            pass

    result["ok"] = len(result["errors"]) == 0
    result["exclusive_deleted"] = len(exclusive_cids)
    result["shared_updated"] = len(shared_cids)
    result["files_moved"] = moved_count
    result["freed_bytes"] = freed_bytes
    result["partial"] = (
        not result["ok"]
        and txn_dir.exists()
        and len(txn_files) > 0
    )
    result["txn_retained_at"] = (
        str(txn_dir) if result["partial"] else ""
    )
    result["failed_steps"] = [
        e.split(":", 1)[0].strip()
        for e in result["errors"] if ":" in e
    ]
    if not result["suggested_action"]:
        result["suggested_action"] = (
            "事务备份已保留；请检查 .txn 目录或从备份恢复后重试"
            if result["partial"] else ""
        )
    return result

def _rollback_restore_records_and_abort(
    library_root: Path,
    txn_dir: Path,
    txn_files: list[str],
    result: dict,
) -> None:
    for fname in txn_files:
        src = txn_dir / fname
        dst = library_root / fname
        if not src.exists():
            continue
        try:
            tmp = dst.with_name(dst.name + ".restore_tmp")
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
        except OSError:
            pass

    result["errors"].append(
        f"txn_retained_at: {txn_dir}"
    )

def mark_as_retrieval_noise(
    cids: list[str],
    library_root: Path,
    config: dict | None = None,
) -> dict:
    library_root = Path(library_root)
    cid_set = {str(c) for c in (cids or []) if c}
    if not cid_set:
        return {"ok": True, "count": 0, "freed_bytes": 0, "errors": []}

    lock_err = _check_pipeline_lock(library_root, config)
    if lock_err:
        return {"ok": False, "count": 0, "freed_bytes": 0,
                "errors": [lock_err]}

    records_path = library_root / "records_final.parquet"
    if not records_path.exists():
        return {"ok": False, "count": 0, "freed_bytes": 0,
                "errors": ["records_final_not_found"]}
    try:
        records = pd.read_parquet(records_path)
    except Exception as exc:
        return {"ok": False, "count": 0, "freed_bytes": 0,
                "errors": [f"records_read_failed: {exc}"]}

    if "special_status" not in records.columns:
        records["special_status"] = annotator.SPECIAL_STATUS_NORMAL
    for field in annotator.NOISE_CLEARED_FIELDS:
        if field not in records.columns:
            records[field] = annotator.noise_cleared_value(field)

    mask = records["canonical_id"].astype(str).isin(cid_set)
    if not mask.any():
        return {"ok": True, "count": 0, "freed_bytes": 0, "errors": []}

    sp_series = (
        records["special_status"].fillna("").astype(str).str.strip()
    )
    normal_mask = mask & sp_series.eq("")
    actually_marked = int(normal_mask.sum())

    if actually_marked > 0:
        records.loc[normal_mask, "special_status"] = (
            annotator.SPECIAL_STATUS_NOISE
        )
        for field in annotator.NOISE_CLEARED_FIELDS:
            records.loc[normal_mask, field] = annotator.noise_cleared_value(field)

    try:
        _save_table(records, records_path)
    except OSError as exc:
        return {"ok": False, "count": 0, "freed_bytes": 0,
                "errors": [f"records_write_failed: {exc}"]}

    freed_bytes = 0
    errors: list[str] = []
    for cid in cid_set:
        safe = core.canonical_id_to_dirname(cid)
        for subdir in ("included", "maybe", "excluded",
                       "unscreened", "inbox"):
            d = library_root / subdir / safe
            if not d.exists():
                continue
            try:
                for fp in d.rglob("*"):
                    if fp.is_file():
                        freed_bytes += fp.stat().st_size
                shutil.rmtree(d, ignore_errors=True)
            except OSError as exc:
                errors.append(f"{cid}:{subdir}: {exc}")

    try:
        fi = _read_file_index_v2(library_root / "file_index.parquet")
        fmask = fi["canonical_id"].astype(str).isin(cid_set)
        if fmask.any():
            fi.loc[fmask, "current_file_path"] = ""
            fi.loc[fmask, "downloaded_file_path"] = ""
            if "file_missing" in fi.columns:
                fi.loc[fmask, "file_missing"] = True
            _save_table(fi, library_root / "file_index.parquet")
    except Exception as exc:
        errors.append(f"file_index: {exc}")

    now = _now_iso()
    _append_screening_audit(
        library_root,
        [
            {"canonical_id": cid, "action": "mark_noise",
             "from_path": "", "to_path": "",
             "timestamp": now, "operator": "ui"}
            for cid in cid_set
        ],
    )

    if actually_marked > 0:
        marked_cids = set(
            records.loc[normal_mask, "canonical_id"].astype(str).tolist()
        )
        _refresh_after_special_change(library_root, marked_cids, errors)

    return {
        "ok": True,
        "count": actually_marked,
        "freed_bytes": freed_bytes,
        "errors": errors,
    }

def mark_as_deleted_from_source(
    cids: list[str],
    library_root: Path,
    config: dict | None = None,
) -> dict:
    library_root = Path(library_root)
    cid_set = {str(c) for c in (cids or []) if c}
    if not cid_set:
        return {"ok": True, "count": 0, "errors": []}

    lock_err = _check_pipeline_lock(library_root, config)
    if lock_err:
        return {"ok": False, "count": 0, "errors": [lock_err]}

    records_path = library_root / "records_final.parquet"
    if not records_path.exists():
        return {"ok": False, "count": 0,
                "errors": ["records_final_not_found"]}
    try:
        records = pd.read_parquet(records_path)
    except Exception as exc:
        return {"ok": False, "count": 0,
                "errors": [f"records_read_failed: {exc}"]}

    if "special_status" not in records.columns:
        records["special_status"] = annotator.SPECIAL_STATUS_NORMAL

    mask = records["canonical_id"].astype(str).isin(cid_set)
    if not mask.any():
        return {"ok": True, "count": 0, "errors": []}

    sp_series = (
        records["special_status"].fillna("").astype(str).str.strip()
    )
    normal_mask = mask & sp_series.eq("")
    actually_marked = int(normal_mask.sum())

    if actually_marked > 0:
        records.loc[normal_mask, "special_status"] = (
            annotator.SPECIAL_STATUS_DELETED
        )

    try:
        _save_table(records, records_path)
    except OSError as exc:
        return {"ok": False, "count": 0,
                "errors": [f"records_write_failed: {exc}"]}

    now = _now_iso()
    _append_screening_audit(
        library_root,
        [
            {"canonical_id": cid, "action": "mark_deleted",
             "from_path": "", "to_path": "",
             "timestamp": now, "operator": "ui"}
            for cid in cid_set
        ],
    )

    errors: list[str] = []
    if actually_marked > 0:
        marked_cids = set(
            records.loc[normal_mask, "canonical_id"].astype(str).tolist()
        )
        _refresh_after_special_change(library_root, marked_cids, errors)

    return {"ok": True, "count": actually_marked, "errors": errors}

def restore_special_status(
    cids: list[str],
    library_root: Path,
    config: dict | None = None,
) -> dict:
    library_root = Path(library_root)
    cid_set = {str(c) for c in (cids or []) if c}
    if not cid_set:
        return {"ok": True, "count": 0, "errors": []}

    lock_err = _check_pipeline_lock(library_root, config)
    if lock_err:
        return {"ok": False, "count": 0, "errors": [lock_err]}

    records_path = library_root / "records_final.parquet"
    if not records_path.exists():
        return {"ok": False, "count": 0,
                "errors": ["records_final_not_found"]}
    try:
        records = pd.read_parquet(records_path)
    except Exception as exc:
        return {"ok": False, "count": 0,
                "errors": [f"records_read_failed: {exc}"]}

    if "special_status" not in records.columns:
        return {"ok": True, "count": 0, "errors": []}

    mask = records["canonical_id"].astype(str).isin(cid_set)
    if not mask.any():
        return {"ok": True, "count": 0, "errors": []}

    sp_series = (
        records["special_status"].fillna("").astype(str).str.strip()
    )
    marked_mask = mask & sp_series.ne("")
    actually_restored = int(marked_mask.sum())

    if actually_restored > 0:
        records.loc[marked_mask, "special_status"] = (
            annotator.SPECIAL_STATUS_NORMAL
        )

    try:
        _save_table(records, records_path)
    except OSError as exc:
        return {"ok": False, "count": 0,
                "errors": [f"records_write_failed: {exc}"]}

    now = _now_iso()
    _append_screening_audit(
        library_root,
        [
            {"canonical_id": cid, "action": "restore_special",
             "from_path": "", "to_path": "",
             "timestamp": now, "operator": "ui"}
            for cid in cid_set
        ],
    )

    errors: list[str] = []
    if actually_restored > 0:
        restored_cids = set(
            records.loc[marked_mask, "canonical_id"].astype(str).tolist()
        )
        _refresh_after_special_change(library_root, restored_cids, errors)

    return {"ok": True, "count": actually_restored, "errors": errors}

def hard_delete_literature(
    cids: list[str],
    library_root: Path,
    config: dict | None = None,
) -> dict:
    library_root = Path(library_root)
    cid_set = {str(c) for c in (cids or []) if c}
    if not cid_set:
        return {"ok": True, "count": 0, "freed_bytes": 0, "errors": []}

    lock_err = _check_pipeline_lock(library_root, config)
    if lock_err:
        return {"ok": False, "count": 0, "freed_bytes": 0,
                "errors": [lock_err]}

    records_path = library_root / "records_final.parquet"
    if not records_path.exists():
        return {"ok": False, "count": 0, "freed_bytes": 0,
                "errors": ["records_final_not_found"]}
    try:
        records = pd.read_parquet(records_path)
    except Exception as exc:
        return {"ok": False, "count": 0, "freed_bytes": 0,
                "errors": [f"records_read_failed: {exc}"]}

    if "special_status" not in records.columns:
        return {"ok": False, "count": 0, "freed_bytes": 0,
                "errors": ["special_status_column_missing"]}

    sp_series = (
        records["special_status"].fillna("").astype(str).str.strip()
    )
    cid_series = records["canonical_id"].astype(str)
    for cid in cid_set:
        cmask = cid_series == cid
        if not cmask.any():
            continue
        cur_status = sp_series[cmask].iloc[0]
        if cur_status != annotator.SPECIAL_STATUS_DELETED:
            return {
                "ok": False, "count": 0, "freed_bytes": 0,
                "errors": [
                    f"{cid}:not_deleted_from_source:{cur_status or 'empty'}"
                ],
            }

    errors: list[str] = []
    deleted_count = 0

    try:
        mask = cid_series.isin(cid_set)
        deleted_count = int(mask.sum())
        records_kept = records[~mask].reset_index(drop=True)
        _save_table(records_kept, records_path)
    except Exception as exc:
        errors.append(f"records: {exc}")

    fp = library_root / "file_index.parquet"
    if fp.exists():
        try:
            fi = pd.read_parquet(fp)
            fmask = fi["canonical_id"].astype(str).isin(cid_set)
            fi_kept = fi[~fmask].reset_index(drop=True)
            _save_table(fi_kept, fp)
        except Exception as exc:
            errors.append(f"file_index: {exc}")

    dp = library_root / "screening_decisions.parquet"
    if dp.exists():
        try:
            decisions = _read_decisions_full_df(dp)
            dmask = decisions["canonical_id"].astype(str).isin(cid_set)
            decisions_kept = decisions[~dmask].reset_index(drop=True)
            _save_table(decisions_kept, dp)
        except Exception as exc:
            errors.append(f"decisions: {exc}")

    freed_bytes = 0
    for cid in cid_set:
        safe = core.canonical_id_to_dirname(cid)
        for subdir in ("included", "maybe", "excluded",
                       "unscreened", "inbox", "trash"):
            d = library_root / subdir / safe
            if not d.exists():
                continue
            try:
                for f in d.rglob("*"):
                    if f.is_file():
                        freed_bytes += f.stat().st_size
                shutil.rmtree(d, ignore_errors=True)
            except OSError as exc:
                errors.append(f"{cid}:{subdir}: {exc}")

    try:
        annotator.update_library_index(library_root)
    except Exception as exc:
        errors.append(f"index: {exc}")

    now = _now_iso()
    _append_screening_audit(
        library_root,
        [
            {"canonical_id": cid, "action": "hard_delete",
             "from_path": "", "to_path": "",
             "timestamp": now, "operator": "ui"}
            for cid in cid_set
        ],
    )

    return {
        "ok": not errors,
        "count": deleted_count,
        "freed_bytes": freed_bytes,
        "errors": errors,
    }