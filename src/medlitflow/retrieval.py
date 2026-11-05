"""retrieval.py — 全文获取、二次追链与文件级去重。"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

import pandas as pd

from medlitflow import core, record_normalize, robustness

__all__ = [
    "retrieve_fulltexts",
    "resolve_candidate_urls",
    "attempt_download",
    "classify_payload",
    "detect_paywall",
    "extract_pdf_links",
    "deduplicate_files",
    "cross_check_library",
    "ACCESS_ROUTE_EUROPEPMC", "ACCESS_ROUTE_PMC",
    "ACCESS_ROUTE_DOI", "ACCESS_ROUTE_PUBLISHER",
    "ACCESS_ROUTE_INHERITED", "ACCESS_ROUTE_UNKNOWN",
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

_DEFAULT_TIMEOUT = 45
_DEFAULT_MAX_ATTEMPTS = 3
_PDF_HEADER = b"%PDF"
_HTML_PAYWALL_SCAN_BYTES = 50_000
_PARTIAL_FLUSH_INTERVAL = 10

_STATUS_429 = 429
_STATUS_BROKEN = (404, 410)
_STATUS_INACCESSIBLE = (401, 402, 403)

_RETRIEVED_EXTRA_COLUMNS = [
    "retrieval_status", "retrieval_reason", "downloaded_file_path",
    "content_format", "selected_url", "attempt_count",
    "redirect_chain_json", "access_route_used",
    "html_second_pass_status", "secondary_pdf_path",
    "file_dedup_decision", "file_download_temp_path",
    "is_library_hit", 
]

_RETRIEVAL_LOG_COLUMNS = [
    "canonical_id", "attempt_index", "url", "source_field",
    "status_code", "content_type", "elapsed_ms", "error_type",
]

_HTML_SECOND_PASS_COLUMNS = [
    "canonical_id", "source_html_path", "second_pass_status",
    "secondary_pdf_path", "selected_pdf_url",
]

_FILE_DEDUP_MANIFEST_COLUMNS = [
    "canonical_id", "source_path", "filename", "extension",
    "sha256", "dedup_group_key", "dedup_decision",
]

ACCESS_ROUTE_EUROPEPMC = "europe_pmc"
ACCESS_ROUTE_PMC = "pmc"
ACCESS_ROUTE_DOI = "doi_resolve"
ACCESS_ROUTE_PUBLISHER = "publisher_direct"
ACCESS_ROUTE_INHERITED = "library_inherited"
ACCESS_ROUTE_UNKNOWN = ""

def resolve_candidate_urls(row: pd.Series) -> list[dict]:
    candidates: list[dict] = []
    seen: set[str] = set()

    def _add(url: Any, source_field: str, priority: int, kind_hint: str) -> None:
        text = core.clean_text(url)
        if not text or not text.startswith(("http://", "https://")):
            return
        if text in seen:
            return
        seen.add(text)
        candidates.append({
            "url": text,
            "source_field": source_field,
            "priority": priority,
            "kind_hint": kind_hint,
        })

    parsed_json: list = []
    raw_json = core.clean_text(row.get("candidate_urls_json", ""))
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, list):
                parsed_json = parsed
        except json.JSONDecodeError:
            parsed_json = []

    _add(row.get("pdf_url"), "pdf_url", 1, "pdf")

    for entry in parsed_json:
        if isinstance(entry, dict):
            kind = core.clean_text(entry.get("kind_hint", "")).lower()
            if kind == "pdf":
                _add(entry.get("url"),
                     core.clean_text(entry.get("source_field", "candidate_urls_json")),
                     1, "pdf")

    _add(row.get("fulltext_url"), "fulltext_url", 2, "xml")

    for entry in parsed_json:
        if isinstance(entry, dict):
            kind = core.clean_text(entry.get("kind_hint", "")).lower()
            if kind == "xml":
                _add(entry.get("url"),
                     core.clean_text(entry.get("source_field", "candidate_urls_json")),
                     2, "xml")

    _add(row.get("landing_page_url"), "landing_page_url", 3, "html")

    for entry in parsed_json:
        if isinstance(entry, dict):
            kind = core.clean_text(entry.get("kind_hint", "")).lower()
            if kind in ("html", "unknown"):
                _add(entry.get("url"),
                     core.clean_text(entry.get("source_field", "candidate_urls_json")),
                     3, "html")

    doi = core.normalize_doi(row.get("doi"))
    if doi:
        _add(f"https://doi.org/{doi}", "doi", 4, "unknown")

    return candidates

def attempt_download(url: str, timeout: int) -> dict:
    return core.http_download(url, timeout)


def classify_payload(payload: bytes, content_type: str, url: str) -> str:
    if not payload:
        return "other"

    if payload.startswith(_PDF_HEADER):
        return "pdf"

    sample = payload[:2000].lstrip()
    sample_lower = sample.lower()

    if sample_lower.startswith(b"<?xml") or sample_lower.startswith(b"<tei"):
        return "xml"
    if (sample_lower.startswith(b"<!doctype html")
            or sample_lower.startswith(b"<html")):
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

# 付费墙常见关键词（负面信号）
_PAYWALL_KEYWORDS = (
    # 登录
    "sign in to", "sign in or", "log in to", "login to",
    "please sign in", "please log in", "register to view",
    # 订阅
    "subscribe to read", "subscribe to view", "choose a subscription",
    # 获取
    "get access", "get full access", "access options",
    "access this article", "access the full",
    # 购买
    "purchase this article", "buy this article", "buy pdf",
    "purchase pdf", "purchase access",
    # 提示
    "you do not have access", "do not have permission",
    "this content is available to subscribers",
    "this article is protected",
    "institutional access required",
    "sign in to continue reading",
)

# 医学全文的必需章节（IMRaD）
_IMRAD_SECTIONS = (
    "introduction", "background",
    "method", "methods", "materials", "patients",
    "result", "results", "findings",
    "discussion", "conclusion", "conclusions",
    "reference", "references", "bibliography",
)

# 全文特征词（图表引用）
_FULLTEXT_FEATURES = (
    "table 1", "table 2", "figure 1", "figure 2",
    "fig. 1", "fig. 2", "fig 1", "fig 2",
    "et al.", "doi:", "pmid",
)

def _paywall_suspicion_score(payload: bytes) -> int:
    """返回 0-15 的付费墙怀疑度评分，越高越可疑。"""
    if not payload:
        return 15

    text = payload[:500_000].decode("utf-8", errors="ignore").lower()
    size = len(payload)
    score = 0
    # ---- 维度 1：体积（0-4）----
    # 完整医学全文通常 100KB+；摘要页 10-50KB；付费墙 5-30KB
    if size < 5_000:
        score += 4
    elif size < 15_000:
        score += 3
    elif size < 30_000:
        score += 2
    elif size < 60_000:
        score += 1
    # ---- 维度 2：段落数（0-3）----
    # 医学全文 <p> 通常 > 30
    p_count = text.count("<p")
    if p_count < 3:
        score += 3
    elif p_count < 10:
        score += 2
    elif p_count < 20:
        score += 1
    # ---- 维度 3：IMRaD 章节（0-3）----
    # 医学全文应含 4+ 章节；付费墙通常 0-1
    section_hits = sum(1 for s in _IMRAD_SECTIONS if s in text)
    if section_hits < 2:
        score += 3
    elif section_hits < 4:
        score += 1
    # ---- 维度 4：付费墙关键词（0-3）----
    head = text[:100_000]  # 只扫前 100KB
    kw_hits = sum(1 for k in _PAYWALL_KEYWORDS if k in head)
    if kw_hits >= 3:
        score += 3
    elif kw_hits >= 2:
        score += 2
    elif kw_hits >= 1:
        score += 1
    # ---- 维度 5：全文特征词（0-2）----
    feature_hits = sum(1 for f in _FULLTEXT_FEATURES if f in text)
    if feature_hits == 0:
        score += 2
    elif feature_hits < 3:
        score += 1

    return score

def _looks_like_paywall_html(payload: bytes) -> bool:
    return _paywall_suspicion_score(payload) >= 8

def detect_paywall(payload: bytes, patterns: list[str]) -> bool:
    """基于用户自定义关键词的付费墙快速检测。"""
    if not payload or not patterns:
        return False
    text = payload[:_HTML_PAYWALL_SCAN_BYTES].decode(
        "utf-8", errors="ignore"
    ).lower()
    for pattern in patterns:
        p = str(pattern or "").strip().lower()
        if p and p in text:
            return True
    return False

def extract_pdf_links(html_text: str, bases: list[str]) -> list[str]:
    if not html_text:
        return []

    import re
    patterns = [
        r"""href=["']([^"']+(?:\.pdf|pdf=|/pdf/|download[^"']*pdf)[^"']*)["']""",
        r"""content=["']([^"']+(?:\.pdf|pdf=|/pdf/)[^"']*)["']""",
    ]

    found: list[str] = []
    for pat in patterns:
        found.extend(re.findall(pat, html_text, flags=re.IGNORECASE))

    links: list[str] = []
    seen: set[str] = set()
    for value in found:
        value = value.strip()
        if not value:
            continue
        resolved = value
        for base in (bases or [""]):
            try:
                candidate = urllib.parse.urljoin(base, value) if base else value
            except (ValueError, TypeError):
                candidate = value
            if candidate.startswith(("http://", "https://")):
                resolved = candidate
                break
        if resolved not in seen:
            seen.add(resolved)
            links.append(resolved)
    return links

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def _file_dedup_group_key(canonical_id: str, meta: dict, sha: str) -> str:
    doi = core.normalize_doi(meta.get("doi", ""))
    if doi:
        return f"doi:{doi}"
    title_norm = core.normalize_title(meta.get("title", ""))
    if title_norm:
        return f"title:{title_norm}"
    return f"hash:{sha}"

def deduplicate_files(
    file_paths: list[Path],
    meta_df: pd.DataFrame,
) -> pd.DataFrame:
    if not file_paths:
        return pd.DataFrame(columns=_FILE_DEDUP_MANIFEST_COLUMNS)

    meta_map: dict[str, dict] = {}
    if meta_df is not None and len(meta_df) > 0:
        for _, m_row in meta_df.iterrows():
            cid = core.clean_text(m_row.get("canonical_id", ""))
            if cid:
                meta_map[cid] = {
                    "doi": m_row.get("doi", ""),
                    "title": m_row.get("title", ""),
                }

    dirname_to_cid: dict[str, str] = {}
    for orig_cid in meta_map:
        dirname_to_cid[core.canonical_id_to_dirname(orig_cid)] = orig_cid

    rows: list[dict] = []
    for path in file_paths:
        path = Path(path)
        if not path.is_file():
            continue
        dirname = path.parent.name
        cid = dirname_to_cid.get(dirname, dirname)
        meta = meta_map.get(cid, {})
        try:
            sha = _sha256_file(path)
            size = path.stat().st_size
        except OSError:
            continue
        rows.append({
            "canonical_id": cid,
            "source_path": str(path),
            "filename": path.name,
            "extension": path.suffix.lower(),
            "sha256": sha,
            "_size": size,
            "dedup_group_key": _file_dedup_group_key(cid, meta, sha),
        })

    if not rows:
        return pd.DataFrame(columns=_FILE_DEDUP_MANIFEST_COLUMNS)

    manifest = pd.DataFrame(rows)
    keep_paths: set[str] = set()
    for _, group in manifest.groupby("dedup_group_key"):
        def _rank(r: dict) -> tuple:
            ext_rank = 0 if r["extension"] == ".pdf" else 1
            return (ext_rank, -int(r["_size"]))
        ordered = sorted(group.to_dict("records"), key=_rank)
        keep_paths.add(ordered[0]["source_path"])

    manifest["dedup_decision"] = manifest["source_path"].apply(
        lambda p: "keep" if p in keep_paths else "duplicate"
    )

    return manifest[_FILE_DEDUP_MANIFEST_COLUMNS]

def _infer_access_route(url: str) -> str:
    if not url:
        return ACCESS_ROUTE_PUBLISHER
    url_l = url.lower()
    if "europepmc.org" in url_l:
        return ACCESS_ROUTE_EUROPEPMC
    if "ncbi.nlm.nih.gov/pmc" in url_l or "pmc.ncbi.nlm.nih.gov" in url_l:
        return ACCESS_ROUTE_PMC
    if "doi.org" in url_l:
        return ACCESS_ROUTE_DOI
    return ACCESS_ROUTE_PUBLISHER

def _now_iso() -> str:
    return core.now_iso()

def _write_partial_parquet(records: list[dict], run_root: Path) -> None:
    if not records:
        return
    interim_dir = Path(run_root) / "interim"
    interim_dir.mkdir(parents=True, exist_ok=True)
    target = interim_dir / "retrieval.partial.parquet"
    tmp = target.with_name(target.name + ".tmp")
    df = pd.DataFrame(records)
    df.to_parquet(tmp, index=False)
    os.replace(tmp, target)

def _process_record(
    row: pd.Series,
    config: dict,
    inbox_dir: Path,
    timeout: int,
    max_attempts: int,
    paywall_patterns: list[str],
    html_second_pass_enabled: bool,
    max_links_per_html: int,
    download_delay: float,
    max_retries_per_candidate: int = 2,
) -> dict:
    canonical_id = core.clean_text(row.get("canonical_id", ""))
    record_dir = inbox_dir / core.canonical_id_to_dirname(canonical_id)
    record_dir.mkdir(parents=True, exist_ok=True)
    retrieval_status = "needs_abstract_fallback"
    retrieval_reason = "no_candidate_url"
    downloaded_path = ""
    content_format = ""
    selected_url = ""
    attempt_count = 0
    redirect_chain: list[str] = []
    html_second_pass_status = "not_attempted"
    secondary_pdf_path = ""

    candidates = resolve_candidate_urls(row)[:max_attempts]
    log_entries: list[dict] = []
    html_second_pass_row: dict | None = None
    final_downloaded_path: Path | None = None

    if not candidates:
        return _build_record_result(
            row, retrieval_status, retrieval_reason, downloaded_path,
            content_format, selected_url, attempt_count, redirect_chain,
            html_second_pass_status, secondary_pdf_path,
            log_entries, html_second_pass_row, final_downloaded_path,
        )

    for idx, cand in enumerate(candidates, start=1):
        url = cand["url"]
        attempt_count = idx
        start = time.monotonic()
        error_type = ""
        status_code = 0
        content_type = ""

        try:
            response = None
            for retry in range(max_retries_per_candidate):
                response = attempt_download(url, timeout)
                status_code = int(response.get("status_code", 0))
                if status_code not in (0, 429, 503):
                    break
                if retry < max_retries_per_candidate - 1:
                    core.interruptible_sleep(2 ** retry)
            core.interruptible_sleep(download_delay)
            status_code = int(response.get("status_code", 0))
            content_type = core.clean_text(response.get("content_type", ""))
            payload: bytes = response.get("payload", b"") or b""
            final_url = core.clean_text(response.get("final_url", url)) or url
            redirect_chain = response.get("redirect_chain", []) or []
            elapsed_ms = int((time.monotonic() - start) * 1000)

            log_entries.append({
                "canonical_id": canonical_id,
                "attempt_index": idx,
                "url": url,
                "source_field": cand["source_field"],
                "status_code": status_code,
                "content_type": content_type,
                "elapsed_ms": elapsed_ms,
                "error_type": error_type,
            })

            # 429
            if status_code == _STATUS_429:
                retrieval_status = "rate_limited"
                retrieval_reason = "rate_limited"
                selected_url = final_url
                break

            # 401/402/403
            if status_code in _STATUS_INACCESSIBLE:
                retrieval_status = "inaccessible"
                retrieval_reason = "paywall"
                selected_url = final_url
                continue

            # 404/410
            if status_code in _STATUS_BROKEN:
                retrieval_status = "broken_link"
                retrieval_reason = "broken_link"
                selected_url = final_url
                continue

            # 5xx 或其他
            if status_code == 0 or status_code >= 500 or not payload:
                continue

            # 内容分类
            kind = classify_payload(payload, content_type, final_url)

            if kind == "pdf":
                pdf_path = record_dir / "article.pdf"
                pdf_path.write_bytes(payload)
                retrieval_status = "fulltext_pdf"
                retrieval_reason = "none"
                downloaded_path = str(pdf_path)
                content_format = "pdf"
                selected_url = final_url
                final_downloaded_path = pdf_path
                break

            if kind == "xml":
                xml_path = record_dir / "article.xml"
                xml_path.write_bytes(payload)
                retrieval_status = "fulltext_xml"
                retrieval_reason = "none"
                downloaded_path = str(xml_path)
                content_format = "xml"
                selected_url = final_url
                final_downloaded_path = xml_path
                break

            if kind == "html":
                if detect_paywall(payload, paywall_patterns):
                    retrieval_status = "inaccessible"
                    retrieval_reason = "paywall"
                    selected_url = final_url
                    continue

                if _looks_like_paywall_html(payload):
                    retrieval_status = "no_fulltext_available"
                    retrieval_reason = "paywall_suspect"
                    selected_url = final_url
                    continue

                html_path = record_dir / "article.html"
                html_path.write_bytes(payload)
                final_downloaded_path = html_path

                html_second_pass_status = "not_attempted"
                secondary_success = False

                if html_second_pass_enabled:
                    html_text = payload.decode("utf-8", errors="ignore")
                    bases = [final_url]
                    pdf_links = extract_pdf_links(html_text, bases)[:max_links_per_html]

                    for link in pdf_links:
                        try:
                            sec_resp = attempt_download(link, timeout)
                            sec_payload = sec_resp.get("payload", b"") or b""
                            sec_ct = sec_resp.get("content_type", "")
                            sec_final = core.clean_text(sec_resp.get("final_url", link)) or link
                            if classify_payload(sec_payload, sec_ct, sec_final) == "pdf":
                                sec_pdf_path = record_dir / "article_secondary.pdf"
                                sec_pdf_path.write_bytes(sec_payload)
                                secondary_pdf_path = str(sec_pdf_path)
                                html_second_pass_status = "pdf_downloaded"
                                retrieval_status = "fulltext_pdf"
                                retrieval_reason = "none"
                                downloaded_path = str(sec_pdf_path)
                                content_format = "pdf"
                                selected_url = sec_final
                                final_downloaded_path = sec_pdf_path
                                secondary_success = True
                                break
                        except Exception:  # noqa: BLE001
                            continue
                        finally:
                            core.interruptible_sleep(download_delay)

                    if not secondary_success:
                        html_second_pass_status = "no_pdf_link_found"

                html_second_pass_row = {
                    "canonical_id": canonical_id,
                    "source_html_path": str(html_path),
                    "second_pass_status": html_second_pass_status,
                    "secondary_pdf_path": secondary_pdf_path,
                    "selected_pdf_url": selected_url if secondary_success else "",
                }

                if not secondary_success:
                    retrieval_status = "fulltext_html"
                    retrieval_reason = "none"
                    downloaded_path = str(html_path)
                    content_format = "html"
                    selected_url = final_url

                break

            continue

        except robustness.InterruptRequested:
            raise
        except Exception as exc:  # noqa: BLE001
            error_type = type(exc).__name__
            elapsed_ms = int((time.monotonic() - start) * 1000)
            log_entries.append({
                "canonical_id": canonical_id,
                "attempt_index": idx,
                "url": url,
                "source_field": cand["source_field"],
                "status_code": 0,
                "content_type": "",
                "elapsed_ms": elapsed_ms,
                "error_type": error_type,
            })
            core.interruptible_sleep(download_delay)
            continue

    return _build_record_result(
        row, retrieval_status, retrieval_reason, downloaded_path,
        content_format, selected_url, attempt_count, redirect_chain,
        html_second_pass_status, secondary_pdf_path,
        log_entries, html_second_pass_row, final_downloaded_path,
    )

def _build_record_result(
    row: pd.Series,
    retrieval_status: str,
    retrieval_reason: str,
    downloaded_path: str,
    content_format: str,
    selected_url: str,
    attempt_count: int,
    redirect_chain: list[str],
    html_second_pass_status: str,
    secondary_pdf_path: str,
    log_entries: list[dict],
    html_second_pass_row: dict | None,
    final_downloaded_path: Path | None,
) -> dict:
    record = row.to_dict()

    access_route = _infer_access_route(selected_url)

    record.update({
        "retrieval_status": retrieval_status,
        "retrieval_reason": retrieval_reason,
        "downloaded_file_path": downloaded_path,
        "content_format": content_format,
        "selected_url": selected_url,
        "attempt_count": int(attempt_count),
        "redirect_chain_json": json.dumps(redirect_chain, ensure_ascii=False),
        "access_route_used": access_route,
        "html_second_pass_status": html_second_pass_status,
        "secondary_pdf_path": secondary_pdf_path,
        "file_dedup_decision": "not_applicable",
        "file_download_temp_path": downloaded_path,
    })

    return {
        "record": record,
        "log_entries": log_entries,
        "html_second_pass": html_second_pass_row,
        "downloaded_path": final_downloaded_path,
    }

def cross_check_library(
    df: pd.DataFrame,
    library_root: Path,
) -> pd.DataFrame:
    df = df.copy()
    df["is_library_hit"] = False
    for col in ("library_retrieval_status", "library_downloaded_file_path",
                "library_current_file_path", "library_file_retention_level",
                "library_first_seen_run", "library_last_seen_run",
                "library_access_route_used",
                "library_special_status"):
        df[col] = ""
    df["library_appearance_count"] = 0

    records_path = library_root / "records_final.parquet"
    file_index_path = library_root / "file_index.parquet"

    if not records_path.exists():
        return df

    try:
        records = pd.read_parquet(records_path)
    except Exception:
        return df
    if len(records) == 0:
        return df

    by_cid: dict[str, dict] = {}
    for r in records.to_dict("records"):
        cid = core.clean_text(r.get("canonical_id", ""))
        if cid:
            by_cid[cid] = r

    by_doi: dict[str, dict] = {}
    for cid, r in by_cid.items():
        doi = core.normalize_doi(r.get("doi", ""))
        if doi:
            by_doi[doi] = r

    fi_by_cid: dict[str, dict] = {}
    if file_index_path.exists():
        try:
            fi = pd.read_parquet(file_index_path)
            for r in fi.to_dict("records"):
                cid = core.clean_text(r.get("canonical_id", ""))
                if cid:
                    fi_by_cid[cid] = r
        except Exception:
            pass

    df_records = df.to_dict("records")
    for idx, row in enumerate(df_records):
        cid = core.clean_text(row.get("canonical_id", ""))
        lib_row = by_cid.get(cid)
        lib_cid = cid
        if lib_row is None:
            doi = core.normalize_doi(row.get("doi", ""))
            if doi:
                lib_row = by_doi.get(doi)
                if lib_row is not None:
                    lib_cid = core.clean_text(lib_row.get("canonical_id", ""))
        if lib_row is None:
            continue

        fi_row = fi_by_cid.get(lib_cid)

        df.at[idx, "is_library_hit"] = True
        df.at[idx, "library_retrieval_status"] = core.clean_text(
            lib_row.get("retrieval_status", "")
        )
        df.at[idx, "library_downloaded_file_path"] = core.clean_text(
            lib_row.get("downloaded_file_path", "")
        )
        df.at[idx, "library_current_file_path"] = (
            core.clean_text(fi_row.get("current_file_path", ""))
            if fi_row is not None else ""
        )
        df.at[idx, "library_file_retention_level"] = (
            core.clean_text(fi_row.get("file_retention_level", ""))
            if fi_row is not None else ""
        )
        df.at[idx, "library_first_seen_run"] = core.clean_text(
            lib_row.get("first_seen_run", "")
        )
        df.at[idx, "library_last_seen_run"] = core.clean_text(
            lib_row.get("last_seen_run", "")
        )
        df.at[idx, "library_appearance_count"] = int(
            lib_row.get("appearance_count", 0) or 0
        )
        df.at[idx, "library_access_route_used"] = core.clean_text(
            lib_row.get("access_route_used", "")
        )
        df.at[idx, "library_special_status"] = core.clean_text(
            lib_row.get("special_status", "")
        )
    return df

def _clone_from_library(
    library_hits: pd.DataFrame,
) -> pd.DataFrame:
    if len(library_hits) == 0:
        return library_hits

    inherit_map = {
        "retrieval_status": "library_retrieval_status",
        "downloaded_file_path": "library_downloaded_file_path",
        "current_file_path": "library_current_file_path",
    }

    df = library_hits.copy()
    for target, src in inherit_map.items():
        if src in df.columns:
            df[target] = df[src].fillna("").astype(str)

    df["retrieval_status"] = df["retrieval_status"].where(
        df["retrieval_status"].str.strip().ne(""),
        "library_hit",
    )
    df["file_dedup_decision"] = "library_existing"
    df["file_download_temp_path"] = ""
    df["selected_url"] = ""
    df["content_format"] = ""
    df["attempt_count"] = 0
    df["redirect_chain_json"] = "[]"
    df["html_second_pass_status"] = "not_attempted"
    df["secondary_pdf_path"] = ""
    df["retrieval_reason"] = ""
    if "library_access_route_used" in df.columns:
        df["access_route_used"] = (
            df["library_access_route_used"].fillna("").astype(str)
        )
        df.loc[
            df["access_route_used"].str.strip() == "",
            "access_route_used",
        ] = ACCESS_ROUTE_INHERITED
    else:
        df["access_route_used"] = ACCESS_ROUTE_INHERITED

    if "library_special_status" in df.columns:
        noise_mask = (
            df["library_special_status"].fillna("").astype(str).str.strip()
            == "retrieval_noise"
        )
        if noise_mask.any():
            df.loc[noise_mask, "retrieval_status"] = "library_hit"
            df.loc[noise_mask, "retrieval_reason"] = "noise_marked"
            # 文件已删，路径清空（防止下游 open_literature_file 找不到文件却以为是 hit）
            df.loc[noise_mask, "downloaded_file_path"] = ""
            df.loc[noise_mask, "current_file_path"] = ""
            df.loc[noise_mask, "file_download_temp_path"] = ""
            df.loc[noise_mask, "content_format"] = ""
            df.loc[noise_mask, "selected_url"] = ""
            df.loc[noise_mask, "access_route_used"] = ACCESS_ROUTE_INHERITED

    return df

def retrieve_fulltexts(
    df: pd.DataFrame,
    config: dict,
    run_root: Path,
    *,
    initial_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if df is None or len(df) == 0:
        base_columns = list(df.columns) if isinstance(df, pd.DataFrame) else []
        return pd.DataFrame(columns=base_columns + _RETRIEVED_EXTRA_COLUMNS)

    run_root = Path(run_root)
    inbox_dir = run_root / "papers" / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    if initial_df is None:
        library_root_str = config.get("library", {}).get("root", "")
        xcheck_cfg = (config.get("retrieval", {}) or {}).get(
            "library_cross_check", {}
        )
        if library_root_str and xcheck_cfg.get("enabled", True):
            library_root = Path(library_root_str)
            if library_root.exists():
                df = cross_check_library(df, library_root)
    else:
        if "is_library_hit" not in df.columns:
            df = df.copy()
            df["is_library_hit"] = False

    if "is_library_hit" in df.columns:
        hit_mask = record_normalize.to_bool_series(df["is_library_hit"])
    else:
        hit_mask = pd.Series([False] * len(df), index=df.index)

    library_hits = df[hit_mask].copy()
    new_records = df[~hit_mask].copy()
    original_columns = list(df.columns)

    retrieval_cfg = config.get("retrieval", {}) or {}
    timeout = int(retrieval_cfg.get("timeout_seconds", _DEFAULT_TIMEOUT))
    max_attempts = int(retrieval_cfg.get("max_attempts_per_record", _DEFAULT_MAX_ATTEMPTS))
    _max_retries_cfg = 2
    try:
        _max_retries_cfg = int(
            retrieval_cfg.get("max_retries_per_candidate", 2)
        )
    except (ValueError, TypeError):
        pass
    max_retries_per_candidate = max(1, min(_max_retries_cfg, 5))
    paywall_patterns = retrieval_cfg.get("paywall_patterns", []) or []
    html_second_pass_cfg = retrieval_cfg.get("html_second_pass", {}) or {}
    html_second_pass_enabled = bool(html_second_pass_cfg.get("enabled", True))
    max_links_per_html = int(html_second_pass_cfg.get("max_links_per_html", 5))
    download_delay = float((config.get("rate_limit", {}) or {}).get("download", 0.25))
    accumulated_records: list[dict] = []
    if initial_df is not None and len(initial_df) > 0:
        accumulated_records.extend(initial_df.to_dict("records"))
    if len(library_hits) > 0:
        library_hits = _clone_from_library(library_hits)
        accumulated_records.extend(library_hits.to_dict("records"))

    retrieved_rows: list[dict] = []
    retrieval_log_rows: list[dict] = []
    html_second_pass_rows: list[dict] = []
    all_downloaded_paths: list[Path] = []

    new_records_list = list(new_records.iterrows())
    total_new = len(new_records_list)

    if total_new > 0:
        _emit_log(f"retrieval: 开始处理 {total_new} 条记录")

    try:
        for i, (_, row) in enumerate(new_records_list, start=1):
            robustness.check_shutdown()

            _emit_progress("retrieval", i, total_new)
            cid_for_log = core.clean_text(row.get("canonical_id", ""))
            _emit_log(f"retrieval: [{i}/{total_new}] {cid_for_log}")

            result = _process_record(
                row, config, inbox_dir, timeout, max_attempts,
                paywall_patterns, html_second_pass_enabled, max_links_per_html,
                download_delay, max_retries_per_candidate,
            )
            rec = result["record"]
            retrieved_rows.append(rec)
            accumulated_records.append(rec)
            retrieval_log_rows.extend(result["log_entries"])
            if result["html_second_pass"]:
                html_second_pass_rows.append(result["html_second_pass"])
            if result["downloaded_path"]:
                all_downloaded_paths.append(result["downloaded_path"])

            _emit_log(
                f"retrieval: {cid_for_log} → "
                f"{rec.get('retrieval_status', '')}"
            )

            if i % _PARTIAL_FLUSH_INTERVAL == 0:
                _write_partial_parquet(accumulated_records, run_root)
    except robustness.InterruptRequested:
        try:
            _write_partial_parquet(accumulated_records, run_root)
        except Exception:  # noqa: BLE001
            pass

        try:
            completed_cids: list[str] = []
            seen_cids: set[str] = set()
            for r in accumulated_records:
                if not isinstance(r, dict):
                    continue
                cid = core.clean_text(r.get("canonical_id", ""))
                if cid and cid not in seen_cids:
                    seen_cids.add(cid)
                    completed_cids.append(cid)

            pending_cids: list[str] = []
            for _, r in new_records_list:
                cid = core.clean_text(r.get("canonical_id", ""))
                if cid and cid not in seen_cids:
                    pending_cids.append(cid)

            robustness.mark_partial(run_root, "retrieval", {
                "completed_cids": completed_cids,
                "pending_cids": pending_cids,
                "completed_count": len(completed_cids),
                "total_count": len(completed_cids) + len(pending_cids),
                "checkpoint_at": _now_iso(),
                "checkpoint_parquet": "interim/retrieval.partial.parquet",
            })
        except Exception:  # noqa: BLE001
            pass

        raise

    if accumulated_records:
        _write_partial_parquet(accumulated_records, run_root)

    retrieved_new = pd.DataFrame(retrieved_rows) if retrieved_rows else pd.DataFrame()

    if len(retrieved_new) > 0 and len(library_hits) > 0:
        combined = pd.concat([retrieved_new, library_hits], axis=0)
        combined = combined.sort_index()
    elif len(retrieved_new) > 0:
        combined = retrieved_new
    elif len(library_hits) > 0:
        combined = library_hits
    else:
        combined = pd.DataFrame()

    if initial_df is not None and len(initial_df) > 0:
        if len(combined) > 0:
            combined = pd.concat([initial_df, combined], axis=0)
        else:
            combined = initial_df

    retrieved_df = combined.reset_index(drop=True)

    for col in _RETRIEVED_EXTRA_COLUMNS:
        if col not in retrieved_df.columns:
            if col == "is_library_hit":
                retrieved_df[col] = False
            else:
                retrieved_df[col] = ""

    drop_cols = [c for c in retrieved_df.columns if c.startswith("library_")]
    if drop_cols:
        retrieved_df = retrieved_df.drop(columns=drop_cols)

    ordered_columns = [
        c for c in original_columns if not c.startswith("library_")
    ] + _RETRIEVED_EXTRA_COLUMNS
    seen: set[str] = set()
    final_columns: list[str] = []
    for c in ordered_columns:
        if c in retrieved_df.columns and c not in seen:
            final_columns.append(c)
            seen.add(c)
    for c in _RETRIEVED_EXTRA_COLUMNS:
        if c in retrieved_df.columns and c not in seen:
            final_columns.append(c)
            seen.add(c)
    retrieved_df = retrieved_df[final_columns]
    output_dir = run_root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    if all_downloaded_paths:
        file_dedup_manifest = deduplicate_files(all_downloaded_paths, retrieved_df)

        if len(file_dedup_manifest) > 0:
            path_to_decision = dict(zip(
                file_dedup_manifest["source_path"],
                file_dedup_manifest["dedup_decision"],
            ))
            group_to_keep = {}
            for _, m_row in file_dedup_manifest.iterrows():
                if m_row["dedup_decision"] == "keep":
                    group_to_keep[m_row["dedup_group_key"]] = m_row["source_path"]
            path_to_group = dict(zip(
                file_dedup_manifest["source_path"],
                file_dedup_manifest["dedup_group_key"],
            ))

            for idx, row in retrieved_df.iterrows():
                temp_path = core.clean_text(row.get("file_download_temp_path", ""))
                if not temp_path:
                    continue
                decision = path_to_decision.get(temp_path, "not_applicable")
                retrieved_df.at[idx, "file_dedup_decision"] = decision
                if decision == "duplicate":
                    group = path_to_group.get(temp_path, "")
                    keep_path = group_to_keep.get(group, "")
                    if keep_path:
                        retrieved_df.at[idx, "downloaded_file_path"] = keep_path

            file_dedup_manifest.to_csv(
                output_dir / "file_dedup_manifest.csv",
                index=False, encoding="utf-8-sig",
            )

    if retrieval_log_rows:
        pd.DataFrame(retrieval_log_rows, columns=_RETRIEVAL_LOG_COLUMNS).to_csv(
            output_dir / "retrieval_log.csv",
            index=False, encoding="utf-8-sig",
        )
    if html_second_pass_rows:
        pd.DataFrame(
            html_second_pass_rows, columns=_HTML_SECOND_PASS_COLUMNS,
        ).to_csv(
            output_dir / "html_second_pass_log.csv",
            index=False, encoding="utf-8-sig",
        )

    return retrieved_df