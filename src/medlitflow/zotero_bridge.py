"""zotero_bridge.py — Zotero 本地 API 封装（推送方向）。"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from medlitflow import core
from medlitflow.record_normalize import split_last_first

__all__ = [
    "sync_to_zotero",
    "link_file",
    "create_item",
    "verify_zotero_links",
    "prune_zotero_links",
    "get_item_by_key",
    "list_items_in_collection",
    "register_log_callback",
    "ZOTERO_LOCAL_API",
    "ZOTERO_LINKS_COLUMNS",
]

_log_callback = None

def register_log_callback(callback) -> None:
    global _log_callback
    _log_callback = callback


def _emit_log(level: str, message: str) -> None:
    if _log_callback is not None:
        try:
            _log_callback(str(level), str(message))
            return
        except Exception:
            pass
    print(f"[zotero_bridge] {message}", file=sys.stderr)

_ZOTERO_LOCAL_BASE = "http://localhost:23119"
ZOTERO_LOCAL_API = f"{_ZOTERO_LOCAL_BASE}/api"
_ZOTERO_LOCAL_CONNECTOR = f"{_ZOTERO_LOCAL_BASE}/connector"
_DEFAULT_TIMEOUT = 30
_DEFAULT_ITEM_TYPE = "journalArticle"
_DEFAULT_LINK_MODE = "imported_file"
_LINK_MODE_WARNED = False
ZOTERO_LINKS_COLUMNS = [
    "canonical_id", "zotero_item_key", "zotero_synced_at",
    "zotero_link_mode", "zotero_collection", "zotero_attachment_key",
]

def _now_iso() -> str:
    return core.now_iso()

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

def _parse_authors(authors_str: str) -> list[dict]:
    """将 "Last First; Last First" 转为 Zotero creators 列表。"""
    result = []
    for name in (authors_str or "").split(";"):
        name = name.strip()
        if not name:
            continue
        last, first = split_last_first(name)
        if not last:
            result.append({"creatorType": "author", "name": name})
            continue
        creator: dict[str, Any] = {"creatorType": "author", "lastName": last}
        if first:
            creator["firstName"] = first
        result.append(creator)
    return result

_SYNC_FLUSH_INTERVAL = 10

def _load_pdf_index(library_root: Path) -> dict[str, Path]:
    fi_path = library_root / "file_index.parquet"
    if not fi_path.exists():
        return {}
    try:
        fi = pd.read_parquet(fi_path)
    except Exception:
        return {}
    if "canonical_id" not in fi.columns:
        return {}
    result: dict[str, Path] = {}
    for row in fi.to_dict("records"):
        cid = core.clean_text(row.get("canonical_id", ""))
        if not cid:
            continue
        for col in ("current_file_path", "downloaded_file_path"):
            path_str = core.clean_text(row.get(col, ""))
            if path_str:
                p = Path(path_str)
                if p.is_file():
                    result[cid] = p
                    break
    return result

def _fetch_actual_item_key(
    connector_key: str,
    title: str = "",
    doi: str = "",
    library_type: str = "user",
    library_id: str = "0",
    max_wait_seconds: float = 6.0,
    poll_interval: float = 0.5,
) -> str:
    clean_title = core.clean_text(title).strip()
    clean_doi = core.normalize_doi(doi)

    import time as _time
    deadline = _time.monotonic() + max_wait_seconds

    url = f"{ZOTERO_LOCAL_API}/{library_type}s/{library_id}/items"
    params = {
        "limit": 25,
        "sort": "dateAdded",
        "direction": "desc",
        "itemType": "journalArticle",
    }

    while True:
        try:
            with httpx.Client(timeout=5) as client:
                r = client.get(url, params=params)
            if r.status_code == 200:
                items = r.json() or []

                if clean_doi:
                    for item in items:
                        item_doi = core.normalize_doi(
                            str(item.get("data", {}).get("DOI", ""))
                        )
                        if item_doi and item_doi == clean_doi:
                            real_key = core.clean_text(item.get("key", ""))
                            if real_key:
                                return real_key

                if clean_title:
                    for item in items:
                        item_title = core.clean_text(
                            str(item.get("data", {}).get("title", ""))
                        ).strip()
                        if item_title and item_title == clean_title:
                            real_key = core.clean_text(item.get("key", ""))
                            if real_key:
                                return real_key

                for item in items:
                    if str(item.get("id", "")) == connector_key:
                        real_key = core.clean_text(item.get("key", ""))
                        if real_key:
                            return real_key
        except Exception:
            pass

        if _time.monotonic() >= deadline:
            break
        core.interruptible_sleep(poll_interval)

    return connector_key

def _update_session_target(session_id: str, target_id: str) -> bool:
    # Connector API 的 saveItems 忽略 collections 字段，总是落默认 collection。
    if not session_id or not target_id:
        return False
    try:
        payload = json.dumps({
            "sessionID": session_id,
            "target": target_id,
        }).encode("utf-8")
        url = f"{_ZOTERO_LOCAL_CONNECTOR}/updateSession"
        with httpx.Client(timeout=10) as client:
            r = client.post(
                url,
                content=payload,
                headers={"Content-Type": "application/json"},
            )
        return r.status_code in (200, 201)
    except Exception:
        return False

def create_item(item_data: dict) -> tuple[str, str, str]:
    library_type = core.clean_text(item_data.get("library_type", "user")) or "user"
    library_id = core.clean_text(str(item_data.get("library_id", "0"))) or "0"

    creators = item_data.get("creators")
    if not creators:
        creators = _parse_authors(core.clean_text(item_data.get("authors", "")))

    import uuid as _uuid
    connector_key = item_data.get("connector_key") or _uuid.uuid4().hex[:8]
    item: dict[str, Any] = {
        "itemType": core.clean_text(
            item_data.get("itemType", _DEFAULT_ITEM_TYPE)
        ) or _DEFAULT_ITEM_TYPE,
        "title": core.clean_text(item_data.get("title", "")),
        "creators": creators,
        "id": connector_key,
    }
    doi = core.clean_text(item_data.get("DOI", ""))
    if doi:
        item["DOI"] = doi
    journal = core.clean_text(item_data.get("publicationTitle", ""))
    if journal:
        item["publicationTitle"] = journal
    date = core.clean_text(item_data.get("date", ""))
    if date:
        item["date"] = date
    tags = item_data.get("tags")
    if tags:
        item["tags"] = tags
    collections = item_data.get("collections")
    if collections:
        item["collections"] = collections

    session_id = item_data.get("session_id") or _uuid.uuid4().hex[:16]
    url = f"{_ZOTERO_LOCAL_CONNECTOR}/saveItems"
    headers = {
        "Content-Type": "application/json",
        "X-Zotero-Connector-API-Version": "3",
    }
    payload = {
        "sessionID": session_id,
        "uri": f"zotero://medlitflow/session/{session_id}",
        "items": [item],
    }
    with httpx.Client(timeout=_DEFAULT_TIMEOUT) as client:
        response = client.post(url, json=payload, headers=headers)
    response.raise_for_status()

    actual_key = _fetch_actual_item_key(
        connector_key,
        title=core.clean_text(item.get("title", "")),
        doi=core.clean_text(item.get("DOI", "")),
        library_type=library_type,
        library_id=library_id,
    )
    return actual_key, session_id, connector_key

def _key_to_connector_target(collection_key: str) -> str:
    # 查 Zotero 本地 SQLite：SELECT collectionID FROM collections WHERE key = ?
    if not collection_key:
        return ""
    import os
    import sqlite3
    import pathlib

    candidates = [
        pathlib.Path.home() / "Zotero" / "zotero.sqlite",
        pathlib.Path(os.environ.get("APPDATA", "")) / "Zotero" / "zotero.sqlite",
        pathlib.Path.home() / "Documents" / "Zotero" / "zotero.sqlite",
    ]
    db_path = next((p for p in candidates if p.is_file()), None)
    if db_path is None:
        return ""
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT collectionID FROM collections WHERE key = ?",
                (collection_key,),
            ).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            return f"C{row[0]}"
    except Exception:
        pass
    return ""

def link_file(
    item_key: str,
    file_path: str,
    link_mode: str = "imported_file",
    *,
    session_id: str = "",
    title: str = "",
    content_type: str = "",
) -> str | None:
    if not item_key or not file_path or not session_id:
        return None

    path_obj = Path(file_path)
    if not path_obj.is_file():
        return None

    if link_mode == "linked_file":
        global _LINK_MODE_WARNED
        if not _LINK_MODE_WARNED:
            _emit_log(
                "warning",
                "Connector API 不支持 linked_file，附件将以 imported_file 方式上传。"
                "如需保持文件引用，请在 Zotero 中手动设置 base_directory。",
            )
            _LINK_MODE_WARNED = True

    suffix = path_obj.suffix.lower()
    if not content_type:
        content_type = {
            ".pdf": "application/pdf",
            ".xml": "application/xml",
            ".html": "text/html",
            ".htm": "text/html",
        }.get(suffix, "application/octet-stream")
    if not title:
        title = {
            ".pdf": "Full Text PDF",
            ".xml": "Full Text XML",
            ".html": "Full Text HTML",
            ".htm": "Full Text HTML",
        }.get(suffix, "Attachment")

    url = f"{_ZOTERO_LOCAL_CONNECTOR}/saveAttachment"
    headers = {
        "Content-Type": content_type,
        "X-Zotero-Connector-API-Version": "3",
        "X-Metadata": json.dumps({
            "sessionID": session_id,
            "parentItemID": item_key,
            "url": f"file://{path_obj.resolve().as_posix()}",
            "title": title,
        }, ensure_ascii=True),
    }

    try:
        pdf_bytes = path_obj.read_bytes()
        with httpx.Client(timeout=_DEFAULT_TIMEOUT) as client:
            response = client.post(url, content=pdf_bytes, headers=headers)
        response.raise_for_status()
        return "uploaded"
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"saveAttachment HTTP {exc.response.status_code}: {exc.response.text[:200]}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"saveAttachment failed: {exc}") from exc

def sync_to_zotero(
    included_df: pd.DataFrame,
    config: dict | None = None,
    progress_callback=None,
) -> None:
    if included_df is None or len(included_df) == 0:
        return

    if config is None:
        config = core.load_config()

    zotero_cfg = config.get("zotero", {}) or {}
    library_root = Path(config["library"]["root"])
    link_mode = core.clean_text(zotero_cfg.get("link_mode", _DEFAULT_LINK_MODE)) or _DEFAULT_LINK_MODE
    default_collection = core.clean_text(zotero_cfg.get("default_collection", ""))
    if not default_collection:
        _import_names = zotero_cfg.get("import_collection_names") or []
        if isinstance(_import_names, list) and _import_names:
            default_collection = core.clean_text(str(_import_names[0]))
    item_type = core.clean_text(zotero_cfg.get("item_type", _DEFAULT_ITEM_TYPE)) or _DEFAULT_ITEM_TYPE
    library_type = core.clean_text(zotero_cfg.get("library_type", "user")) or "user"
    library_id = core.clean_text(str(zotero_cfg.get("library_id", "0"))) or "0"

    zotero_links_path = library_root / "zotero_links.parquet"

    pdf_index = _load_pdf_index(library_root)

    applied_collection_name: str = ""
    applied_collection_key: str = ""
    if default_collection:
        from medlitflow import zotero_import
        _cols: list = []
        for _attempt in range(3):
            if _attempt > 0:
                core.interruptible_sleep(0.5 if _attempt == 1 else 1.0)
            try:
                _cols = zotero_import.list_collections(library_root, config)
            except Exception as _exc:
                _emit_log(
                    "error",
                    f"预加载 Zotero collection 列表失败（{_exc}），"
                    f"同步已中止。请确认 Zotero 桌面端已运行后重试。",
                )
                raise RuntimeError(
                    f"Zotero 分类列表加载失败：{_exc}"
                ) from _exc
            if _cols:
                break
        if not _cols:
            _emit_log(
                "error",
                "Zotero Connector 未就绪（分类列表为空），同步已中止。"
                "请等待 Zotero 启动完成后重试。",
            )
            raise RuntimeError(
                "Zotero Connector 未就绪（分类列表为空），同步已中止"
            )
        _name_to_key: dict[str, str] = {}
        for _c in _cols:
            _n = core.clean_text(_c.get("name", ""))
            _k = core.clean_text(_c.get("key", ""))
            if _n and _k:
                _name_to_key[_n] = _k
        _k = _name_to_key.get(default_collection, "")
        if not _k:
            _emit_log(
                "error",
                f"配置的分类 '{default_collection}' 在 Zotero 中不存在，"
                f"同步已中止。请在 Zotero 中创建该分类后重试，"
                f"或在 Zotero 页点「⚙ 配置」重新选择。",
            )
            raise RuntimeError(
                f"配置的分类 '{default_collection}' 在 Zotero 中不存在"
            )
        applied_collection_key = _k
        applied_collection_name = default_collection

    _target_id = (
        _key_to_connector_target(applied_collection_key)
        if applied_collection_key else ""
    )

    all_rows_df = _load_table(zotero_links_path, ZOTERO_LINKS_COLUMNS)
    existing_cids = set(all_rows_df["canonical_id"].astype(str).tolist())
    pending_new_rows: list[dict] = []

    def _flush() -> None:
        """把 pending_new_rows 合并到 all_rows_df 并原子落盘。

        内存一次 concat（O(K)，K=累积行数）+ 一次原子写，
        整体摊还 O(N²/FLUSH_INTERVAL) 但常数极小（500 行写盘 < 50ms）。
        """
        nonlocal all_rows_df, pending_new_rows
        if not pending_new_rows:
            return
        all_rows_df = pd.concat(
            [all_rows_df, pd.DataFrame(pending_new_rows)],
            ignore_index=True,
        ).drop_duplicates(subset=["canonical_id"], keep="last")
        _save_table(all_rows_df, zotero_links_path)
        pending_new_rows = []

    now = _now_iso()
    total = len(included_df)

    from medlitflow import robustness

    try:
        for i, row in enumerate(included_df.to_dict("records"), start=1):
            robustness.check_shutdown()

            if progress_callback is not None:
                try:
                    progress_callback(i, total)
                except Exception:
                    pass

            cid = core.clean_text(row.get("canonical_id", ""))
            if not cid or cid in existing_cids:
                continue

            item_data = {
                "library_type": library_type,
                "library_id": library_id,
                "itemType": item_type,
                "title": core.clean_text(row.get("title", "")),
                "authors": core.clean_text(row.get("authors", "")),
                "DOI": core.clean_text(row.get("doi", "")),
                "publicationTitle": core.clean_text(row.get("journal", "")),
                "date": core.clean_text(row.get("year", "")),
            }
            if applied_collection_key:
                item_data["collections"] = [applied_collection_key]

            try:
                item_key, session_id, connector_key = create_item(item_data)
            except Exception as exc:
                _append_zotero_audit(library_root, {
                    "canonical_id": cid,
                    "action": "create_item_failed",
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "timestamp": now,
                })
                continue

            pdf_path = pdf_index.get(cid)
            attachment_key = ""
            if pdf_path is not None and session_id:
                try:
                    attachment_key = link_file(
                        connector_key, str(pdf_path), link_mode,
                        session_id=session_id,
                    ) or ""
                except Exception as exc:
                    _append_zotero_audit(library_root, {
                        "canonical_id": cid,
                        "action": "link_file_failed",
                        "error": type(exc).__name__,
                        "message": str(exc),
                        "timestamp": now,
                    })

            if _target_id and session_id:
                _moved = _update_session_target(session_id, _target_id)
                if not _moved:
                    _append_zotero_audit(library_root, {
                        "canonical_id": cid,
                        "action": "update_session_failed",
                        "error": "move_failed",
                        "message": (
                            f"session={session_id}, target={_target_id}"
                        ),
                        "timestamp": now,
                    })

            pending_new_rows.append({
                "canonical_id": cid,
                "zotero_item_key": item_key,
                "zotero_synced_at": now,
                "zotero_link_mode": link_mode,
                "zotero_collection": applied_collection_name,
                "zotero_attachment_key": attachment_key,
            })
            existing_cids.add(cid)

            if (i % _SYNC_FLUSH_INTERVAL == 0) and pending_new_rows:
                _flush()
    finally:
        _flush()

    merged = all_rows_df
    stale_days = int(zotero_cfg.get("stale_warning_days", 90))
    _warn_stale_links(merged, stale_days)

def _warn_stale_links(links: pd.DataFrame, stale_days: int) -> None:
    if links is None or len(links) == 0 or stale_days <= 0:
        return

    now_dt = datetime.now(timezone.utc)
    cutoff = now_dt.timestamp() - stale_days * 86400
    stale_count = 0

    for _, row in links.iterrows():
        synced_at = core.clean_text(row.get("zotero_synced_at", ""))
        if not synced_at:
            continue
        try:
            dt = datetime.fromisoformat(synced_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt.timestamp() < cutoff:
                stale_count += 1
        except (ValueError, TypeError):
            continue

    if stale_count > 0:
        _emit_log(
            "warning",
            f"{stale_count} 条记录已超过 {stale_days} 天未同步。"
            f"运行 `--stage zotero-sync --zotero-verify` 查看详情。",
        )

def verify_zotero_links(
    library_root: Path,
    config: dict | None = None,
    stale_warning_days: int | None = None,
    progress_callback=None,
) -> dict:
    links = _load_table(
        library_root / "zotero_links.parquet", ZOTERO_LINKS_COLUMNS,
    )
    result = {
        "total": len(links),
        "alive": 0,                    # 按行计数（不是 unique key 数）
        "confirmed_missing": [],       # 404/410 → 允许 prune
        "unverified": [],              # 超时/连接失败/5xx/429 → prune 拒绝
        "invalid": [],                 # item_key 空/非法 → 永不删
        "stale": [],
        "confirmed_missing_report": None,
        "unverified_report": None,
        "stale_report": None,
    }
    if len(links) == 0:
        return result

    library_type = "user"
    library_id = "0"
    if config:
        zc = config.get("zotero", {}) or {}
        library_type = (
            core.clean_text(zc.get("library_type", "user")) or "user"
        )
        library_id = (
            core.clean_text(str(zc.get("library_id", "0"))) or "0"
        )
        if stale_warning_days is None:
            stale_warning_days = int(zc.get("stale_warning_days", 90))
    if stale_warning_days is None:
        stale_warning_days = 90

    now_dt = datetime.now(timezone.utc)
    stale_cutoff = now_dt.timestamp() - stale_warning_days * 86400

    from concurrent.futures import ThreadPoolExecutor, as_completed

    total = len(links)
    link_records = links.to_dict("records")
    item_keys_in_order = [
        core.clean_text(r.get("zotero_item_key", ""))
        for r in link_records
    ]

    alive_keys: set[str] = set()
    valid_keys = [k for k in item_keys_in_order if k]
    base_url = f"{ZOTERO_LOCAL_API}/{library_type}s/{library_id}/items"

    def _check_one(key: str) -> tuple[str, str]:
        try:
            with httpx.Client(timeout=5) as c:
                r = c.get(f"{base_url}/{key}")
            if r.status_code == 200:
                return key, "alive"
            if r.status_code in (404, 410):
                return key, "confirmed_missing"
            return key, "unverified"
        except Exception:
            return key, "unverified"

    key_to_status: dict[str, str] = {}
    unique_valid_keys = list(dict.fromkeys(
        k for k in item_keys_in_order if k
    ))

    if unique_valid_keys:
        done_count = 0
        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = {ex.submit(_check_one, k): k for k in unique_valid_keys}
            for fut in as_completed(futures):
                key, status = fut.result()
                key_to_status[key] = status
                done_count += 1
                if progress_callback is not None and done_count % 10 == 0:
                    try:
                        progress_callback(done_count, len(unique_valid_keys))
                    except Exception:
                        pass
        if progress_callback is not None:
            try:
                progress_callback(len(unique_valid_keys), len(unique_valid_keys))
            except Exception:
                pass

    alive_count = 0
    for r in link_records:
        item_key = core.clean_text(r.get("zotero_item_key", ""))
        cid = core.clean_text(r.get("canonical_id", ""))
        synced_at = core.clean_text(r.get("zotero_synced_at", ""))

        if not item_key:
            result["invalid"].append({
                "canonical_id": cid,
                "zotero_item_key": "",
                "zotero_synced_at": synced_at,
                "reason": "empty_key",
            })
            continue

        status = key_to_status.get(item_key, "unverified")

        if status == "alive":
            alive_count += 1
        elif status == "confirmed_missing":
            result["confirmed_missing"].append({
                "canonical_id": cid,
                "zotero_item_key": item_key,
                "zotero_synced_at": synced_at,
                "http_status": 404,
            })
        else:
            result["unverified"].append({
                "canonical_id": cid,
                "zotero_item_key": item_key,
                "zotero_synced_at": synced_at,
                "reason": "network_or_timeout",
            })

        if synced_at and stale_cutoff is not None:
            try:
                dt = datetime.fromisoformat(synced_at.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt.timestamp() < stale_cutoff:
                    days_ago = int((now_dt - dt).days)
                    result["stale"].append({
                        "canonical_id": cid,
                        "zotero_item_key": item_key,
                        "zotero_synced_at": synced_at,
                        "days_since_sync": days_ago,
                    })
            except (ValueError, TypeError):
                pass

    result["alive"] = alive_count

    n_invalid = len(result["invalid"])
    checkable = total - n_invalid
    n_unverified = len(result["unverified"])

    if total > 0 and checkable > 0 and alive_count == 0:
        if n_unverified == checkable:
            raise RuntimeError(
                f"Zotero 校验全部失败（{n_unverified}/{checkable} 条无法验证）。"
                f"请确认：① Zotero 桌面端已运行；"
                f"② 设置 → 高级 → 允许此计算机上的其他应用程序与 Zotero 通信 已勾选。"
                f"已中止（不写报告，不修改 zotero_links.parquet）。"
            )

    if result["confirmed_missing"]:
        path = library_root / "zotero_links_confirmed_missing.csv"
        pd.DataFrame(result["confirmed_missing"]).to_csv(
            path, index=False, encoding="utf-8-sig",
        )
        result["confirmed_missing_report"] = path

    if result["unverified"]:
        path = library_root / "zotero_links_unverified.csv"
        pd.DataFrame(result["unverified"]).to_csv(
            path, index=False, encoding="utf-8-sig",
        )
        result["unverified_report"] = path

    if result["stale"]:
        path = library_root / "zotero_links_stale.csv"
        pd.DataFrame(result["stale"]).to_csv(
            path, index=False, encoding="utf-8-sig",
        )
        result["stale_report"] = path

    return result

def prune_zotero_links(
    library_root: Path,
    config: dict | None = None,
    force: bool = False,
) -> dict:
    verify = verify_zotero_links(library_root, config)

    confirmed_missing = verify["confirmed_missing"]
    unverified = verify["unverified"]
    invalid = verify["invalid"]

    if unverified and not force:
        raise RuntimeError(
            f"{len(unverified)} 条记录暂无法验证（网络/超时/服务异常），"
            f"为防误删已拒绝清理。请确认 Zotero 稳定后重试，"
            f"或用 --yes 强制继续（只删确认失效的记录，未验证的将保留）。"
        )

    if not confirmed_missing:
        return {
            "before": verify["total"],
            "after": verify["total"],
            "removed": 0,
            "confirmed_missing_count": 0,
            "unverified_count": len(unverified),
            "invalid_count": len(invalid),
        }

    links = _load_table(
        library_root / "zotero_links.parquet", ZOTERO_LINKS_COLUMNS,
    )
    before = len(links)
    missing_cids = {m["canonical_id"] for m in confirmed_missing}
    links = links[
        ~links["canonical_id"].astype(str).isin(missing_cids)
    ].copy()
    after = len(links)
    _save_table(links, library_root / "zotero_links.parquet")

    return {
        "before": before,
        "after": after,
        "removed": before - after,
        "confirmed_missing_count": len(confirmed_missing),
        "unverified_count": len(unverified),
        "invalid_count": len(invalid),
    }

def get_item_by_key(
    item_key: str,
    library_type: str = "user",
    library_id: str = "0",
) -> dict | None:
    if not item_key:
        return None
    try:
        url = f"{ZOTERO_LOCAL_API}/{library_type}s/{library_id}/items/{item_key}"
        with httpx.Client(timeout=5) as client:
            r = client.get(url)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception:
        return None

def list_items_in_collection(
    collection_key: str,
    library_type: str = "user",
    library_id: str = "0",
    limit: int = 100,
) -> list[dict]:
    if not collection_key:
        return []
    items: list[dict] = []
    start = 0
    page_size = max(1, min(int(limit), 100))
    try:
        with httpx.Client(timeout=10) as client:
            while True:
                url = (
                    f"{ZOTERO_LOCAL_API}/{library_type}s/{library_id}"
                    f"/collections/{collection_key}/items"
                )
                r = client.get(url, params={
                    "limit": page_size,
                    "start": start,
                    "sort": "dateAdded",
                    "direction": "asc",
                })
                if r.status_code != 200:
                    break
                page = r.json() or []
                if not page:
                    break
                items.extend(page)
                if len(page) < page_size:
                    break
                start += len(page)
                if start > 10000:
                    break
    except Exception:
        pass
    return items

def _resolve_pdf_path(row, library_root: Path) -> Path | None:
    cid = core.clean_text(row.get("canonical_id", ""))
    if not cid:
        return None

    file_index_path = library_root / "file_index.parquet"
    if not file_index_path.exists():
        return None
    try:
        fi = pd.read_parquet(file_index_path)
    except Exception:
        return None
    if "canonical_id" not in fi.columns:
        return None

    match = fi[fi["canonical_id"].astype(str) == cid]
    if len(match) == 0:
        return None

    for col in ("current_file_path", "downloaded_file_path"):
        if col not in match.columns:
            continue
        path_str = core.clean_text(match.iloc[0].get(col, ""))
        if not path_str:
            continue
        p = Path(path_str)
        if p.is_file():
            return p
    return None

def _append_zotero_audit(library_root: Path, entry: dict) -> None:
    path = library_root / "zotero_audit.csv"
    row_df = pd.DataFrame([entry])
    if path.exists():
        try:
            existing = pd.read_csv(path, dtype=str).fillna("")
            row_df = pd.concat([existing, row_df], ignore_index=True)
        except Exception:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        row_df.to_csv(tmp, index=False, encoding="utf-8-sig")
        os.replace(tmp, path)
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise