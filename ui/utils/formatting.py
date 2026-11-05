"""ui/utils/formatting.py — UI 层纯函数格式化工具。"""

from __future__ import annotations

import math
from typing import Any


SOURCE_VALUE_TO_DISPLAY = {
    "pubmed": "PubMed",
    "pmc": "PMC",
    "europepmc": "Europe PMC",
    "crossref": "Crossref",
    "openalex": "OpenAlex",
}


def first_author(authors: Any) -> str:
    if not authors:
        return ""
    parts = [p.strip() for p in str(authors).split(";") if p.strip()]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} 等"


def format_year(year: Any) -> str:
    if year is None:
        return ""
    if isinstance(year, float) and math.isnan(year):
        return ""
    text = str(year).strip()
    if text.lower() in ("nan", "none"):
        return ""
    if text.endswith(".0"):
        text = text[:-2]
    return text


def first_source(source_databases: Any) -> str:
    if not source_databases:
        return ""
    parts = [
        p.strip()
        for p in str(source_databases).split(";")
        if p.strip()
    ]
    return parts[0] if parts else ""


def all_sources(source_databases: Any) -> str:
    if not source_databases:
        return ""
    parts = [
        p.strip()
        for p in str(source_databases).split(";")
        if p.strip()
    ]
    if not parts:
        return ""
    return ", ".join(
        SOURCE_VALUE_TO_DISPLAY.get(p.lower(), p) for p in parts
    )


def format_strategies_short(strategy_names: Any) -> str:
    if not strategy_names:
        return "—"
    parts = [
        p.strip()
        for p in str(strategy_names).split(";")
        if p.strip()
    ]
    if not parts:
        return "—"
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} +{len(parts) - 1}"


def safe_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return 0