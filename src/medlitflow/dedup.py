"""dedup.py — 级联式去重与摘要变体标签。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pandas as pd

from medlitflow import core, record_normalize

__all__ = [
    "deduplicate",
    "merge_group",
]

_DEFAULT_ABSTRACT_SIMILARITY_THRESHOLD = 0.05

_CONFLICT_FIELDS = ("title", "journal", "year", "doi")

_RICHNESS_FIELDS = (
    "title", "abstract", "authors", "journal", "year",
    "doi", "pmid", "pmcid", "fulltext_url", "pdf_url", "license",
)

_DEFAULT_SOURCE_PRIORITY = {
    "pubmed": 5, "pmc": 4, "europepmc": 3, "crossref": 2, "openalex": 1,
}

_STRONG_KEY_FIELDS = ("doi_key", "pmid", "pmcid")

_STAGE2_COLUMNS = [
    "source_database", "source_record_id", "query_name", "search_query",
    "title", "abstract", "abstract_missing", "authors", "journal", "year",
    "doi", "pmid", "pmcid",
    "publication_type_raw", "publication_type_normalized",
    "publication_type_category",
    "open_access_flag",
    "fulltext_url", "pdf_url", "landing_page_url",
    "license", "licenses_json", "candidate_urls_json",
    "raw_response_path",
    "canonical_id",
    "abstract_final", "abstract_variants_json", "abstract_variant_count",
    "abstract_variant_status", "abstract_divergence_type",
    "abstract_selection_rule",
    "source_databases", "search_queries", "source_record_ids",
    "matched_sources_count", "matched_rows_count", "primary_source",
    "dedup_rule",
    "has_conflict", "conflict_fields",
    "strategy_names",
    "title_key", "authors_key", "journal_key", "doi_key", "year_key",
]

_is_empty = record_normalize.is_empty
_safe_str = record_normalize.safe_str

def _select_field_by_priority(
    group: pd.DataFrame,
    field: str,
    source_priority: dict,
) -> str:
    sorted_rows = sorted(
        group.to_dict("records"),
        key=lambda r: -source_priority.get(r.get("source_database", ""), 0),
    )
    for row in sorted_rows:
        value = _safe_str(row.get(field, ""))
        if value:
            return value
    return ""

def _select_year_by_priority(
    group: pd.DataFrame,
    source_priority: dict,
) -> int | None:
    sorted_rows = sorted(
        group.to_dict("records"),
        key=lambda r: -source_priority.get(r.get("source_database", ""), 0),
    )
    for row in sorted_rows:
        value = row.get("year")
        if value is None:
            continue
        if isinstance(value, float) and value != value:  # NaN
            continue
        try:
            return int(value)
        except (ValueError, TypeError):
            continue
    return None

def _row_richness(row: pd.Series) -> int:
    count = 0
    for field in _RICHNESS_FIELDS:
        if not _is_empty(row.get(field)):
            count += 1
    return count

def _combine_unique(values, separator: str = "; ") -> str:
    seen: list[str] = []
    for value in values:
        text = _safe_str(value)
        if text and text not in seen:
            seen.append(text)
    return separator.join(seen)

def _combine_list_field(values, separator: str = "; ") -> str:
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _safe_str(value)
        if not text:
            continue
        for item in text.split(";"):
            item = item.strip()
            if item and item not in seen:
                seen.add(item)
                merged.append(item)
    return separator.join(merged)

def _combine_json_lists(values) -> str:
    merged: list[Any] = []
    markers: set[str] = set()
    for value in values:
        if _is_empty(value):
            continue
        if isinstance(value, str):
            text = value.strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
                items = parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                items = [text]
        elif isinstance(value, list):
            items = value
        else:
            items = [value]
        for item in items:
            marker = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if marker not in markers:
                markers.add(marker)
                merged.append(item)
    return json.dumps(merged, ensure_ascii=False)

def _pick_first_url(values) -> str:
    for value in values:
        text = _safe_str(value)
        if text.startswith(("http://", "https://")):
            return text
    return ""

def _compute_content_hash(text: str) -> str:
    # 薄封装（保留为 dedup 内部调用点，避免多处直改）
    return core.hash_abstract(text)

def _build_abstract_variants(group: pd.DataFrame) -> list[dict]:
    variants: list[dict] = []
    for _, row in group.iterrows():
        abstract = _safe_str(row.get("abstract", ""))
        if not abstract:
            continue
        variants.append({
            "source": _safe_str(row.get("source_database", "")),
            "content": abstract,
            "content_hash": _compute_content_hash(abstract),
            "length": len(abstract),
        })
    return variants

def _classify_variant_status(
    variants: list[dict],
    threshold: float = _DEFAULT_ABSTRACT_SIMILARITY_THRESHOLD,
) -> str:
    if len(variants) <= 1:
        return "single"
    hashes = {v["content_hash"] for v in variants}
    if len(hashes) == 1:
        return "identical"
    lengths = [v["length"] for v in variants]
    max_len, min_len = max(lengths), min(lengths)
    if max_len > 0 and (max_len - min_len) / max_len < threshold:
        return "near_identical"
    return "divergent"

def _infer_divergence_type(group: pd.DataFrame, variants: list[dict]) -> str:
    if not variants:
        return ""

    pub_types = {
        _safe_str(row.get("publication_type_normalized", "")).lower()
        for _, row in group.iterrows()
    }
    has_preprint = any("preprint" in pt for pt in pub_types)
    has_journal = any("journal" in pt for pt in pub_types)
    if has_preprint and has_journal:
        return "preprint_vs_published"

    errata_kw = ("erratum", "correction", "corrigendum", "retraction")
    for v in variants:
        text = v["content"].lower()
        if any(kw in text for kw in errata_kw):
            return "erratum"

    return "unknown"

def _select_abstract_final(
    variants: list[dict],
    status: str,
    source_priority: dict,
) -> tuple[str, str]:
    if not variants:
        return "", "first_by_source_priority"

    if status in ("single", "identical", "near_identical"):
        sorted_variants = sorted(
            variants,
            key=lambda v: -source_priority.get(v["source"], 0),
        )
        return sorted_variants[0]["content"], "first_by_source_priority"

    longest = max(variants, key=lambda v: v["length"])
    return longest["content"], "longest"

def _detect_conflicts(group: pd.DataFrame) -> tuple[bool, str]:
    conflict_fields: list[str] = []
    for display_field in _CONFLICT_FIELDS:
        key_field = record_normalize._KEY_FIELD_MAP.get(
            display_field, display_field,
        )
        use_key = key_field in group.columns
        values = set()
        for _, row in group.iterrows():
            raw = row.get(key_field if use_key else display_field, "")
            value = _safe_str(raw)
            if value:
                values.add(value)
        if len(values) > 1:
            conflict_fields.append(display_field)
    return bool(conflict_fields), ";".join(conflict_fields)

def merge_group(group: pd.DataFrame, config: dict) -> dict:
    dedup_cfg = config.get("dedup", {}) or {}
    source_priority = dedup_cfg.get("source_priority", _DEFAULT_SOURCE_PRIORITY)
    similarity_threshold = float(
        dedup_cfg.get("abstract_similarity_threshold",
                      _DEFAULT_ABSTRACT_SIMILARITY_THRESHOLD)
    )

    ranked = group.copy()
    ranked["_richness"] = ranked.apply(_row_richness, axis=1)
    ranked["_priority"] = ranked["source_database"].map(
        lambda s: source_priority.get(s, 0)
    )
    ranked = ranked.sort_values(
        ["_richness", "_priority"],
        ascending=[False, False],
    )
    primary = ranked.iloc[0]
    title = _select_field_by_priority(group, "title", source_priority)
    journal = _select_field_by_priority(group, "journal", source_priority)
    doi = _select_field_by_priority(group, "doi", source_priority)
    pmid = _select_field_by_priority(group, "pmid", source_priority)
    pmcid = _select_field_by_priority(group, "pmcid", source_priority)
    pub_type_raw = _select_field_by_priority(group, "publication_type_raw", source_priority)
    pub_type_norm = _select_field_by_priority(group, "publication_type_normalized", source_priority)
    pub_type_cat = _select_field_by_priority(group, "publication_type_category", source_priority)
    year = _select_year_by_priority(group, source_priority)

    canonical_id = _safe_str(primary.get("canonical_id", ""))
    source_database = _safe_str(primary.get("source_database", ""))
    source_record_id = _safe_str(primary.get("source_record_id", ""))
    query_name = _safe_str(primary.get("query_name", ""))
    search_query = _safe_str(primary.get("search_query", ""))
    abstract = _safe_str(primary.get("abstract", ""))
    raw_response_path = _safe_str(primary.get("raw_response_path", ""))
    dedup_rule = _safe_str(primary.get("dedup_rule", ""))

    authors = _combine_list_field(group["authors"].tolist())
    source_databases = _combine_list_field(group["source_database"].tolist())
    source_record_ids = _combine_list_field(group["source_record_id"].tolist())
    search_queries = _combine_unique(group["search_query"].tolist(), separator=" || ")
    strategy_names = _combine_list_field(group["query_name"].tolist())

    _oa_series = group["open_access_flag"]
    if _oa_series.dtype != bool:
        _oa_series = (
            _oa_series.astype(str).str.strip().str.lower()
            .isin(["true", "1", "yes"])
        )
    open_access_flag = bool(_oa_series.any())

    fulltext_url = _pick_first_url(group["fulltext_url"].tolist())
    pdf_url = _pick_first_url(group["pdf_url"].tolist())
    landing_page_url = _pick_first_url(group["landing_page_url"].tolist())

    candidate_urls_json = _combine_json_lists(group["candidate_urls_json"].tolist())
    licenses_json = _combine_json_lists(group["licenses_json"].tolist())

    license_text = _combine_unique(group["license"].tolist())

    matched_sources_count = int(group["source_database"].nunique())
    matched_rows_count = int(len(group))
    primary_source = source_database

    has_conflict, conflict_fields = _detect_conflicts(group)

    variants = _build_abstract_variants(group)
    variant_count = len(variants)
    variant_status = _classify_variant_status(variants, similarity_threshold)
    if variant_status == "divergent":
        divergence_type = _infer_divergence_type(group, variants)
    else:
        divergence_type = ""
    abstract_final, selection_rule = _select_abstract_final(
        variants, variant_status, source_priority,
    )
    abstract_missing = not bool(abstract_final)

    merged = {
        "source_database": source_database,
        "source_record_id": source_record_id,
        "query_name": query_name,
        "search_query": search_query,
        "title": title,
        "abstract": abstract,
        "abstract_missing": abstract_missing,
        "authors": authors,
        "journal": journal,
        "year": year,
        "doi": doi,
        "pmid": pmid,
        "pmcid": pmcid,
        "publication_type_raw": pub_type_raw,
        "publication_type_normalized": pub_type_norm,
        "publication_type_category": pub_type_cat,
        "open_access_flag": open_access_flag,
        "fulltext_url": fulltext_url,
        "pdf_url": pdf_url,
        "landing_page_url": landing_page_url,
        "license": license_text,
        "licenses_json": licenses_json,
        "candidate_urls_json": candidate_urls_json,
        "raw_response_path": raw_response_path,
        "canonical_id": canonical_id,
        "abstract_final": abstract_final,
        "abstract_variants_json": json.dumps(variants, ensure_ascii=False),
        "abstract_variant_count": variant_count,
        "abstract_variant_status": variant_status,
        "abstract_divergence_type": divergence_type,
        "abstract_selection_rule": selection_rule,
        "source_databases": source_databases,
        "search_queries": search_queries,
        "source_record_ids": source_record_ids,
        "matched_sources_count": matched_sources_count,
        "matched_rows_count": matched_rows_count,
        "primary_source": primary_source,
        "dedup_rule": dedup_rule,
        "has_conflict": has_conflict,
        "conflict_fields": conflict_fields,
        "strategy_names": strategy_names,
    }

    return record_normalize.rebuild_keys(merged)

def _infer_rule_from_canonical_id(canonical_id: str) -> str:
    if canonical_id.startswith("doi:"):
        return "doi_exact"
    if canonical_id.startswith("pmid:"):
        return "pmid_exact"
    if canonical_id.startswith("pmcid:"):
        return "pmcid_exact"
    if canonical_id.startswith("title:"):
        return "normalized_title"
    if canonical_id.startswith("row:"):
        return "no_key_fallback"
    return "unknown"

def _select_canonical_id_from_component(
    group: pd.DataFrame,
    source_priority: dict,
) -> str:
    sorted_rows = sorted(
        group.to_dict("records"),
        key=lambda r: -source_priority.get(r.get("source_database", ""), 0),
    )

    for row in sorted_rows:
        doi = core.normalize_doi(row.get("doi", ""))
        if doi:
            return f"doi:{doi}"
    for row in sorted_rows:
        pmid = core.normalize_pmid(row.get("pmid", ""))
        if pmid:
            return f"pmid:{pmid}"
    for row in sorted_rows:
        pmcid = core.normalize_pmcid(row.get("pmcid", ""))
        if pmcid:
            return f"pmcid:{pmcid}"
    for row in sorted_rows:
        title = core.clean_text(row.get("title", ""))
        if title:
            tn = core.normalize_title(title)
            if tn:
                h = hashlib.sha256(tn.encode("utf-8")).hexdigest()[:16]
                return f"title:{h}"

    row = sorted_rows[0] if sorted_rows else {}
    src = _safe_str(row.get("source_database", "unknown"))
    rid = _safe_str(row.get("source_record_id", "unknown"))
    return f"row:{src}:{rid}"

def deduplicate(
    df: pd.DataFrame,
    config: dict,
) -> tuple[pd.DataFrame, dict]:
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=_STAGE2_COLUMNS), {
            "raw_rows": 0,
            "deduplicated_rows": 0,
            "removed_as_duplicates": 0,
            "multi_source_groups": 0,
            "rule_counts": {},
        }

    data = df.copy().reset_index(drop=True)
    n = len(data)

    data["canonical_id"] = data.apply(
        lambda row: core.generate_canonical_id(row.to_dict()),
        axis=1,
    )

    parent: list[int] = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for key_field in _STRONG_KEY_FIELDS:
        groups_map: dict[str, list[int]] = {}
        for i in range(n):
            raw = _safe_str(data.iloc[i].get(key_field, "")).strip()
            if not raw:
                continue
            if key_field == "pmid":
                v = core.normalize_pmid(raw)
            elif key_field == "pmcid":
                v = core.normalize_pmcid(raw)
            else:
                v = raw
            if v:
                groups_map.setdefault(v, []).append(i)
        for idxs in groups_map.values():
            if len(idxs) < 2:
                continue
            for i in idxs[1:]:
                union(idxs[0], i)

    title_groups: dict[str, list[int]] = {}
    for i in range(n):
        tk = _safe_str(data.iloc[i].get("title_key", "")).strip()
        if tk:
            title_groups.setdefault(tk, []).append(i)

    for tk, idxs in title_groups.items():
        if len(idxs) < 2:
            continue
        has_any_no_strong = False
        for i in idxs:
            has_strong = any(
                _safe_str(data.iloc[i].get(f, "")).strip()
                for f in _STRONG_KEY_FIELDS
            )
            if not has_strong:
                has_any_no_strong = True
                break
        if has_any_no_strong:
            for i in idxs[1:]:
                union(idxs[0], i)

    components: dict[int, list[int]] = {}
    for i in range(n):
        root = find(i)
        components.setdefault(root, []).append(i)

    dedup_cfg = config.get("dedup", {}) or {}
    source_priority = dedup_cfg.get(
        "source_priority", _DEFAULT_SOURCE_PRIORITY,
    )

    merged_rows: list[dict] = []
    for root in sorted(components.keys()):
        idxs = components[root]
        group = data.iloc[idxs].copy()

        final_cid = _select_canonical_id_from_component(
            group, source_priority,
        )

        single_cids = set(group["canonical_id"].astype(str))
        if len(single_cids) == 1:
            dedup_rule = _infer_rule_from_canonical_id(
                next(iter(single_cids))
            )
        else:
            dedup_rule = "transitive_merge"

        group["canonical_id"] = final_cid
        group["dedup_rule"] = dedup_rule

        merged_rows.append(merge_group(group, config))

    dedup_df = pd.DataFrame(merged_rows)

    for col in _STAGE2_COLUMNS:
        if col not in dedup_df.columns:
            dedup_df[col] = ""
    dedup_df = dedup_df[_STAGE2_COLUMNS]

    dedup_rule_counts = dedup_df["dedup_rule"].value_counts().to_dict()
    stats = {
        "raw_rows": int(n),
        "deduplicated_rows": int(len(dedup_df)),
        "removed_as_duplicates": int(n - len(dedup_df)),
        "multi_source_groups": int(
            (dedup_df["matched_sources_count"] > 1).sum()
        ),
        "rule_counts": {
            str(k): int(v) for k, v in dedup_rule_counts.items()
        },
    }
    return dedup_df, stats