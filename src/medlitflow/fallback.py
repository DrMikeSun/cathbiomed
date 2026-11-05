"""fallback.py — 三级摘要回退（本地 raw / PubMed EFetch / Europe PMC）。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from medlitflow import core, robustness

__all__ = ["fallback_abstracts"]

_NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_EUROPEPMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

_NEEDS_FALLBACK_STATUSES = frozenset({
    "needs_abstract_fallback", "broken_link", "inaccessible", "no_fulltext_available",
})

_DEFAULT_PUBMED_BATCH_SIZE = 200

_FALLBACK_EXTRA_COLUMNS = [
    "final_retrieval_status", "abstract_source", "fallback_level",
]

_FALLBACK_LOG_COLUMNS = [
    "canonical_id", "fallback_level", "abstract_source",
    "fallback_status", "abstract_length", "error_message",
]

_MAX_ABSTRACT_CHARS = 100_000   # 100 KB 上限

def _truncate_abstract(text: str, label: str = "") -> str:
    if not text or len(text) <= _MAX_ABSTRACT_CHARS:
        return text
    original_len = len(text)
    truncated = text[:_MAX_ABSTRACT_CHARS] + "...[truncated]"
    msg = (
        f"fallback: 超长 abstract 截断（{label}）"
        f"原长 {original_len} → {_MAX_ABSTRACT_CHARS}"
    )
    try:
        from medlitflow import pipeline as _pipeline
        _pipeline.get_interactor().log(msg)
    except Exception:  # noqa: BLE001
        pass
    return truncated

def _hash_abstract(text: str) -> str:
    return core.hash_abstract(text)

def _classify_variant_status(
    variants: list[dict],
    threshold: float,
) -> str:
    if len(variants) <= 1:
        return "single"
    hashes = {v.get("content_hash", "") for v in variants}
    if len(hashes) == 1:
        return "identical"
    lengths = [int(v.get("length", 0)) for v in variants]
    max_len = max(lengths) if lengths else 0
    min_len = min(lengths) if lengths else 0
    if max_len > 0 and (max_len - min_len) / max_len < threshold:
        return "near_identical"
    return "divergent"

def _safe_load_variants(raw: Any) -> list[dict]:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []

def _resolve_run_root(config: dict) -> Path | None:
    try:
        output_root = Path(config["project"]["output_root"])
        run_name = config["project"].get("run_name", "")
        if not run_name:
            return None
        return output_root / "runs" / run_name
    except (KeyError, TypeError):
        return None

def _extract_abstract_from_pubmed_xml(xml_text: str, pmid: str) -> str:
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return ""

    for article in root.findall(".//PubmedArticle"):
        article_pmid = core.clean_text(
            article.findtext(".//MedlineCitation/PMID", "")
        )
        if article_pmid != pmid:
            continue
        parts: list[str] = []
        for at in article.findall(".//Abstract/AbstractText"):
            label = core.clean_text(at.get("Label", ""))
            text = core.clean_text(" ".join(at.itertext()))
            if text:
                parts.append(f"{label}: {text}" if label else text)
        return " ".join(parts)
    return ""

def _extract_abstract_from_raw_file(
    raw_path: Path,
    source_database: str,
    source_record_id: str,
) -> str:
    if not raw_path.is_file():
        return ""
    try:
        text = raw_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""

    # PubMed XML
    if source_database in ("pubmed", "pmc"):
        if text.lstrip().startswith(("<?xml", "<PubmedArticleSet", "<pmc-articleset")):
            return _extract_abstract_from_pubmed_xml(text, source_record_id)

    # JSON 响应
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return ""

    # Europe PMC
    if isinstance(data, dict):
        result_list = data.get("resultList", {})
        if isinstance(result_list, dict):
            for item in result_list.get("result", []) or []:
                if not isinstance(item, dict):
                    continue
                item_id = core.clean_text(item.get("id", ""))
                if item_id == source_record_id or core.clean_text(item.get("pmid", "")) == source_record_id:
                    return core.clean_text(item.get("abstractText", ""))
        # Crossref
        message = data.get("message", {})
        if isinstance(message, dict):
            for item in message.get("items", []) or []:
                if not isinstance(item, dict):
                    continue
                if core.normalize_doi(item.get("DOI", "")) == core.normalize_doi(source_record_id):
                    abstract = core.clean_text(item.get("abstract", ""))
                    return re.sub(r"<[^>]+>", " ", abstract).strip()
        # OpenAlex
        results = data.get("results", [])
        if isinstance(results, list):
            for item in results:
                if not isinstance(item, dict):
                    continue
                inverted = item.get("abstract_inverted_index")
                if inverted:
                    return _reconstruct_openalex_abstract(inverted)

    return ""

def _reconstruct_openalex_abstract(inverted_index: Any) -> str:
    if not isinstance(inverted_index, dict):
        return ""
    positions: dict[int, str] = {}
    for token, values in inverted_index.items():
        for pos in values or []:
            positions[int(pos)] = token
    if not positions:
        return ""
    return core.clean_text(" ".join(positions[k] for k in sorted(positions)))

def _index_from_raw_text(
    text: str,
    source_db: str,
    index: dict[str, str],
) -> None:
    # pubmed / pmc：XML 场景，按 PMID / PMCID 索引
    if source_db in ("pubmed", "pmc"):
        stripped = text.lstrip()
        if stripped.startswith(("<?xml", "<PubmedArticleSet", "<pmc-articleset")):
            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(text)
            except ET.ParseError:
                return
            # PubmedArticle：按 PMID 索引
            for article in root.findall(".//PubmedArticle"):
                pmid = core.clean_text(
                    article.findtext(".//MedlineCitation/PMID", "")
                )
                if not pmid:
                    continue
                parts: list[str] = []
                for at in article.findall(".//Abstract/AbstractText"):
                    label = core.clean_text(at.get("Label", ""))
                    t = core.clean_text(" ".join(at.itertext()))
                    if t:
                        parts.append(f"{label}: {t}" if label else t)
                if parts:
                    index[pmid] = " ".join(parts)
            # PMC JATS：按 PMCID 索引
            for article in root.findall(".//article"):
                pmcid_elem = article.find(
                    ".//article-id[@pub-id-type='pmc']"
                )
                if pmcid_elem is None:
                    continue
                pmcid = core.normalize_pmcid(pmcid_elem.text)
                if not pmcid:
                    continue
                parts: list[str] = []
                for p in article.findall(".//abstract//p"):
                    t = core.clean_text("".join(p.itertext()))
                    if t:
                        parts.append(t)
                if parts:
                    index[pmcid] = " ".join(parts)
            return

    # JSON 场景
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return

    if not isinstance(data, dict):
        return

    # Europe PMC：按 id / pmid 索引
    result_list = data.get("resultList", {})
    if isinstance(result_list, dict):
        for item in result_list.get("result", []) or []:
            if not isinstance(item, dict):
                continue
            abstract = core.clean_text(item.get("abstractText", ""))
            if not abstract:
                continue
            item_id = core.clean_text(item.get("id", ""))
            pmid = core.clean_text(item.get("pmid", ""))
            if item_id:
                index[item_id] = abstract
            if pmid:
                index[pmid] = abstract

    # Crossref：按 DOI 索引
    message = data.get("message", {})
    if isinstance(message, dict):
        for item in message.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            doi = core.normalize_doi(item.get("DOI", ""))
            if not doi:
                continue
            abstract = core.clean_text(item.get("abstract", ""))
            abstract = re.sub(r"<[^>]+>", " ", abstract).strip()
            if abstract:
                index[doi] = abstract

    # OpenAlex：按 DOI / PMID 索引
    results = data.get("results", [])
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            inverted = item.get("abstract_inverted_index")
            if not inverted:
                continue
            abstract = _reconstruct_openalex_abstract(inverted)
            if not abstract:
                continue
            ids = item.get("ids") or {}
            doi = core.normalize_doi(ids.get("doi") or item.get("doi"))
            pmid = core.normalize_pmid(ids.get("pmid"))
            if doi:
                index[doi] = abstract
            if pmid:
                index[pmid] = abstract

def _try_local_raw(fallback_df: pd.DataFrame) -> pd.DataFrame:
    _raw_index_cache: dict[tuple[Path, str], dict[str, str]] = {}

    def _get_index(raw_dir: Path, source_db: str) -> dict[str, str]:
        key = (raw_dir, source_db)
        if key in _raw_index_cache:
            return _raw_index_cache[key]

        index: dict[str, str] = {}
        patterns = (
            ("*.xml", "*.json")
            if source_db in ("pubmed", "pmc")
            else ("*.json", "*.xml")
        )
        for pattern in patterns:
            for raw_file in raw_dir.glob(pattern):
                robustness.check_shutdown()
                try:
                    text = raw_file.read_text(
                        encoding="utf-8", errors="ignore"
                    )
                except OSError:
                    continue
                _index_from_raw_text(text, source_db, index)
        _raw_index_cache[key] = index
        return index

    for idx, row in fallback_df.iterrows():
        robustness.check_shutdown()

        raw_path_str = core.clean_text(row.get("raw_response_path", ""))
        if not raw_path_str:
            continue
        raw_dir = Path(raw_path_str)
        if not raw_dir.exists():
            continue
        source_db = core.clean_text(row.get("source_database", ""))
        source_record_id = core.clean_text(row.get("source_record_id", ""))
        if not source_record_id:
            continue

        abstract = ""
        if raw_dir.is_file():
            abstract = _extract_abstract_from_raw_file(
                raw_dir, source_db, source_record_id,
            )
        elif raw_dir.is_dir():
            index = _get_index(raw_dir, source_db)
            abstract = index.get(source_record_id, "")

        if abstract:
            fallback_df.at[idx, "abstract_final"] = abstract
            fallback_df.at[idx, "abstract_source"] = "local_raw"
            fallback_df.at[idx, "fallback_level"] = 1
    return fallback_df

def _try_pubmed_efetch(
    fallback_df: pd.DataFrame,
    config: dict,
    batch_size: int,
) -> dict[str, str]:
    rows_with_pmid = [
        (idx, row) for idx, row in fallback_df.iterrows()
        if core.normalize_pmid(row.get("pmid", ""))
    ]
    if not rows_with_pmid:
        return {}

    email = config.get("api", {}).get("email", "")
    api_key = config.get("api", {}).get("ncbi_api_key", "")
    delay = float(config.get("rate_limit", {}).get("pubmed", 0.34))

    results: dict[str, str] = {}
    for chunk in core.chunked(rows_with_pmid, batch_size):
        robustness.check_shutdown()
        pmids = [core.normalize_pmid(row.get("pmid", "")) for _, row in chunk]
        params = {
            "db": "pubmed",
            "id": ",".join(pmids),
            "rettype": "abstract",
            "retmode": "xml",
        }
        if email:
            params["email"] = email
        if api_key:
            params["api_key"] = api_key

        try:
            xml_text = core.http_get_text(
                f"{_NCBI_BASE}/efetch.fcgi",
                params=params,
                headers={},
                delay=delay,
            )
        except robustness.InterruptRequested:
            raise
        except Exception:  # noqa: BLE001
            continue

        for idx, row in chunk:
            pmid = core.normalize_pmid(row.get("pmid", ""))
            abstract = _extract_abstract_from_pubmed_xml(xml_text, pmid)
            if abstract:
                cid = core.clean_text(row.get("canonical_id", ""))
                results[cid] = abstract
    return results

def _try_europepmc(
    fallback_df: pd.DataFrame,
    config: dict,
) -> dict[str, str]:
    rows_with_doi = [
        (idx, row) for idx, row in fallback_df.iterrows()
        if core.normalize_doi(row.get("doi", ""))
    ]
    if not rows_with_doi:
        return {}

    delay = float(config.get("rate_limit", {}).get("europepmc", 0.2))
    batch_size = int(
        config.get("fallback", {}).get("europepmc_batch_size", 30)
    )
    page_size = min(batch_size * 2, 100)

    results: dict[str, str] = {}

    doi_to_rows: dict[str, list[tuple[int, pd.Series]]] = {}
    for idx, row in rows_with_doi:
        doi = core.normalize_doi(row.get("doi", ""))
        doi_to_rows.setdefault(doi, []).append((idx, row))

    unique_dois = list(doi_to_rows.keys())

    for chunk in core.chunked(unique_dois, batch_size):
        robustness.check_shutdown()

        # Europe PMC 支持 OR 组合
        query = " OR ".join(f"DOI:{doi}" for doi in chunk)
        try:
            payload = core.http_get_json(
                _EUROPEPMC_SEARCH_URL,
                params={
                    "query": query,
                    "format": "json",
                    "resultType": "core",
                    "pageSize": page_size,
                },
                headers={},
                delay=delay,
            )
        except robustness.InterruptRequested:
            raise
        except Exception:  # noqa: BLE001
            continue

        result_list = (
            payload.get("resultList", {}).get("result", []) or []
        )

        # 按 DOI 建立索引，回填
        for item in result_list:
            if not isinstance(item, dict):
                continue
            abstract = core.clean_text(item.get("abstractText", ""))
            if not abstract:
                continue
            # Europe PMC 返回的 DOI 字段可能大小写不同
            item_doi = core.normalize_doi(item.get("doi", ""))
            if item_doi and item_doi in doi_to_rows:
                for idx, row in doi_to_rows[item_doi]:
                    cid = core.clean_text(row.get("canonical_id", ""))
                    if cid:
                        results[cid] = abstract

    return results

def _apply_abstract_processing(
    df: pd.DataFrame,
    fallback_mask: pd.Series,
    threshold: float,
) -> pd.DataFrame:
    idx_list = df.index[fallback_mask].tolist()
    if len(idx_list) == 0:
        return df

    sub = df.loc[fallback_mask].copy()

    new_variants_json: list[str] = []
    new_variant_count: list[int] = []
    new_variant_status: list[str] = []
    new_divergence_type: list[str] = []
    new_selection_rule: list[str] = []
    new_abstract_final: list[str] = []

    for _idx, row in sub.iterrows():
        existing_final = core.clean_text(row.get("abstract_final", ""))
        fallback_abstract = core.clean_text(row.get("_fallback_abstract", ""))
        source = core.clean_text(row.get("abstract_source", "fallback"))

        variants = _safe_load_variants(row.get("abstract_variants_json", "[]"))
        original_status = core.clean_text(
            row.get("abstract_variant_status", "")
        )

        merged_final = existing_final
        merged_selection_rule = core.clean_text(
            row.get("abstract_selection_rule", "")
        )
        merged_divergence = core.clean_text(
            row.get("abstract_divergence_type", "")
        )

        if fallback_abstract:
            fallback_abstract = _truncate_abstract(
                fallback_abstract, f"apply/{_idx}",
            )
            new_variant = {
                "source": source,
                "content": fallback_abstract,
                "content_hash": _hash_abstract(fallback_abstract),
                "length": len(fallback_abstract),
            }
            existing_hashes = {
                v.get("content_hash") for v in variants
            }
            if new_variant["content_hash"] not in existing_hashes:
                variants.append(new_variant)

            if not existing_final:
                merged_final = fallback_abstract
                merged_selection_rule = "fallback"
            else:
                merged_selection_rule = "fallback_appended"

        new_status = _classify_variant_status(variants, threshold)

        if original_status == "divergent":
            new_status = "divergent"
        elif new_status == "divergent":
            merged_divergence = "unknown"

        new_variants_json.append(
            json.dumps(variants, ensure_ascii=False)
        )
        new_variant_count.append(len(variants))
        new_variant_status.append(new_status)
        new_divergence_type.append(merged_divergence)
        new_selection_rule.append(merged_selection_rule)
        new_abstract_final.append(merged_final)

    df.loc[fallback_mask, "abstract_variants_json"] = new_variants_json
    df.loc[fallback_mask, "abstract_variant_count"] = new_variant_count
    df.loc[fallback_mask, "abstract_variant_status"] = new_variant_status
    df.loc[fallback_mask, "abstract_divergence_type"] = new_divergence_type
    df.loc[fallback_mask, "abstract_selection_rule"] = new_selection_rule
    df.loc[fallback_mask, "abstract_final"] = new_abstract_final

    return df

def fallback_abstracts(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    similarity_threshold = float(
        config.get("dedup", {}).get("abstract_similarity_threshold", 0.05)
    )

    if df is None or len(df) == 0:
        base_columns = list(df.columns) if isinstance(df, pd.DataFrame) else []
        return pd.DataFrame(columns=base_columns + _FALLBACK_EXTRA_COLUMNS)

    fallback_cfg = config.get("fallback", {}) or {}
    fallback_enabled = bool(fallback_cfg.get("enabled", True))
    pubmed_batch_size = int(fallback_cfg.get("pubmed_batch_size", _DEFAULT_PUBMED_BATCH_SIZE))
    local_raw_lookup = bool(fallback_cfg.get("local_raw_lookup", True))

    df = df.copy()

    if "special_status" in df.columns:
        noise_mask = (
            df["special_status"].fillna("").astype(str).str.strip()
            == "retrieval_noise"
        )
    else:
        noise_mask = pd.Series([False] * len(df), index=df.index)

    status_series = df.get("retrieval_status", pd.Series([""] * len(df))).fillna("")
    html_second_pass = df.get("html_second_pass_status", pd.Series([""] * len(df))).fillna("")

    mask_status = status_series.isin(_NEEDS_FALLBACK_STATUSES) & ~noise_mask
    mask_html_failed = (
        (status_series == "fulltext_html")
        & (html_second_pass == "no_pdf_link_found")
    ) & ~noise_mask
    fallback_mask = mask_status | mask_html_failed

    df["final_retrieval_status"] = ""
    df["abstract_source"] = ""
    df["fallback_level"] = 0

    if noise_mask.any():
        df.loc[noise_mask, "final_retrieval_status"] = "metadata_only"
        df.loc[noise_mask, "abstract_source"] = "search_metadata"
        df.loc[noise_mask, "fallback_level"] = 0

    no_fallback = ~fallback_mask & ~noise_mask
    _raw_status = (
        df.loc[no_fallback, "retrieval_status"]
        .fillna("").astype(str)
    )
    _final_valid = {"fulltext_pdf", "fulltext_xml", "fulltext_html"}
    df.loc[no_fallback, "final_retrieval_status"] = _raw_status.where(
        _raw_status.isin(_final_valid), "metadata_only",
    )
    df.loc[no_fallback, "abstract_source"] = "search_metadata"
    df.loc[no_fallback, "fallback_level"] = 0

    if not fallback_enabled:
        df.loc[fallback_mask, "final_retrieval_status"] = df.loc[
            fallback_mask, "retrieval_status"
        ].apply(
            lambda s: s if s in ("fulltext_pdf", "fulltext_xml", "fulltext_html")
            else "metadata_only"
        )
        df.loc[fallback_mask, "abstract_source"] = "search_metadata"
        df.loc[fallback_mask, "fallback_level"] = 0
        return df

    fallback_df = df[fallback_mask].copy()

    fallback_df["_fallback_abstract"] = ""

    if local_raw_lookup:
        robustness.check_shutdown()
        fallback_df = _try_local_raw(fallback_df)
        level1_mask = fallback_df["fallback_level"] == 1
        fallback_df.loc[level1_mask, "_fallback_abstract"] = fallback_df.loc[
            level1_mask, "abstract_final"
        ]

    pending = fallback_df[fallback_df["fallback_level"] == 0]
    if len(pending) > 0:
        pubmed_results = _try_pubmed_efetch(pending, config, pubmed_batch_size)
        if pubmed_results:
            mapped_abstract = fallback_df["canonical_id"].map(pubmed_results)
            hit_mask = mapped_abstract.notna()
            if hit_mask.any():
                fallback_df.loc[hit_mask, "_fallback_abstract"] = mapped_abstract[hit_mask]
                fallback_df.loc[hit_mask, "abstract_source"] = "pubmed_efetch"
                fallback_df.loc[hit_mask, "fallback_level"] = 2

    pending = fallback_df[fallback_df["fallback_level"] == 0]
    if len(pending) > 0:
        epmc_results = _try_europepmc(pending, config)
        if epmc_results:
            mapped_abstract = fallback_df["canonical_id"].map(epmc_results)
            hit_mask = mapped_abstract.notna()
            if hit_mask.any():
                fallback_df.loc[hit_mask, "_fallback_abstract"] = mapped_abstract[hit_mask]
                fallback_df.loc[hit_mask, "abstract_source"] = "europepmc"
                fallback_df.loc[hit_mask, "fallback_level"] = 3

    has_abstract = pd.Series(
        [
            bool(core.clean_text(x))
            for x in fallback_df["_fallback_abstract"].tolist()
        ],
        index=fallback_df.index,
        dtype=bool,
    )
    fallback_df.loc[has_abstract, "final_retrieval_status"] = "abstract_only"
    fallback_df.loc[~has_abstract, "final_retrieval_status"] = "metadata_only"

    no_source = pd.Series(
        [
            not core.clean_text(x)
            for x in fallback_df["abstract_source"].tolist()
        ],
        index=fallback_df.index,
        dtype=bool,
    )
    fill_none_mask = (~has_abstract) & no_source
    fallback_df.loc[fill_none_mask, "abstract_source"] = "none"

    for col in _FALLBACK_EXTRA_COLUMNS:
        df.loc[fallback_mask, col] = fallback_df[col].values
    df.loc[fallback_mask, "_fallback_abstract"] = fallback_df["_fallback_abstract"].values

    df = _apply_abstract_processing(df, fallback_mask, similarity_threshold)

    if "_fallback_abstract" in df.columns:
        df = df.drop(columns=["_fallback_abstract"])

    run_root = _resolve_run_root(config)
    if run_root is not None:
        output_dir = run_root / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        fallback_sub = df.loc[fallback_mask]
        if len(fallback_sub) > 0:
            log_df = pd.DataFrame({
                "canonical_id": fallback_sub["canonical_id"].apply(
                    core.clean_text
                ),
                "fallback_level": pd.to_numeric(
                    fallback_sub["fallback_level"], errors="coerce"
                ).fillna(0).astype(int),
                "abstract_source": fallback_sub["abstract_source"].apply(
                    core.clean_text
                ),
                "fallback_status": fallback_sub["final_retrieval_status"].apply(
                    core.clean_text
                ),
                "abstract_length": fallback_sub["abstract_final"].apply(
                    lambda x: len(core.clean_text(x))
                ),
                "error_message": "",
            })
            log_df = log_df[_FALLBACK_LOG_COLUMNS]
            log_df.to_csv(
                output_dir / "fallback_log.csv",
                index=False, encoding="utf-8-sig",
            )

    return df