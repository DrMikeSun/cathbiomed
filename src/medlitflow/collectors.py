"""collectors.py — 四源文献采集器。"""

from __future__ import annotations

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from medlitflow import core, query_translator, record_normalize, robustness

__all__ = [
    "collect_all",
    "collect_pubmed",
    "collect_europepmc",
    "collect_crossref",
    "collect_openalex",
    "resolve_effective_limits",
]

def _emit_progress(stage: str, current: int, total: int) -> None:
    """向注入的 Interactor 发送进度事件。失败静默。"""
    try:
        from medlitflow import pipeline
        pipeline.get_interactor().progress(stage, current, total)
    except Exception:  # noqa: BLE001
        pass

def _emit_log(message: str) -> None:
    """向注入的 Interactor 发送日志事件。失败静默。"""
    try:
        from medlitflow import pipeline
        pipeline.get_interactor().log(message)
    except Exception:  # noqa: BLE001
        pass

def _check_interrupt() -> None:
    from medlitflow import robustness
    robustness.check_shutdown()

_NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_EUROPEPMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_CROSSREF_URL = "https://api.crossref.org/works"
_OPENALEX_URL = "https://api.openalex.org/works"

_PMC_EFETCH_BATCH = 200
_PUBMED_EFETCH_BATCH = 100

_PARALLEL_SOURCES = ("pubmed", "europepmc", "crossref", "openalex")
_SOURCE_ORDER = ("pubmed", "pmc", "europepmc", "crossref", "openalex")
_RAW_WRITE_LOCK = threading.Lock()
_STAGE1_FIELDS = [
    "source_database", "source_record_id", "query_name", "search_query",
    "title", "abstract", "abstract_missing", "authors", "journal", "year",
    "doi", "pmid", "pmcid",
    "publication_type_raw", "publication_type_normalized",
    "publication_type_category",
    "open_access_flag",
    "fulltext_url", "pdf_url", "landing_page_url",
    "license", "licenses_json", "candidate_urls_json",
    "raw_response_path",
    "title_key", "authors_key", "journal_key", "doi_key", "year_key",
]

def _timestamp_slug() -> str:
    return core.timestamp_slug()

def _resolve_run_root(config: dict) -> Path:
    output_root = Path(config["project"]["output_root"])
    run_name = config["project"].get("run_name", "")
    if not run_name:
        raise ValueError("config.project.run_name 未设置")
    return output_root / "runs" / run_name

def _save_raw_response(raw_dir: Path, prefix: str, payload: Any) -> str:
    with _RAW_WRITE_LOCK:
        raw_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{prefix}_{_timestamp_slug()}.json"
        file_path = raw_dir / filename
        file_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(file_path)

def _save_raw_xml(raw_dir: Path, prefix: str, xml_text: str) -> str:
    with _RAW_WRITE_LOCK:
        raw_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{prefix}_{_timestamp_slug()}.xml"
        file_path = raw_dir / filename
        file_path.write_text(xml_text, encoding="utf-8")
        return str(file_path)

def _log_source_failure(source: str, exc: BaseException) -> None:
    message = (
        f"[collectors] 数据源 {source} 采集失败，已跳过："
        f"{type(exc).__name__}: {exc}"
    )
    _emit_log(message)
    print(message, file=sys.stderr)

def _json_or_empty(value: Any) -> str:
    if value is None:
        return "[]"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)

def _empty_stage1_record() -> dict:
    return {f: "" for f in _STAGE1_FIELDS}

def _finalize_record(record: dict, raw_dir: Path) -> dict:
    base = _empty_stage1_record()
    base.update(record)
    if "year" in base and base["year"] in ("", None):
        base["year"] = None
    if "abstract_missing" not in base or base["abstract_missing"] is None:
        base["abstract_missing"] = not bool(base.get("abstract"))
    if "open_access_flag" not in base or base["open_access_flag"] is None:
        base["open_access_flag"] = False
    if not base.get("licenses_json"):
        base["licenses_json"] = "[]"
    if not base.get("candidate_urls_json"):
        base["candidate_urls_json"] = "[]"
    if not base.get("raw_response_path"):
        base["raw_response_path"] = str(raw_dir)
    if not str(base.get("source_record_id", "") or "").strip():
        fallback_id = _derive_fallback_record_id(base)
        base["source_record_id"] = fallback_id
        msg = (
            f"[collectors] {base.get('source_database', '?')} "
            f"记录缺少官方 ID（pmcid/pmid），已兜底为 {fallback_id}；"
            f"title={str(base.get('title', ''))[:80]!r}"
        )
        _emit_log(msg)
        print(msg, file=sys.stderr)
    return record_normalize.normalize_record(
        base, base.get("source_database", ""),
    )

def _derive_fallback_record_id(base: dict) -> str:
    import hashlib as _hashlib
    import uuid as _uuid

    doi = core.normalize_doi(base.get("doi", ""))
    if doi:
        return f"doi:{doi}"

    title = core.clean_text(base.get("title", ""))
    if title:
        h = _hashlib.sha256(title.encode("utf-8")).hexdigest()[:16]
        return f"title:{h}"

    return f"missing:{_uuid.uuid4().hex[:8]}"

def _resolve_queries(config: dict) -> list[dict]:
    injected = config.get("queries")
    if isinstance(injected, list) and injected:
        return injected
    return []

def _find_query(queries: list[dict], strategy_name: str) -> dict | None:
    for q in queries:
        if q.get("name") == strategy_name:
            return q
    return None

def _resolve_enabled_sources(config: dict) -> dict[str, bool]:
    sources_cfg = (config.get("retrieval", {}) or {}).get("sources", {}) or {}
    if not sources_cfg:
        return {src: True for src in _SOURCE_ORDER}
    return {
        src: bool((sources_cfg.get(src, {}) or {}).get("enabled", True))
        for src in _SOURCE_ORDER
    }

# 各数据源单次 API 请求的上限（由各 API 官方文档规定）
# 语义：控制单次 HTTP 请求的 pageSize / rows / per-page / retmax
_PAGE_SIZE_LIMITS = {
    "pubmed": 10_000,       # esearch retmax 上限（单次请求）
    "europepmc": 1_000,     # pageSize 上限
    "crossref": 1_000,      # rows 上限
    "openalex": 200,        # per-page 上限
}

# 各数据源的总量安全上限（防止分页请求数爆炸）
# 语义：
#   - pubmed：esearch retmax 硬上限 10000（无法突破）
#   - europepmc：API 无硬上限，安全阈值 10000
#   - crossref：offset 硬上限 10000（offset + rows ≤ 10000）
#   - openalex：API 无硬上限，安全阈值 10000
_TOTAL_LIMITS = {
    "pubmed": 10_000,
    "pmc": 10_000, 
    "europepmc": 10_000,
    "crossref": 10_000,
    "openalex": 10_000,
}

# Crossref 分页硬约束：offset + rows ≤ 10000
_CROSSREF_OFFSET_MAX = 10_000

def _extract_max_results(config: dict, source: str) -> int:
    retrieval = config.get("retrieval", {}) or {}
    configured = int(retrieval.get("max_results_per_query", 1000))
    total_limit = _TOTAL_LIMITS.get(source, 1000)

    if configured <= 0:
        return total_limit
    return min(configured, total_limit)

def resolve_effective_limits(config: dict) -> dict[str, int]:
    sources_cfg = (config.get("retrieval", {}) or {}).get("sources", {}) or {}

    limits: dict[str, int] = {}
    for src in _SOURCE_ORDER:
        enabled = bool((sources_cfg.get(src, {}) or {}).get("enabled", True))
        if not enabled:
            continue
        limits[src] = _extract_max_results(config, src)
    return limits

def collect_all(
    config: dict,
    strategy_name: str,
    run_name: str,
    last_run_dates: dict[str, str] | None = None,
) -> pd.DataFrame:
    queries = _resolve_queries(config)
    query_cfg = _find_query(queries, strategy_name)
    if query_cfg is None:
        raise ValueError(
            f"config['queries'] 中未找到 strategy_name={strategy_name}"
        )

    config_copy = dict(config)
    config_copy["project"] = dict(config["project"])
    config_copy["project"]["run_name"] = run_name

    translated = query_translator.translate_pubmed_query(
        query_cfg["pubmed"],
        incremental_dates=last_run_dates,
    )

    europepmc_query = query_cfg.get("europepmc")
    if europepmc_query and last_run_dates and "europepmc" in last_run_dates:
        start = last_run_dates["europepmc"]
        end = core.today_iso()
        europepmc_query = (
            f'({europepmc_query}) AND FIRST_PDATE:[{start} TO {end}]'
        )
    if not europepmc_query:
        europepmc_query = translated["europepmc"]

    if "crossref" in query_cfg and query_cfg["crossref"]:
        filt = core.clean_text(query_cfg.get("crossref_filter", ""))
        if last_run_dates and "crossref" in last_run_dates:
            start = last_run_dates["crossref"]
            end = core.today_iso()
            date_f = f"from-pub-date:{start},until-pub-date:{end}"
            filt = f"{filt},{date_f}" if filt else date_f
        crossref_queries = [{
            "query.bibliographic": query_cfg["crossref"],
            **({"filter": filt} if filt else {}),
        }]
    else:
        crossref_queries = translated["crossref"]

    if "openalex" in query_cfg and query_cfg["openalex"]:
        filt = core.clean_text(query_cfg.get("openalex_filter", ""))
        if last_run_dates and "openalex" in last_run_dates:
            start = last_run_dates["openalex"]
            end = core.today_iso()
            date_f = (
                f"from_publication_date:{start},"
                f"to_publication_date:{end}"
            )
            filt = f"{filt},{date_f}" if filt else date_f
        openalex_queries = [{
            "search": query_cfg["openalex"],
            **({"filter": filt} if filt else {}),
        }]
    else:
        openalex_queries = translated["openalex"]

    enabled = _resolve_enabled_sources(config)
    retrieval_cfg = config.get("retrieval", {}) or {}
    parallel = bool(retrieval_cfg.get("parallel", False))
    max_workers = int(retrieval_cfg.get("max_workers", 4))
    tasks: dict[str, Any] = {}

    if enabled["pubmed"]:
        tasks["pubmed"] = lambda: _collect_pubmed_single_channel_task(
            db="pubmed",
            strategy_name=strategy_name,
            search_query=translated["pubmed"],
            max_results=_extract_max_results(config, "pubmed"),
            config_copy=config_copy,
        )
    if enabled["pmc"]:
        tasks["pmc"] = lambda: _collect_pubmed_single_channel_task(
            db="pmc",
            strategy_name=strategy_name,
            search_query=translated["pubmed"],
            max_results=_extract_max_results(config, "pmc"),
            config_copy=config_copy,
        )
    if enabled["europepmc"]:
        tasks["europepmc"] = lambda: collect_europepmc(
            query={"name": strategy_name, "query": europepmc_query},
            max_results=_extract_max_results(config, "europepmc"),
            config=config_copy,
        )
    if enabled["crossref"]:
        tasks["crossref"] = lambda: collect_crossref(
            query={"name": strategy_name, "search_queries": crossref_queries},
            max_results=_extract_max_results(config, "crossref"),
            config=config_copy,
        )
    if enabled["openalex"]:
        tasks["openalex"] = lambda: collect_openalex(
            query={"name": strategy_name, "search_queries": openalex_queries},
            max_results=_extract_max_results(config, "openalex"),
            config=config_copy,
        )

    results: dict[str, list[dict]] = {}

    total_tasks = len(tasks)
    _emit_log(f"collect: 启用源 {list(tasks.keys())}")

    if parallel and len(tasks) > 1:
        executor = ThreadPoolExecutor(max_workers=max_workers)
        futures = {
            executor.submit(fn): name for name, fn in tasks.items()
        }
        for name in tasks.keys():
            _emit_log(f"collect: 开始 {name}")

        completed = 0
        try:
            from concurrent.futures import wait, FIRST_COMPLETED

            pending = set(futures.keys())
            while pending:
                _check_interrupt()
                done, pending = wait(
                    pending,
                    timeout=1.0,
                    return_when=FIRST_COMPLETED,
                )
                for fut in done:
                    name = futures[fut]
                    completed += 1
                    try:
                        results[name] = fut.result()
                        _emit_log(
                            f"collect: {name} 完成，"
                            f"{len(results[name])} 条"
                        )
                    except robustness.InterruptRequested:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        results[name] = []
                        _log_source_failure(name, exc)
                    _emit_progress("collect", completed, total_tasks)
        finally:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                executor.shutdown(wait=False)
    else:
        for i, (name, fn) in enumerate(tasks.items(), start=1):
            _emit_log(f"collect: 开始 {name}")
            _check_interrupt()
            try:
                results[name] = fn()
                _emit_log(
                    f"collect: {name} 完成，{len(results[name])} 条"
                )
            except robustness.InterruptRequested:
                raise
            except Exception as exc:  # noqa: BLE001
                results[name] = []
                _log_source_failure(name, exc)
            _check_interrupt()
            _emit_progress("collect", i, total_tasks)

    _emit_log(
        f"collect: 全部完成，合并前共 "
        f"{sum(len(v) for v in results.values())} 条"
    )

    # pubmed 任务内部已按 pubmed → pmc 顺序返回，因此这里按
    # _SOURCE_ORDER 汇总：pubmed → pmc → europepmc → crossref → openalex
    all_rows: list[dict] = []
    all_rows.extend(results.get("pubmed", []))
    all_rows.extend(results.get("pmc", []))
    all_rows.extend(results.get("europepmc", []))
    all_rows.extend(results.get("crossref", []))
    all_rows.extend(results.get("openalex", []))

    if not all_rows:
        return pd.DataFrame(columns=_STAGE1_FIELDS)

    df = pd.DataFrame(all_rows)

    for col in _STAGE1_FIELDS:
        if col not in df.columns:
            df[col] = ""

    before = len(df)
    df = df.drop_duplicates(
        subset=["source_database", "source_record_id"],
        keep="first",
    ).reset_index(drop=True)
    after = len(df)

    if after < before:
        msg = (
            f"[collectors] 单源内去重：{before} → {after}"
            f"（移除 {before - after} 条）"
        )
        _emit_log(msg)
        print(msg, file=sys.stderr)

    def _is_title_empty(t: Any) -> bool:
        if t is None:
            return True
        if isinstance(t, float) and t != t:   # NaN
            return True
        if isinstance(t, str):
            return not t.strip()
        return False

    title_empty_mask = df["title"].apply(_is_title_empty)
    dropped_count = int(title_empty_mask.sum())
    if dropped_count > 0:
        dropped_by_source = (
            df.loc[title_empty_mask, "source_database"]
            .astype(str)
            .value_counts()
            .to_dict()
        )
        df = df[~title_empty_mask].reset_index(drop=True)

        src_dist = ", ".join(
            f"{k}: {v}" for k, v in sorted(dropped_by_source.items())
        )
        _emit_log(
            f"collect: 剔除 {dropped_count} 条无 title 记录"
            f"（来源分布：{src_dist}）"
        )

        try:
            run_root = (
                Path(config["project"]["output_root"]) / "runs" / run_name
            )
            quality_dir = run_root / "interim"
            quality_dir.mkdir(parents=True, exist_ok=True)
            quality_path = quality_dir / "collect_quality.json"
            tmp_path = quality_path.with_name(quality_path.name + ".tmp")
            tmp_path.write_text(
                json.dumps(
                    {
                        "dropped_no_title": dropped_count,
                        "by_source": dropped_by_source,
                        "recorded_at": datetime.now(
                            timezone.utc
                        ).isoformat(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(tmp_path, quality_path)
        except (OSError, KeyError, TypeError):
            pass

    return df[_STAGE1_FIELDS]

def collect_pubmed(query: dict, max_results: int, config: dict) -> list[dict]:
    enabled = {"pubmed": True, "pmc": True}
    return _collect_pubmed_channels(
        query, max_results, config, enabled,
    )

def _collect_pubmed_channels(
    query: dict,
    max_results: int,
    config: dict,
    enabled: dict[str, bool],
) -> list[dict]:
    query_name = query.get("name", "")
    search_query = query.get("query", "")
    if not search_query:
        return []

    run_root = _resolve_run_root(config)
    raw_dir = run_root / "raw" / "pubmed"

    delay = config["rate_limit"]["pubmed"]
    email = config["api"].get("email", "")
    api_key = config["api"].get("ncbi_api_key", "")
    pmc_max = _extract_max_results(config, "pmc")

    rows: list[dict] = []
    if enabled.get("pubmed", True):
        rows.extend(_collect_pubmed_channel(
            query_name, search_query, max_results,
            delay, email, api_key, raw_dir, db="pubmed",
        ))
    if enabled.get("pmc", True):
        rows.extend(_collect_pubmed_channel(
            query_name, search_query, pmc_max,
            delay, email, api_key, raw_dir, db="pmc",
        ))
    return rows

def _collect_pubmed_single_channel_task(
    db: str,
    strategy_name: str,
    search_query: str,
    max_results: int,
    config_copy: dict,
) -> list[dict]:
    run_root = _resolve_run_root(config_copy)
    raw_dir = run_root / "raw" / "pubmed"
    return _collect_pubmed_channel(
        query_name=strategy_name,
        search_query=search_query,
        max_results=max_results,
        delay=config_copy["rate_limit"]["pubmed"],
        email=config_copy["api"].get("email", ""),
        api_key=config_copy["api"].get("ncbi_api_key", ""),
        raw_dir=raw_dir,
        db=db,
    )

def _collect_pubmed_channel(
    query_name: str,
    search_query: str,
    max_results: int,
    delay: float,
    email: str,
    api_key: str,
    raw_dir: Path,
    db: str,
) -> list[dict]:
    # NCBI esearch retmax 单次上限 10000（当前版本不支持 history 分页）
    retmax_capped = min(max_results, _PAGE_SIZE_LIMITS["pubmed"])
    if max_results > retmax_capped:
        _emit_log(
            f"{db}: retmax 从 {max_results} 截断到 {retmax_capped}"
            f"（NCBI esearch 单次上限 10000；"
            f"如需更多，请细化检索式或分批执行）"
        )

    esearch_params = {
        "db": db,
        "term": search_query,
        "retmax": retmax_capped,
        "retmode": "json",
    }
    if email:
        esearch_params["email"] = email
    if api_key:
        esearch_params["api_key"] = api_key

    _emit_log(f"{db}: esearch 请求（retmax={retmax_capped}）")
    esearch_json = core.http_get_json(
        f"{_NCBI_BASE}/esearch.fcgi",
        params=esearch_params,
        headers={},
        delay=delay,
    )

    _check_interrupt()

    id_list = esearch_json.get("esearchresult", {}).get("idlist", [])

    _emit_log(f"{db}: esearch 返回 {len(id_list)} 条 ID")
    if not id_list:
        return []

    batch_size = _PUBMED_EFETCH_BATCH if db == "pubmed" else _PMC_EFETCH_BATCH
    rows: list[dict] = []

    chunks = list(core.chunked(id_list, batch_size))
    total_batches = len(chunks)

    for batch_idx, chunk in enumerate(chunks, start=1):
        _check_interrupt()

        _emit_log(
            f"{db}: efetch 批次 {batch_idx}/{total_batches} 请求"
            f"（{len(chunk)} 条）"
        )

        if db == "pmc":
            ids = ",".join(_numeric_pmc_id(i) for i in chunk)
        else:
            ids = ",".join(chunk)

        efetch_params = {"db": db, "id": ids, "retmode": "xml"}
        if email:
            efetch_params["email"] = email
        if api_key:
            efetch_params["api_key"] = api_key

        efetch_xml = core.http_get_text(
            f"{_NCBI_BASE}/efetch.fcgi",
            params=efetch_params,
            headers={},
            delay=delay,
        )

        xml_path_str = _save_raw_xml(
            raw_dir, f"{db}_efetch_{query_name}", efetch_xml,
        )
        _save_raw_response(
            raw_dir, f"{db}_efetch_{query_name}",
            {"xml_path": xml_path_str, "xml_length": len(efetch_xml)},
        )

        parsed = core.parse_pubmed_xml(efetch_xml)

        _emit_log(
            f"{db}: efetch 批次 {batch_idx} 返回 "
            f"{len(efetch_xml)} 字节，解析 {len(parsed)} 条"
        )
        _check_interrupt()
        for record in parsed:
            rows.append(
                _finalize_pubmed_record(record, query_name, search_query, raw_dir)
            )

    return rows

def _numeric_pmc_id(pmcid: str) -> str:
    text = str(pmcid).upper()
    if text.startswith("PMC"):
        text = text[3:]
    return "".join(ch for ch in text if ch.isdigit())


def _finalize_pubmed_record(
    record: dict, query_name: str, search_query: str, raw_dir: Path,
) -> dict:
    pmcid = record.get("pmcid", "")
    pmid = record.get("pmid", "")
    source_db = record.get("source_database", "pubmed")

    if pmcid:
        fulltext_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
        pdf_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/"
    else:
        fulltext_url = ""
        pdf_url = ""

    if source_db == "pubmed" and pmid:
        landing = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    else:
        landing = fulltext_url

    candidate_urls = [u for u in [fulltext_url, pdf_url] if u]
    record.update({
        "query_name": query_name,
        "search_query": search_query,
        "open_access_flag": bool(pmcid),
        "fulltext_url": fulltext_url,
        "pdf_url": pdf_url,
        "landing_page_url": landing,
        "license": "",
        "licenses_json": "[]",
        "candidate_urls_json": _json_or_empty(candidate_urls),
        "raw_response_path": str(raw_dir),
    })
    return _finalize_record(record, raw_dir)

def collect_europepmc(query: dict, max_results: int, config: dict) -> list[dict]:
    query_name = query.get("name", "")
    search_query = query.get("query", "")
    if not search_query:
        return []

    run_root = _resolve_run_root(config)
    raw_dir = run_root / "raw" / "europepmc"

    delay = config["rate_limit"]["europepmc"]
    page_size = min(_PAGE_SIZE_LIMITS["europepmc"], max_results)

    rows: list[dict] = []
    cursor = "*"        # Europe PMC cursorMark 起始值
    retrieved = 0
    first_payload: dict | None = None

    while retrieved < max_results:
        _check_interrupt()

        _emit_log(
            f"europepmc: 分页请求（cursor={cursor[:24]}...，"
            f"pageSize={page_size}）"
        )
        payload = core.http_get_json(
            _EUROPEPMC_SEARCH_URL,
            params={
                "query": search_query,
                "format": "json",
                "resultType": "core",
                "pageSize": page_size,
                "cursorMark": cursor,
            },
            headers={},
            delay=delay,
        )
        if first_payload is None:
            first_payload = payload
            _save_raw_response(raw_dir, f"search_{query_name}", payload)

        results = payload.get("resultList", {}).get("result", []) or []

        _emit_log(f"europepmc: 分页返回 {len(results)} 条")
        if not results:
            break

        for item in results:
            if retrieved >= max_results:
                break
            rows.append(_parse_europepmc_item(
                item, query_name, search_query, raw_dir,
            ))
            retrieved += 1

        next_cursor = core.clean_text(payload.get("nextCursorMark", ""))
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    return rows

def _parse_europepmc_item(
    item: dict, query_name: str, search_query: str, raw_dir: Path,
) -> dict:
    pmcid = core.normalize_pmcid(item.get("pmcid"))
    pmid = core.normalize_pmid(item.get("pmid"))
    doi = core.normalize_doi(item.get("doi"))

    fulltext_urls: list[dict] = []
    pdf_url_from_list = ""
    for entry in (item.get("fullTextUrlList") or {}).get("fullTextUrl", []) or []:
        url = core.clean_text(entry.get("url"))
        style = core.clean_text(entry.get("documentStyle")).lower()
        if url:
            fulltext_urls.append({"url": url, "style": style})
            if style == "pdf" and not pdf_url_from_list:
                pdf_url_from_list = url

    has_pdf = core.clean_text(item.get("hasPDF")).upper() == "Y"
    if not pdf_url_from_list and has_pdf and pmcid:
        pdf_url_from_list = f"https://europepmc.org/articles/{pmcid}?pdf=render"

    if pmcid:
        fulltext_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
    elif fulltext_urls:
        fulltext_url = fulltext_urls[0]["url"]
    else:
        fulltext_url = ""

    landing = (f"https://europepmc.org/article/MED/{pmid}"
               if pmid else fulltext_url)

    pub_type_raw = core.clean_text(item.get("pubType"))
    pub_type_norm = core.normalize_publication_type(pub_type_raw, "europepmc")
    pub_type_cat = core.categorize_publication_type(pub_type_norm)

    license_text = core.clean_text(item.get("license"))
    licenses = [license_text] if license_text else []

    is_oa = core.clean_text(item.get("isOpenAccess")).upper() == "Y"

    candidate_urls = []
    for entry in fulltext_urls:
        candidate_urls.append({
            "url": entry["url"],
            "source_field": "fullTextUrlList",
            "kind_hint": entry["style"] or "unknown",
        })
    if pdf_url_from_list:
        candidate_urls.append({
            "url": pdf_url_from_list,
            "source_field": "pdf_url",
            "kind_hint": "pdf",
        })

    year_raw = item.get("pubYear")
    try:
        year = int(str(year_raw)) if year_raw else None
    except (TypeError, ValueError):
        year = None

    record = {
        "source_database": "europepmc",
        "source_record_id": core.clean_text(item.get("id")),
        "query_name": query_name,
        "search_query": search_query,
        "title": core.clean_text(item.get("title")),
        "abstract": core.clean_text(item.get("abstractText")),
        "abstract_missing": not bool(core.clean_text(item.get("abstractText"))),
        "authors": record_normalize.normalize_authors_from_europepmc(item.get("authorString")),
        "journal": core.clean_text(item.get("journalTitle")),
        "year": year,
        "doi": doi,
        "pmid": pmid,
        "pmcid": pmcid,
        "publication_type_raw": pub_type_raw,
        "publication_type_normalized": pub_type_norm,
        "publication_type_category": pub_type_cat,
        "open_access_flag": is_oa,
        "fulltext_url": fulltext_url,
        "pdf_url": pdf_url_from_list,
        "landing_page_url": landing,
        "license": license_text,
        "licenses_json": _json_or_empty(licenses),
        "candidate_urls_json": _json_or_empty(candidate_urls),
        "raw_response_path": str(raw_dir),
    }
    return _finalize_record(record, raw_dir)

def collect_crossref(query: dict, max_results: int, config: dict) -> list[dict]:
    query_name = query.get("name", "")
    search_queries = query.get("search_queries", [])
    if not search_queries:
        return []

    run_root = _resolve_run_root(config)
    raw_dir = run_root / "raw" / "crossref"

    delay = config["rate_limit"]["crossref"]
    email = config["api"].get("email", "")
    headers = {"User-Agent": f"medlitflow/0.1 (mailto:{email or 'none@example.com'})"}
    rows_per_query = max(1, max_results // max(len(search_queries), 1))

    rows: list[dict] = []
    for idx, sq in enumerate(search_queries):
        params: dict = {"rows": min(rows_per_query, _PAGE_SIZE_LIMITS["crossref"])}
        if sq.get("query.bibliographic"):
            params["query.bibliographic"] = sq["query.bibliographic"]
        if sq.get("query.author"):
            params["query.author"] = sq["query.author"]
        if sq.get("filter"):
            params["filter"] = sq["filter"]

        retrieved = 0
        page_size = min(rows_per_query, _PAGE_SIZE_LIMITS["crossref"])
        offset = 0
        while retrieved < rows_per_query:
            _check_interrupt()

            rows_to_fetch = min(page_size, rows_per_query - retrieved)
            if offset + rows_to_fetch > _CROSSREF_OFFSET_MAX:
                _emit_log(
                    f"crossref: 达 offset 上限 {_CROSSREF_OFFSET_MAX}，"
                    f"已停止翻页（已获取 {retrieved}/{rows_per_query} 条）"
                )
                break

            params["rows"] = rows_to_fetch
            params["offset"] = offset

            _emit_log(
                f"crossref: 分页请求（offset={offset}，"
                f"rows={rows_to_fetch}）"
            )
            payload = core.http_get_json(_CROSSREF_URL, params=params,
                                          headers=headers, delay=delay)
            _save_raw_response(
                raw_dir, f"search_{query_name}_{idx}_{offset}", payload
            )
            items = payload.get("message", {}).get("items", []) or []

            _emit_log(f"crossref: 分页返回 {len(items)} 条")
            if not items:
                break
            for item in items:
                if retrieved >= rows_per_query:
                    break
                rows.append(_parse_crossref_item(item, query_name, sq, raw_dir))
                retrieved += 1
            if len(items) < params["rows"]:
                break
            offset += params["rows"]

    return rows

def _parse_crossref_item(
    item: dict, query_name: str, sq: dict, raw_dir: Path,
) -> dict:
    abstract_raw = item.get("abstract", "") or ""
    abstract_cleaned = abstract_raw.replace("<jats:p>", "\n").replace("</jats:p>", "")
    abstract = core.clean_text(abstract_cleaned)

    links = item.get("link", []) or []
    pdf_url = ""
    fulltext_url = ""
    candidate_urls = []
    for link in links:
        url = core.clean_text(link.get("URL"))
        ct = core.clean_text(link.get("content-type")).lower()
        if not url:
            continue
        kind = "pdf" if "pdf" in ct else ("xml" if "xml" in ct else "html")
        candidate_urls.append({
            "url": url,
            "source_field": "link",
            "kind_hint": kind,
        })
        if not pdf_url and "pdf" in ct:
            pdf_url = url
        if not fulltext_url:
            fulltext_url = url
    if not fulltext_url:
        fulltext_url = core.clean_text(
            (item.get("resource") or {}).get("primary", {}).get("URL")
        )

    licenses: list[str] = []
    for entry in item.get("license", []) or []:
        u = core.clean_text(entry.get("URL"))
        if u:
            licenses.append(u)

    is_oa = any(
        ("creativecommons.org" in u or "opensource.org" in u)
        for u in licenses
    )

    year: int | None = None
    for key in ("published-print", "published-online", "issued", "created"):
        parts = ((item.get(key) or {}).get("date-parts") or [])
        if parts and parts[0]:
            try:
                year = int(parts[0][0])
                break
            except (TypeError, ValueError):
                continue

    pub_type_raw = core.clean_text(item.get("type"))
    pub_type_norm = core.normalize_publication_type(pub_type_raw, "crossref")
    pub_type_cat = core.categorize_publication_type(pub_type_norm)

    search_query_str = (
        sq.get("query.bibliographic", "")
        or sq.get("filter", "")
        or ""
    )
    if sq.get("query.author"):
        search_query_str = (search_query_str + " " + sq["query.author"]).strip()

    record = {
        "source_database": "crossref",
        "source_record_id": core.clean_text(item.get("DOI")) or core.clean_text(item.get("URL")),
        "query_name": query_name,
        "search_query": search_query_str,
        "title": core.clean_text(" ".join(item.get("title", []) or [])),
        "abstract": abstract,
        "abstract_missing": not bool(abstract),
        "authors": record_normalize.normalize_authors_from_crossref(item),
        "journal": core.clean_text(" ".join(item.get("container-title", []) or [])),
        "year": year,
        "doi": core.normalize_doi(item.get("DOI")),
        "pmid": "",
        "pmcid": "",
        "publication_type_raw": pub_type_raw,
        "publication_type_normalized": pub_type_norm,
        "publication_type_category": pub_type_cat,
        "open_access_flag": is_oa,
        "fulltext_url": fulltext_url,
        "pdf_url": pdf_url,
        "landing_page_url": core.clean_text(item.get("URL")),
        "license": "; ".join(licenses),
        "licenses_json": _json_or_empty(licenses),
        "candidate_urls_json": _json_or_empty(candidate_urls),
        "raw_response_path": str(raw_dir),
    }
    return _finalize_record(record, raw_dir)

def collect_openalex(query: dict, max_results: int, config: dict) -> list[dict]:
    query_name = query.get("name", "")
    search_queries = query.get("search_queries", [])
    if not search_queries:
        return []

    run_root = _resolve_run_root(config)
    raw_dir = run_root / "raw" / "openalex"

    delay = config["rate_limit"]["openalex"]
    email = config["api"].get("email", "")
    openalex_key = str(
        config.get("api", {}).get("openalex_api_key", "") or ""
    ).strip()
    headers = {"User-Agent": f"medlitflow/0.1 (mailto:{email or 'none@example.com'})"}
    rows_per_query = max(1, max_results // max(len(search_queries), 1))

    rows: list[dict] = []
    for idx, sq in enumerate(search_queries):
        cursor = "*"
        retrieved = 0
        while retrieved < rows_per_query:
            _check_interrupt()

            params: dict = {
                "per-page": min(
                    _PAGE_SIZE_LIMITS["openalex"],
                    rows_per_query - retrieved,
                ),
                "cursor": cursor,
            }
            if sq.get("search"):
                params["search"] = sq["search"]
            if sq.get("filter"):
                params["filter"] = sq["filter"]
            if openalex_key:
                params["api_key"] = openalex_key

            _emit_log(
                f"openalex: 分页请求（cursor={cursor[:24]}...，"
                f"per-page={params['per-page']}）"
            )
            payload = core.http_get_json(_OPENALEX_URL, params=params,
                                          headers=headers, delay=delay)
            _save_raw_response(raw_dir, f"search_{query_name}_{idx}", payload)
            items = payload.get("results", []) or []

            _emit_log(f"openalex: 分页返回 {len(items)} 条")
            if not items:
                break
            for item in items:
                if retrieved >= rows_per_query:
                    break
                rows.append(_parse_openalex_item(item, query_name, sq, raw_dir))
                retrieved += 1
            cursor = (payload.get("meta") or {}).get("next_cursor", "")
            if not cursor or len(items) < params["per-page"]:
                break

    return rows

def _parse_openalex_item(
    item: dict, query_name: str, sq: dict, raw_dir: Path,
) -> dict:
    abstract = _reconstruct_openalex_abstract(item.get("abstract_inverted_index"))

    oa = item.get("open_access", {}) or {}
    best_oa = item.get("best_oa_location") or {}
    primary = item.get("primary_location") or {}

    pdf_url = core.clean_text(best_oa.get("pdf_url"))
    oa_url = (
        core.clean_text(oa.get("oa_url"))
        or pdf_url
        or core.clean_text(best_oa.get("landing_page_url"))
        or core.clean_text(primary.get("landing_page_url"))
    )

    license_text = (
        core.clean_text(best_oa.get("license"))
        or core.clean_text(primary.get("license"))
    )
    licenses = [license_text] if license_text else []

    ids = item.get("ids") or {}
    doi = core.normalize_doi(item.get("doi") or ids.get("doi"))
    pmid = core.normalize_pmid(ids.get("pmid"))

    pub_type_raw = core.clean_text(item.get("type"))
    pub_type_norm = core.normalize_publication_type(pub_type_raw, "openalex")
    pub_type_cat = core.categorize_publication_type(pub_type_norm)

    candidate_urls = []
    if pdf_url:
        candidate_urls.append({
            "url": pdf_url, "source_field": "best_oa_location.pdf_url",
            "kind_hint": "pdf",
        })
    if oa_url and oa_url != pdf_url:
        candidate_urls.append({
            "url": oa_url, "source_field": "open_access.oa_url",
            "kind_hint": "unknown",
        })

    year: int | None = None
    py = item.get("publication_year")
    if py is not None:
        try:
            year = int(py)
        except (TypeError, ValueError):
            year = None

    search_query_str = (
        sq.get("search")
        or sq.get("filter")
        or ""
    )

    record = {
        "source_database": "openalex",
        "source_record_id": core.clean_text(item.get("id")),
        "query_name": query_name,
        "search_query": search_query_str,
        "title": core.clean_text(item.get("title")),
        "abstract": abstract,
        "abstract_missing": not bool(abstract),
        "authors": record_normalize.normalize_authors_from_openalex(item),
        "journal": core.clean_text((primary.get("source") or {}).get("display_name")),
        "year": year,
        "doi": doi,
        "pmid": pmid,
        "pmcid": "",
        "publication_type_raw": pub_type_raw,
        "publication_type_normalized": pub_type_norm,
        "publication_type_category": pub_type_cat,
        "open_access_flag": bool(oa.get("is_oa")),
        "fulltext_url": oa_url,
        "pdf_url": pdf_url,
        "landing_page_url": core.clean_text(item.get("id")),
        "license": license_text,
        "licenses_json": _json_or_empty(licenses),
        "candidate_urls_json": _json_or_empty(candidate_urls),
        "raw_response_path": str(raw_dir),
    }
    return _finalize_record(record, raw_dir)

def _reconstruct_openalex_abstract(inverted_index: dict | None) -> str:
    if not inverted_index:
        return ""
    positions: dict[int, str] = {}
    for token, values in inverted_index.items():
        for pos in values or []:
            positions[pos] = token
    if not positions:
        return ""
    return core.clean_text(" ".join(positions[k] for k in sorted(positions)))