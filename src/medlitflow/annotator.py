"""medlitflow annotator.py — 标注与入库层。

实现：
- build_final_table：把上游各阶段的产出拼成宽表
- merge_into_library：把本次 run 的宽表合并到 library 主表
- write_summary_report：写运行摘要报告
- filter_by_publication_type：按文章类型分类筛选
- update_library_index：更新 library 查询索引"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from medlitflow import core, record_normalize

from medlitflow.retrieval import (
    ACCESS_ROUTE_EUROPEPMC, ACCESS_ROUTE_PMC,
    ACCESS_ROUTE_DOI, ACCESS_ROUTE_PUBLISHER,
    ACCESS_ROUTE_INHERITED, ACCESS_ROUTE_UNKNOWN,
)

__all__ = [
    "build_final_table",
    "merge_into_library",
    "write_summary_report",
    "filter_by_publication_type",
    "update_library_index",
    "update_report_metrics_section",
    "SPECIAL_STATUS_NORMAL",
    "SPECIAL_STATUS_NOISE",
    "SPECIAL_STATUS_DELETED",
    "NOISE_CLEARED_FIELDS",
    "noise_cleared_value",
    "RECORDS_FINAL_COLUMNS",
    "default_for_field",
]

_DEFAULT_ABSTRACT_SIMILARITY_THRESHOLD = 0.05

_CORE_FIELDS = (
    "title",
    "journal",
    "year",
    "doi",
    "authors",
    "abstract_final",
)

_CONFLICT_LOG_EXTRA_FIELDS = ("pmid", "pmcid")

_STAGE1_DROP_FIELDS = (
    "abstract",
    "query_name",
    "search_query",
    "source_record_id",
    "raw_response_path",
    "source_database",
)

_RUNTIME_DROP_FIELDS = ("is_library_hit",)

_RECORDS_FINAL_COLUMNS = [
    "title", "abstract_missing", "authors", "journal", "year",
    "doi", "pmid", "pmcid",
    "publication_type_raw", "publication_type_normalized",
    "publication_type_category",
    "open_access_flag",
    "fulltext_url", "pdf_url", "landing_page_url",
    "license", "licenses_json", "candidate_urls_json",
    "canonical_id",
    "abstract_final", "abstract_variants_json", "abstract_variant_count",
    "abstract_variant_status", "abstract_divergence_type",
    "abstract_selection_rule",
    "source_databases", "search_queries", "source_record_ids",
    "matched_sources_count", "matched_rows_count", "primary_source",
    "dedup_rule",
    "has_conflict", "conflict_fields", "strategy_names",
    "retrieval_status", "retrieval_reason", "downloaded_file_path",
    "content_format", "selected_url", "attempt_count",
    "redirect_chain_json", "access_route_used",
    "html_second_pass_status", "secondary_pdf_path",
    "file_dedup_decision", "file_download_temp_path",
    "final_retrieval_status", "abstract_source", "fallback_level",
    "citation_count", "reference_count", "influential_citation_count",
    "journal_impact_factor", "journal_jcr_quartile",
    "author_count", "institution_count", "country_codes",
    "metrics_status",
    "first_seen_run", "last_seen_run", "appearance_count",
    "matched_strategy_count",
    "special_status",
    "data_origin", "import_tags_json",
]

# 语义三选一：
#   ""                     → 正常（默认）
#   "retrieval_noise"      → 检索噪声（用户标记，元数据保留）
#   "deleted_from_source"  → 数据源已删除（原文已移除）
SPECIAL_STATUS_NORMAL = ""
SPECIAL_STATUS_NOISE = "retrieval_noise"
SPECIAL_STATUS_DELETED = "deleted_from_source"
_VALID_SPECIAL_STATUS = frozenset({
    SPECIAL_STATUS_NORMAL, SPECIAL_STATUS_NOISE, SPECIAL_STATUS_DELETED,
})

_NOISE_CLEARED_FIELDS = (
    "abstract_final", "abstract_variants_json", "abstract_variant_count",
    "abstract_variant_status", "abstract_divergence_type",
    "abstract_selection_rule", "abstract_missing",
    "citation_count", "reference_count", "influential_citation_count",
    "journal_impact_factor", "journal_jcr_quartile",
    "author_count", "institution_count", "country_codes", "metrics_status",
)

_INDEX_FIELDS = [
    # 标识 (3)
    "canonical_id", "doi", "pmid",
    # 书目 (4)
    "title", "authors", "journal", "year",
    # 内容 (1)
    "abstract_final",
    # 类型 (1)
    "publication_type_category",
    # 溯源 (3)
    "strategy_names", "source_databases", "matched_strategy_count",
    # 指标 (3)
    "citation_count", "journal_impact_factor", "journal_jcr_quartile",
    # 追踪 (3)
    "first_seen_run", "last_seen_run", "appearance_count",
    # 决策 (4)
    "user_decision", "user_decision_date", "user_notes",
    "prescreen_label_snapshot",
    # 文件 (3)
    "current_file_path", "file_retention_level", "downloaded_file_path",
    # Zotero (2)
    "zotero_item_key", "zotero_synced_at",
    # 质量 (1)
    "has_conflict",
    # URL (3)
    "pdf_url", "fulltext_url", "open_access_flag",
    # special 状态
    "special_status",
    # 数据来源
    "data_origin",
]

_FIELD_STRATEGIES = record_normalize.FIELD_STRATEGIES

def _now_iso() -> str:
    return core.now_iso()

def _get_version() -> str:
    from medlitflow import __version__
    return __version__

_is_empty = record_normalize.is_empty
_safe_str = record_normalize.safe_str

def _safe_int(value: Any) -> int | None:
    if _is_empty(value):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None

def _get_similarity_threshold(config: dict | None = None) -> float:
    if config is not None:
        try:
            return float(
                config.get("dedup", {}).get(
                    "abstract_similarity_threshold",
                    _DEFAULT_ABSTRACT_SIMILARITY_THRESHOLD,
                )
            )
        except (ValueError, TypeError):
            pass
    try:
        cfg = core.load_config()
        return float(
            cfg.get("dedup", {}).get(
                "abstract_similarity_threshold",
                _DEFAULT_ABSTRACT_SIMILARITY_THRESHOLD,
            )
        )
    except Exception:
        return _DEFAULT_ABSTRACT_SIMILARITY_THRESHOLD

def _default_for_field(field: str) -> Any:
    if field == "special_status":
        return SPECIAL_STATUS_NORMAL
    numeric_nullable = {
        "citation_count", "reference_count", "influential_citation_count",
        "journal_impact_factor", "author_count", "institution_count",
    }
    if field in numeric_nullable or field == "year":
        return None
    if field in ("journal_jcr_quartile", "country_codes"):
        return ""
    if field in ("has_conflict", "open_access_flag", "abstract_missing"):
        return False
    if field in ("appearance_count", "matched_sources_count",
                 "matched_rows_count", "abstract_variant_count",
                 "attempt_count", "fallback_level",
                 "matched_strategy_count"):
        return 0
    if field == "data_origin":
        return "retrieval"
    if field == "import_tags_json":
        return ""
    return ""

def _noise_cleared_value(field: str) -> Any:
    if field == "abstract_missing":
        return True
    if field == "abstract_variants_json":
        return "[]"
    if field == "metrics_status":
        return "unavailable"
    return _default_for_field(field)

NOISE_CLEARED_FIELDS = _NOISE_CLEARED_FIELDS
noise_cleared_value = _noise_cleared_value

RECORDS_FINAL_COLUMNS = _RECORDS_FINAL_COLUMNS
default_for_field = _default_for_field

def _clear_noise_fields_in_dict(merged: dict) -> None:
    for field in _NOISE_CLEARED_FIELDS:
        merged[field] = _noise_cleared_value(field)

_split_conflict_fields = record_normalize.split_conflict_fields

def _compute_matched_strategy_count(strategy_names: str) -> int:
    if not strategy_names:
        return 0
    parts = [p.strip() for p in strategy_names.split(";") if p.strip()]
    return len(parts)

def _normalize_for_compare(value: Any, field: str) -> str:
    if _is_empty(value):
        return ""
    text = core.clean_text(value)
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

def _detect_cross_run_conflict(
    run_row: pd.Series,
    lib_row: pd.Series,
) -> tuple[bool, list[str]]:
    conflict_fields: list[str] = []
    for field in _CORE_FIELDS:
        if field == "abstract_final":
            old_val = _normalize_for_compare(lib_row.get(field), field)
            new_val = _normalize_for_compare(run_row.get(field), field)
        else:
            old_val = record_normalize.make_key(
                lib_row.get(field), field,
            )
            new_val = record_normalize.make_key(
                run_row.get(field), field,
            )
        if old_val and new_val and old_val != new_val:
            conflict_fields.append(field)
    return bool(conflict_fields), conflict_fields

def _classify_variant_status(
    variants: list,
    threshold: float = _DEFAULT_ABSTRACT_SIMILARITY_THRESHOLD,
) -> str:
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
    if max_len > 0 and (max_len - min_len) / max_len < threshold:
        return "near_identical"
    return "divergent"

_parse_json_list = record_normalize._parse_json_list
_increment_count = record_normalize._increment_count
_max_int = record_normalize._max_int

def _map_entity_papers(df: pd.DataFrame,
                       entity_papers: pd.DataFrame) -> None:
    if entity_papers is None or len(entity_papers) == 0:
        return
    cols = ["canonical_id", "citation_count", "reference_count",
            "influential_citation_count", "metrics_status"]
    cols = [c for c in cols if c in entity_papers.columns]
    if "canonical_id" not in cols:
        return
    paper_df = (
        entity_papers[cols]
        .drop_duplicates(subset=["canonical_id"], keep="first")
        .set_index("canonical_id")
    )
    cid_series = df["canonical_id"].apply(_safe_str)
    hit = cid_series.isin(paper_df.index)
    if not hit.any():
        return
    for k in ("citation_count", "reference_count",
              "influential_citation_count", "metrics_status"):
        if k not in paper_df.columns:
            continue
        mapped = cid_series[hit].map(paper_df[k])
        df.loc[hit, k] = mapped.values

def _map_journal_metrics(df: pd.DataFrame,
                         relation_published_in: pd.DataFrame,
                         entity_journals: pd.DataFrame) -> None:
    if (relation_published_in is None or len(relation_published_in) == 0
            or entity_journals is None or len(entity_journals) == 0):
        return
    if not {"canonical_id", "journal_id"}.issubset(
            relation_published_in.columns):
        return
    if not {"journal_id", "journal_impact_factor",
            "journal_jcr_quartile"}.issubset(entity_journals.columns):
        return

    paper_journal = (
        relation_published_in[["canonical_id", "journal_id"]]
        .merge(
            entity_journals[["journal_id", "journal_impact_factor",
                             "journal_jcr_quartile"]],
            on="journal_id", how="inner",
        )
        .drop_duplicates(subset=["canonical_id"], keep="first")
    )
    metrics_df = paper_journal.set_index("canonical_id")[
        ["journal_impact_factor", "journal_jcr_quartile"]
    ]

    cid_series = df["canonical_id"].apply(_safe_str)
    hit = cid_series.isin(metrics_df.index)
    if not hit.any():
        return
    jif_mapped = cid_series[hit].map(metrics_df["journal_impact_factor"])
    df.loc[hit, "journal_impact_factor"] = jif_mapped.values
    jq_mapped = (
        cid_series[hit]
        .map(metrics_df["journal_jcr_quartile"])
        .fillna("")
        .apply(_safe_str)
    )
    df.loc[hit, "journal_jcr_quartile"] = jq_mapped.values

def _map_author_count(df: pd.DataFrame,
                      relation_authored: pd.DataFrame) -> None:
    if relation_authored is None or len(relation_authored) == 0:
        return
    if not {"canonical_id", "author_id"}.issubset(
            relation_authored.columns):
        return
    author_count = relation_authored.groupby("canonical_id")[
        "author_id"].nunique()

    cid_series = df["canonical_id"].apply(_safe_str)
    hit = cid_series.isin(author_count.index)
    if not hit.any():
        return
    mapped = cid_series[hit].map(author_count).astype(int)
    df.loc[hit, "author_count"] = mapped.values

def _map_institution_count(df: pd.DataFrame,
                           relation_authored: pd.DataFrame,
                           relation_affiliated: pd.DataFrame) -> None:
    if (relation_authored is None or len(relation_authored) == 0
            or relation_affiliated is None or len(relation_affiliated) == 0):
        return
    if not {"canonical_id", "author_id"}.issubset(
            relation_authored.columns):
        return
    if not {"author_id", "institution_id"}.issubset(
            relation_affiliated.columns):
        return

    paper_insts = relation_authored[["canonical_id", "author_id"]].merge(
        relation_affiliated[["author_id", "institution_id"]],
        on="author_id", how="inner",
    )
    inst_count = paper_insts.groupby("canonical_id")[
        "institution_id"].nunique()

    cid_series = df["canonical_id"].apply(_safe_str)
    hit = cid_series.isin(inst_count.index)
    if not hit.any():
        return
    mapped = cid_series[hit].map(inst_count).astype(int)
    df.loc[hit, "institution_count"] = mapped.values

def _map_country_codes(df: pd.DataFrame,
                       relation_authored: pd.DataFrame,
                       relation_affiliated: pd.DataFrame,
                       entity_institutions: pd.DataFrame) -> None:
    if (relation_authored is None or len(relation_authored) == 0
            or relation_affiliated is None or len(relation_affiliated) == 0
            or entity_institutions is None or len(entity_institutions) == 0):
        return
    if not {"canonical_id", "author_id"}.issubset(
            relation_authored.columns):
        return
    if not {"author_id", "institution_id"}.issubset(
            relation_affiliated.columns):
        return
    if not {"institution_id", "country_code"}.issubset(
            entity_institutions.columns):
        return

    paper_countries = (
        relation_authored[["canonical_id", "author_id"]]
        .merge(relation_affiliated[["author_id", "institution_id"]],
               on="author_id", how="inner")
        .merge(entity_institutions[["institution_id", "country_code"]],
               on="institution_id", how="inner")
    )
    paper_countries = paper_countries.dropna(subset=["country_code"])
    paper_countries = paper_countries[
        paper_countries["country_code"].astype(str).str.strip() != ""
    ]
    if len(paper_countries) == 0:
        return
    country_map = paper_countries.groupby("canonical_id")["country_code"].apply(
        lambda s: ";".join(sorted({str(x) for x in s if x}))
    )

    cid_series = df["canonical_id"].apply(_safe_str)
    hit = cid_series.isin(country_map.index)
    if not hit.any():
        return
    mapped = cid_series[hit].map(country_map)
    df.loc[hit, "country_codes"] = mapped.values

def build_final_table(
    dedup_df: pd.DataFrame,
    retrieval_df: pd.DataFrame,
    fallback_df: pd.DataFrame,
    enrichment_tables: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    if fallback_df is None or len(fallback_df) == 0:
        return pd.DataFrame(columns=_RECORDS_FINAL_COLUMNS)

    df = fallback_df.copy()
    df = df.drop_duplicates(
        subset=["canonical_id"], keep="first"
    ).reset_index(drop=True)

    for col in ("citation_count", "reference_count",
                "influential_citation_count",
                "journal_impact_factor", "author_count",
                "institution_count"):
        if col not in df.columns:
            df[col] = None
    for col in ("metrics_status", "journal_jcr_quartile", "country_codes"):
        if col not in df.columns:
            df[col] = ""

    df["first_seen_run"] = ""
    df["last_seen_run"] = ""
    df["appearance_count"] = 0

    enrichment_tables = enrichment_tables or {}
    _map_entity_papers(df, enrichment_tables.get("entity_papers"))
    _map_journal_metrics(
        df,
        enrichment_tables.get("relation_published_in"),
        enrichment_tables.get("entity_journals"),
    )
    _map_author_count(df, enrichment_tables.get("relation_authored"))
    _map_institution_count(
        df,
        enrichment_tables.get("relation_authored"),
        enrichment_tables.get("relation_affiliated"),
    )
    _map_country_codes(
        df,
        enrichment_tables.get("relation_authored"),
        enrichment_tables.get("relation_affiliated"),
        enrichment_tables.get("entity_institutions"),
    )

    df["matched_strategy_count"] = df["strategy_names"].apply(
        _compute_matched_strategy_count
    )

    drop_cols = [c for c in (_STAGE1_DROP_FIELDS + _RUNTIME_DROP_FIELDS)
                 if c in df.columns]
    df = df.drop(columns=drop_cols)

    for col in _RECORDS_FINAL_COLUMNS:
        if col not in df.columns:
            df[col] = _default_for_field(col)

    _sp = df["special_status"].fillna("").astype(str).str.strip()
    df["special_status"] = _sp.where(_sp.ne(""), SPECIAL_STATUS_NORMAL)

    return df[_RECORDS_FINAL_COLUMNS].copy()

def _merge_field_by_strategy(field: str, old_val: Any, new_val: Any) -> Any:
    return record_normalize.merge_field_by_strategy(
        field, old_val, new_val,
    )

def _recompute_abstract_fields(
    merged: dict,
    run_row: pd.Series,
    lib_row: pd.Series,
    similarity_threshold: float,
) -> None:
    old_variants = _parse_json_list(lib_row.get("abstract_variants_json", "[]"))
    new_variants = _parse_json_list(run_row.get("abstract_variants_json", "[]"))

    seen: set[str] = set()
    merged_variants: list = []
    for v in old_variants + new_variants:
        if isinstance(v, dict):
            h = v.get("content_hash", "")
            if h and h in seen:
                continue
            if h:
                seen.add(h)
        merged_variants.append(v)

    merged["abstract_variants_json"] = json.dumps(
        merged_variants, ensure_ascii=False
    )
    merged["abstract_variant_count"] = len(merged_variants)

    new_status = _classify_variant_status(
        merged_variants, similarity_threshold
    )
    merged["abstract_variant_status"] = new_status

    old_status = _safe_str(lib_row.get("abstract_variant_status", ""))
    if old_status == "divergent":
        merged["abstract_divergence_type"] = _safe_str(
            lib_row.get("abstract_divergence_type", "")
        )
    elif new_status == "divergent":
        merged["abstract_divergence_type"] = "unknown"
    else:
        merged["abstract_divergence_type"] = ""

    abstract_final = merged.get("abstract_final", "")
    merged["abstract_missing"] = not bool(_safe_str(abstract_final))

    old_final = _safe_str(lib_row.get("abstract_final", ""))
    if old_final:
        merged["abstract_selection_rule"] = _safe_str(
            lib_row.get("abstract_selection_rule", "")
        )
    else:
        merged["abstract_selection_rule"] = _safe_str(
            run_row.get("abstract_selection_rule", "")
        )

def _merge_one_record(
    run_row: pd.Series,
    lib_row: pd.Series,
    similarity_threshold: float,
) -> dict:
    merged: dict = {}

    has_cross_conflict, cross_conflict_fields = _detect_cross_run_conflict(
        run_row, lib_row
    )
    conflict_set = set(cross_conflict_fields)

    for field in _CONFLICT_LOG_EXTRA_FIELDS:
        old_val = _normalize_for_compare(lib_row.get(field), field)
        new_val = _normalize_for_compare(run_row.get(field), field)
        if old_val and new_val and old_val != new_val:
            conflict_set.add(field)

    has_cross_conflict = bool(conflict_set)

    _deferred_fields = {
        "first_seen_run", "last_seen_run", "appearance_count",
        "has_conflict", "conflict_fields",
        "abstract_variant_count", "abstract_variant_status",
        "abstract_divergence_type", "abstract_missing",
        "abstract_selection_rule", "abstract_variants_json",
        "matched_strategy_count",
        "special_status",
    }

    for field in _RECORDS_FINAL_COLUMNS:
        if field in _deferred_fields:
            continue
        if field in _CORE_FIELDS and field in conflict_set:
            merged[field] = lib_row.get(field)
            continue
        old_val = lib_row.get(field)
        new_val = run_row.get(field)
        merged[field] = _merge_field_by_strategy(field, old_val, new_val)

    merged["first_seen_run"] = _safe_str(lib_row.get("first_seen_run", ""))
    merged["last_seen_run"] = _safe_str(run_row.get("last_seen_run", ""))
    merged["appearance_count"] = _increment_count(
        lib_row.get("appearance_count")
    )

    old_has = bool(lib_row.get("has_conflict", False))
    new_has = bool(run_row.get("has_conflict", False))
    merged["has_conflict"] = old_has or new_has or has_cross_conflict

    old_cf = _split_conflict_fields(_safe_str(
        lib_row.get("conflict_fields", "")
    ))
    new_cf = _split_conflict_fields(_safe_str(
        run_row.get("conflict_fields", "")
    ))
    all_cf = old_cf | new_cf | conflict_set
    merged["conflict_fields"] = ";".join(sorted(all_cf))

    merged["matched_strategy_count"] = _compute_matched_strategy_count(
        _safe_str(merged.get("strategy_names", ""))
    )

    _recompute_abstract_fields(
        merged, run_row, lib_row, similarity_threshold
    )

    lib_special = _safe_str(lib_row.get("special_status", ""))
    if lib_special not in _VALID_SPECIAL_STATUS:
        lib_special = SPECIAL_STATUS_NORMAL
    merged["special_status"] = lib_special
    if lib_special == SPECIAL_STATUS_NOISE:
        _clear_noise_fields_in_dict(merged)

    return merged

def merge_into_library(
    run_df: pd.DataFrame,
    library_df: pd.DataFrame,
    config: dict | None = None,
) -> pd.DataFrame:
    if run_df is None or len(run_df) == 0:
        if library_df is None or len(library_df) == 0:
            return pd.DataFrame()
        return library_df.copy()

    for col in ("first_seen_run", "last_seen_run"):
        if col not in run_df.columns:
            raise ValueError(
                f"run_df 缺少 {col} 列；pipeline 必须先注入 run_name"
            )
        if run_df[col].fillna("").astype(str).str.strip().eq("").all():
            raise ValueError(
                f"run_df.{col} 全为空；pipeline 未注入 run_name"
            )

    if library_df is None or len(library_df) == 0:
        new_df = run_df.copy()
        new_df = new_df[
            new_df["canonical_id"].astype(str).str.strip() != ""
        ].reset_index(drop=True)
        new_df["appearance_count"] = 1
        for col in _RECORDS_FINAL_COLUMNS:
            if col not in new_df.columns:
                new_df[col] = _default_for_field(col)
        return new_df[_RECORDS_FINAL_COLUMNS].reset_index(drop=True)

    lib_by_cid: dict[str, pd.Series] = {}
    for _, lib_row in library_df.iterrows():
        cid = _safe_str(lib_row.get("canonical_id", ""))
        if cid:
            lib_by_cid[cid] = lib_row

    similarity_threshold = _get_similarity_threshold(config)

    merged_rows: list[dict] = []
    seen_cids: set[str] = set()

    for _, run_row in run_df.iterrows():
        cid = _safe_str(run_row.get("canonical_id", ""))
        if not cid:
            continue
        seen_cids.add(cid)
        if cid in lib_by_cid:
            merged_rows.append(_merge_one_record(
                run_row, lib_by_cid[cid], similarity_threshold,
            ))
        else:
            new_record = run_row.to_dict()
            new_record["appearance_count"] = 1
            new_record["first_seen_run"] = _safe_str(
                run_row.get("first_seen_run", "")
            )
            new_record["last_seen_run"] = _safe_str(
                run_row.get("last_seen_run", "")
            )
            merged_rows.append(new_record)

    for cid, lib_row in lib_by_cid.items():
        if cid not in seen_cids:
            merged_rows.append(lib_row.to_dict())

    result = pd.DataFrame(merged_rows)
    for col in _RECORDS_FINAL_COLUMNS:
        if col not in result.columns:
            result[col] = _default_for_field(col)

    return result[_RECORDS_FINAL_COLUMNS].reset_index(drop=True)

def _load_parquet_or_raise(path: Path, label: str = "") -> pd.DataFrame:
    # 严格加载：不存在 → 空表；存在但读失败 → RuntimeError（不静默）
    # 静默返回空表会产出缺数据的 index.parquet，用户误判决策丢失后可能
    # 重新筛选覆盖真实数据。严格读取让损坏显式暴露。
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"parquet 损坏：{path}"
            f"（{type(exc).__name__}: {exc}）"
            f"。请从 .txn/ 或备份恢复后再重试。"
            + (f" 模块={label}" if label else "")
        ) from exc

def update_library_index(
    library_root: Path,
    canonical_ids: list[str] | None = None,
) -> None:
    library_root = Path(library_root)
    library_root.mkdir(parents=True, exist_ok=True)

    records = _load_parquet_or_raise(
        library_root / "records_final.parquet",
        label="update_library_index.records",
    )
    if len(records) == 0:
        target = library_root / "index.parquet"
        tmp = target.with_name(target.name + ".tmp")
        pd.DataFrame(columns=_INDEX_FIELDS).to_parquet(tmp, index=False)
        os.replace(tmp, target)
        return

    decisions = _load_parquet_or_raise(
        library_root / "screening_decisions.parquet",
        label="update_library_index.decisions",
    )
    file_index = _load_parquet_or_raise(
        library_root / "file_index.parquet",
        label="update_library_index.file_index",
    )
    zotero_links = _load_parquet_or_raise(
        library_root / "zotero_links.parquet",
        label="update_library_index.zotero_links",
    )

    index_df = records.copy()
    index_df = index_df.drop(columns=["downloaded_file_path"], errors="ignore")

    if len(decisions) > 0 and "canonical_id" in decisions.columns:
        dec_cols = ["canonical_id", "user_decision", "user_decision_date",
                    "user_notes", "prescreen_label_snapshot"]
        dec_cols = [c for c in dec_cols if c in decisions.columns]
        decisions_dedup = decisions[dec_cols].drop_duplicates(
            subset=["canonical_id"], keep="last",
        )
        index_df = index_df.merge(
            decisions_dedup, on="canonical_id", how="left",
        )

    if len(file_index) > 0 and "canonical_id" in file_index.columns:
        fi_cols = ["canonical_id", "current_file_path",
                   "file_retention_level", "downloaded_file_path"]
        fi_cols = [c for c in fi_cols if c in file_index.columns]
        index_df = index_df.merge(
            file_index[fi_cols], on="canonical_id", how="left",
        )

    if len(zotero_links) > 0 and "canonical_id" in zotero_links.columns:
        zl_cols = ["canonical_id", "zotero_item_key", "zotero_synced_at"]
        zl_cols = [c for c in zl_cols if c in zotero_links.columns]
        index_df = index_df.merge(
            zotero_links[zl_cols], on="canonical_id", how="left",
        )

    for col in _INDEX_FIELDS:
        if col not in index_df.columns:
            index_df[col] = _default_for_field(col)

    index_df = index_df[_INDEX_FIELDS].copy()

    target = library_root / "index.parquet"
    tmp = target.with_name(target.name + ".tmp")
    index_df.to_parquet(tmp, index=False)
    os.replace(tmp, target)

def filter_by_publication_type(
    df: pd.DataFrame,
    categories: list[str],
) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame()
    if "publication_type_category" not in df.columns:
        return df
    if not categories:
        return df.iloc[0:0].copy()
    return df[df["publication_type_category"].isin(categories)].copy()

def _resolve_strategy_name(final_df: pd.DataFrame) -> str:
    if "strategy_names" not in final_df.columns or len(final_df) == 0:
        return "unknown_strategy"
    names = str(final_df.iloc[0].get("strategy_names", "") or "")
    first = names.split(";")[0].strip()
    return first or "unknown_strategy"

def _dual_time_strings(iso_str: str) -> tuple[str, str]:
    # 拆为 (本地, UTC)；naive 视为 UTC；失败时返回 (原串, 原串)
    if not iso_str:
        return "", ""
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt_utc = dt.replace(tzinfo=timezone.utc)
        else:
            dt_utc = dt.astimezone(timezone.utc)
        dt_local = dt_utc.astimezone()
        return dt_local.isoformat(), dt_utc.isoformat()
    except (ValueError, TypeError):
        return str(iso_str), str(iso_str)


def _build_restriction_statement(meta: dict) -> str:
    parts: list[str] = []
    if meta.get("language"):
        parts.append(f"限制语言为 {meta['language']}")
    else:
        parts.append("未施加语言限制")

    if meta.get("date_range"):
        parts.append(f"检索时间范围：{meta['date_range']}")
    elif meta.get("is_first_run", True):
        parts.append("检索全部历史文献，无时间窗口限制")
    elif meta.get("from_date") and meta.get("to_date"):
        parts.append(f"检索时间范围：{meta['from_date']} 至 {meta['to_date']}")
    else:
        parts.append("检索全部历史文献，无时间窗口限制")

    return "；".join(parts)

def _load_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

def _load_run_metadata(run_root: Path) -> dict:
    return _load_json_file(run_root / "output" / "run_metadata.json")

def _load_query_execution(run_root: Path) -> dict:
    return _load_json_file(run_root / "output" / "query_execution.json")

def _build_query_table_rows(
    query_execution: dict,
) -> tuple[list[tuple[str, str, str]], str]:
    rows: list[tuple[str, str, str]] = []
    if query_execution:
        p = query_execution.get("pubmed") or {}
        if p:
            rows.append(("PubMed", str(p.get("query", "")),
                         str(p.get("filters", ""))))
        pm = query_execution.get("pmc") or {}
        if pm:
            rows.append(("PMC", str(pm.get("query", "")),
                         str(pm.get("filters", ""))))
        e = query_execution.get("europepmc") or {}
        if e:
            rows.append(("Europe PMC", str(e.get("query", "")),
                         str(e.get("filters", ""))))
        cr = query_execution.get("crossref") or []
        if cr:
            q = " || ".join(
                str(x.get("query.bibliographic", "") or x.get("query", ""))
                for x in cr if isinstance(x, dict)
            )
            f = "; ".join(str(x.get("filter", ""))
                          for x in cr if isinstance(x, dict))
            rows.append(("Crossref", q, f))
        oa = query_execution.get("openalex") or []
        if oa:
            q = " || ".join(
                str(x.get("search", "") or x.get("query", ""))
                for x in oa if isinstance(x, dict)
            )
            f = "; ".join(str(x.get("filter", ""))
                          for x in oa if isinstance(x, dict))
            rows.append(("OpenAlex", q, f))
        return rows, "执行快照"

    return [], "无"

def _pct(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0.0"
    return f"{numerator / denominator * 100:.1f}"

def _column_or_default(df: pd.DataFrame, col: str,
                       default: Any) -> pd.Series:
    if col in df.columns:
        return df[col]
    return pd.Series([default] * len(df))

def write_summary_report(
    final_df: pd.DataFrame,
    run_root: Path,
    library_root: Path | None = None,
) -> None:
    run_root = Path(run_root)
    output_dir = run_root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "summary_report.md"

    if final_df is None or len(final_df) == 0:
        report_path.write_text(
            "# unknown_strategy 检索报告\n\n（本次 run 无记录）\n",
            encoding="utf-8",
        )
        return

    strategy_name = _resolve_strategy_name(final_df)
    run_metadata = _load_run_metadata(run_root)
    query_execution = _load_query_execution(run_root)

    run_name = str(run_metadata.get("run_name", run_root.name))
    timestamp = str(run_metadata.get("started_at", _now_iso()))
    run_date = timestamp[:10] if timestamp else ""
    timestamp_local, timestamp_utc = _dual_time_strings(timestamp)
    restriction_stmt = _build_restriction_statement(run_metadata)

    pubmed_query = ""
    query_source_tag = "未知"
    if "search_queries" in final_df.columns and len(final_df) > 0:
        pubmed_query = str(
            final_df.iloc[0].get("search_queries", "") or ""
        )
        query_source_tag = (
            "从 final_df.search_queries 提取（可能包含日期过滤"
            "与多源混合，仅供追溯）"
        )

    query_rows, exec_source_tag = _build_query_table_rows(query_execution)

    n_total = len(final_df)
    raw_total = int(
        pd.to_numeric(
            _column_or_default(final_df, "matched_rows_count", 0),
            errors="coerce",
        ).fillna(0).sum()
    )

    if raw_total < n_total:
        raw_total = n_total

    source_col = _column_or_default(final_df, "source_databases", "").fillna("")
    src_counter = {"pubmed": 0, "pmc": 0, "europepmc": 0,
                   "crossref": 0, "openalex": 0}
    for s in source_col:
        for part in str(s).split(";"):
            k = part.strip().lower()
            if k in src_counter:
                src_counter[k] += 1

    multi = pd.to_numeric(
        _column_or_default(final_df, "matched_sources_count", 0), errors="coerce"
    ).fillna(0)
    count_2 = int((multi == 2).sum())
    count_3 = int((multi == 3).sum())
    count_4p = int((multi >= 4).sum())
    multi_rate = _pct(count_2 + count_3 + count_4p, n_total)

    final_status = _column_or_default(
        final_df, "final_retrieval_status", ""
    ).fillna("")
    pdf_count = int((final_status == "fulltext_pdf").sum())
    xml_count = int((final_status == "fulltext_xml").sum())
    html_count = int((final_status == "fulltext_html").sum())
    abstract_only_count = int((final_status == "abstract_only").sum())
    metadata_only_count = int((final_status == "metadata_only").sum())
    fulltext_total = pdf_count + xml_count + html_count
    route = _column_or_default(final_df, "access_route_used", "").fillna("")
    route_europepmc = int((route == ACCESS_ROUTE_EUROPEPMC).sum())
    route_pmc = int((route == ACCESS_ROUTE_PMC).sum())
    route_doi = int((route == ACCESS_ROUTE_DOI).sum())
    route_pub = int((route == ACCESS_ROUTE_PUBLISHER).sum())
    route_inherited = int((route == ACCESS_ROUTE_INHERITED).sum())

    known_routes = {
        ACCESS_ROUTE_EUROPEPMC, ACCESS_ROUTE_PMC,
        ACCESS_ROUTE_DOI, ACCESS_ROUTE_PUBLISHER,
        ACCESS_ROUTE_INHERITED, ACCESS_ROUTE_UNKNOWN,
    }
    route_unknown = int((~route.isin(known_routes)).sum())

    reason = _column_or_default(final_df, "retrieval_reason", "").fillna("")
    reason_paywall = int((reason == "paywall").sum())
    reason_paywall_suspect = int((reason == "paywall_suspect").sum())
    reason_broken = int((reason == "broken_link").sum())
    reason_no_url = int((reason == "no_candidate_url").sum())
    reason_rate = int((reason == "rate_limited").sum())
    reason_corr = int((reason == "corrupted").sum())
    reason_none = int((reason == "none").sum())

    known_reasons = {
        "paywall", "paywall_suspect", "broken_link",
        "no_candidate_url", "rate_limited", "corrupted",
        "none", "",
    }
    reason_unknown = int((~reason.isin(known_reasons)).sum())

    variant_status = _column_or_default(
        final_df, "abstract_variant_status", ""
    ).fillna("")
    vs_single = int((variant_status == "single").sum())
    vs_identical = int((variant_status == "identical").sum())
    vs_near = int((variant_status == "near_identical").sum())
    vs_divergent = int((variant_status == "divergent").sum())

    has_conflict_series = _column_or_default(
        final_df, "has_conflict", False
    ).fillna(False).astype(bool)
    conflict_count = int(has_conflict_series.sum())

    doi_series = _column_or_default(final_df, "doi", "").fillna("").astype(str)
    cover_doi = int(doi_series.str.strip().ne("").sum())
    cover_citation = int(
        pd.to_numeric(_column_or_default(final_df, "citation_count", None),
                      errors="coerce").notna().sum()
    )
    cover_jcr = int(
        pd.to_numeric(
            _column_or_default(final_df, "journal_impact_factor", None),
            errors="coerce",
        ).notna().sum()
    )
    author_series = pd.to_numeric(
        _column_or_default(final_df, "author_count", None), errors="coerce"
    )
    cover_authors = int((author_series.fillna(0) > 0).sum())
    inst_series = pd.to_numeric(
        _column_or_default(final_df, "institution_count", None),
        errors="coerce",
    )
    cover_institutions = int((inst_series.fillna(0) > 0).sum())

    # 生成 markdown
    L: list[str] = []
    L.append(f"# {strategy_name} 检索报告")
    L.append("")
    L.append(f"**Run ID**: `{run_name}`")
    L.append(f"**执行时间（本地）**: `{timestamp_local}`"
             "（来源：run_metadata.json.started_at）")
    L.append(f"**执行时间（UTC）**: `{timestamp_utc}`")
    L.append(f"**检索策略**: `{strategy_name}`")
    L.append("")
    L.append("## 1. 检索策略")
    L.append("")
    L.append("### 1.1 原始检索式（PubMed）")
    L.append("")
    L.append("```")
    L.append(pubmed_query or "（缺失）")
    L.append("```")
    L.append("")
    L.append(f"检索式来源：{query_source_tag}")
    L.append("")
    L.append("### 1.2 各数据源实际执行查询")
    L.append("")
    if query_rows:
        L.append("| 数据源 | 查询式 | 限制条件 |")
        L.append("|---|---|---|")
        for src, q, filt in query_rows:
            q_esc = q.replace("|", "\\|")
            f_esc = filt.replace("|", "\\|")
            L.append(f"| {src} | `{q_esc}` | `{f_esc}` |")
        L.append("")
        L.append(f"来源：{exec_source_tag}")
    else:
        L.append("（无 query_execution.json）")
    L.append("")
    L.append("### 1.3 检索约束")
    L.append("")
    L.append(f"- **检索日期**: `{run_date}`"
             "（来源：run_metadata.json.started_at 的日期部分）")
    date_range_val = run_metadata.get("date_range", "") or ""
    L.append(f"- **时间范围**: `{date_range_val}`"
             "（来源：run_metadata.json.date_range；缺失时按限制说明推导）")
    lang_val = run_metadata.get("language", "") or ""
    L.append(f"- **语言限制**: `{lang_val}`（来源：run_metadata.json.language）")
    L.append("- **其他限制**: `""`（当前版本恒为空字符串）")

    effective_limits = run_metadata.get("effective_max_results_per_source", {}) or {}
    if effective_limits:
        limits_str = "；".join(
            f"{k}={v}" for k, v in sorted(effective_limits.items())
        )
        L.append(f"- **各源有效上限（每查询）**: `{limits_str}`")
        L.append(
            "  - 说明：单次 HTTP 请求上限（Crossref rows / OpenAlex per-page / "
            "Europe PMC pageSize / PubMed retmax）分别为 1000/200/1000/10000；"
            "各源总量取上述有效值，Crossref 单查询 offset 上限 10000"
        )
    else:
        L.append("- **各源有效上限（每查询）**: `""`（run_metadata 未记录）")
    L.append("")
    L.append("## 2. 采集结果")
    L.append("")
    L.append("| 数据源 | 命中数 | 说明 |")
    L.append("|---|---|---|")
    L.append(f"| PubMed | {src_counter['pubmed']} | esearch 返回 |")
    L.append(f"| PMC | {src_counter['pmc']} | 单独通道 |")
    L.append(f"| Europe PMC | {src_counter['europepmc']} | |")
    L.append(f"| Crossref | {src_counter['crossref']} | |")
    L.append(f"| OpenAlex | {src_counter['openalex']} | |")
    L.append(f"| **合计（去重前）** | **{raw_total}** | |")
    L.append(
        f"| **去重后** | **{n_total}** | "
        f"去重率 {_pct(raw_total - n_total, raw_total)}% |"
    )
    L.append("")

    n_noise = 0
    n_deleted = 0
    if library_root is not None:
        _lib_records_path = Path(library_root) / "records_final.parquet"
        if _lib_records_path.exists():
            try:
                _lib_df = pd.read_parquet(
                    _lib_records_path, columns=["special_status"],
                )
                if "special_status" in _lib_df.columns:
                    _sp = (
                        _lib_df["special_status"].fillna("")
                        .astype(str).str.strip().str.lower()
                    )
                    n_noise = int(_sp.eq("retrieval_noise").sum())
                    n_deleted = int(_sp.eq("deleted_from_source").sum())
            except Exception:
                pass

    if n_noise > 0 or n_deleted > 0:
        L.append(
            f"| 🗑 特殊标记·检索噪声 | {n_noise} | "
            f"library 全量（含历史），元数据保留 |"
        )
        L.append(
            f"| ⚠ 特殊标记·数据源删除 | {n_deleted} | "
            f"library 全量（含历史），待彻底删除或恢复 |"
        )
        L.append("")

    # PMC 库含更多预印本/会议摘要/病例报告，同检索式下命中数常显著高于
    # PubMed（数据特性，非 bug）。比值 > 2.0 时提示用户核对。
    pubmed_cnt = int(src_counter.get("pubmed", 0))
    pmc_cnt = int(src_counter.get("pmc", 0))
    if pubmed_cnt > 0 and pmc_cnt > pubmed_cnt * 2.0:
        L.append(
            f"> ⚠️ **PMC 命中数显著高于 PubMed**"
            f"（PMC={pmc_cnt}，PubMed={pubmed_cnt}，比值 "
            f"{pmc_cnt / pubmed_cnt:.1f}×）。"
        )
        L.append(
            "> PMC 库含更多预印本、会议摘要、病例报告等非正式发表文献，"
            "命中数偏高为数据特性（与 PubMed 使用同一检索式）。"
            "如需降低噪声，可在设置中关闭 PMC 通道。"
        )
        L.append("")
    L.append("### 2.1 跨数据源重叠分析")
    L.append("")
    L.append("| 重叠数 | 文献数 | 占比 |")
    L.append("|---|---|---|")
    L.append(f"| 命中 2 个源 | {count_2} | {_pct(count_2, n_total)}% |")
    L.append(f"| 命中 3 个源 | {count_3} | {_pct(count_3, n_total)}% |")
    L.append(f"| 命中 ≥4 个源 | {count_4p} | {_pct(count_4p, n_total)}% |")
    L.append(f"| **多源重叠率** | **{multi_rate}%** | |")
    L.append("")
    L.append("## 3. 全文获取")
    L.append("")
    L.append("| 状态 | 数量 | 占比 |")
    L.append("|---|---|---|")
    L.append(f"| 成功获取 PDF | {pdf_count} | {_pct(pdf_count, n_total)}% |")
    L.append(f"| 成功获取 XML | {xml_count} | {_pct(xml_count, n_total)}% |")
    L.append(f"| HTML 二次追链成功 | {html_count} | {_pct(html_count, n_total)}% |")
    L.append(f"| 摘要回退 | {abstract_only_count} | "
             f"{_pct(abstract_only_count, n_total)}% |")
    L.append(f"| 元数据仅 | {metadata_only_count} | "
             f"{_pct(metadata_only_count, n_total)}% |")
    L.append("")
    L.append("### 3.1 访问通道分析")
    L.append("")
    L.append("| 通道 | 数量 |")
    L.append("|---|---|")
    L.append(f"| Europe PMC | {route_europepmc} |")
    L.append(f"| PMC | {route_pmc} |")
    L.append(f"| DOI 解析 | {route_doi} |")
    L.append(f"| 出版商直链 | {route_pub} |")
    L.append(f"| 继承自 library | {route_inherited} |")
    L.append(f"| 未分类（旧数据/空） | {route_unknown} |")
    L.append(f"| **合计** | **{route_europepmc + route_pmc + route_doi + route_pub + route_inherited + route_unknown}** |")
    L.append("")
    L.append("### 3.2 失败原因")
    L.append("")
    L.append("| 原因 | 数量 |")
    L.append("|---|---|")
    L.append(f"| 付费墙拦截 | {reason_paywall} |")
    L.append(f"| 疑似付费墙（结构化判据） | {reason_paywall_suspect} |")
    L.append(f"| 链接失效 | {reason_broken} |")
    L.append(f"| 无候选 URL | {reason_no_url} |")
    L.append(f"| 速率受限 | {reason_rate} |")
    L.append(f"| 文件损坏 | {reason_corr} |")
    L.append(f"| 成功（none） | {reason_none} |")
    L.append(f"| 未分类（旧数据/空） | {reason_unknown} |")
    L.append(f"| **合计** | **{reason_paywall + reason_paywall_suspect + reason_broken + reason_no_url + reason_rate + reason_corr + reason_none + reason_unknown}** |")
    L.append("")
    L.append("## 4. 去重与冲突")
    L.append("")
    L.append("| 摘要变体状态 | 数量 |")
    L.append("|---|---|")
    L.append(f"| single | {vs_single} |")
    L.append(f"| identical | {vs_identical} |")
    L.append(f"| near_identical | {vs_near} |")
    L.append(f"| divergent | {vs_divergent} |")
    L.append("")
    L.append(f"**关键字段冲突**：{conflict_count} 篇"
             f"（{_pct(conflict_count, n_total)}%）")
    L.append("")
    L.append("## 5. 增强层覆盖")
    L.append("")
    L.append("| 指标 | 覆盖数 | 占比 |")
    L.append("|---|---|---|")
    L.append(f"| 有 DOI | {cover_doi} | {_pct(cover_doi, n_total)}% |")
    L.append(f"| 有引用数据 | {cover_citation} | "
             f"{_pct(cover_citation, n_total)}% |")
    L.append(f"| 有期刊指标（JCR） | {cover_jcr} | "
             f"{_pct(cover_jcr, n_total)}% |")
    L.append(f"| 有作者信息 | {cover_authors} | "
             f"{_pct(cover_authors, n_total)}% |")
    L.append(f"| 有机构信息 | {cover_institutions} | "
             f"{_pct(cover_institutions, n_total)}% |")
    L.append("")
    L.append("判定规则：")
    L.append("- 有 DOI: doi 字段非空")
    L.append("- 有引用数据: citation_count 字段非空")
    L.append("- 有期刊指标（JCR）: journal_impact_factor 字段非空")
    L.append("- 有作者信息: author_count 字段非空且 > 0")
    L.append("- 有机构信息: institution_count 字段非空且 > 0")
    L.append("")
    L.append("### 指标更新记录")
    L.append("")
    L.append("<!-- METRICS_UPDATE_SECTION_START -->")
    L.append("（尚未更新。点击「更新指标」后此处会显示最新数据。）")
    L.append("<!-- METRICS_UPDATE_SECTION_END -->")
    L.append("")
    L.append("## 6. 数据质量说明")
    L.append("")
    L.append("- 所有记录经过 `canonical_id` 统一标识")
    L.append("- 跨数据源使用标准化去重规则（DOI → PMID → PMCID → 标准化标题）")
    L.append("- 摘要变体完整保留，未做人工取舍")
    L.append("- 全文获取采用三级回退机制（候选 URL → 付费墙检测 → HTML 二次追链）")
    collect_quality = _load_json_file(
        run_root / "interim" / "collect_quality.json"
    )
    dropped_no_title = int(collect_quality.get("dropped_no_title", 0) or 0)
    if dropped_no_title > 0:
        dropped_by_source = collect_quality.get("by_source", {}) or {}
        src_dist = "、".join(
            f"{k}: {v}"
            for k, v in sorted(dropped_by_source.items())
        ) or "未细分"
        L.append(
            f"- **采集阶段剔除无标题记录 {dropped_no_title} 条**"
            f"（来源分布：{src_dist}）"
        )
        L.append(
            "  - 判定规则：`title` 字段为空（None / NaN / 空字符串）"
        )
        L.append(
            "  - 剔除原因：无学术可读性；不纳入后续去重、筛选与 PRISMA 统计"
        )
    L.append("")
    L.append("## 7. 检索方法说明")
    L.append("")
    source_db_list = "; ".join(
        f"{k}({v})" for k, v in src_counter.items() if v > 0
    ) or "无"
    L.append(
        f"> 本研究采用 medlitflow 进行文献检索"
        f"（检索日期：{run_date}），覆盖数据源：{source_db_list}。"
        f"{restriction_stmt}。"
    )
    if effective_limits:
        L.append(">")
        L.append(
            f"> 各源采集上限（每查询）：{limits_str}；"
            f"Crossref 单查询 offset 上限 10000（超限仅保留前 10000 条）。"
        )
    L.append(">")
    L.append(
        "> 检索语义说明："
        "PubMed 与 PMC 共用方括号字段语法（如 [tiab]），"
        "Europe PMC 使用 TITLE:/ABS:/MESH: 前缀，"
        "OpenAlex 的 ti/ab/tiab/mh/au 字段标签通过 filter 精确匹配"
        "（自由词走 search 相关性搜索），"
        "Crossref 的 review/systematic review/meta-analysis 类型"
        "未施加 type filter（Crossref 无对应细粒度 type）。"
        "各源命中数差异源自库本身覆盖范围不同（如 PMC 含更多预印本），"
        "非检索错误。"
    )
    L.append(">")
    L.append(
        f"> 检索共获得 {raw_total} 条原始记录，经去重后保留 {n_total} 篇。"
        f"全文获取采用三级回退策略，成功获取 {fulltext_total} 篇"
        f"（{_pct(fulltext_total, n_total)}%），"
        f"{abstract_only_count} 篇回退至摘要层。"
    )
    L.append("")
    L.append("---")
    L.append(f"**生成时间**: `{_now_iso()}`")
    L.append(f"**medlitflow 版本**: {_get_version()}")

    report_path.write_text("\n".join(L), encoding="utf-8")

def update_report_metrics_section(
    report_path: Path,
    coverage: dict,
    updated_at: str,
    total: int,
    filled_before: int,
    filled_after: int,
) -> None:
    report_path = Path(report_path)
    if not report_path.exists():
        return
    try:
        text = report_path.read_text(encoding="utf-8")
    except OSError:
        return

    start_marker = "<!-- METRICS_UPDATE_SECTION_START -->"
    end_marker = "<!-- METRICS_UPDATE_SECTION_END -->"

    n_total = int(coverage.get("total", 0) or 0)
    c = int(coverage.get("citation", 0) or 0)
    j = int(coverage.get("jcr", 0) or 0)
    a = int(coverage.get("authors", 0) or 0)
    i = int(coverage.get("institutions", 0) or 0)
    cc = int(coverage.get("country_codes", 0) or 0)
    ts_short = str(updated_at or "")[:16].replace("T", " ")

    new_body = (
        f"（截至 {ts_short}）\n"
        f"\n"
        f"| 指标 | 覆盖数 | 占比 |\n"
        f"|---|---|---|\n"
        f"| 有引用数据 | {c} | {_pct(c, n_total)}% |\n"
        f"| 有期刊指标（JCR） | {j} | {_pct(j, n_total)}% |\n"
        f"| 有作者信息 | {a} | {_pct(a, n_total)}% |\n"
        f"| 有机构信息 | {i} | {_pct(i, n_total)}% |\n"
        f"| 有国家代码 | {cc} | {_pct(cc, n_total)}% |\n"
        f"\n"
        f"**本次更新**：处理 {total} 条，字段覆盖 {filled_before} → "
        f"{filled_after}（增量 {filled_after - filled_before}）\n"
    )
    new_block = f"{start_marker}\n{new_body}{end_marker}"

    start_idx = text.find(start_marker)
    end_idx = text.find(end_marker)

    if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
        if not text.endswith("\n"):
            text += "\n"
        text += "\n" + new_block + "\n"
    else:
        text = (
            text[:start_idx]
            + new_block
            + text[end_idx + len(end_marker):]
        )

    tmp = report_path.with_name(report_path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, report_path)
    except OSError:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass