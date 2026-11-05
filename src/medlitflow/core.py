"""core.py — 统一工具层（配置 / 标准化 / HTTP / XML 解析 / 出版类型）。"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import yaml

_USER_AGENT = "medlitflow/0.1"
_DEFAULT_TIMEOUT = 15
_DEFAULT_RETRIES = 3
# 含 500/502/504：Crossref / OpenAlex 偶发 502/504，单次网关抖动若不重试
# 会被 _log_source_failure 吞掉整个源（整源 0 条）；500 是 PubMed 偶发重启。
_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
RETRY_AFTER_MAX_SECONDS = 60.0

_INVALID_DIR_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WS_RE = re.compile(r"\s+")

from medlitflow.record_normalize import (
    clean_text,
    normalize_doi,
    normalize_pmid,
    normalize_pmcid,
    normalize_title,
)

_APP_TZ_ENV_KEY = "MEDLITFLOW_TZ"

def _app_tz():
    tz_name = os.environ.get(_APP_TZ_ENV_KEY, "").strip()
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(tz_name)
        except Exception:  # ImportError / KeyError / ValueError
            pass
    return None

def now_iso() -> str:
    tz = _app_tz()
    try:
        if tz is None:
            return datetime.now().astimezone().isoformat()
        return datetime.now(tz).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()

def today_iso() -> str:
    return now_iso()[:10]

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def timestamp_slug() -> str:
    tz = _app_tz()
    try:
        dt = datetime.now(tz) if tz is not None else datetime.now()
        return dt.strftime("%Y%m%dT%H%M%S%f")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")

_STRUCTURED_LABEL_RE = re.compile(
    r"\b(?:"
    r"background|methods?|results?|conclusions?|objectives?|"
    r"introduction|discussion|findings|aims?|purposes?|setting|"
    r"design|participants?|interventions?|exposures?|"
    r"main outcome(?:s)?(?: and measures?)?|"
    r"outcome measures?|measurements?|significance|importance|"
    r"limitations?|clinical trial registration|registration|"
    r"funding|context|design and setting|"
    r"results and conclusions|conclusion and relevance"
    r")\s*:\s*",
    re.IGNORECASE,
)

__all__ = [
    "ConfigError",
    "RetryExhaustedError",
    "load_config",
    "find_project_root",
    "user_data_root",
    "clean_text",
    "normalize_doi",
    "normalize_pmid",
    "normalize_pmcid",
    "normalize_title",
    "hash_abstract",
    "generate_canonical_id",
    "generate_legacy_record_id",
    "canonical_id_to_dirname",
    "http_get_json",
    "http_get_text",
    "http_download",
    "append_http_audit",
    "interruptible_sleep",
    "RETRY_AFTER_MAX_SECONDS",
    "chunked",
    "stable_file_stem",
    "content_extension",
    "parse_pubmed_xml",
    "normalize_publication_type",
    "categorize_publication_type",
    "open_file_with_default_app",
    "secure_file_permissions",
    "now_iso",
    "today_iso",
    "utc_now_iso",
    "timestamp_slug",
]

class ConfigError(Exception):
    """配置文件缺失或格式错误。"""

class RetryExhaustedError(Exception):
    """HTTP 请求重试次数用尽。"""

_http_audit_dir: Path | None = None
_http_audit_lock = threading.Lock()

def _register_http_audit_sink(run_root: Path) -> None:
    global _http_audit_dir
    _http_audit_dir = Path(run_root) / "audit"


_SENSITIVE_QUERY_KEYS = frozenset({
    "api_key", "apikey", "api-key", "apikey".replace("-", "_"),
    "key",
    "email", "mailto",
})


def _redact_url_for_audit(url: str) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(str(url))
    except (ValueError, TypeError):
        return str(url)
    if not parts.query:
        return str(url)
    try:
        items = parse_qsl(parts.query, keep_blank_values=True)
    except (ValueError, TypeError):
        return str(url)
    redacted = [
        (k, "REDACTED" if k.lower() in _SENSITIVE_QUERY_KEYS else v)
        for k, v in items
    ]
    try:
        new_query = urlencode(redacted, doseq=True)
    except (ValueError, TypeError):
        return str(url)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
    )


def _append_http_audit(entry: dict) -> None:
    if _http_audit_dir is None:
        return
    try:
        _http_audit_dir.mkdir(parents=True, exist_ok=True)
        log_path = _http_audit_dir / "http_audit.jsonl"
        safe_entry = {
            **entry,
            "url": _redact_url_for_audit(str(entry.get("url") or "")),
        }
        line = json.dumps(safe_entry, ensure_ascii=False) + "\n"
        with _http_audit_lock:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        return

def append_http_audit(entry: dict) -> None:
    _append_http_audit(entry)

def _iso_now() -> str:
    return now_iso()

def find_project_root() -> Path:
    env_root = os.environ.get("MEDLITFLOW_ROOT")
    if env_root:
        return Path(env_root).resolve()

    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)

    current = Path(__file__).resolve()
    for parent in [current.parent, *current.parents]:
        if (parent / "config" / "default.yaml").exists():
            return parent
    return Path.cwd()

_find_project_root = find_project_root

def user_data_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return find_project_root()

def secure_file_permissions(path: Path) -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        import getpass
        import subprocess
        username = getpass.getuser()
        if not username:
            return False
        result = subprocess.run(
            [
                "icacls", str(path),
                "/inheritance:r",
                "/grant:r", f"{username}:F",
            ],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False

def _resolve_output_root(config: dict) -> Path:
    raw = config.get("project", {}).get("output_root", "./data")
    if not isinstance(raw, str):
        return user_data_root() / "data"
    if raw.startswith("/"):
        return Path(raw)
    if raw.startswith("./"):
        return (user_data_root() / raw[2:]).resolve()
    return (user_data_root() / raw).resolve()


def _anchor_paths(config: dict) -> dict:
    output_root = _resolve_output_root(config)
    path_key_suffixes = ("_root", "_dir", "_path", "_directory")

    def _is_path_key(key: str) -> bool:
        return (
            any(suffix in key for suffix in path_key_suffixes)
            or key in ("output_root", "root")
        )

    def _anchor(value: str, key: str = "") -> str:
        if value.startswith(("http://", "https://")):
            return value
        if value.startswith("/"):
            return value
        if value.startswith("./") or value.startswith("../"):
            return str((user_data_root() / value).resolve())
        if key == "output_root":
            return str(output_root)
        if "/" in value:
            return str((output_root / value).resolve())
        return value

    def _walk(node):
        if isinstance(node, dict):
            result: dict = {}
            for key, value in node.items():
                if isinstance(value, dict):
                    result[key] = _walk(value)
                elif _is_path_key(key) and isinstance(value, str):
                    result[key] = _anchor(value, key)
                else:
                    result[key] = value
            return result
        return node

    return _walk(config)

def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result

def load_config(path: str | Path | None = None) -> dict:
    project_root = _find_project_root()

    default_path = project_root / "config" / "default.yaml"
    if not default_path.exists():
        raise ConfigError(f"默认配置文件缺失：{default_path}")

    try:
        base = yaml.safe_load(default_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"默认配置解析失败：{exc}") from exc

    if path is None:
        merged = base
    else:
        user_path = Path(path)
        if not user_path.is_absolute():
            user_path = (project_root / user_path).resolve()
        if not user_path.exists():
            raise ConfigError(f"用户配置文件缺失：{user_path}")
        try:
            user = yaml.safe_load(user_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"用户配置解析失败：{exc}") from exc
        merged = _deep_merge(base, user)

    return _anchor_paths(merged)

def hash_abstract(value: Any) -> str:
    """对摘要文本做稳定的 SHA256 前 16 位 hash。"""
    if value is None:
        return hashlib.sha256(b"").hexdigest()[:16]
    text = str(value)
    if not text:
        return hashlib.sha256(b"").hexdigest()[:16]

    stripped = _STRUCTURED_LABEL_RE.sub(" ", text)
    normalized = _WS_RE.sub("", stripped).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

def generate_canonical_id(row: dict) -> str:
    """生成 canonical_id。优先级：DOI > PMID > PMCID > 标题哈希 > 行回退。"""
    doi = normalize_doi(row.get("doi"))
    if doi:
        return f"doi:{doi}"

    pmid = normalize_pmid(row.get("pmid"))
    if pmid:
        return f"pmid:{pmid}"

    pmcid = normalize_pmcid(row.get("pmcid"))
    if pmcid:
        return f"pmcid:{pmcid}"

    title_norm = normalize_title(row.get("title"))
    if title_norm:
        digest = hashlib.sha256(title_norm.encode("utf-8")).hexdigest()[:16]
        return f"title:{digest}"

    source = row.get("source_database", "unknown")
    record_id = row.get("source_record_id", "unknown")
    return f"row:{source}:{record_id}"

def generate_legacy_record_id(index: int) -> str:
    return f"KWRAW-{index:06d}"

_MAX_CANONICAL_DIRNAME_LEN = 150

def canonical_id_to_dirname(canonical_id: str) -> str:
    if not canonical_id:
        return "_unknown"
    safe = _INVALID_DIR_CHARS_RE.sub("_", canonical_id)
    safe = safe.strip(" .")
    if not safe:
        return "_unknown"
    if len(safe) > _MAX_CANONICAL_DIRNAME_LEN:
        digest = hashlib.sha256(
            canonical_id.encode("utf-8")
        ).hexdigest()[:12]
        safe = f"{safe[:_MAX_CANONICAL_DIRNAME_LEN - 13]}_{digest}"
    return safe

def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        wait = float(raw)
    except ValueError:
        return None
    return min(wait, RETRY_AFTER_MAX_SECONDS)

def _backoff_seconds(attempt: int) -> float:
    return float(2 ** (attempt - 1))

def _check_shutdown_safe() -> bool:
    try:
        from medlitflow import robustness
        return robustness.is_shutdown_requested()
    except Exception:  # noqa: BLE001
        return False

def _interruptible_sleep(seconds: float) -> None:
    if seconds <= 0:
        return
    end = time.monotonic() + seconds
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        if _check_shutdown_safe():
            from medlitflow import robustness
            raise robustness.InterruptRequested(
                f"用户取消（HTTP 等待 {seconds:.1f}s 被中断）"
            )
        time.sleep(min(0.3, remaining))

interruptible_sleep = _interruptible_sleep

def _do_request(
    method: str,
    url: str,
    params: dict | None,
    headers: dict | None,
    delay: float,
    retries: int,
    accept: str,
    timeout: int = _DEFAULT_TIMEOUT,
) -> httpx.Response:
    """内部：执行 HTTP 请求，处理 429 / 500 / 502 / 503 / 504 重试与审计。"""
    request_headers = {"User-Agent": _USER_AGENT, "Accept": accept}
    if headers:
        request_headers.update(headers)

    last_exc: Exception | None = None

    for attempt in range(1, retries + 1):
        if _check_shutdown_safe():
            from medlitflow import robustness
            raise robustness.InterruptRequested(
                f"HTTP {method} {url} 被用户取消（attempt={attempt}）"
            )

        start = time.monotonic()
        try:
            _timeout_cfg = httpx.Timeout(
                connect=5.0,
                read=float(timeout),
                write=float(timeout),
                pool=5.0,
            )
            with httpx.Client(
                timeout=_timeout_cfg, follow_redirects=True,
            ) as client:
                with client.stream(
                    method, url, params=params, headers=request_headers
                ) as response:
                    buffer = bytearray()
                    for chunk in response.iter_bytes(8192):
                        if _check_shutdown_safe():
                            from medlitflow import robustness
                            raise robustness.InterruptRequested(
                                f"用户取消（HTTP {method} 响应中断）"
                            )
                        buffer.extend(chunk)
                    response._content = bytes(buffer)
            elapsed_ms = int((time.monotonic() - start) * 1000)

            _append_http_audit({
                "url": str(response.url),
                "method": method,
                "status_code": response.status_code,
                "elapsed_ms": elapsed_ms,
                "retry_count": attempt - 1,
                "timestamp": _iso_now(),
            })

            if response.status_code in _RETRY_STATUS_CODES:
                if attempt < retries:
                    wait = _retry_after_seconds(response)
                    if wait is None:
                        wait = _backoff_seconds(attempt)
                    _interruptible_sleep(wait)
                    continue

                raise RetryExhaustedError(
                    f"HTTP {method} {url} 重试 {retries} 次后仍返回 "
                    f"{response.status_code}"
                )

            _interruptible_sleep(delay)
            return response

        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < retries:
                _interruptible_sleep(_backoff_seconds(attempt))
                continue

    if last_exc is not None:
        raise RetryExhaustedError(
            f"HTTP {method} {url} 重试 {retries} 次后失败：{last_exc}"
        ) from last_exc
    raise RetryExhaustedError(
        f"HTTP {method} {url} 重试 {retries} 次后仍失败"
    )

def http_get_json(
    url: str,
    params: dict,
    headers: dict,
    delay: float,
    retries: int = _DEFAULT_RETRIES,
) -> dict:
    """GET 请求，返回 JSON dict。处理 429 / 500 / 502 / 503 / 504 重试。"""
    response = _do_request(
        "GET", url, params, headers, delay, retries, "application/json"
    )
    response.raise_for_status()
    return response.json()

def http_get_text(
    url: str,
    params: dict,
    headers: dict,
    delay: float,
    retries: int = _DEFAULT_RETRIES,
) -> str:
    """GET 请求，返回文本。处理 429 / 500 / 502 / 503 / 504 重试。"""
    response = _do_request(
        "GET", url, params, headers, delay, retries, "text/xml, text/html, */*"
    )
    response.raise_for_status()
    return response.text

def http_download(url: str, timeout: int) -> dict:
    from medlitflow import robustness
    start = time.monotonic()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            with client.stream("GET", url, headers={"User-Agent": _USER_AGENT}) as response:
                payload_buf = bytearray()
                for chunk in response.iter_bytes(8192):
                    if _check_shutdown_safe():
                        raise robustness.InterruptRequested("用户取消（下载中断）")
                    payload_buf.extend(chunk)
                
        elapsed_ms = int((time.monotonic() - start) * 1000)

        _append_http_audit({
            "url": url,
            "method": "GET",
            "status_code": response.status_code,
            "elapsed_ms": elapsed_ms,
            "retry_count": 0,
            "timestamp": _iso_now(),
        })

        return {
            "payload": bytes(payload_buf),
            "content_type": response.headers.get("Content-Type", ""),
            "final_url": str(response.url),
            "status_code": response.status_code,
            "redirect_chain": [str(r.url) for r in response.history],
        }
    except robustness.InterruptRequested:
        raise
    except httpx.HTTPError:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _append_http_audit({
            "url": url,
            "method": "GET",
            "status_code": 0,
            "elapsed_ms": elapsed_ms,
            "retry_count": 0,
            "timestamp": _iso_now(),
        })
        return {
            "payload": b"",
            "content_type": "",
            "final_url": url,
            "status_code": 0,
            "redirect_chain": [],
        }

def chunked(items: list, size: int) -> Iterator[list]:
    if size <= 0:
        raise ValueError(f"size 必须 > 0，实际为 {size}")
    for i in range(0, len(items), size):
        yield items[i : i + size]

def _safe_slug(text: str, max_len: int = 120) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", text)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    return cleaned[:max_len] or "record"

def stable_file_stem(row: dict) -> str:
    authors = clean_text(row.get("authors", ""))
    first_author = "unknown_author"
    if authors:
        first = authors.split(";")[0].strip()
        surname = re.split(r"[\s,]+", first)[0] if first else ""
        if surname:
            first_author = _safe_slug(surname.lower(), max_len=40)

    year_raw = row.get("year")
    if year_raw is None or (isinstance(year_raw, float) and year_raw != year_raw):
        year = "unknown_year"
    else:
        year = clean_text(year_raw) or "unknown_year"

    title = (
        clean_text(row.get("title", ""))
        or clean_text(row.get("doi", ""))
        or "untitled"
    )
    title_slug = _safe_slug(title.lower(), max_len=60)

    return _safe_slug(f"{first_author}_{year}_{title_slug}", max_len=110)

def content_extension(content_type: str, url: str) -> str:
    ct = clean_text(content_type).lower()
    url_l = (url or "").lower()

    if "pdf" in ct or url_l.endswith(".pdf") or "/pdf/" in url_l or "pdf=" in url_l:
        return ".pdf"
    if "xml" in ct or url_l.endswith(".xml"):
        return ".xml"
    if "html" in ct or ct.startswith("text/"):
        return ".html"
    return ".bin"

def open_file_with_default_app(path: str | Path) -> bool:
    import subprocess
    import sys

    file_path = Path(path)
    if not file_path.is_file():
        return False

    try:
        if sys.platform.startswith("win"):
            os.startfile(str(file_path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["open", str(file_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                ["xdg-open", str(file_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return True
    except (OSError, FileNotFoundError):
        return False

_NORMALIZED_TYPE_MAP = {
    # Europe PMC
    "research-article": "Journal Article",
    "review": "Review",
    "editorial": "Editorial",
    "letter": "Letter",
    "correction": "Correction",
    "retraction": "Retraction",
    "retraction of publication": "Retraction",
    # Crossref / OpenAlex
    "journal-article": "Journal Article",
    "posted-content": "Preprint",
    "book-chapter": "Book Chapter",
    "proceedings-article": "Congress",
    "book": "Book",
    "dataset": "Dataset",
    "dissertation": "Dissertation",
    "report": "Report",
}

_RESEARCH_TYPES = frozenset({
    "Journal Article",
    "Randomized Controlled Trial",
    "Clinical Trial",
    "Comparative Study",
    "Research Article",
})
_REVIEW_TYPES = frozenset({
    "Review",
    "Systematic Review",
    "Meta-Analysis",
})
_EDITORIAL_TYPES = frozenset({
    "Editorial",
    "Comment",
    "Letter",
    "News",
})

def normalize_publication_type(raw_type: str, source: str) -> str:
    if not raw_type:
        return ""

    if source == "pubmed":
        return clean_text(raw_type)

    parts = [p.strip() for p in re.split(r"[;,]", raw_type) if p.strip()]
    normalized: list[str] = []
    for part in parts:
        key = part.lower()
        mapped = _NORMALIZED_TYPE_MAP.get(key, part)
        if mapped not in normalized:
            normalized.append(mapped)
    return "; ".join(normalized)

def categorize_publication_type(normalized_type: str) -> str:
    if not normalized_type:
        return "other"

    types = [t.strip() for t in re.split(r"[;,]", normalized_type) if t.strip()]

    if any(t in _REVIEW_TYPES for t in types):
        return "review"
    if any(t in _EDITORIAL_TYPES for t in types):
        return "editorial"
    if any(t in _RESEARCH_TYPES for t in types):
        return "research"
    return "other"

def parse_pubmed_xml(xml_text: str) -> list[dict]:
    if not xml_text or not xml_text.strip():
        return []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    records: list[dict] = []

    # PubMed 格式
    for article in root.findall(".//PubmedArticle"):
        record = _parse_pubmed_article(article)
        if record:
            records.append(record)

    # PMC JATS 格式（efetch db=pmc）
    for article in root.findall(".//article"):
        record = _parse_pmc_article(article)
        if record:
            records.append(record)

    return records

def _parse_pubmed_article(article: ET.Element) -> dict:
    medline = article.find("MedlineCitation")
    if medline is None:
        return {}

    pmid_elem = medline.find("PMID")
    pmid = clean_text(pmid_elem.text) if pmid_elem is not None else ""

    art = medline.find("Article")
    if art is None:
        return {}

    # 标题
    title = ""
    title_elem = art.find("ArticleTitle")
    if title_elem is not None:
        title = clean_text("".join(title_elem.itertext()))

    # 摘要
    abstract_parts: list[str] = []
    abstract_missing = True
    abstract_el = art.find("Abstract")
    if abstract_el is not None:
        for at in abstract_el.findall("AbstractText"):
            label = at.get("Label", "")
            text = clean_text("".join(at.itertext()))
            if text:
                abstract_missing = False
                abstract_parts.append(f"{label}: {text}" if label else text)
    abstract = " ".join(abstract_parts)

    # 作者
    authors: list[str] = []
    author_list = art.find("AuthorList")
    if author_list is not None:
        for author in author_list.findall("Author"):
            last = author.findtext("LastName", "")
            fore = author.findtext("ForeName", "")
            collective = author.findtext("CollectiveName", "")
            if collective:
                authors.append(clean_text(collective))
            elif last or fore:
                name = f"{clean_text(last)} {clean_text(fore)}".strip()
                if name:
                    authors.append(name)

    # 期刊
    journal = ""
    journal_el = art.find("Journal")
    if journal_el is not None:
        journal = clean_text(journal_el.findtext("Title", ""))

    # 年份（优先 ArticleDate，其次 PubDate/Year）
    year: int | None = None
    for date_el in art.findall(".//ArticleDate"):
        y = date_el.findtext("Year", "")
        if y:
            try:
                year = int(y)
                break
            except ValueError:
                pass
    if year is None:
        pub_date = art.find(".//PubDate")
        if pub_date is not None:
            y = pub_date.findtext("Year", "")
            if y:
                try:
                    year = int(y)
                except ValueError:
                    pass

    # DOI / PMCID（从 PubmedData/ArticleIdList）
    doi = ""
    pmcid = ""
    for article_id in article.findall(".//ArticleId"):
        id_type = article_id.get("IdType", "")
        id_value = clean_text(article_id.text)
        if id_type == "doi":
            doi = normalize_doi(id_value)
        elif id_type == "pmc":
            pmcid = normalize_pmcid(id_value)

    # 出版类型
    pub_types: list[str] = []
    for pt in art.findall(".//PublicationType"):
        text = clean_text(pt.text)
        if text and text not in pub_types:
            pub_types.append(text)
    raw_type = "; ".join(pub_types)
    normalized_type = normalize_publication_type(raw_type, "pubmed")

    return {
        "source_database": "pubmed",
        "source_record_id": pmid,
        "title": title,
        "abstract": abstract,
        "abstract_missing": abstract_missing,
        "authors": "; ".join(authors),
        "journal": journal,
        "year": year,
        "doi": doi,
        "pmid": pmid,
        "pmcid": pmcid,
        "publication_type_raw": raw_type,
        "publication_type_normalized": normalized_type,
        "publication_type_category": categorize_publication_type(normalized_type),
    }

def _parse_pmc_article(article: ET.Element) -> dict:
    pmcid_elem = article.find(".//article-id[@pub-id-type='pmc']")
    pmcid = normalize_pmcid(pmcid_elem.text) if pmcid_elem is not None else ""

    pmid_elem = article.find(".//article-id[@pub-id-type='pmid']")
    pmid = clean_text(pmid_elem.text) if pmid_elem is not None else ""

    doi_elem = article.find(".//article-id[@pub-id-type='doi']")
    doi = normalize_doi(doi_elem.text) if doi_elem is not None else ""

    title_elem = article.find(".//article-title")
    title = clean_text("".join(title_elem.itertext())) if title_elem is not None else ""

    abstract_parts: list[str] = []
    for p in article.findall(".//abstract//p"):
        text = clean_text("".join(p.itertext()))
        if text:
            abstract_parts.append(text)
    abstract = " ".join(abstract_parts)

    # 作者
    authors: list[str] = []
    for contrib in article.findall(".//contrib[@contrib-type='author']"):
        surname = contrib.findtext(".//surname", "")
        given = contrib.findtext(".//given-names", "")
        name = f"{clean_text(surname)} {clean_text(given)}".strip()
        if name:
            authors.append(name)

    # 年份
    year: int | None = None
    year_elem = article.find(".//pub-date/year")
    if year_elem is not None:
        try:
            year = int(clean_text(year_elem.text))
        except ValueError:
            pass

    journal = ""
    journal_title_elem = article.find(".//journal-title")
    if journal_title_elem is not None:
        journal = clean_text(
            "".join(journal_title_elem.itertext())
        )

    return {
        "source_database": "pmc",
        "source_record_id": pmcid or pmid,
        "title": title,
        "abstract": abstract,
        "abstract_missing": not abstract,
        "authors": "; ".join(authors),
        "journal": journal,
        "year": year,
        "doi": doi,
        "pmid": pmid,
        "pmcid": pmcid,
        "publication_type_raw": "",
        "publication_type_normalized": "",
        "publication_type_category": "other",
    }