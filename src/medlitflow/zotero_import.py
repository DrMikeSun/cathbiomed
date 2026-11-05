"""medlitflow zotero_import.py — Zotero → 平台（拉取方向）。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import httpx
import pandas as pd

from medlitflow import core, annotator, robustness
from medlitflow.zotero_bridge import (
    ZOTERO_LOCAL_API,
    ZOTERO_LINKS_COLUMNS,
    _emit_log,
    _load_table,
    _save_table,
    get_item_by_key,
    list_items_in_collection,
)

__all__ = [
    "list_collections",
    "scan_collections_for_import",
    "import_zotero_items",
    "withdraw_imported_items",
    "clear_zotero_links",
    "cleanup_orphan_zotero_imports",
]

_TIMEOUT = 10

def list_collections(
    library_root: Path,
    config: dict | None = None,
) -> list[dict]:
    library_type = "user"
    library_id = "0"
    if config:
        zc = config.get("zotero", {}) or {}
        library_type = core.clean_text(zc.get("library_type", "user")) or "user"
        library_id = core.clean_text(str(zc.get("library_id", "0"))) or "0"

    url = f"{ZOTERO_LOCAL_API}/{library_type}s/{library_id}/collections"
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            r = client.get(url)
        if r.status_code != 200:
            _emit_log("warning", f"list_collections 失败：HTTP {r.status_code}")
            return []
        payload = r.json() or []
    except Exception as exc:
        _emit_log("warning", f"list_collections 异常：{exc}")
        return []

    result: list[dict] = []
    for item in payload:
        try:
            data = item.get("data", {}) or {}
            key = core.clean_text(item.get("key", ""))
            name = core.clean_text(data.get("name", ""))
            count = int((item.get("meta", {}) or {}).get("numItems", 0) or 0)
            if key and name:
                result.append({"key": key, "name": name, "item_count": count})
        except Exception:
            continue
    return result

def scan_collections_for_import(
    collection_names: list[str],
    library_root: Path,
    config: dict | None = None,
) -> pd.DataFrame:
    columns = [
        "canonical_id", "title", "authors", "journal", "year",
        "doi", "pmid", "pmcid", "abstract_final", "zotero_item_key",
        "import_collection", "import_tags_json",
    ]

    if not collection_names:
        raise ValueError("collection_names 不能为空")

    library_root = Path(library_root)

    records_path = library_root / "records_final.parquet"
    existing_cids: set[str] = set()
    if records_path.exists():
        try:
            rec_df = pd.read_parquet(records_path, columns=["canonical_id"])
            existing_cids = set(rec_df["canonical_id"].astype(str).tolist())
        except Exception:
            pass

    links = _load_table(
        library_root / "zotero_links.parquet", ZOTERO_LINKS_COLUMNS
    )
    existing_keys: set[str] = (
        set(links["zotero_item_key"].astype(str).tolist())
        if len(links) > 0 else set()
    )

    all_cols = list_collections(library_root, config)
    name_to_key = {c["name"]: c["key"] for c in all_cols}
    selected: list[tuple[str, str]] = []
    missing_names: list[str] = []
    for name in collection_names:
        if name in name_to_key:
            selected.append((name, name_to_key[name]))
        else:
            missing_names.append(name)
    if missing_names:
        _emit_log(
            "warning",
            f"以下配置分类在 Zotero 中不存在，已跳过：{missing_names}。"
            f"请在 Zotero 页点 [⚙ 配置] 重新选择。",
        )
    if not selected:
        _emit_log("warning", f"未匹配到任何 collection：{collection_names}")
        return pd.DataFrame(columns=columns)

    rows: list[dict] = []
    cid_to_idx: dict[str, int] = {}
    seen_keys: set[str] = set()

    for collection_name, collection_key in selected:
        robustness.check_shutdown()
        items = list_items_in_collection(collection_key)
        for idx, item in enumerate(items, start=1):
            if idx % 20 == 0:
                robustness.check_shutdown()
            try:
                data = item.get("data", {}) or {}
                item_type = core.clean_text(
                    data.get("itemType", "")
                ).lower()
                if item_type in ("attachment", "note"):
                    continue
                if core.clean_text(data.get("parentItem", "")):
                    continue

                zk = core.clean_text(item.get("key", ""))
                if not zk or zk in existing_keys or zk in seen_keys:
                    continue
                cid = _canonical_id_from_zotero_item(item)
                if not cid or cid in existing_cids:
                    continue

                if cid in cid_to_idx:
                    existing_idx = cid_to_idx[cid]
                    existing = rows[existing_idx]
                    cur_col = core.clean_text(
                        existing.get("import_collection", "")
                    )
                    cur_set = set(
                        s.strip() for s in cur_col.split(";") if s.strip()
                    )
                    if collection_name not in cur_set:
                        cur_set.add(collection_name)
                        existing["import_collection"] = "; ".join(
                            sorted(cur_set)
                        )
                    seen_keys.add(zk)
                    continue

                row = _zotero_item_to_record(item, collection_name)
                if row is None:
                    continue
                row["canonical_id"] = cid
                row["zotero_item_key"] = zk
                rows.append(row)
                cid_to_idx[cid] = len(rows) - 1
                seen_keys.add(zk)
            except Exception:
                continue

    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows)[columns]

def _canonical_id_from_zotero_item(item: dict) -> str:
    """从 Zotero 条目生成 canonical_id（DOI > PMID > PMCID > title hash）。"""
    data = item.get("data", {}) or {}
    doi = core.normalize_doi(data.get("DOI", ""))
    if doi:
        return f"doi:{doi}"
    extra = core.clean_text(data.get("extra", ""))
    m = re.search(r"PMID[:\s]+(\d+)", extra, re.IGNORECASE)
    if m:
        pmid = core.normalize_pmid(m.group(1))
        if pmid:
            return f"pmid:{pmid}"
    m = re.search(r"PMCID[:\s]+(PMC\d+)", extra, re.IGNORECASE)
    if m:
        pmcid = core.normalize_pmcid(m.group(1))
        if pmcid:
            return f"pmcid:{pmcid}"
    title = core.clean_text(data.get("title", ""))
    if title:
        norm = core.normalize_title(title)
        if norm:
            h = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
            return f"title:{h}"
    return ""

def _zotero_item_to_record(item: dict, collection_name: str) -> dict | None:
    data = item.get("data", {}) or {}
    
    creators = data.get("creators", []) or []
    parts: list[str] = []
    for c in creators:
        if not isinstance(c, dict):
            continue
        last = core.clean_text(c.get("lastName", ""))
        first = core.clean_text(c.get("firstName", ""))
        name = core.clean_text(c.get("name", ""))
        if last and first:
            parts.append(f"{last} {first}")
        elif last:
            parts.append(last)
        elif name:
            parts.append(name)
    authors = "; ".join(parts)

    date_str = core.clean_text(data.get("date", ""))
    year: int | None = None
    if date_str:
        m = re.search(r"\b(\d{4})\b", date_str)
        if m:
            try:
                year = int(m.group(1))
            except ValueError:
                pass

    tags = data.get("tags", []) or []
    tag_names = [
        core.clean_text(t.get("tag", ""))
        for t in tags if isinstance(t, dict)
    ]
    tag_names = [t for t in tag_names if t]
    tags_json = json.dumps(tag_names, ensure_ascii=False)

    extra = core.clean_text(data.get("extra", ""))
    pmid = ""
    pmcid = ""
    m = re.search(r"PMID[:\s]+(\d+)", extra, re.IGNORECASE)
    if m:
        pmid = core.normalize_pmid(m.group(1))
    m = re.search(r"PMCID[:\s]+(PMC\d+)", extra, re.IGNORECASE)
    if m:
        pmcid = core.normalize_pmcid(m.group(1))

    return {
        "title": core.clean_text(data.get("title", "")),
        "authors": authors,
        "journal": core.clean_text(data.get("publicationTitle", "")),
        "year": year,
        "doi": core.normalize_doi(data.get("DOI", "")),
        "pmid": pmid,
        "pmcid": pmcid,
        "abstract_final": core.clean_text(data.get("abstractNote", "")),
        "import_tags_json": tags_json,
        "import_collection": collection_name,
    }

def import_zotero_items(
    candidates_df: pd.DataFrame,
    library_root: Path,
    config: dict | None = None,
) -> dict:
    library_root = Path(library_root)
    result = {"imported": 0, "skipped": 0, "errors": []}

    if candidates_df is None or len(candidates_df) == 0:
        return result

    records_path = library_root / "records_final.parquet"
    records_df = (
        pd.read_parquet(records_path)
        if records_path.exists()
        else pd.DataFrame(columns=annotator.RECORDS_FINAL_COLUMNS)
    )
    links_df = _load_table(
        library_root / "zotero_links.parquet", ZOTERO_LINKS_COLUMNS
    )

    existing_cids = set(records_df["canonical_id"].astype(str).tolist())
    existing_keys = (
        set(links_df["zotero_item_key"].astype(str).tolist())
        if len(links_df) > 0 else set()
    )

    link_mode = "imported_file"
    if config:
        zc = config.get("zotero", {}) or {}
        link_mode = core.clean_text(
            zc.get("link_mode", "imported_file")
        ) or "imported_file"

    now = core.now_iso()
    new_records: list[dict] = []
    new_links: list[dict] = []

    for idx, row in enumerate(
        candidates_df.to_dict("records"), start=1
    ):
        if idx % 10 == 0:
            robustness.check_shutdown()
        cid = str(row.get("canonical_id", "") or "").strip()
        item_key = str(row.get("zotero_item_key", "") or "").strip()

        if not cid or not item_key:
            result["skipped"] += 1
            continue
        if cid in existing_cids or item_key in existing_keys:
            result["skipped"] += 1
            continue

        if get_item_by_key(item_key) is None:
            result["errors"].append({
                "canonical_id": cid,
                "item_key": item_key,
                "error": "Zotero 中已不存在",
            })
            continue

        record = {
            col: annotator.default_for_field(col)
            for col in annotator.RECORDS_FINAL_COLUMNS
        }
        for k in (
            "title", "authors", "journal", "year", "doi",
            "pmid", "pmcid", "abstract_final", "import_tags_json",
        ):
            if k in row:
                record[k] = row[k]
        record["canonical_id"] = cid
        record["data_origin"] = "zotero_import"
        record["first_seen_run"] = ""
        record["last_seen_run"] = ""
        record["appearance_count"] = 0
        new_records.append(record)

        _import_col = row.get("import_collection", "")
        if _import_col is None or (
            isinstance(_import_col, float) and _import_col != _import_col
        ):
            _import_col = ""
        _import_col = str(_import_col).strip()

        new_links.append({
            "canonical_id": cid,
            "zotero_item_key": item_key,
            "zotero_synced_at": now,
            "zotero_link_mode": link_mode,
            "zotero_collection": _import_col,
            "zotero_attachment_key": "",
        })
        result["imported"] += 1

    if new_records:
        combined = pd.concat(
            [records_df, pd.DataFrame(new_records)], ignore_index=True,
        )
        _save_table(combined, records_path)

        combined_links = pd.concat(
            [links_df, pd.DataFrame(new_links)], ignore_index=True,
        )
        _save_table(combined_links, library_root / "zotero_links.parquet")

        annotator.update_library_index(library_root)
        _emit_log("info", f"引入 {result['imported']} 条记录")

    return result

def withdraw_imported_items(
    canonical_ids: list[str],
    library_root: Path,
) -> dict:
    library_root = Path(library_root)
    result = {"withdrawn": 0, "skipped": [], "errors": []}

    if not canonical_ids:
        return result

    records_path = library_root / "records_final.parquet"
    if not records_path.exists():
        result["skipped"] = list(canonical_ids)
        return result

    records_df = pd.read_parquet(records_path)
    links_df = _load_table(
        library_root / "zotero_links.parquet", ZOTERO_LINKS_COLUMNS
    )

    to_remove: set[str] = set()
    for cid in canonical_ids:
        match = records_df[records_df["canonical_id"].astype(str) == cid]
        if len(match) == 0:
            result["skipped"].append(cid)
            continue
        origin = str(match.iloc[0].get("data_origin", "") or "")
        if origin != "zotero_import":
            result["skipped"].append(cid)
            continue
        to_remove.add(cid)

    if not to_remove:
        return result

    new_records = records_df[
        ~records_df["canonical_id"].astype(str).isin(to_remove)
    ]
    _save_table(new_records, records_path)

    if len(links_df) > 0:
        new_links = links_df[
            ~links_df["canonical_id"].astype(str).isin(to_remove)
        ]
        _save_table(new_links, library_root / "zotero_links.parquet")

    annotator.update_library_index(library_root)

    result["withdrawn"] = len(to_remove)
    _emit_log("info", f"撤回 {result['withdrawn']} 条记录")
    return result

def clear_zotero_links(
    canonical_ids: list[str],
    library_root: Path,
) -> dict:
    library_root = Path(library_root)
    result = {"cleared": 0, "skipped": [], "errors": []}

    if not canonical_ids:
        return result

    links_df = _load_table(
        library_root / "zotero_links.parquet", ZOTERO_LINKS_COLUMNS
    )
    if len(links_df) == 0:
        result["skipped"] = list(canonical_ids)
        return result

    existing_cids = set(links_df["canonical_id"].astype(str).tolist())
    to_remove: set[str] = set()
    for cid in canonical_ids:
        if cid in existing_cids:
            to_remove.add(cid)
        else:
            result["skipped"].append(cid)

    if not to_remove:
        return result

    new_links = links_df[
        ~links_df["canonical_id"].astype(str).isin(to_remove)
    ]
    _save_table(new_links, library_root / "zotero_links.parquet")

    annotator.update_library_index(library_root)

    result["cleared"] = len(to_remove)
    _emit_log("info", f"清理 Zotero 关联 {result['cleared']} 条")
    return result

def cleanup_orphan_zotero_imports(
    library_root: Path,
) -> dict:
    library_root = Path(library_root)
    result = {"cleaned": 0, "cids": []}

    records_path = library_root / "records_final.parquet"
    if not records_path.exists():
        return result

    try:
        records_df = pd.read_parquet(records_path)
    except Exception:
        return result
    if "data_origin" not in records_df.columns:
        return result

    _origin = records_df["data_origin"].fillna("").astype(str)
    import_cids = set(
        records_df.loc[
            _origin.eq("zotero_import"), "canonical_id"
        ].astype(str).tolist()
    )
    if not import_cids:
        return result

    links_df = _load_table(
        library_root / "zotero_links.parquet", ZOTERO_LINKS_COLUMNS,
    )
    linked_cids = (
        set(links_df["canonical_id"].astype(str).tolist())
        if len(links_df) > 0 else set()
    )

    orphan_cids = import_cids - linked_cids
    if not orphan_cids:
        return result

    new_records = records_df[
        ~records_df["canonical_id"].astype(str).isin(orphan_cids)
    ]
    _save_table(new_records, records_path)

    annotator.update_library_index(library_root)

    result["cleaned"] = len(orphan_cids)
    result["cids"] = sorted(orphan_cids)
    _emit_log(
        "info",
        f"已清理 {len(orphan_cids)} 条强杀导致的孤儿 zotero_import 记录"
        f"（Zotero 中若仍存在，可在'可引入'卡重新导入）",
    )
    return result