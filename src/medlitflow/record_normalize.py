"""record_normalize.py — 记录字段归一化的单一真相源。

公开接口见 __all__"""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any

__all__ = [
    "clean_text",
    "normalize_doi", "normalize_pmid", "normalize_pmcid", "normalize_title",
    "split_last_first",
    "normalize_authors_from_europepmc",
    "normalize_authors_from_crossref",
    "normalize_authors_from_openalex",
    "safe_str", "is_empty", "split_conflict_fields",
    "strategy_regex", "to_bool_series",
    "_KEYED_FIELDS", "_KEY_FIELD_MAP",
    "make_key", "normalize_record", "rebuild_keys",
    "make_field_strategies", "merge_field_by_strategy",
    "FIELD_STRATEGIES",
    "OVERWRITE_NEW_FIELDS", "RECOMPUTE_FIELDS",
]

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_DIGITS_RE = re.compile(r"\d+")

def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN
        return ""
    text = html_lib.unescape(str(value))
    text = _HTML_TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()

def normalize_doi(value: Any) -> str:
    text = clean_text(value).lower()
    for prefix in (
        "https://doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
        "http://dx.doi.org/",
        "doi:",
    ):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.strip().rstrip(".")

def normalize_pmid(value: Any) -> str:
    text = clean_text(value)
    match = _DIGITS_RE.search(text)
    return match.group(0) if match else ""


def normalize_pmcid(value: Any) -> str:
    text = clean_text(value).upper()
    if text.startswith("PMC"):
        text = text[3:]
    match = _DIGITS_RE.search(text)
    return f"PMC{match.group(0)}" if match else ""


def normalize_title(value: Any) -> str:
    text = clean_text(value).lower()
    text = _NON_ALNUM_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()

_NAME_PARTICLES = frozenset({
    "van", "von", "der", "den", "zu", "zum", "zur", "auf",
    "del", "della", "dos", "das", "do", "da",
    "de", "du", "des", "le", "la", "les",
    "di", "dei", "degli", "delle",
    "al", "el", "bin", "bint", "ibn", "abd",
})

_CHINESE_COMPOUND_SURNAMES = frozenset({
    "ouyang", "sima", "zhuge", "shangguan", "duanmu",
    "ximen", "dongfang", "huangfu", "weichi", "yuchi",
    "gongsun", "murong", "situ", "sikong", "nangong",
    "xiahou", "helan", "wanyan",
})

def split_last_first(name: str) -> tuple[str, str]:
    text = clean_text(name)
    if not text:
        return "", ""

    parts = text.split()
    if len(parts) == 1:
        return parts[0], ""

    first_word_lower = parts[0].lower().rstrip(".,")
    if first_word_lower in _CHINESE_COMPOUND_SURNAMES:
        return parts[0], " ".join(parts[1:])

    last_parts: list[str] = []
    i = 0
    while i < len(parts):
        word = parts[i]
        last_parts.append(word)
        i += 1
        if word.lower().rstrip(".,") in _NAME_PARTICLES:
            continue
        break

    last = " ".join(last_parts)
    first = " ".join(parts[i:])
    return last, first

def _reverse_first_last(display_name: str) -> str:
    text = clean_text(display_name)
    if not text:
        return ""
    parts = text.split()
    if len(parts) <= 1:
        return text

    i = len(parts) - 1
    last_parts = [parts[i]]
    i -= 1
    while i >= 0 and parts[i].lower().rstrip(".,") in _NAME_PARTICLES:
        last_parts.insert(0, parts[i])
        i -= 1

    first_parts = parts[:i + 1]
    return " ".join(last_parts + first_parts)

def _join_author_list(authors: list[str]) -> str:
    parts: list[str] = []
    for a in authors:
        text = clean_text(a)
        if text:
            parts.append(text)
    return "; ".join(parts)

def normalize_authors_from_europepmc(author_string: Any) -> str:
    """Europe PMC authorString "Smith J, Wang L" → "Smith J; Wang L"。"""
    text = clean_text(author_string)
    if not text:
        return ""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    return _join_author_list(parts)

def normalize_authors_from_crossref(item: dict) -> str:
    authors: list[str] = []
    for author in item.get("author", []) or []:
        given = clean_text(author.get("given"))
        family = clean_text(author.get("family"))
        if family and given:
            authors.append(f"{family} {given}")
        elif family:
            authors.append(family)
        elif given:
            authors.append(given)
    return _join_author_list(authors)

def normalize_authors_from_openalex(item: dict) -> str:
    authors: list[str] = []
    for authorship in item.get("authorships", []) or []:
        display = clean_text(
            (authorship.get("author") or {}).get("display_name")
        )
        if not display:
            continue
        authors.append(_reverse_first_last(display))
    return _join_author_list(authors)

def is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False

def safe_str(value: Any) -> str:
    if is_empty(value):
        return ""
    return clean_text(value)

def split_conflict_fields(s: str) -> set[str]:
    return {p.strip() for p in (s or "").split(";") if p.strip()}

def strategy_regex(strategy: str) -> str:
    return rf"(?:^|;)\s*{re.escape(strategy)}\s*(?:;|$)"


def to_bool_series(series: Any) -> Any:
    if series.dtype == bool:
        return series
    s = series.astype(str).str.strip().str.lower()
    return s.isin(["true", "1", "yes"]).astype(bool)

_KEYED_FIELDS: tuple[str, ...] = (
    "title", "authors", "journal", "doi", "year",
)

_KEY_FIELD_MAP: dict[str, str] = {
    "title": "title_key",
    "journal": "journal_key",
    "year": "year_key",
    "doi": "doi_key",
    "authors": "authors_key",
}

def make_key(display_value: Any, field: str) -> str:
    if field in ("title", "journal"):
        return normalize_title(display_value)
    if field == "doi":
        return normalize_doi(display_value)
    if field == "year":
        if is_empty(display_value):
            return ""
        try:
            return str(int(float(display_value)))
        except (ValueError, TypeError):
            return ""
    if field == "authors":
        text = safe_str(display_value)
        if not text:
            return ""
        parts = [
            normalize_title(a.strip())
            for a in text.split(";")
            if a.strip()
        ]
        parts = [p for p in parts if p]
        return "|".join(sorted(parts))
    return ""

def normalize_record(record: dict, source: str) -> dict:
    result = dict(record)
    for field in _KEYED_FIELDS:
        result[f"{field}_key"] = make_key(result.get(field), field)
    return result

def rebuild_keys(merged: dict) -> dict:
    result = dict(merged)
    for field in _KEYED_FIELDS:
        result[f"{field}_key"] = make_key(result.get(field), field)
    return result

OVERWRITE_NEW_FIELDS = frozenset({
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
    "last_seen_run", "matched_rows_count",
})

RECOMPUTE_FIELDS = frozenset({
    "has_conflict", "conflict_fields",
    "abstract_variant_count", "abstract_variant_status",
    "abstract_divergence_type", "abstract_missing",
    "matched_strategy_count",
})


def make_field_strategies() -> dict[str, str]:
    m: dict[str, str] = {}
    for f in ("dedup_rule", "first_seen_run", "primary_source"):
        m[f] = "keep_old"
    for f in ("doi", "pmid", "pmcid"):
        m[f] = "non_empty_prefer_old"
    for f in (
        "title", "authors", "journal", "year",
        "publication_type_raw", "publication_type_normalized",
        "publication_type_category", "abstract_final",
        "abstract_selection_rule",
        "fulltext_url", "pdf_url", "landing_page_url",
    ):
        m[f] = "keep_old_if_non_empty"
    for f in OVERWRITE_NEW_FIELDS:
        m[f] = "overwrite_new"
    for f in ("strategy_names", "source_databases", "source_record_ids"):
        m[f] = "union_semicolon"
    m["search_queries"] = "union_doublepipe"
    for f in ("candidate_urls_json", "licenses_json"):
        m[f] = "union_json"
    m["abstract_variants_json"] = "union_json_by_key"
    m["open_access_flag"] = "or_bool"
    m["matched_sources_count"] = "max"
    m["appearance_count"] = "increment"
    for f in RECOMPUTE_FIELDS:
        m[f] = "recompute"
    m["data_origin"] = "overwrite_new"
    m["import_tags_json"] = "union_json"
    return m

FIELD_STRATEGIES: dict[str, str] = make_field_strategies()

def _merge_semicolon(old_val: Any, new_val: Any) -> str:
    items: list[str] = []
    seen: set[str] = set()
    for val in (old_val, new_val):
        text = safe_str(val)
        if not text:
            continue
        for item in text.split(";"):
            item = item.strip()
            if item and item not in seen:
                seen.add(item)
                items.append(item)
    return "; ".join(items)

def _merge_doublepipe(old_val: Any, new_val: Any) -> str:
    items: list[str] = []
    seen: set[str] = set()
    for val in (old_val, new_val):
        text = safe_str(val)
        if not text:
            continue
        for item in text.split("||"):
            item = item.strip()
            if item and item not in seen:
                seen.add(item)
                items.append(item)
    return " || ".join(items)

def _parse_json_list(value: Any) -> list:
    if is_empty(value):
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []

def _merge_json(
    old_val: Any, new_val: Any, dedup_key: str | None = None,
) -> str:
    old_list = _parse_json_list(old_val)
    new_list = _parse_json_list(new_val)
    result: list = []
    seen: set[str] = set()
    for item in old_list + new_list:
        if dedup_key and isinstance(item, dict):
            key_val = item.get(dedup_key)
            if key_val is None or key_val == "":
                result.append(item)
                continue
            marker = f"{dedup_key}:{key_val}"
        else:
            marker = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return json.dumps(result, ensure_ascii=False)

def _increment_count(old_val: Any) -> int:
    n = _safe_int(old_val)
    if n is None:
        return 1
    return n + 1

def _max_int(old_val: Any, new_val: Any) -> int:
    a = _safe_int(old_val) or 0
    b = _safe_int(new_val) or 0
    return max(a, b)

def _safe_int(value: Any) -> int | None:
    if is_empty(value):
        return None
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None

def merge_field_by_strategy(
    field: str,
    old_val: Any,
    new_val: Any,
    strategies: dict[str, str] | None = None,
) -> Any:
    strat = (strategies or FIELD_STRATEGIES).get(
        field, "keep_old_if_non_empty",
    )
    if strat in ("keep_old", "keep_old_if_non_empty", "non_empty_prefer_old"):
        return old_val if not is_empty(old_val) else new_val
    if strat == "overwrite_new":
        return new_val
    if strat == "union_semicolon":
        return _merge_semicolon(old_val, new_val)
    if strat == "union_doublepipe":
        return _merge_doublepipe(old_val, new_val)
    if strat == "union_json":
        return _merge_json(old_val, new_val)
    if strat == "union_json_by_key":
        return _merge_json(old_val, new_val, dedup_key="content_hash")
    if strat == "or_bool":
        return bool(old_val) or bool(new_val)
    if strat == "max":
        return _max_int(old_val, new_val)
    if strat == "increment":
        return old_val
    if strat == "recompute":
        if field == "has_conflict":
            return bool(old_val) or bool(new_val)
        if field == "conflict_fields":
            return _merge_semicolon(old_val, new_val)
        return old_val
    return new_val