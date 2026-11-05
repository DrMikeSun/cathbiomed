"""enrichment.py — 增强层。

输出 8 张表：entity_papers / entity_authors / entity_institutions /
entity_journals / relation_cites / relation_authored / relation_affiliated /
relation_published_in """

from __future__ import annotations

import difflib
import hashlib
import json
import os
import time
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from medlitflow import core, record_normalize, robustness

__all__ = [
    "enrich_all",
    "fetch_paper_batch",
    "fetch_citations",
    "fetch_affiliations",
    "fetch_journal_metrics",
    "request_interrupt",
    "trigger_citation_enrichment_for_confirmed_strategies",
    "refresh_metrics_for_cids",
]

def _emit_progress(stage: str, current: int, total: int) -> None:
    try:
        from medlitflow import pipeline
        pipeline.get_interactor().progress(stage, current, total)
    except Exception:  # noqa: BLE001
        pass

def _emit_log(message: str) -> None:
    try:
        from medlitflow import pipeline
        pipeline.get_interactor().log(message)
    except Exception:  # noqa: BLE001
        pass

_S2_BASE = "https://api.semanticscholar.org/graph/v1"
_OPENALEX_BASE = "https://api.openalex.org"
_NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

_DEFAULT_TIMEOUT = 45
_S2_BATCH_MAX = 500

_ENTITY_PAPERS_COLUMNS = [
    "canonical_id", "title", "year", "doi", "abstract_final",
    "citation_count", "reference_count", "influential_citation_count",
    "metrics_status",
]

_ENTITY_AUTHORS_COLUMNS = [
    "author_id", "name", "h_index", "paper_count",
    "citation_count", "orcid", "affiliations_raw",
]

_ENTITY_INSTITUTIONS_COLUMNS = [
    "institution_id", "name", "ror_id",
    "country_code", "type", "lineage_json",
]

_ENTITY_JOURNALS_COLUMNS = [
    "journal_id", "journal_name", "journal_abbr",
    "index_type", "major_subject",
    "journal_impact_factor", "journal_jcr_quartile",
]

_RELATION_CITES_COLUMNS = [
    "citing_canonical_id", "cited_canonical_id",
    "is_influential", "citation_intents_json",
    "citation_source_used",
]

_RELATION_AUTHORED_COLUMNS = [
    "canonical_id", "author_id", "author_position",
]

_RELATION_AFFILIATED_COLUMNS = [
    "author_id", "institution_id",
    "raw_affiliation_string", "years_json",
]

_RELATION_PUBLISHED_IN_COLUMNS = [
    "canonical_id", "journal_id", "volume", "pages", "year",
]

_CITATION_PROCESSED_COLUMNS = [
    "canonical_id", "strategy_name",
    "references_fetched_at", "citations_fetched_at",
    "last_citations_count", "citation_source_used",
]

_CITATION_SOURCE_REGISTRY_COLUMNS = [
    "current_source", "is_degraded",
    "degraded_since", "last_source_check_at",
]

_AFFILIATION_WIDE_COLUMNS = _RELATION_AFFILIATED_COLUMNS + [
    "institution_name", "institution_ror", "institution_country",
    "institution_type", "institution_lineage_json",
]

_JCR_COLUMN_ALIASES: dict[str, list[str]] = {
    "Journal Name": ["Journal Name", "journal_name", "Journal", "Full Journal Title"],
    "Abbreviation": ["Abbreviation", "abbr", "Journal Abbreviation"],
    "Index": ["Index", "index_type"],
    "Major Subject": ["Major Subject", "subject", "category"],
    "Impact Factor": ["Impact Factor", "IF", "JIF", "impact_factor"],
    "Q category": ["Q category", "quartile", "Q"],
}

_JCR_TABLE_CACHE: dict[tuple[str, float], tuple[pd.DataFrame, dict]] = {}

def _filter_noise_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return df
    if "special_status" not in df.columns:
        return df
    noise_mask = (
        df["special_status"].fillna("").astype(str).str.strip()
        == "retrieval_noise"
    )
    if not noise_mask.any():
        return df
    return df[~noise_mask].copy()

def request_interrupt() -> None:
    robustness.request_shutdown(detail="enrichment 请求中断")

def _should_interrupt() -> bool:
    return robustness.is_shutdown_requested()

def _now_iso() -> str:
    return core.now_iso()

def _parse_date(value: str) -> date:
    if not value:
        return date(1970, 1, 1)
    try:
        if "T" in value:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return date(1970, 1, 1)

def _library_root(config: dict) -> Path:
    return Path(config["library"]["root"])

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
    path = Path(path)
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


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if isinstance(value, float) and value != value:
            return None
        return int(value)
    except (ValueError, TypeError):
        return None

_to_bool_series = record_normalize.to_bool_series

def _compute_author_id(name: str) -> str:
    norm = core.normalize_title(name) or name.lower().strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]

def _compute_journal_id(name: str) -> str:
    norm = core.normalize_title(name) or name.lower().strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _external_ids_to_canonical_id(paper: dict, ext_ids: dict) -> str:
    doi = core.normalize_doi(ext_ids.get("DOI") or paper.get("doi"))
    if doi:
        return f"doi:{doi}"
    pmid = core.normalize_pmid(ext_ids.get("PubMed"))
    if pmid:
        return f"pmid:{pmid}"
    pmcid = core.normalize_pmcid(ext_ids.get("PubMedCentral"))
    if pmcid:
        return f"pmcid:{pmcid}"
    title = core.clean_text(paper.get("title", ""))
    if title:
        digest = hashlib.sha256(
            core.normalize_title(title).encode("utf-8")
        ).hexdigest()[:16]
        return f"title:{digest}"
    return ""

def _check_source(source: str, config: dict) -> bool:
    """探测指定数据源是否可用。
    source ∈ {semantic_scholar, openalex, pubmed}
    使用固定锚点文献（Hanahan & Weinberg 2011），通过直接 ID 查找。
    锚点从 config 读取：pmid/doi/title_keyword。
    断言：HTTP 200 + JSON 可解析 + 标题含 title_keyword
    """
    probe_cfg = config["enrichment"]["citation"]["source_probe"]
    anchor_cfg = probe_cfg["anchor"]
    attempts = int(probe_cfg["attempts"])
    interval = float(probe_cfg["attempt_interval_seconds"])

    keyword = anchor_cfg["title_keyword"].lower()
    doi = anchor_cfg["doi"]
    pmid = anchor_cfg["pmid"]

    api_cfg = config.get("api", {}) or {}
    email = core.clean_text(api_cfg.get("email", ""))
    ncbi_api_key = core.clean_text(api_cfg.get("ncbi_api_key", ""))
    s2_api_key = core.clean_text(api_cfg.get("s2_api_key", ""))
    openalex_api_key = core.clean_text(api_cfg.get("openalex_api_key", ""))

    for attempt in range(1, attempts + 1):
        ok = False
        try:
            if source == "semantic_scholar":
                url = f"{_S2_BASE}/paper/DOI:{doi}"
                params = {"fields": "title,year"}
                headers: dict = {}
                if s2_api_key:
                    headers["x-api-key"] = s2_api_key
                resp = core.http_get_json(url, params=params, headers=headers, delay=1.0)
                title = (resp.get("title") or "").lower()
                ok = keyword in title
            elif source == "openalex":
                url = f"{_OPENALEX_BASE}/works/doi:{doi}"
                params: dict = {}
                if email:
                    params["mailto"] = email
                if openalex_api_key:
                    params["api_key"] = openalex_api_key
                resp = core.http_get_json(url, params=params, headers={}, delay=0.5)
                title = (resp.get("title") or "").lower()
                ok = keyword in title
            elif source == "pubmed":
                url = f"{_NCBI_BASE}/esummary.fcgi"
                params = {"db": "pubmed", "id": pmid, "retmode": "json"}
                if email:
                    params["email"] = email
                if ncbi_api_key:
                    params["api_key"] = ncbi_api_key
                resp = core.http_get_json(url, params=params, headers={}, delay=0.34)
                result = (resp.get("result") or {}).get(pmid, {})
                title = (result.get("title") or "").lower()
                ok = keyword in title
        except robustness.InterruptRequested:
            raise
        except Exception:  # noqa: BLE001
            ok = False

        if ok:
            return True
        if attempt < attempts:
            core.interruptible_sleep(interval)

    return False

def _ensure_global_source(library_root: Path, config: dict) -> None:
    registry_path = library_root / "citation_source_registry.parquet"
    cooldown_hours = float(
        config["enrichment"]["citation"]["source_probe"]["retry_cooldown_hours"]
    )

    need_probe = True
    if registry_path.exists():
        try:
            existing = pd.read_parquet(registry_path)
            if len(existing) > 0:
                row = existing.iloc[0]
                is_degraded = bool(row.get("is_degraded", False))
                last_check_str = str(row.get("last_source_check_at", ""))

                if is_degraded and last_check_str:
                    try:
                        last_check = datetime.fromisoformat(
                            last_check_str.replace("Z", "+00:00")
                        )
                        if last_check.tzinfo is None:
                            last_check = last_check.replace(tzinfo=timezone.utc)
                        now = datetime.now(timezone.utc)
                        delta_hours = (now - last_check).total_seconds() / 3600
                        if delta_hours < cooldown_hours:
                            need_probe = False
                    except ValueError:
                        need_probe = True
        except Exception:  # noqa: BLE001
            need_probe = True

    if not need_probe:
        return

    now = _now_iso()

    if _check_source("semantic_scholar", config):
        new_row = {
            "current_source": "semantic_scholar",
            "is_degraded": False,
            "degraded_since": "",
            "last_source_check_at": now,
        }
    else:
        fallback_sources = config["enrichment"]["citation"]["fallback_sources"]
        current_source = ""
        for src in fallback_sources:
            if _check_source(src, config):
                current_source = src
                break

        degraded_since = now
        if registry_path.exists():
            try:
                existing = pd.read_parquet(registry_path)
                if len(existing) > 0:
                    prev = str(existing.iloc[0].get("degraded_since", ""))
                    if prev:
                        degraded_since = prev
            except Exception:  # noqa: BLE001
                pass

        new_row = {
            "current_source": current_source,
            "is_degraded": True,
            "degraded_since": degraded_since,
            "last_source_check_at": now,
        }

    _save_table(
        pd.DataFrame([new_row], columns=_CITATION_SOURCE_REGISTRY_COLUMNS),
        registry_path,
    )

def _get_global_source(library_root: Path) -> tuple[str, bool]:
    registry_path = library_root / "citation_source_registry.parquet"
    if not registry_path.exists():
        return "", True
    try:
        df = pd.read_parquet(registry_path)
        if len(df) == 0:
            return "", True
        row = df.iloc[0]
        return (
            str(row.get("current_source", "") or ""),
            bool(row.get("is_degraded", True)),
        )
    except Exception:  # noqa: BLE001
        return "", True

def _collect_to_process(
    include_cids: list[str],
    library_root: Path,
    config: dict,
) -> list[tuple[str, str]]:
    current_source, is_degraded = _get_global_source(library_root)

    if not current_source:
        return []

    processed_path = library_root / "citation_processed.parquet"
    processed_df = _load_table(processed_path, _CITATION_PROCESSED_COLUMNS)
    processed_map: dict[str, dict] = {}
    for _, row in processed_df.iterrows():
        cid = core.clean_text(row.get("canonical_id", ""))
        if cid:
            processed_map[cid] = row.to_dict()

    today = datetime.now().date()
    refresh_days = int(
        config["enrichment"]["citation"]["citations_refresh_days"]
    )

    to_process: list[tuple[str, str]] = []
    for cid in include_cids:
        rec = processed_map.get(cid)

        if rec is None:
            to_process.append((cid, "initial"))
            continue

        if rec.get("citation_source_used") == "semantic_scholar":
            if not is_degraded:
                days = (
                    today - _parse_date(str(rec.get("citations_fetched_at", "")))
                ).days
                if days >= refresh_days:
                    to_process.append((cid, "citations_refresh"))
            continue

        if not is_degraded and current_source == "semantic_scholar":
            to_process.append((cid, "source_upgrade"))
            continue
        continue

    to_process.sort(key=lambda x: x[0])
    return to_process

_S2_CITATIONS_PAGE_SIZE = 1000
_S2_CITATIONS_MAX_PAGES_DEFAULT = 2

def _s2_fetch_citations(
    doi: str,
    config: dict,
    pmid: str = "",
) -> pd.DataFrame:
    if doi:
        paper_id = f"DOI:{doi}"
        cited_cid = f"doi:{doi}"
    elif pmid:
        paper_id = f"PMID:{pmid}"
        cited_cid = f"pmid:{pmid}"
    else:
        return pd.DataFrame(columns=_RELATION_CITES_COLUMNS)

    url = f"{_S2_BASE}/paper/{paper_id}/citations"
    delay = float(config.get("rate_limit", {}).get("semantic_scholar", 3.0))
    max_pages = int(
        config.get("enrichment", {})
              .get("citation", {})
              .get("s2_citations_max_pages", _S2_CITATIONS_MAX_PAGES_DEFAULT)
    )

    all_items: list[dict] = []
    for page in range(max_pages):
        offset = page * _S2_CITATIONS_PAGE_SIZE
        params = {
            "fields": "title,authors,year,externalIds,intents,isInfluential",
            "limit": _S2_CITATIONS_PAGE_SIZE,
            "offset": offset,
        }
        try:
            resp = core.http_get_json(
                url, params=params, headers={}, delay=delay,
            )
        except robustness.InterruptRequested:
            raise
        except Exception:  # noqa: BLE001
            break
        data = resp.get("data", []) or []
        if not data:
            break
        all_items.extend(data)
        if len(data) < _S2_CITATIONS_PAGE_SIZE:
            break

    rows: list[dict] = []
    for item in all_items:
        citing_paper = item.get("citingPaper") or {}
        if not citing_paper:
            continue
        ext_ids = citing_paper.get("externalIds") or {}
        citing_cid = _external_ids_to_canonical_id(citing_paper, ext_ids)
        if not citing_cid:
            continue
        rows.append({
            "citing_canonical_id": citing_cid,
            "cited_canonical_id": cited_cid,
            "is_influential": bool(item.get("isInfluential", False)),
            "citation_intents_json": json.dumps(
                item.get("intents", []) or [], ensure_ascii=False
            ),
            "citation_source_used": "semantic_scholar",
        })

    return pd.DataFrame(rows, columns=_RELATION_CITES_COLUMNS)

def _openalex_fetch_citations(
    doi: str,
    config: dict,
    pmid: str = "",
) -> pd.DataFrame:
    if doi:
        lookup_url = f"{_OPENALEX_BASE}/works/doi:{doi}"
        cited_cid = f"doi:{doi}"
    elif pmid:
        lookup_url = f"{_OPENALEX_BASE}/works/pmid:{pmid}"
        cited_cid = f"pmid:{pmid}"
    else:
        return pd.DataFrame(columns=_RELATION_CITES_COLUMNS)

    mailto = config.get("api", {}).get("email", "")
    delay = float(config.get("rate_limit", {}).get("openalex", 0.2))

    try:
        params_lookup: dict = {}
        if mailto:
            params_lookup["mailto"] = mailto
        work = core.http_get_json(
            lookup_url, params=params_lookup, headers={}, delay=delay,
        )
    except robustness.InterruptRequested:
        raise
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=_RELATION_CITES_COLUMNS)

    work_url = work.get("id", "")
    if not work_url:
        return pd.DataFrame(columns=_RELATION_CITES_COLUMNS)

    w_id = work_url.rsplit("/", 1)[-1]

    try:
        url = f"{_OPENALEX_BASE}/works"
        params: dict = {"filter": f"cites:{w_id}", "per-page": 200}
        if mailto:
            params["mailto"] = mailto
        resp = core.http_get_json(url, params=params, headers={}, delay=delay)
    except robustness.InterruptRequested:
        raise
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=_RELATION_CITES_COLUMNS)

    results = resp.get("results", []) or []
    rows: list[dict] = []

    for item in results:
        ext_ids: dict = {}
        ids = item.get("ids") or {}
        if ids.get("doi"):
            ext_ids["DOI"] = str(ids["doi"]).replace("https://doi.org/", "")
        if ids.get("pmid"):
            ext_ids["PubMed"] = str(ids["pmid"]).rstrip("/").rsplit("/", 1)[-1]
        citing_cid = _external_ids_to_canonical_id(item, ext_ids)
        if not citing_cid:
            continue
        rows.append({
            "citing_canonical_id": citing_cid,
            "cited_canonical_id": cited_cid,
            "is_influential": False,
            "citation_intents_json": "[]",
            "citation_source_used": "openalex",
        })

    return pd.DataFrame(rows, columns=_RELATION_CITES_COLUMNS)

def _pubmed_fetch_citations(pmid: str, config: dict) -> pd.DataFrame:
    if not pmid:
        return pd.DataFrame(columns=_RELATION_CITES_COLUMNS)

    url = f"{_NCBI_BASE}/elink.fcgi"
    params = {
        "dbfrom": "pubmed",
        "db": "pubmed",
        "id": pmid,
        "cmd": "neighbor",
        "linkname": "pubmed_pubmed_citedin",
        "retmode": "json",
    }
    email = config.get("api", {}).get("email", "")
    if email:
        params["email"] = email
    api_key = config.get("api", {}).get("ncbi_api_key", "")
    if api_key:
        params["api_key"] = api_key
    delay = float(config.get("rate_limit", {}).get("pubmed", 0.34))

    try:
        resp = core.http_get_json(url, params=params, headers={}, delay=delay)
    except robustness.InterruptRequested:
        raise
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=_RELATION_CITES_COLUMNS)

    rows: list[dict] = []
    cited_cid = f"pmid:{pmid}"
    for ls in resp.get("linksets", []) or []:
        for lsdb in ls.get("linksetdbs", []) or []:
            if lsdb.get("linkname") == "pubmed_pubmed_citedin":
                for citing_pmid in lsdb.get("links", []) or []:
                    rows.append({
                        "citing_canonical_id": f"pmid:{citing_pmid}",
                        "cited_canonical_id": cited_cid,
                        "is_influential": False,
                        "citation_intents_json": "[]",
                        "citation_source_used": "pubmed",
                    })

    return pd.DataFrame(rows, columns=_RELATION_CITES_COLUMNS)

def _fetch_citations_from_source(
    source: str,
    doi: str,
    pmid: str,
    config: dict,
) -> pd.DataFrame:
    if source == "semantic_scholar":
        return _s2_fetch_citations(doi, config, pmid)
    if source == "openalex":
        return _openalex_fetch_citations(doi, config, pmid)
    if source == "pubmed":
        return _pubmed_fetch_citations(pmid, config)
    return pd.DataFrame(columns=_RELATION_CITES_COLUMNS)

_CITATION_FLUSH_INTERVAL = 50

def _write_citation_results_batch(
    batch: list[tuple[str, pd.DataFrame]],
    library_root: Path,
) -> None:
    if not batch:
        return
    relation_path = library_root / "relations" / "relation_cites.parquet"
    relation_path.parent.mkdir(parents=True, exist_ok=True)

    if relation_path.exists():
        try:
            existing = pd.read_parquet(relation_path)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"relation_cites 读取失败，已中止写回：{relation_path}"
                f"（{type(exc).__name__}: {exc}）"
            ) from exc
        for c in _RELATION_CITES_COLUMNS:
            if c not in existing.columns:
                existing[c] = ""
        existing = existing[_RELATION_CITES_COLUMNS].copy()
    else:
        existing = pd.DataFrame(columns=_RELATION_CITES_COLUMNS)

    cited_cids = {cid for cid, _ in batch}
    existing = existing[~existing["cited_canonical_id"].isin(cited_cids)]

    non_empty = [df for _, df in batch if df is not None and len(df) > 0]
    if non_empty:
        merged = pd.concat([existing, *non_empty], ignore_index=True)
    else:
        merged = existing

    tmp_path = relation_path.with_suffix(".parquet.tmp")
    merged.to_parquet(tmp_path, index=False)
    os.replace(tmp_path, relation_path)

def _append_citation_log(library_root: Path, entry: dict) -> None:
    library_root.mkdir(parents=True, exist_ok=True)
    log_path = library_root / "enrich_citation_log.jsonl"
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass

def _run_citation_enrichment(
    to_process: list[tuple[str, str]],
    library_root: Path,
    config: dict,
) -> list[str]:
    if not to_process:
        return []

    current_source, _ = _get_global_source(library_root)
    if not current_source:
        return []

    records_map: dict[str, dict] = {}
    records_path = library_root / "records_final.parquet"
    if records_path.exists():
        try:
            records = pd.read_parquet(records_path)
            for _, row in records.iterrows():
                cid = core.clean_text(row.get("canonical_id", ""))
                if cid:
                    records_map[cid] = row.to_dict()
        except Exception:  # noqa: BLE001
            pass

    processed_path = library_root / "citation_processed.parquet"
    processed_df = _load_table(processed_path, _CITATION_PROCESSED_COLUMNS)
    processed_map: dict[str, int] = {}
    for idx, row in processed_df.iterrows():
        cid = core.clean_text(row.get("canonical_id", ""))
        if cid:
            processed_map[cid] = int(idx)

    now = _now_iso()
    successfully_processed: list[str] = []
    pending_writes: list[tuple[str, pd.DataFrame]] = []
    pending_processed: list[dict] = []

    def _flush() -> None:
        nonlocal processed_df, pending_writes, pending_processed
        if not pending_writes:
            return
        try:
            _write_citation_results_batch(pending_writes, library_root)
        except Exception:  # noqa: BLE001
            pending_writes = []
            pending_processed = []
            return
        for entry in pending_processed:
            cid = entry["canonical_id"]
            if cid in processed_map:
                idx = processed_map[cid]
                for k, v in entry.items():
                    processed_df.at[idx, k] = v
            else:
                processed_df = pd.concat(
                    [processed_df, pd.DataFrame([entry])],
                    ignore_index=True,
                )
                processed_map[cid] = len(processed_df) - 1
            successfully_processed.append(cid)
        pending_writes = []
        pending_processed = []

    for cid, reason in to_process:
        robustness.check_shutdown()

        rec = records_map.get(cid, {})
        doi = core.normalize_doi(rec.get("doi", ""))
        pmid = core.normalize_pmid(rec.get("pmid", ""))

        if not doi and cid.startswith("doi:"):
            doi = cid[4:]
        if not pmid and cid.startswith("pmid:"):
            pmid = cid[5:]

        try:
            edges = _fetch_citations_from_source(
                current_source, doi, pmid, config,
            )
        except robustness.InterruptRequested:
            _flush()
            if successfully_processed:
                _save_table(processed_df, processed_path)
            raise
        except Exception:  # noqa: BLE001
            continue

        strategy_names_str = str(rec.get("strategy_names", "") or "")
        first_strategy = (
            strategy_names_str.split(";")[0].strip()
            if strategy_names_str else ""
        )
        new_entry = {
            "canonical_id": cid,
            "strategy_name": first_strategy,
            "references_fetched_at": now,
            "citations_fetched_at": now,
            "last_citations_count": int(len(edges)),
            "citation_source_used": current_source,
        }
        pending_writes.append((cid, edges))
        pending_processed.append(new_entry)

        if len(pending_writes) >= _CITATION_FLUSH_INTERVAL:
            _flush()

    _flush()
    if successfully_processed:
        _save_table(processed_df, processed_path)

    return successfully_processed

def _list_confirmed_strategies(library_root: Path) -> list[str]:
    registry_path = library_root.parent / "archive" / "strategy_registry.parquet"
    if not registry_path.exists():
        return []
    try:
        registry = pd.read_parquet(registry_path)
    except Exception:  # noqa: BLE001
        return []
    if "strategy_status" not in registry.columns:
        return []
    confirmed = registry[registry["strategy_status"] == "confirmed"]
    return sorted(confirmed["strategy_name"].astype(str).tolist())

def _get_included_and_maybe_cids(
    strategy_name: str,
    library_root: Path,
) -> list[str]:
    records_path = library_root / "records_final.parquet"
    if not records_path.exists():
        return []

    try:
        records = pd.read_parquet(records_path)
    except Exception:  # noqa: BLE001
        return []

    if "strategy_names" not in records.columns:
        return []

    def _matches(s: Any) -> bool:
        if not isinstance(s, str) or not s:
            return False
        return strategy_name in [n.strip() for n in s.split(";")]

    mask = records["strategy_names"].apply(_matches)

    if "special_status" in records.columns:
        noise_mask = (
            records["special_status"].fillna("").astype(str).str.strip()
            == "retrieval_noise"
        )
        mask = mask & ~noise_mask

    candidate_cids = set(records.loc[mask, "canonical_id"].astype(str).tolist())

    decisions_path = library_root / "screening_decisions.parquet"
    if not decisions_path.exists():
        return sorted(candidate_cids)

    try:
        decisions = pd.read_parquet(decisions_path)
    except Exception:  # noqa: BLE001
        return sorted(candidate_cids)

    if len(decisions) == 0 or "user_decision" not in decisions.columns:
        return sorted(candidate_cids)

    user_decision = decisions["user_decision"].astype(str)
    include_maybe = set(
        decisions.loc[
            user_decision.isin(["include", "maybe"]),
            "canonical_id",
        ].astype(str).tolist()
    )

    return sorted(candidate_cids & include_maybe)

def _trigger_citation_enrichment_for_confirmed_strategies(
    library_root: Path,
    config: dict,
) -> None:
    _ensure_global_source(library_root, config)

    current_source, _ = _get_global_source(library_root)
    if not current_source:
        return

    remaining_quota = int(
        config["enrichment"]["citation"]["max_records_per_run"]
    )

    for strategy_name in _list_confirmed_strategies(library_root):
        if remaining_quota <= 0:
            break
        if _should_interrupt():
            break

        cids = _get_included_and_maybe_cids(strategy_name, library_root)
        to_process_full = _collect_to_process(cids, library_root, config)

        if not to_process_full:
            continue

        to_process = to_process_full[:remaining_quota]
        is_truncated = len(to_process_full) > len(to_process)

        processed = _run_citation_enrichment(to_process, library_root, config)
        remaining_quota -= len(processed)

        if _should_interrupt():
            event = "interrupted"
        elif len(processed) == len(to_process):
            event = "completed"
        else:
            event = "partial"

        _append_citation_log(library_root, {
            "run_at": _now_iso(),
            "event": event,
            "strategy_name": strategy_name,
            "processed_count": len(processed),
            "total_pending": len(to_process_full),
            "is_complete": (not is_truncated) and len(processed) == len(to_process_full),
            "is_truncated_by_quota": is_truncated,
            "current_source": current_source,
        })

def trigger_citation_enrichment_for_confirmed_strategies(
    library_root: Path,
    config: dict,
) -> None:
    return _trigger_citation_enrichment_for_confirmed_strategies(
        library_root, config,
    )

def _post_json_with_audit(
    url: str,
    params: dict,
    body: dict,
    headers: dict,
    delay: float,
    retries: int = 3,
) -> dict | None:
    request_headers = {"User-Agent": "medlitflow/0.1", "Accept": "application/json"}
    if headers:
        request_headers.update(headers)

    for attempt in range(1, retries + 1):
        start = time.monotonic()
        try:
            with httpx.Client(timeout=_DEFAULT_TIMEOUT) as client:
                resp = client.post(
                    url, params=params, json=body, headers=request_headers,
                )
            elapsed_ms = int((time.monotonic() - start) * 1000)

            core.append_http_audit({
                "url": url,
                "method": "POST",
                "status_code": resp.status_code,
                "elapsed_ms": elapsed_ms,
                "retry_count": attempt - 1,
                "timestamp": _now_iso(),
            })

            if resp.status_code in (429, 503):
                if attempt < retries:
                    wait_raw = resp.headers.get("Retry-After")
                    try:
                        wait = float(wait_raw) if wait_raw else 2 ** (attempt - 1)
                    except ValueError:
                        wait = 2 ** (attempt - 1)
                    wait = min(wait, core.RETRY_AFTER_MAX_SECONDS)
                    core.interruptible_sleep(wait)
                    continue
                return None

            if resp.status_code >= 500:
                if attempt < retries:
                    core.interruptible_sleep(2 ** (attempt - 1))
                    continue
                return None

            resp.raise_for_status()
            core.interruptible_sleep(delay)
            return resp.json()
        except httpx.HTTPError:
            if attempt < retries:
                core.interruptible_sleep(2 ** (attempt - 1))
                continue
            return None
    return None

def fetch_paper_batch(
    dois: list[str],
    fields: str,
    config: dict,
) -> dict:
    if not dois:
        return {}

    if len(dois) > _S2_BATCH_MAX:
        dois = dois[:_S2_BATCH_MAX]

    url = f"{_S2_BASE}/paper/batch"
    params = {"fields": fields}
    body = {"ids": [f"DOI:{d}" for d in dois]}

    api_key = config.get("api", {}).get("s2_api_key", "")
    headers: dict = {}
    if api_key:
        headers["x-api-key"] = api_key

    delay = float(config.get("rate_limit", {}).get("semantic_scholar", 3.0))

    data = _post_json_with_audit(url, params, body, headers, delay)
    if not isinstance(data, list):
        core.append_http_audit({
            "url": url,
            "method": "POST",
            "status_code": -2,
            "elapsed_ms": 0,
            "retry_count": 0,
            "timestamp": _now_iso(),
            "note": (
                f"S2 batch 返回非 list（{type(data).__name__}），"
                f"可能 429 或服务端异常"
            ),
        })
        return {}

    result: dict = {}
    for doi, item in zip(dois, data):
        if item:
            result[doi] = item
    return result

def fetch_citations(
    canonical_id: str,
    doi: str,
    config: dict,
) -> pd.DataFrame:
    library_root = _library_root(config)
    current_source, _ = _get_global_source(library_root)
    if not current_source:
        current_source = "semantic_scholar"
    return _fetch_citations_from_source(current_source, doi, "", config)

def fetch_affiliations(dois: list[str], config: dict) -> pd.DataFrame:
    if not dois:
        return pd.DataFrame(columns=_AFFILIATION_WIDE_COLUMNS)

    mailto = config.get("api", {}).get("email", "")
    delay = float(config.get("rate_limit", {}).get("openalex", 0.2))
    max_workers = int(
        config.get("enrichment", {})
              .get("openalex_concurrency", 4)
    )

    valid_dois = [d for d in dois if d]

    def _fetch_one(doi: str) -> list[dict]:
        local_rows: list[dict] = []
        try:
            url = f"{_OPENALEX_BASE}/works/doi:{doi}"
            params: dict = {}
            if mailto:
                params["mailto"] = mailto
            resp = core.http_get_json(
                url, params=params, headers={}, delay=delay,
            )
        except robustness.InterruptRequested:
            raise
        except Exception:  # noqa: BLE001
            return local_rows

        for authorship in resp.get("authorships", []) or []:
            author = authorship.get("author") or {}
            author_name = core.clean_text(author.get("display_name", ""))
            if not author_name:
                continue
            author_id = _compute_author_id(author_name)
            raw_aff_str = "; ".join(
                authorship.get("raw_affiliation_strings", []) or []
            )
            for inst in authorship.get("institutions", []) or []:
                inst_url = core.clean_text(inst.get("id", ""))
                if not inst_url:
                    continue
                inst_short_id = inst_url.rsplit("/", 1)[-1]
                local_rows.append({
                    "author_id": author_id,
                    "institution_id": inst_short_id,
                    "raw_affiliation_string": raw_aff_str,
                    "years_json": "[]",
                    "institution_name": core.clean_text(inst.get("display_name", "")),
                    "institution_ror": core.clean_text(inst.get("ror", "")),
                    "institution_country": core.clean_text(inst.get("country_code", "")),
                    "institution_type": core.clean_text(inst.get("type", "")),
                    "institution_lineage_json": json.dumps(
                        inst.get("lineage", []) or [], ensure_ascii=False,
                    ),
                })
        return local_rows

    rows: list[dict] = []
    total = len(valid_dois)
    completed = 0

    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {
            executor.submit(_fetch_one, doi): doi
            for doi in valid_dois
        }
        for fut in as_completed(futures):
            robustness.check_shutdown()
            try:
                rows.extend(fut.result())
            except robustness.InterruptRequested:
                raise
            except Exception:  # noqa: BLE001
                pass
            completed += 1
            if completed % 50 == 0 or completed == total:
                _emit_log(f"enrich: OpenAlex 机构进度 {completed}/{total}")
                _emit_progress("enrich_openalex", completed, total)
    finally:
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)

    return pd.DataFrame(rows, columns=_AFFILIATION_WIDE_COLUMNS)

def _normalize_jcr_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns=lambda c: str(c).strip() if isinstance(c, str) else c)

    rename_map: dict[str, str] = {}
    for standard, aliases in _JCR_COLUMN_ALIASES.items():
        aliases_stripped = {a.strip() for a in aliases}
        for col in df.columns:
            if str(col).strip() in aliases_stripped:
                rename_map[col] = standard
                break
    return df.rename(columns=rename_map)

def _parse_jcr_table(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=str, keep_default_na=False) 
    elif suffix == ".csv":
        df = pd.read_csv(path, dtype=str, keep_default_na=False) 
    else:
        raise ValueError(f"不支持的文件格式：{suffix}")

    df = _normalize_jcr_columns(df)

    required = [
        "Journal Name", "Abbreviation", "Index",
        "Major Subject", "Impact Factor", "Q category",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        actual_cols = [repr(c) for c in df.columns]
        raise ValueError(
            f"JCR 表缺少列：{missing}\n"
            f"实际列名：{actual_cols}\n"
            f"提示：检查列名是否有尾随空格或特殊字符"
        )

    rows: list[dict] = []
    for _, row in df.iterrows():
        impact_raw = str(row.get("Impact Factor", "") or "").strip()
        if impact_raw.startswith("<"):
            try:
                impact = float(impact_raw[1:]) / 2.0
            except ValueError:
                impact = None
        elif impact_raw and impact_raw.upper() != "N/A":
            try:
                impact = float(impact_raw)
            except ValueError:
                impact = None
        else:
            impact = None

        quartile = str(row.get("Q category", "") or "").strip()
        if quartile.upper() == "N/A":
            quartile = ""

        rows.append({
            "journal_name": str(row.get("Journal Name", "") or "").strip(),
            "journal_abbr": str(row.get("Abbreviation", "") or "").strip(),
            "index_type": str(row.get("Index", "") or "").strip(),
            "major_subject": str(row.get("Major Subject", "") or "").strip(),
            "journal_impact_factor": impact,
            "journal_jcr_quartile": quartile,
        })

    return pd.DataFrame(rows)

# 期刊名比对停用词（用于 token 级匹配，去掉 journal/international 等泛词）
_JOURNAL_STOPWORDS = frozenset({
    "the", "of", "and", "for", "in", "on", "a", "an", "to",
    "journal", "journals", "international", "official",
})

# 首字母缩写停用词（仅去掉纯连接词，**保留 journal 等语义词**）
_INITIALS_STOPWORDS = frozenset({
    "the", "of", "and", "for", "in", "on", "a", "an", "to",
})

def _tokens(name: str) -> set[str]:
    norm = core.normalize_title(name)
    if not norm:
        return set()
    return {t for t in norm.split() if t and t not in _JOURNAL_STOPWORDS}

def _initials(name: str) -> str:
    """首字母缩写（用于 JCR abbr 匹配）。

    例：Journal of Clinical Oncology → jco
    例：NEJM → nejm（单 token 视为完整缩写）
    例：N Engl J Med → nejm
    例：Balkan Medical Journal → bmj
    例：BMJ → bmj
    例：The Lancet → lancet（单 token，去 the）
    例：BMJ-British Medical Journal → bmj
    例：JAMA-Journal of the American... → jama """
    norm = core.normalize_title(name)
    if not norm:
        return ""

    raw_name = str(name)
    if "-" in raw_name:
        first_seg = raw_name.split("-", 1)[0].strip()
        if (
            first_seg
            and first_seg.isascii()
            and first_seg.isalpha()
            and first_seg.isupper()
            and 2 <= len(first_seg) <= 10
        ):
            return first_seg.lower()

    tokens = [
        t for t in norm.split() if t and t not in _INITIALS_STOPWORDS
    ]
    if not tokens:
        return ""
    if len(tokens) == 1:
        return tokens[0]
    return "".join(t[0] for t in tokens)

def _build_jcr_index(jcr_df: pd.DataFrame) -> dict:
    rows: list[dict] = jcr_df.to_dict("records")
    by_norm_name: dict[str, dict] = {}
    by_norm_abbr: dict[str, dict] = {}
    by_initials: dict[str, list[dict]] = {}
    by_first_char: dict[str, list[dict]] = {}
    by_token: dict[str, list[dict]] = {}

    for row in rows:
        norm_name = core.normalize_title(row.get("journal_name", ""))
        norm_abbr = core.normalize_title(row.get("journal_abbr", ""))
        name_tokens = _tokens(row.get("journal_name", ""))
        abbr_tokens = _tokens(row.get("journal_abbr", ""))
        name_initials = _initials(row.get("journal_name", ""))
        abbr_initials = _initials(row.get("journal_abbr", ""))

        row["_norm_name"] = norm_name
        row["_norm_abbr"] = norm_abbr
        row["_name_tokens"] = name_tokens
        row["_abbr_tokens"] = abbr_tokens
        row["_name_initials"] = name_initials
        row["_abbr_initials"] = abbr_initials

        if norm_name:
            by_norm_name.setdefault(norm_name, row)
            if norm_name.startswith("the "):
                stripped = norm_name[4:]
                if stripped:
                    by_norm_name.setdefault(stripped, row)
        if norm_abbr:
            by_norm_abbr.setdefault(norm_abbr, row)
            if norm_abbr.startswith("the "):
                stripped = norm_abbr[4:]
                if stripped:
                    by_norm_abbr.setdefault(stripped, row)
        if name_initials:
            lst = by_initials.setdefault(name_initials, [])
            if not any(r is row for r in lst):
                lst.append(row)
        if abbr_initials:
            lst = by_initials.setdefault(abbr_initials, [])
            if not any(r is row for r in lst):
                lst.append(row)
        if norm_name:
            by_first_char.setdefault(norm_name[0], []).append(row)
        if norm_abbr:
            by_first_char.setdefault(norm_abbr[0], []).append(row)

        for t in (name_tokens | abbr_tokens):
            by_token.setdefault(t, []).append(row)

    return {
        "rows": rows,
        "by_norm_name": by_norm_name,
        "by_norm_abbr": by_norm_abbr,
        "by_initials": by_initials,
        "by_first_char": by_first_char,
        "by_token": by_token,
    }

def _match_journal(
    name: str,
    jcr_index: dict,
    threshold: float,
) -> dict | None:
    if not jcr_index or not jcr_index.get("rows"):
        return None

    norm_name = core.normalize_title(name)
    if not norm_name:
        return None

    norm_name_lookup = norm_name
    if norm_name_lookup.startswith("the "):
        norm_name_lookup = norm_name_lookup[4:]

    name_tokens = _tokens(name)
    name_initials = _initials(name)
    jcr_rows = jcr_index["rows"]

    row = jcr_index["by_norm_name"].get(norm_name_lookup)
    if row is not None:
        return row

    row = jcr_index["by_norm_abbr"].get(norm_name_lookup)
    if row is not None:
        return row

    if name_initials and len(name_initials) >= 2:
        candidates = jcr_index["by_initials"].get(name_initials, [])
        if len(candidates) == 1:
            return candidates[0]

    if norm_name_lookup:
        prefix_pattern = norm_name_lookup + " "
        first_char = norm_name_lookup[0]
        for row in jcr_index.get("by_first_char", {}).get(first_char, []):
            cand_name = row["_norm_name"]
            cand_abbr = row["_norm_abbr"]
            if cand_name.startswith(prefix_pattern) or \
               cand_abbr.startswith(prefix_pattern):
                return row

    for row in jcr_rows:
        for cand_tokens in (row["_name_tokens"], row["_abbr_tokens"]):
            if not cand_tokens or not name_tokens:
                continue
            if cand_tokens == name_tokens:
                return row
            if cand_tokens.issubset(name_tokens) or name_tokens.issubset(cand_tokens):
                if len(cand_tokens & name_tokens) >= 2 or \
                   (len(cand_tokens) == 1 and len(name_tokens) == 1):
                    return row

    best_match: dict | None = None
    best_score = threshold

    if norm_name:
        first_char = norm_name[0]
        n_len = len(norm_name)
        candidates_l3 = jcr_index.get("by_first_char", {}).get(first_char, [])
        for row in candidates_l3:
            for cand in (row["_norm_name"], row["_norm_abbr"]):
                if not cand:
                    continue

                c_len = len(cand)
                if abs(c_len - n_len) > max(n_len, c_len) * 0.5:
                    continue
                score = difflib.SequenceMatcher(None, norm_name, cand).ratio()
                if score > best_score:
                    best_score = score
                    best_match = row

    name_sorted = " ".join(sorted(name_tokens)) if name_tokens else ""
    if name_sorted and name_tokens:
        seen_ids: set[int] = set()
        candidates_l4: list[dict] = []
        for t in name_tokens:
            for row in jcr_index.get("by_token", {}).get(t, []):
                rid = id(row)
                if rid not in seen_ids:
                    seen_ids.add(rid)
                    candidates_l4.append(row)

        for row in candidates_l4:
            cand_tokens = row["_name_tokens"] or row["_abbr_tokens"]
            if not cand_tokens:
                continue
            if not (cand_tokens & name_tokens):
                continue
            cand_sorted = " ".join(sorted(cand_tokens))
            score = difflib.SequenceMatcher(
                None, name_sorted, cand_sorted,
            ).ratio()
            if score > best_score:
                best_score = score
                best_match = row

    return best_match

def fetch_journal_metrics(
    journal_names: list[str],
    config: dict,
) -> pd.DataFrame:
    """从 JCR 本地表查询期刊指标。"""
    jcr_cfg = config.get("enrichment", {}).get("journal_metrics", {})
    data_dir_raw = jcr_cfg.get("data_dir", "reference/")
    data_dir = Path(data_dir_raw)
    if not data_dir.is_absolute():
        output_root = Path(config["project"]["output_root"])
        data_dir = output_root / data_dir

    year = int(jcr_cfg.get("active_year", 2025))
    filename_tpl = jcr_cfg.get("filename_template", "JCR IF {year}.xlsx")
    default_tpl = jcr_cfg.get(
        "default_filename_template", "JCR IF {year}.default.xlsx"
    )

    user_file = data_dir / filename_tpl.format(year=year)
    default_file = data_dir / default_tpl.format(year=year)

    jcr_path: Path | None = None
    if user_file.exists():
        jcr_path = user_file
    elif default_file.exists():
        jcr_path = default_file

    if jcr_path is None:
        raise FileNotFoundError(
            f"未找到 JCR IF {year} 表（检查 {user_file} 或 {default_file}）"
        )

    try:
        jcr_mtime = jcr_path.stat().st_mtime
    except OSError:
        jcr_mtime = 0.0
    cache_key = (str(jcr_path), jcr_mtime)

    if cache_key in _JCR_TABLE_CACHE:
        jcr_df, jcr_index = _JCR_TABLE_CACHE[cache_key]
    else:
        jcr_df = _parse_jcr_table(jcr_path)
        jcr_index = _build_jcr_index(jcr_df)
        _JCR_TABLE_CACHE.clear()
        _JCR_TABLE_CACHE[cache_key] = (jcr_df, jcr_index)

    threshold = float(jcr_cfg.get("fuzzy_match_threshold", 0.85))
    _emit_log(
        f"enrich: JCR 预索引完成（{len(jcr_index['rows'])} 行）"
    )

    results: list[dict] = []
    total = len(journal_names)
    for i, name in enumerate(journal_names, start=1):
        if i % 50 == 1:
            
            robustness.check_shutdown()
            _emit_log(f"enrich: JCR 匹配进度 {i}/{total}")

        if not name:
            continue
        
        matched = _match_journal(name, jcr_index, threshold)
        results.append({
            "journal_name": name,
            "matched_name": (matched or {}).get("journal_name", ""),
            "journal_abbr": (matched or {}).get("journal_abbr", ""),
            "index_type": (matched or {}).get("index_type", ""),
            "major_subject": (matched or {}).get("major_subject", ""),
            "journal_impact_factor": (matched or {}).get("journal_impact_factor"),
            "journal_jcr_quartile": (matched or {}).get("journal_jcr_quartile", ""),
        })

    return pd.DataFrame(results)

def _build_entity_papers(
    df: pd.DataFrame,
    paper_meta_map: dict[str, dict],
) -> pd.DataFrame:
    rows: list[dict] = []
    for _, row in df.iterrows():
        cid = core.clean_text(row.get("canonical_id", ""))
        if not cid:
            continue
        doi = core.normalize_doi(row.get("doi", ""))
        meta = paper_meta_map.get(doi, {}) if doi else {}

        citation_count = _safe_int(meta.get("citationCount")) if meta else None
        reference_count = _safe_int(meta.get("referenceCount")) if meta else None
        influential = _safe_int(meta.get("influentialCitationCount")) if meta else None

        if not doi:
            metrics_status = "unavailable"
        elif not meta:
            metrics_status = "unavailable"
        elif (citation_count is not None
              and reference_count is not None
              and influential is not None):
            metrics_status = "available"
        elif (citation_count is not None
              or reference_count is not None
              or influential is not None):
            metrics_status = "partial"
        else:
            metrics_status = "unavailable"

        year_raw = row.get("year")
        year = _safe_int(year_raw) if pd.notna(year_raw) else None

        rows.append({
            "canonical_id": cid,
            "title": core.clean_text(row.get("title", "")),
            "year": year,
            "doi": doi,
            "abstract_final": core.clean_text(row.get("abstract_final", "")),
            "citation_count": citation_count,
            "reference_count": reference_count,
            "influential_citation_count": influential,
            "metrics_status": metrics_status,
        })

    return pd.DataFrame(rows, columns=_ENTITY_PAPERS_COLUMNS)

def _build_entity_journals(
    df: pd.DataFrame,
    journal_metrics_map: dict[str, dict],
) -> pd.DataFrame:
    seen: dict[str, dict] = {}
    for _, row in df.iterrows():
        name = core.clean_text(row.get("journal", ""))
        if not name:
            continue
        meta = journal_metrics_map.get(name, {})
        canonical_name = meta.get("matched_name", "") or name
        jid = _compute_journal_id(canonical_name)
        if jid in seen:
            continue
        seen[jid] = {
            "journal_id": jid,
            "journal_name": name,
            "journal_abbr": meta.get("journal_abbr", ""),
            "index_type": meta.get("index_type", ""),
            "major_subject": meta.get("major_subject", ""),
            "journal_impact_factor": meta.get("journal_impact_factor"),
            "journal_jcr_quartile": meta.get("journal_jcr_quartile", ""),
        }
    return pd.DataFrame(list(seen.values()), columns=_ENTITY_JOURNALS_COLUMNS)

def _build_authors_and_relations(
    df: pd.DataFrame,
    paper_meta_map: dict[str, dict],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    authors_seen: dict[str, dict] = {}
    authored_rows: list[dict] = []

    for _, row in df.iterrows():
        cid = core.clean_text(row.get("canonical_id", ""))
        if not cid:
            continue
        doi = core.normalize_doi(row.get("doi", ""))
        meta = paper_meta_map.get(doi, {}) if doi else {}
        authors_list = meta.get("authors", []) if meta else []

        for position, a in enumerate(authors_list, start=1):
            name = core.clean_text(a.get("name", ""))
            if not name:
                continue
            aid = _compute_author_id(name)
            if aid not in authors_seen:
                authors_seen[aid] = {
                    "author_id": aid,
                    "name": name,
                    "h_index": None,
                    "paper_count": None,
                    "citation_count": None,
                    "orcid": "",
                    "affiliations_raw": "",
                }
            authored_rows.append({
                "canonical_id": cid,
                "author_id": aid,
                "author_position": position,
            })

    entity_authors = pd.DataFrame(
        list(authors_seen.values()), columns=_ENTITY_AUTHORS_COLUMNS,
    )
    relation_authored = pd.DataFrame(
        authored_rows, columns=_RELATION_AUTHORED_COLUMNS,
    )
    return entity_authors, relation_authored

def _build_relation_published_in(
    df: pd.DataFrame,
    journal_metrics_map: dict[str, dict],
) -> pd.DataFrame:
    rows: list[dict] = []
    for _, row in df.iterrows():
        cid = core.clean_text(row.get("canonical_id", ""))
        name = core.clean_text(row.get("journal", ""))
        if not cid or not name:
            continue
        meta = journal_metrics_map.get(name, {})
        canonical_name = meta.get("matched_name", "") or name
        year_raw = row.get("year")
        year = _safe_int(year_raw) if pd.notna(year_raw) else None
        rows.append({
            "canonical_id": cid,
            "journal_id": _compute_journal_id(canonical_name),
            "volume": "",
            "pages": "",
            "year": year,
        })
    return pd.DataFrame(rows, columns=_RELATION_PUBLISHED_IN_COLUMNS)

def _build_relation_affiliated(
    dois: list[str],
    config: dict,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    raw = fetch_affiliations(dois, config)
    if len(raw) == 0:
        return (
            pd.DataFrame(columns=_ENTITY_INSTITUTIONS_COLUMNS),
            pd.DataFrame(columns=_RELATION_AFFILIATED_COLUMNS),
        )

    inst_map: dict[str, dict] = {}

    for _, row in raw.iterrows():
        iid = str(row["institution_id"])
        if iid and iid not in inst_map:
            inst_map[iid] = {
                "institution_id": iid,
                "name": row["institution_name"],
                "ror_id": row["institution_ror"],
                "country_code": row["institution_country"],
                "type": row["institution_type"],
                "lineage_json": row["institution_lineage_json"],
            }

    entity_institutions = pd.DataFrame(
        list(inst_map.values()), columns=_ENTITY_INSTITUTIONS_COLUMNS,
    )
    relation_affiliated = (
        raw[_RELATION_AFFILIATED_COLUMNS]
        .drop_duplicates(subset=["author_id", "institution_id"]) 
        .reset_index(drop=True)   
    )
    return entity_institutions, relation_affiliated

def _load_library_relations_for_hits(
    hit_cids: set[str],
    library_root: Path,
) -> dict[str, pd.DataFrame]:
    empty = {
        "entity_journals": pd.DataFrame(columns=_ENTITY_JOURNALS_COLUMNS),
        "entity_institutions": pd.DataFrame(columns=_ENTITY_INSTITUTIONS_COLUMNS),
        "relation_authored": pd.DataFrame(columns=_RELATION_AUTHORED_COLUMNS),
        "relation_affiliated": pd.DataFrame(columns=_RELATION_AFFILIATED_COLUMNS),
        "relation_published_in": pd.DataFrame(columns=_RELATION_PUBLISHED_IN_COLUMNS),
    }
    if not hit_cids:
        return empty

    relations_dir = library_root / "relations"
    entities_dir = library_root / "entities"

    relation_published_in = _load_table(
        relations_dir / "relation_published_in.parquet",
        _RELATION_PUBLISHED_IN_COLUMNS,
    )
    relation_authored = _load_table(
        relations_dir / "relation_authored.parquet",
        _RELATION_AUTHORED_COLUMNS,
    )
    relation_affiliated = _load_table(
        relations_dir / "relation_affiliated.parquet",
        _RELATION_AFFILIATED_COLUMNS,
    )
    entity_journals = _load_table(
        entities_dir / "entity_journals.parquet",
        _ENTITY_JOURNALS_COLUMNS,
    )
    entity_institutions = _load_table(
        entities_dir / "entity_institutions.parquet",
        _ENTITY_INSTITUTIONS_COLUMNS,
    )

    if len(relation_published_in) > 0:
        relation_published_in = relation_published_in[
            relation_published_in["canonical_id"].astype(str).isin(hit_cids)
        ].reset_index(drop=True)

    if len(relation_authored) > 0:
        relation_authored = relation_authored[
            relation_authored["canonical_id"].astype(str).isin(hit_cids)
        ].reset_index(drop=True)

    if len(relation_affiliated) > 0 and len(relation_authored) > 0:
        hit_author_ids = set(relation_authored["author_id"].astype(str).tolist())
        relation_affiliated = relation_affiliated[
            relation_affiliated["author_id"].astype(str).isin(hit_author_ids)
        ].reset_index(drop=True)

    if len(entity_journals) > 0 and len(relation_published_in) > 0:
        hit_journal_ids = set(
            relation_published_in["journal_id"].astype(str).tolist()
        )
        entity_journals = entity_journals[
            entity_journals["journal_id"].astype(str).isin(hit_journal_ids)
        ].reset_index(drop=True)

    if len(entity_institutions) > 0 and len(relation_affiliated) > 0:
        hit_inst_ids = set(
            relation_affiliated["institution_id"].astype(str).tolist()
        )
        entity_institutions = entity_institutions[
            entity_institutions["institution_id"].astype(str).isin(hit_inst_ids)
        ].reset_index(drop=True)

    return {
        "entity_journals": entity_journals,
        "entity_institutions": entity_institutions,
        "relation_authored": relation_authored,
        "relation_affiliated": relation_affiliated,
        "relation_published_in": relation_published_in,
    }

def enrich_all(
    df: pd.DataFrame,
    config: dict,
    run_root: Path,
) -> dict[str, pd.DataFrame]:
    library_root = _library_root(config)

    if df is None or len(df) == 0:
        return {
            "entity_papers": pd.DataFrame(columns=_ENTITY_PAPERS_COLUMNS),
            "entity_authors": pd.DataFrame(columns=_ENTITY_AUTHORS_COLUMNS),
            "entity_institutions": pd.DataFrame(columns=_ENTITY_INSTITUTIONS_COLUMNS),
            "entity_journals": pd.DataFrame(columns=_ENTITY_JOURNALS_COLUMNS),
            "relation_cites": pd.DataFrame(columns=_RELATION_CITES_COLUMNS),
            "relation_authored": pd.DataFrame(columns=_RELATION_AUTHORED_COLUMNS),
            "relation_affiliated": pd.DataFrame(columns=_RELATION_AFFILIATED_COLUMNS),
            "relation_published_in": pd.DataFrame(columns=_RELATION_PUBLISHED_IN_COLUMNS),
        }

    df = _filter_noise_rows(df)
    if df is None or len(df) == 0:
        return {
            "entity_papers": pd.DataFrame(columns=_ENTITY_PAPERS_COLUMNS),
            "entity_authors": pd.DataFrame(columns=_ENTITY_AUTHORS_COLUMNS),
            "entity_institutions": pd.DataFrame(columns=_ENTITY_INSTITUTIONS_COLUMNS),
            "entity_journals": pd.DataFrame(columns=_ENTITY_JOURNALS_COLUMNS),
            "relation_cites": pd.DataFrame(columns=_RELATION_CITES_COLUMNS),
            "relation_authored": pd.DataFrame(columns=_RELATION_AUTHORED_COLUMNS),
            "relation_affiliated": pd.DataFrame(columns=_RELATION_AFFILIATED_COLUMNS),
            "relation_published_in": pd.DataFrame(columns=_RELATION_PUBLISHED_IN_COLUMNS),
        }

    if "is_library_hit" in df.columns:
        hit_mask = _to_bool_series(df["is_library_hit"])
        working_df = df[~hit_mask].copy()
        library_hit_df = df[hit_mask].copy()
    else:
        working_df = df.copy()
        library_hit_df = pd.DataFrame()

    paper_meta_map: dict[str, dict] = {}
    if len(working_df) > 0:
        dois = [
            core.normalize_doi(d)
            for d in working_df["doi"].dropna().astype(str).tolist()
        ]
        dois = [d for d in dois if d]
        unique_dois = list(dict.fromkeys(dois))

        batch_size = int(config.get("enrichment", {}).get("s2_batch_size", 500))
        fields = (
            "title,year,externalIds,citationCount,referenceCount,"
            "influentialCitationCount,authors"
        )

        s2_batches = list(
            core.chunked(unique_dois, min(batch_size, _S2_BATCH_MAX))
        )
        total_s2_batches = len(s2_batches)
        _emit_log(
            f"enrich: S2 共 {total_s2_batches} 个批次"
            f"（{len(unique_dois)} 个 DOI）"
        )

        for i, batch in enumerate(s2_batches, start=1):
            robustness.check_shutdown()
            _emit_progress("enrich", i, total_s2_batches)
            try:
                batch_result = fetch_paper_batch(batch, fields, config)
                paper_meta_map.update(batch_result)
                _emit_log(
                    f"enrich: S2 批次 {i}/{total_s2_batches} 完成，"
                    f"命中 {len(batch_result)}"
                )
            except Exception:  # noqa: BLE001
                continue

        if (unique_dois and not paper_meta_map
                and not _should_interrupt()):
            print(
                f"[enrichment] 警告：S2 batch 全部失败"
                f"（{len(unique_dois)} 个 DOI），"
                f"citation 指标将为 unavailable。"
                f"建议配置 s2_api_key 或检查网络。",
                file=sys.stderr,
            )

    library_enrich_map: dict[str, dict] = {}
    library_relations: dict[str, pd.DataFrame] = {
        "entity_journals": pd.DataFrame(columns=_ENTITY_JOURNALS_COLUMNS),
        "entity_institutions": pd.DataFrame(columns=_ENTITY_INSTITUTIONS_COLUMNS),
        "relation_authored": pd.DataFrame(columns=_RELATION_AUTHORED_COLUMNS),
        "relation_affiliated": pd.DataFrame(columns=_RELATION_AFFILIATED_COLUMNS),
        "relation_published_in": pd.DataFrame(columns=_RELATION_PUBLISHED_IN_COLUMNS),
    }
    if len(library_hit_df) > 0:
        hit_cids = {
            core.clean_text(row.get("canonical_id", ""))
            for _, row in library_hit_df.iterrows()
        }
        hit_cids.discard("")

        library_records_path = library_root / "records_final.parquet"
        if library_records_path.exists():
            try:
                lib_records = pd.read_parquet(library_records_path)
                enrich_cols = [
                    "citation_count", "reference_count",
                    "influential_citation_count",
                    "journal_impact_factor", "journal_jcr_quartile",
                    "author_count",
                    "institution_count", "country_codes",
                ]
                for _, lr in lib_records.iterrows():
                    cid = core.clean_text(lr.get("canonical_id", ""))
                    if cid and cid in hit_cids:
                        library_enrich_map[cid] = {
                            c: lr.get(c)
                            for c in enrich_cols
                            if c in lib_records.columns
                        }
            except Exception:  # noqa: BLE001
                pass

        library_relations = _load_library_relations_for_hits(hit_cids, library_root)

    journal_names: list[str] = []
    for _, row in working_df.iterrows():
        name = core.clean_text(row.get("journal", ""))
        if name:
            journal_names.append(name)
    unique_journals = list(dict.fromkeys(journal_names))

    journal_metrics_map: dict[str, dict] = {}
    if unique_journals:
        _emit_log(f"enrich: JCR 开始匹配 {len(unique_journals)} 个期刊")
        try:
            jcr_df = fetch_journal_metrics(unique_journals, config)
            for _, r in jcr_df.iterrows():
                journal_metrics_map[r["journal_name"]] = r.to_dict()
            _emit_log(
                f"enrich: JCR 匹配完成 "
                f"{len(journal_metrics_map)}/{len(unique_journals)}"
            )
        except FileNotFoundError as exc:
            _append_citation_log(
                library_root,
                {
                    "run_at": _now_iso(),
                    "event": "jcr_missing",
                    "message": f"JCR 表缺失：{exc}",
                },
            )
        except Exception as exc:
            _append_citation_log(
                library_root,
                {
                    "run_at": _now_iso(),
                    "event": "jcr_parse_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:500],
                },
            )

    entity_papers = _build_entity_papers(working_df, paper_meta_map)

    if len(library_hit_df) > 0:
        hit_rows: list[dict] = []
        for _, row in library_hit_df.iterrows():
            cid = core.clean_text(row.get("canonical_id", ""))
            if not cid:
                continue
            enrich = library_enrich_map.get(cid, {})
            year_raw = row.get("year")
            year = _safe_int(year_raw) if pd.notna(year_raw) else None

            citation_count = _safe_int(enrich.get("citation_count"))
            reference_count = _safe_int(enrich.get("reference_count"))
            influential = _safe_int(enrich.get("influential_citation_count"))

            if (citation_count is not None
                    and reference_count is not None
                    and influential is not None):
                metrics_status = "available"
            elif (citation_count is not None
                or reference_count is not None
                or influential is not None):
                metrics_status = "partial"
            else:
                metrics_status = "unavailable"

            hit_rows.append({
                "canonical_id": cid,
                "title": core.clean_text(row.get("title", "")),
                "year": year,
                "doi": core.normalize_doi(row.get("doi", "")),
                "abstract_final": core.clean_text(row.get("abstract_final", "")),
                "citation_count": citation_count,
                "reference_count": reference_count,
                "influential_citation_count": influential,
                "metrics_status": metrics_status,
            })
        if hit_rows:
            entity_papers = pd.concat(
                [entity_papers, pd.DataFrame(hit_rows, columns=_ENTITY_PAPERS_COLUMNS)],
                ignore_index=True,
            )

    entity_journals = _build_entity_journals(working_df, journal_metrics_map)
    entity_authors, relation_authored = _build_authors_and_relations(
        working_df, paper_meta_map,
    )
    relation_published_in = _build_relation_published_in(working_df, journal_metrics_map)

    dois_for_affil = [
        core.normalize_doi(d)
        for d in working_df["doi"].dropna().astype(str).tolist()
    ]
    dois_for_affil = [d for d in dois_for_affil if d]

    _emit_log(f"enrich: OpenAlex 机构处理 {len(dois_for_affil)} 个 DOI")
    entity_institutions, relation_affiliated = _build_relation_affiliated(
        dois_for_affil, config,
    )

    _emit_log(
        f"enrich: OpenAlex 完成，"
        f"机构 {len(entity_institutions)}，"
        f"关联 {len(relation_affiliated)}"
    )

    relation_cites_path = library_root / "relations" / "relation_cites.parquet"
    relation_cites = _load_table(relation_cites_path, _RELATION_CITES_COLUMNS)

    if len(library_hit_df) > 0:
        entity_journals = pd.concat(
            [entity_journals, library_relations["entity_journals"]],
            ignore_index=True,
        ).drop_duplicates(subset=["journal_id"], keep="first")

        entity_institutions = pd.concat(
            [entity_institutions, library_relations["entity_institutions"]],
            ignore_index=True,
        ).drop_duplicates(subset=["institution_id"], keep="first")

        relation_authored = pd.concat(
            [relation_authored, library_relations["relation_authored"]],
            ignore_index=True,
        ).drop_duplicates(subset=["canonical_id", "author_id"], keep="first")

        relation_affiliated = pd.concat(
            [relation_affiliated, library_relations["relation_affiliated"]],
            ignore_index=True,
        ).drop_duplicates(subset=["author_id", "institution_id"], keep="first")

        relation_published_in = pd.concat(
            [relation_published_in, library_relations["relation_published_in"]],
            ignore_index=True,
        ).drop_duplicates(subset=["canonical_id", "journal_id"], keep="first")

    return {
        "entity_papers": entity_papers,
        "entity_authors": entity_authors,
        "entity_institutions": entity_institutions,
        "entity_journals": entity_journals,
        "relation_cites": relation_cites,
        "relation_authored": relation_authored,
        "relation_affiliated": relation_affiliated,
        "relation_published_in": relation_published_in,
    }

def _metric_is_empty(value: Any) -> bool:
    """判断指标字段是否为空（None / NaN / 空字符串）。"""
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    if isinstance(value, str):
        return not value.strip()
    return False

def _count_filled_metrics(records: pd.DataFrame) -> int:
    if records is None or len(records) == 0:
        return 0
    n = 0
    if "citation_count" in records.columns:
        n += int(records["citation_count"].notna().sum())
    if "journal_impact_factor" in records.columns:
        n += int(records["journal_impact_factor"].notna().sum())
    if "author_count" in records.columns:
        ac = pd.to_numeric(records["author_count"], errors="coerce")
        n += int((ac.fillna(0) > 0).sum())
    if "institution_count" in records.columns:
        ic = pd.to_numeric(records["institution_count"], errors="coerce")
        n += int((ic.fillna(0) > 0).sum())
    return n

def _collect_metric_cids_to_fill(
    records: pd.DataFrame,
    only_missing: bool = True,
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {
        "citation": [],
        "journal": [],
        "affiliation": [],
    }
    if records is None or len(records) == 0:
        return result

    n = len(records)
    cid_col = records["canonical_id"].fillna("").astype(str).str.strip()
    has_cid = cid_col.ne("")

    def _norm_col(col_name, norm_fn):
        if col_name not in records.columns:
            return pd.Series([""] * n, index=records.index)
        return records[col_name].fillna("").astype(str).apply(norm_fn)

    def _is_empty_series(col_name):
        if col_name not in records.columns:
            return pd.Series([True] * n, index=records.index)
        s = records[col_name]
        s_str = s.astype(str).str.strip().str.lower()
        return (
            s.isna()
            | s_str.eq("")
            | s_str.eq("nan")
            | s_str.eq("none")
        )

    doi_col = _norm_col("doi", core.normalize_doi)
    pmid_col = _norm_col("pmid", core.normalize_pmid)

    has_id_for_s2 = doi_col.ne("") | pmid_col.ne("")
    citation_missing = (
        _is_empty_series("citation_count")
        | _is_empty_series("reference_count")
        | _is_empty_series("influential_citation_count")
    )
    if only_missing:
        mask_citation = has_cid & has_id_for_s2 & citation_missing
    else:
        mask_citation = has_cid & has_id_for_s2

    if "journal" in records.columns:
        journal_col = records["journal"].fillna("").astype(str).str.strip()
    else:
        journal_col = pd.Series([""] * n, index=records.index)
    has_journal = journal_col.ne("")
    jif_missing = _is_empty_series("journal_impact_factor")
    if only_missing:
        mask_journal = has_cid & has_journal & jif_missing
    else:
        mask_journal = has_cid & has_journal

    if "institution_count" in records.columns:
        ic_col = pd.to_numeric(
            records["institution_count"], errors="coerce"
        ).fillna(0)
    else:
        ic_col = pd.Series([0] * n, index=records.index)
    if "country_codes" in records.columns:
        cc_col = records["country_codes"].fillna("").astype(str).str.strip()
    else:
        cc_col = pd.Series([""] * n, index=records.index)
    affil_missing = ic_col.eq(0) | cc_col.eq("")
    has_doi = doi_col.ne("")
    if only_missing:
        mask_affiliation = has_cid & has_doi & affil_missing
    else:
        mask_affiliation = has_cid & has_doi

    result["citation"] = cid_col[mask_citation].tolist()
    result["journal"] = cid_col[mask_journal].tolist()
    result["affiliation"] = cid_col[mask_affiliation].tolist()
    return result

def _fill_citation_metrics(
    cids: list[str],
    records: pd.DataFrame,
    config: dict,
) -> dict[str, dict]:
    if not cids:
        return {}

    cid_set = set(cids)
    n = len(records)
    cid_col = records["canonical_id"].fillna("").astype(str).str.strip()
    if "doi" in records.columns:
        doi_col = records["doi"].fillna("").astype(str).apply(
            core.normalize_doi
        )
    else:
        doi_col = pd.Series([""] * n, index=records.index)
    mask = cid_col.isin(cid_set) & doi_col.ne("")
    cid_to_doi: dict[str, str] = dict(zip(cid_col[mask], doi_col[mask]))

    if not cid_to_doi:
        _emit_log("enrich: S2 跳过（无有效 DOI）")
        return {}

    fields = (
        "title,year,externalIds,citationCount,referenceCount,"
        "influentialCitationCount,authors"
    )
    unique_dois = list(dict.fromkeys(cid_to_doi.values()))
    batch_size = int(
        config.get("enrichment", {}).get("s2_batch_size", 500)
    )
    batches = list(
        core.chunked(unique_dois, min(batch_size, _S2_BATCH_MAX))
    )
    total_batches = len(batches)
    _emit_log(
        f"enrich: S2 batch 开始，{total_batches} 批 / "
        f"{len(unique_dois)} DOI"
    )

    doi_to_meta: dict[str, dict] = {}
    
    for i, batch in enumerate(batches, start=1):
        robustness.check_shutdown()
        _emit_progress("metrics_s2", i, total_batches)
        try:
            batch_result = fetch_paper_batch(batch, fields, config)
            doi_to_meta.update(batch_result)
        except robustness.InterruptRequested:
            raise
        except Exception:  # noqa: BLE001
            continue

    _emit_log(
        f"enrich: S2 batch 完成，命中 "
        f"{len(doi_to_meta)} / {len(unique_dois)}"
    )

    out: dict[str, dict] = {}
    for cid, doi in cid_to_doi.items():
        meta = doi_to_meta.get(doi)
        if not meta:
            continue
        entry: dict = {}

        cc = _safe_int(meta.get("citationCount"))
        if cc is not None:
            entry["citation_count"] = cc
        rc = _safe_int(meta.get("referenceCount"))
        if rc is not None:
            entry["reference_count"] = rc
        ic = _safe_int(meta.get("influentialCitationCount"))
        if ic is not None:
            entry["influential_citation_count"] = ic

        authors_list = meta.get("authors", []) or []
        author_ids: list[str] = []
        for a in authors_list:
            name = core.clean_text(a.get("name", ""))
            if name:
                author_ids.append(_compute_author_id(name))
        if author_ids:
            entry["author_count"] = len(author_ids)
            entry["_author_ids"] = author_ids

        if any(k for k in entry if not k.startswith("_")):
            out[cid] = entry

    return out

def _fill_journal_metrics(
    cids: list[str],
    records: pd.DataFrame,
    config: dict,
) -> dict[str, dict]:
    """用 JCR 表补 journal_impact_factor / journal_jcr_quartile。"""
    if not cids:
        return {}

    cid_set = set(cids)
    n = len(records)
    cid_col = records["canonical_id"].fillna("").astype(str).str.strip()
    if "journal" in records.columns:
        journal_col = records["journal"].fillna("").astype(str).str.strip()
    else:
        journal_col = pd.Series([""] * n, index=records.index)
    mask = cid_col.isin(cid_set) & journal_col.ne("")
    cid_to_journal: dict[str, str] = dict(
        zip(cid_col[mask], journal_col[mask])
    )

    if not cid_to_journal:
        _emit_log("enrich: JCR 跳过（无有效 journal）")
        return {}
    
    unique_journals = list(dict.fromkeys(cid_to_journal.values()))
    _emit_log(f"enrich: JCR 匹配 {len(unique_journals)} 个期刊")

    try:
        jcr_df = fetch_journal_metrics(unique_journals, config)
    except FileNotFoundError as exc:
        _emit_log(f"enrich: JCR 表缺失，跳过：{exc}")
        return {}
    except Exception as exc:  # noqa: BLE001
        _emit_log(f"enrich: JCR 匹配失败：{exc}")
        return {}

    jcr_map: dict[str, dict] = {}
    for _, r in jcr_df.iterrows():
        jcr_map[str(r["journal_name"])] = r.to_dict()

    out: dict[str, dict] = {}
    for cid, journal in cid_to_journal.items():
        m = jcr_map.get(journal)
        if not m:
            continue
        entry: dict = {}
        jif = m.get("journal_impact_factor")
        if jif is not None and not (
            isinstance(jif, float) and jif != jif
        ):
            try:
                entry["journal_impact_factor"] = float(jif)
            except (ValueError, TypeError):
                pass
        jq = core.clean_text(m.get("journal_jcr_quartile", ""))
        if jq:
            entry["journal_jcr_quartile"] = jq
        if entry:
            out[cid] = entry

    return out

def _fill_affiliation_metrics(
    cids: list[str],
    records: pd.DataFrame,
    s2_map: dict[str, dict],
    config: dict,
) -> dict[str, dict]:
    if not cids:
        return {}

    cid_set = set(cids)
    n = len(records)
    cid_col = records["canonical_id"].fillna("").astype(str).str.strip()
    if "doi" in records.columns:
        doi_col = records["doi"].fillna("").astype(str).apply(
            core.normalize_doi
        )
    else:
        doi_col = pd.Series([""] * n, index=records.index)
    mask = cid_col.isin(cid_set) & doi_col.ne("")
    cid_to_doi: dict[str, str] = dict(zip(cid_col[mask], doi_col[mask]))
    cid_to_author_ids: dict[str, list[str]] = {}
    for cid, entry in s2_map.items():
        if cid not in cid_set:
            continue
        author_ids = entry.get("_author_ids", [])
        if author_ids:
            cid_to_author_ids[cid] = author_ids

    if not cid_to_doi:
        _emit_log("enrich: OpenAlex 跳过（无有效 DOI）")
        return {}

    if not cid_to_author_ids:
        _emit_log(
            "enrich: OpenAlex 跳过（无 S2 作者数据，无法聚合机构）"
        )
        return {}

    unique_dois = list(dict.fromkeys(cid_to_doi.values()))
    _emit_log(
        f"enrich: OpenAlex 机构查询 {len(unique_dois)} 个 DOI"
    )

    try:
        raw = fetch_affiliations(unique_dois, config)
    except Exception as exc:  # noqa: BLE001
        _emit_log(f"enrich: OpenAlex 查询失败：{exc}")
        return {}

    if len(raw) == 0:
        _emit_log("enrich: OpenAlex 无结果")
        return {}

    inst_country: dict[str, str] = {}
    author_to_insts: dict[str, set] = {}

    for _, r in raw.iterrows():
        iid = str(r.get("institution_id", "") or "")
        aid = str(r.get("author_id", "") or "")
        cc = core.clean_text(r.get("institution_country", ""))
        if iid and cc:
            inst_country[iid] = cc
        if aid and iid:
            author_to_insts.setdefault(aid, set()).add(iid)

    out: dict[str, dict] = {}
    for cid, author_ids in cid_to_author_ids.items():
        all_insts: set = set()
        for aid in author_ids:
            all_insts.update(author_to_insts.get(aid, set()))
        if not all_insts:
            continue
        entry: dict = {"institution_count": len(all_insts)}
        countries: set = set()
        for iid in all_insts:
            cc = inst_country.get(iid, "")
            if cc:
                countries.add(cc)
        if countries:
            entry["country_codes"] = ";".join(sorted(countries))
        out[cid] = entry

    return out

def _merge_fill_results(
    *maps: dict[str, dict],
) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for m in maps:
        for cid, fields in m.items():
            if cid not in result:
                result[cid] = {}
            for k, v in fields.items():
                if k.startswith("_"):
                    continue
                result[cid][k] = v
    return result

def _write_metrics_back(
    library_root: Path,
    metrics_map: dict[str, dict],
) -> int:
    if not metrics_map:
        return 0

    records_path = library_root / "records_final.parquet"
    if not records_path.exists():
        return 0

    records = pd.read_parquet(records_path)
    if len(records) == 0 or not metrics_map:
        return 0

    if "special_status" in records.columns:
        noise_cid_set = set(
            records.loc[
                records["special_status"]
                .fillna("").astype(str).str.strip()
                == "retrieval_noise",
                "canonical_id",
            ].astype(str).tolist()
        )
        if noise_cid_set:
            metrics_map = {
                k: v for k, v in metrics_map.items()
                if k not in noise_cid_set
            }
            if not metrics_map:
                return 0

    n = len(records)
    cid_col = records["canonical_id"].fillna("").astype(str).str.strip()

    metrics_df = pd.DataFrame.from_dict(metrics_map, orient="index")
    if metrics_df.empty:
        return 0

    cid_in_map = cid_col.isin(metrics_df.index)

    changed_mask = pd.Series(False, index=records.index)

    def _notempty_series(col_name):
        if col_name not in records.columns:
            return pd.Series(False, index=records.index)
        s = records[col_name]
        s_str = s.astype(str).str.strip().str.lower()
        return ~(
            s.isna()
            | s_str.eq("")
            | s_str.eq("nan")
            | s_str.eq("none")
        )

    for col_name in metrics_df.columns:
        if col_name not in records.columns:
            continue

        aligned_new = cid_col.map(metrics_df[col_name])
        aligned_str = aligned_new.astype(str).str.strip().str.lower()
        new_not_empty = ~(
            aligned_new.isna()
            | aligned_str.eq("")
            | aligned_str.eq("nan")
            | aligned_str.eq("none")
        )
        old_empty = ~_notempty_series(col_name)
        need_fill = cid_in_map & old_empty & new_not_empty
        if need_fill.any():
            records.loc[need_fill, col_name] = aligned_new[need_fill]
            changed_mask = changed_mask | need_fill

    if "citation_count" in records.columns:
        cc_notna = pd.to_numeric(
            records["citation_count"], errors="coerce"
        ).notna()
    else:
        cc_notna = pd.Series(False, index=records.index)
    if "reference_count" in records.columns:
        rc_notna = pd.to_numeric(
            records["reference_count"], errors="coerce"
        ).notna()
    else:
        rc_notna = pd.Series(False, index=records.index)
    if "influential_citation_count" in records.columns:
        ic_notna = pd.to_numeric(
            records["influential_citation_count"], errors="coerce"
        ).notna()
    else:
        ic_notna = pd.Series(False, index=records.index)

    new_status = pd.Series("unavailable", index=records.index)
    new_status[cc_notna | rc_notna | ic_notna] = "partial"
    new_status[cc_notna & rc_notna & ic_notna] = "available"

    if "metrics_status" not in records.columns:
        records["metrics_status"] = ""
    old_status = records["metrics_status"].fillna("").astype(str)
    status_changed = cid_in_map & old_status.ne(new_status)
    if status_changed.any():
        records.loc[status_changed, "metrics_status"] = new_status[status_changed]
        changed_mask = changed_mask | status_changed

    updated_count = int(changed_mask.sum())
    if updated_count == 0:
        return 0

    tmp = records_path.with_name(records_path.name + ".tmp")
    records.to_parquet(tmp, index=False)
    os.replace(tmp, records_path)

    return updated_count

def refresh_metrics_for_cids(
    cids: list[str],
    library_root: Path,
    config: dict,
    only_missing: bool = True,
) -> dict:
    empty_result = {
        "total": 0,
        "filled_before": 0,
        "filled_after": 0,
        "increased": 0,
        "s2_hits": 0,
        "jcr_hits": 0,
        "openalex_hits": 0,
    }

    if not cids:
        return empty_result

    library_root = Path(library_root)
    records_path = library_root / "records_final.parquet"
    if not records_path.exists():
        _emit_log("enrich: records_final.parquet 不存在")
        return empty_result

    records_all = pd.read_parquet(records_path)
    if len(records_all) == 0:
        return empty_result
    cid_set = set(str(c) for c in cids)
    mask = records_all["canonical_id"].astype(str).isin(cid_set)

    if "special_status" in records_all.columns:
        noise_mask = (
            records_all["special_status"].fillna("").astype(str).str.strip()
            == "retrieval_noise"
        )
        mask = mask & ~noise_mask

    records = records_all[mask].copy()
    if len(records) == 0:
        _emit_log("enrich: 无匹配 cid（或全部为 noise）")
        return empty_result

    filled_before = _count_filled_metrics(records)
    _emit_log(
        f"enrich: 处理 {len(records)} 条；"
        f"更新前指标覆盖 {filled_before}"
    )

    to_fill = _collect_metric_cids_to_fill(records, only_missing)
    _emit_log(
        f"enrich: 待补 citation={len(to_fill['citation'])}，"
        f"journal={len(to_fill['journal'])}，"
        f"affiliation={len(to_fill['affiliation'])}"
    )

    if to_fill["citation"]:
        _ensure_global_source(library_root, config)
        current_source, _ = _get_global_source(library_root)
        if not current_source:
            _emit_log(
                "enrich: 数据源探测失败；citation 维度将跳过。"
                "请检查 S2 API key 或网络。"
            )

    s2_map: dict[str, dict] = {}
    if to_fill["citation"]:
        s2_map = _fill_citation_metrics(
            to_fill["citation"], records, config,
        )
    robustness.check_shutdown()

    jcr_map: dict[str, dict] = {}
    if to_fill["journal"]:
        jcr_map = _fill_journal_metrics(
            to_fill["journal"], records, config,
        )
    robustness.check_shutdown()

    affil_map: dict[str, dict] = {}
    if to_fill["affiliation"]:
        affil_map = _fill_affiliation_metrics(
            to_fill["affiliation"], records, s2_map, config,
        )
    robustness.check_shutdown()

    metrics_map = _merge_fill_results(s2_map, jcr_map, affil_map)
    _emit_log(f"enrich: 合并后 {len(metrics_map)} 条待写")

    updated = _write_metrics_back(library_root, metrics_map)
    _emit_log(f"enrich: 实际更新 {updated} 行")

    records_after_all = pd.read_parquet(records_path)
    mask = records_after_all["canonical_id"].astype(str).isin(cid_set)

    if "special_status" in records_after_all.columns:
        noise_mask = (
            records_after_all["special_status"]
            .fillna("").astype(str).str.strip()
            == "retrieval_noise"
        )
        mask = mask & ~noise_mask
    records_after = records_after_all[mask]
    filled_after = _count_filled_metrics(records_after)

    return {
        "total": int(len(records)),
        "filled_before": int(filled_before),
        "filled_after": int(filled_after),
        "increased": int(filled_after - filled_before),
        "s2_hits": int(len(s2_map)),
        "jcr_hits": int(len(jcr_map)),
        "openalex_hits": int(len(affil_map)),
    }