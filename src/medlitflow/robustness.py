"""robustness.py — 健壮性基础模块。

提供：启动体检、单实例锁、权限/磁盘检查、信号处理、partial 标记、
Schema 校验、阶段 done 标记、API 变更检测、中断机制（_shutdown_event +
InterruptRequested + request/is/check/reset/get_shutdown_detail）。"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from medlitflow import core

__all__ = [
    "startup_health_check",
    "acquire_lock",
    "release_lock",
    "is_lock_stale",
    "check_permissions",
    "check_disk_space",
    "register_signal_handler",
    "mark_partial",
    "load_partial",
    "validate_stage_output",
    "write_stage_done",
    "read_stage_done",
    "detect_api_change",
    "InterruptRequested",
    "request_shutdown",
    "is_shutdown_requested",
    "check_shutdown",
    "reset_shutdown",
    "get_shutdown_detail",
]

_HEALTH_CHECK_TOTAL_TIMEOUT_SECONDS = 5.0
_API_PROBE_TIMEOUT_SECONDS = 4.0

_LOCK_FILENAME = ".pipeline.lock"

_DEFAULT_DISK_REQUIRED_MB = 1024

_STAGE_DONE_DIR = ".stage_done"
_PARTIAL_SUFFIX = ".partial.json"
_DONE_SUFFIX = ".done"

_JCR_SEPTEMBER_BOUNDARY = 9
_DEFAULT_JCR_ACTIVE_YEAR = 2025

_ANCHOR_DOI = "10.1016/j.cell.2011.02.013"
_ANCHOR_PMID = "21376230"
_ANCHOR_TITLE_KEYWORD = "hallmarks"

_HEALTH_CHECK_SOURCES = ("pubmed", "europepmc", "crossref", "openalex")

_FORBIDDEN_DIR_NAMES = frozenset({
    # Windows
    "windows", "system32", "syswow64",
    "program files", "program files (x86)",
    "programdata", "perflogs",
    # Unix / macOS
    "etc", "usr", "bin", "sbin", "boot",
    "dev", "proc", "sys", "var", "opt",
    "library", "system", "applications",
})

_SIGNAL_REGISTERED = False
_shutdown_event = threading.Event()
_shutdown_detail: str = ""

_AUDIT_LOCK = threading.Lock()

def _now_iso() -> str:
    return core.now_iso()


def _permission_error(path: Path, message: str) -> dict:
    return {
        "category": "permission",
        "source": str(path),
        "message": message,
        "action": "检查文件系统权限或以管理员身份运行",
    }

class InterruptRequested(Exception):
    """协作式中断异常。

    由 `check_shutdown()` 在长循环内部抛出；上层捕获后写入 partial 状态并向上传播。"""

def request_shutdown(detail: str = "") -> None:
    global _shutdown_detail
    _shutdown_event.set()
    if detail and not _shutdown_detail:
        _shutdown_detail = detail

def is_shutdown_requested() -> bool:
    return _shutdown_event.is_set()

def check_shutdown() -> None:
    if _shutdown_event.is_set():
        raise InterruptRequested(
            _shutdown_detail or "中断已请求"
        )

def reset_shutdown() -> None:
    global _shutdown_detail
    _shutdown_event.clear()
    _shutdown_detail = ""

def get_shutdown_detail() -> str:
    return _shutdown_detail

def _resolve_enabled_sources(config: dict) -> list[str]:
    sources_cfg = config.get("retrieval", {}).get("sources")
    if not sources_cfg:
        return list(_HEALTH_CHECK_SOURCES)
    return [
        src
        for src in _HEALTH_CHECK_SOURCES
        if bool(sources_cfg.get(src, {}).get("enabled", True))
    ]

def _http_probe_json(url: str, params: dict, timeout: float) -> dict:
    headers = {
        "User-Agent": "medlitflow/0.1",
        "Accept": "application/json",
    }
    start = time.monotonic()

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url, params=params, headers=headers)
    except httpx.HTTPError:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        with _AUDIT_LOCK:
            core.append_http_audit({
                "url": url,
                "method": "GET",
                "status_code": 0,
                "elapsed_ms": elapsed_ms,
                "retry_count": 0,
                "timestamp": _now_iso(),
            })
        raise

    elapsed_ms = int((time.monotonic() - start) * 1000)
    with _AUDIT_LOCK:
        core.append_http_audit({
            "url": str(response.url),
            "method": "GET",
            "status_code": response.status_code,
            "elapsed_ms": elapsed_ms,
            "retry_count": 0,
            "timestamp": _now_iso(),
        })

    response.raise_for_status()
    return response.json()

def _probe_source(source: str, config: dict) -> dict:
    result: dict = {"source": source, "ok": False, "message": "", "details": {}}

    anchor_cfg = (
        config.get("enrichment", {})
        .get("citation", {})
        .get("source_probe", {})
        .get("anchor", {})
    )
    doi = anchor_cfg.get("doi", _ANCHOR_DOI)
    pmid = anchor_cfg.get("pmid", _ANCHOR_PMID)
    keyword = str(
        anchor_cfg.get("title_keyword", _ANCHOR_TITLE_KEYWORD)
    ).lower()

    try:
        if source == "pubmed":
            url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
            params = {"db": "pubmed", "id": pmid, "retmode": "json"}
            payload = _http_probe_json(url, params, _API_PROBE_TIMEOUT_SECONDS)
            entry = (payload.get("result") or {}).get(pmid) or {}
            title = str(entry.get("title", "")).lower()
            if keyword in title:
                result["ok"] = True
            else:
                result["message"] = f"PubMed 标题未匹配 keyword={keyword!r}"

        elif source == "europepmc":
            url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            params = {"query": f"DOI:{doi}", "format": "json", "pageSize": 1}
            payload = _http_probe_json(url, params, _API_PROBE_TIMEOUT_SECONDS)
            results = (payload.get("resultList") or {}).get("result") or []
            title = str(results[0].get("title", "")).lower() if results else ""
            if keyword in title:
                result["ok"] = True
            else:
                result["message"] = f"Europe PMC 标题未匹配 keyword={keyword!r}"

        elif source == "crossref":
            url = f"https://api.crossref.org/works/{doi}"
            payload = _http_probe_json(url, {}, _API_PROBE_TIMEOUT_SECONDS)
            titles = (payload.get("message") or {}).get("title") or []
            title = str(titles[0]).lower() if titles else ""
            if keyword in title:
                result["ok"] = True
            else:
                result["message"] = f"Crossref 标题未匹配 keyword={keyword!r}"

        elif source == "openalex":
            url = f"https://api.openalex.org/works/doi:{doi}"
            params: dict = {}
            mailto = config.get("api", {}).get("email", "")
            if mailto:
                params["mailto"] = mailto
            payload = _http_probe_json(url, params, _API_PROBE_TIMEOUT_SECONDS)
            title = str(payload.get("title", "")).lower()
            if keyword in title:
                result["ok"] = True
            else:
                result["message"] = f"OpenAlex 标题未匹配 keyword={keyword!r}"

        else:
            result["message"] = f"未知数据源：{source}"

    except Exception as exc:  # noqa: BLE001
        result["message"] = f"{type(exc).__name__}: {exc}"

    return result

def _check_local_environment(config: dict) -> dict:
    blockers: list[dict] = []
    warnings: list[dict] = []

    project_cfg = config.get("project", {}) or {}
    library_cfg = config.get("library", {}) or {}
    enrichment_cfg = config.get("enrichment", {}) or {}
    jcr_cfg = enrichment_cfg.get("journal_metrics", {}) or {}

    if "output_root" not in project_cfg:
        blockers.append({
            "category": "config",
            "source": "project.output_root",
            "message": "配置缺失 project.output_root",
            "action": "检查 config/default.yaml",
        })
    if "root" not in library_cfg:
        blockers.append({
            "category": "config",
            "source": "library.root",
            "message": "配置缺失 library.root",
            "action": "检查 config/default.yaml",
        })
    if "active_year" not in jcr_cfg:
        warnings.append({
            "category": "config",
            "source": "enrichment.journal_metrics.active_year",
            "message": "配置缺失 enrichment.journal_metrics.active_year，回退到 2025",
            "action": "显式配置 JCR 表年份",
        })

    if any(b["category"] == "config" for b in blockers):
        return {"blockers": blockers, "warnings": warnings}

    output_root = Path(project_cfg["output_root"])
    library_root = Path(library_cfg["root"])
    inbox = library_root / "inbox"

    perm_failures = check_permissions([output_root, library_root, inbox])
    blockers.extend(perm_failures)

    disk = check_disk_space(output_root, _DEFAULT_DISK_REQUIRED_MB)
    if not disk.get("ok"):
        blockers.append({
            "category": "disk",
            "source": str(output_root),
            "message": disk.get("message", "磁盘空间不足"),
            "action": "清理磁盘或调整阈值",
            "details": {
                "free_mb": disk.get("free_mb", 0.0),
                "required_mb": disk.get("required_mb", _DEFAULT_DISK_REQUIRED_MB),
            },
        })

    return {"blockers": blockers, "warnings": warnings}

def _check_jcr_outdated(config: dict) -> list[dict]:
    """JCR 过时检测（9 月分界）"""
    jcr_cfg = config.get("enrichment", {}).get("journal_metrics", {}) or {}
    active_year = int(jcr_cfg.get("active_year", _DEFAULT_JCR_ACTIVE_YEAR))
    today = datetime.now(timezone.utc).date()
    current_year = today.year
    month = today.month

    if month >= _JCR_SEPTEMBER_BOUNDARY:
        outdated = current_year > active_year + 1
        expected = current_year - 1
    else:
        outdated = current_year > active_year + 2
        expected = current_year - 2

    if not outdated:
        return []

    return [{
        "category": "config",
        "source": "jcr",
        "message": f"JCR active_year={active_year} 已过时，期望 {expected}",
        "action": f"更新 JCR 表至 {expected}，或确认继续使用旧表",
        "details": {
            "active_year": active_year,
            "current_year": current_year,
            "current_month": month,
            "expected_year": expected,
        },
    }]

def _check_jcr_file_exists(config: dict) -> list[dict]:
    jcr_cfg = config.get("enrichment", {}).get("journal_metrics", {}) or {}
    data_dir_raw = jcr_cfg.get("data_dir", "reference/")
    data_dir = Path(data_dir_raw)
    if not data_dir.is_absolute():
        output_root = Path(config["project"]["output_root"])
        data_dir = output_root / data_dir

    year = int(jcr_cfg.get("active_year", _DEFAULT_JCR_ACTIVE_YEAR))
    filename_tpl = jcr_cfg.get("filename_template", "JCR IF {year}.xlsx")
    default_tpl = jcr_cfg.get(
        "default_filename_template", "JCR IF {year}.default.xlsx"
    )

    user_file = data_dir / filename_tpl.format(year=year)
    default_file = data_dir / default_tpl.format(year=year)

    if user_file.exists() or default_file.exists():
        return []

    return [{
        "category": "config",
        "source": "jcr",
        "message": f"JCR 文件缺失：{user_file} 或 {default_file}",
        "action": "放置 JCR 表到 reference/ 目录",
        "details": {
            "user_file": str(user_file),
            "default_file": str(default_file),
            "active_year": year,
        },
    }]

def _is_safe_dir(d: Path) -> tuple[bool, str]:
    try:
        resolved = d.resolve() if d.exists() else d.absolute()
    except OSError:
        return True, ""

    if resolved.parent == resolved:
        return False, f"禁止在根目录创建：{d}"

    check_parts = resolved.parts[:2]
    for part in check_parts:
        normalized = str(part).lower().strip("/\\:")
        if normalized and normalized in _FORBIDDEN_DIR_NAMES:
            return False, f"禁止在系统目录创建：{d}"

    return True, ""

def _check_data_health(config: dict) -> dict:
    blockers: list[dict] = []
    warnings: list[dict] = []

    warnings.extend(_check_jcr_outdated(config))
    warnings.extend(_check_jcr_file_exists(config))

    library_root = Path(config["library"]["root"])
    for fname in ("records_final.parquet", "index.parquet"):
        path = library_root / fname
        if path.exists() and not os.access(path, os.R_OK):
            warnings.append({
                "category": "permission",
                "source": str(path),
                "message": f"{fname} 存在但不可读",
                "action": "检查文件权限",
            })

    output_root = Path(config["project"]["output_root"])
    required_dirs = [
        output_root,
        library_root,
        library_root / "inbox",
    ]
    for d in required_dirs:
        if d.exists():
            if not os.access(d, os.W_OK):
                blockers.append({
                    "category": "permission",
                    "source": str(d),
                    "message": f"目录存在但不可写：{d}",
                    "action": "检查文件系统权限",
                })
            continue

        safe, msg = _is_safe_dir(d)
        if not safe:
            blockers.append({
                "category": "config",
                "source": str(d),
                "message": msg,
                "action": (
                    "检查 config.default.yaml 中的 "
                    "project.output_root / library.root 配置"
                ),
            })
            continue

        try:
            d.mkdir(parents=True, exist_ok=True)
            if not os.access(d, os.W_OK):
                blockers.append({
                    "category": "permission",
                    "source": str(d),
                    "message": f"目录已创建但不可写：{d}",
                    "action": "检查文件系统权限",
                })
        except OSError as exc:
            blockers.append({
                "category": "permission",
                "source": str(d),
                "message": (
                    f"无法创建目录：{d}"
                    f"（{type(exc).__name__}: {exc}）"
                ),
                "action": "检查父目录权限或手动创建目录",
            })

    return {"blockers": blockers, "warnings": warnings}

def startup_health_check(config: dict) -> dict:
    """应用启动后 5 秒内完成并行体检。"""
    blockers: list[dict] = []
    warnings: list[dict] = []

    enabled_sources = _resolve_enabled_sources(config)
    if not enabled_sources:
        blockers.append({
            "category": "config",
            "source": "retrieval.sources",
            "message": "所有数据源均已关闭",
            "action": "在配置中至少启用一个数据源",
        })
        return {"ok": False, "blockers": blockers, "warnings": warnings}

    probe_results: dict[str, dict] = {}
    timeout_sources: set[str] = set()
    local_result: dict = {"blockers": [], "warnings": []}
    data_result: dict = {"blockers": [], "warnings": []}

    executor = ThreadPoolExecutor(max_workers=6)
    try:
        futures: dict = {}
        for src in enabled_sources:
            futures[executor.submit(_probe_source, src, config)] = ("api", src)
        futures[executor.submit(_check_local_environment, config)] = ("local", "environment")
        futures[executor.submit(_check_data_health, config)] = ("data", "health")

        processed_futures: set = set()

        def _consume(future, kind: str, name: str) -> None:
            nonlocal local_result, data_result
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                if kind == "api":
                    probe_results[name] = {
                        "source": name,
                        "ok": False,
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                else:
                    blockers.append({
                        "category": "config",
                        "source": name,
                        "message": f"体检任务异常：{type(exc).__name__}: {exc}",
                        "action": "检查配置与网络",
                    })
                return
            if kind == "api":
                probe_results[name] = result
            elif kind == "local":
                local_result = result or {"blockers": [], "warnings": []}
            elif kind == "data":
                data_result = result or {"blockers": [], "warnings": []}

        try:
            for future in as_completed(
                futures, timeout=_HEALTH_CHECK_TOTAL_TIMEOUT_SECONDS
            ):
                kind, name = futures[future]
                processed_futures.add(future)
                _consume(future, kind, name)
        except TimeoutError:
            pass

        for future, (kind, name) in futures.items():
            if future in processed_futures:
                continue
            if future.done():
                _consume(future, kind, name)
            elif kind == "api":
                timeout_sources.add(name)
            else:
                warnings.append({
                    "category": "config",
                    "source": name,
                    "message": (
                        f"{name} 体检任务超时"
                        f"（{_HEALTH_CHECK_TOTAL_TIMEOUT_SECONDS}s）"
                    ),
                    "action": "检查本地磁盘 / 文件系统响应",
                })
    finally:
        executor.shutdown(wait=False)

    blockers.extend(local_result.get("blockers", []))
    warnings.extend(local_result.get("warnings", []))
    blockers.extend(data_result.get("blockers", []))
    warnings.extend(data_result.get("warnings", []))

    ok_count = sum(1 for r in probe_results.values() if r.get("ok"))
    total_probed = len(probe_results) + len(timeout_sources)
    all_failed = ok_count == 0 and total_probed > 0

    for src, r in probe_results.items():
        if r.get("ok"):
            continue
        entry = {
            "category": "api",
            "source": src,
            "message": r.get("message") or f"{src} 探测失败",
            "action": "检查网络或稍后重试",
        }
        (blockers if all_failed else warnings).append(entry)

    for src in sorted(timeout_sources):
        entry = {
            "category": "api",
            "source": src,
            "message": f"{src} 探测超时（{_HEALTH_CHECK_TOTAL_TIMEOUT_SECONDS}s）",
            "action": "稍后重试或检查网络",
        }
        (blockers if all_failed else warnings).append(entry)

    return {
        "ok": len(blockers) == 0,
        "blockers": blockers,
        "warnings": warnings,
    }

def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if handle == 0:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

def is_lock_stale(lock_path: Path) -> bool:
    path = Path(lock_path)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return True
    if not isinstance(data, dict):
        return True
    try:
        pid = int(data.get("pid", 0))
    except (ValueError, TypeError):
        return True
    if pid <= 0:
        return True
    return not _is_pid_alive(pid)

def acquire_lock(output_root: Path) -> bool:
    lock_path = Path(output_root) / _LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    for _attempt in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _attempt > 0:
                return False
            try:
                existing = json.loads(lock_path.read_text(encoding="utf-8"))
                pid = int(existing.get("pid", 0))
            except (json.JSONDecodeError, OSError, ValueError):
                pid = 0
            if pid > 0 and _is_pid_alive(pid):
                return False
            stale_path = lock_path.with_name(
                f"{lock_path.name}.stale-{os.getpid()}"
            )
            try:
                os.replace(lock_path, stale_path)
            except FileNotFoundError:
                pass
            except OSError:
                return False
            try:
                if stale_path.exists():
                    stale_path.unlink()
            except OSError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(
                {"pid": os.getpid(), "acquired_at": _now_iso()},
                fh, ensure_ascii=False,
            )
        return True
    return False

def release_lock(output_root: Path) -> None:
    lock_path = Path(output_root) / _LOCK_FILENAME
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass

def check_permissions(paths: list[Path]) -> list[dict]:
    failures: list[dict] = []
    for raw in paths:
        path = Path(raw)
        target = path
        while not target.exists() and target != target.parent:
            target = target.parent

        if not target.exists():
            failures.append(_permission_error(path, "路径及父目录均不存在"))
            continue

        if not os.access(target, os.R_OK | os.W_OK):
            failures.append(_permission_error(path, "权限不足"))
    return failures

def check_disk_space(
    output_root: Path,
    required_mb: int = 1024,
) -> dict:
    target = Path(output_root)
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        usage = shutil.disk_usage(target)
        free_mb = usage.free / (1024 * 1024)
        ok = free_mb >= required_mb
        return {
            "ok": ok,
            "free_mb": float(free_mb),
            "required_mb": int(required_mb),
            "message": f"磁盘剩余 {free_mb:.1f} MB，要求 {required_mb} MB",
        }
    except OSError as exc:
        return {
            "ok": False,
            "free_mb": 0.0,
            "required_mb": int(required_mb),
            "message": f"无法读取磁盘空间：{exc}",
        }

def register_signal_handler() -> None:
    global _SIGNAL_REGISTERED
    if _SIGNAL_REGISTERED:
        return

    def _handler(signum, frame):  # noqa: ARG001
        if signum == signal.SIGINT:
            detail = "用户主动中断（SIGINT）"
        elif hasattr(signal, "SIGTERM") and signum == signal.SIGTERM:
            detail = "收到 SIGTERM（可能为系统或用户 kill）"
        else:
            detail = f"收到信号 {signum}"

        request_shutdown(detail=detail)

    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handler)
    _SIGNAL_REGISTERED = True

def mark_partial(run_root: Path, stage: str, data: dict) -> None:
    path = Path(run_root) / _STAGE_DONE_DIR / f"{stage}{_PARTIAL_SUFFIX}"
    path.parent.mkdir(parents=True, exist_ok=True)

    now = _now_iso()
    started_at = now
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            started_at = existing.get("started_at", now)
        except (json.JSONDecodeError, OSError):
            started_at = now

    payload = {
        "stage": stage,
        "started_at": started_at,
        "last_update": now,
        "data": data,
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)

def load_partial(run_root: Path, stage: str) -> dict | None:
    path = Path(run_root) / _STAGE_DONE_DIR / f"{stage}{_PARTIAL_SUFFIX}"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return data

_SCHEMA_BY_STAGE: dict[str, dict] = {
    "collect": {
        "primary_key": ["source_database", "source_record_id"],
        "required": [
            "source_database", "source_record_id", "query_name",
            "search_query", "title", "abstract_missing",
            "open_access_flag", "raw_response_path",
        ],
        "enums": {
            "source_database": [
                "pubmed", "pmc", "europepmc", "crossref", "openalex",
            ],
            "publication_type_category": [
                "research", "review", "editorial", "other", "",
            ],
        },
        "optional_check": ["authors", "journal", "doi", "pmid", "pmcid"],
    },
    "dedup": {
        "primary_key": "canonical_id",
        "required": [
            "canonical_id", "title", "abstract_variants_json",
            "abstract_variant_count", "abstract_variant_status",
            "source_databases", "source_record_ids",
            "matched_sources_count", "matched_rows_count",
            "primary_source", "dedup_rule", "has_conflict",
            "strategy_names",
        ],
        "enums": {
            "abstract_variant_status": [
                "single", "identical", "near_identical", "divergent",
            ],
            "abstract_divergence_type": [
                "preprint_vs_published", "erratum", "version_update",
                "different_language", "publisher_duplicate", "unknown", "",
            ],
        },
        "optional_check": ["authors", "journal", "doi", "pmid", "pmcid"],
    },
    "retrieval": {
        "primary_key": "canonical_id",
        "required": [
            "canonical_id", "retrieval_status", "attempt_count",
            "html_second_pass_status", "file_dedup_decision",
        ],
        "enums": {
            "retrieval_status": [
                "fulltext_pdf", "fulltext_xml", "fulltext_html",
                "needs_abstract_fallback", "rate_limited",
                "broken_link", "inaccessible", "library_hit", "no_fulltext_available",
            ],
            "file_dedup_decision": [
                "keep", "duplicate", "not_applicable", "library_existing",
            ],
            "html_second_pass_status": [
                "not_attempted", "pdf_downloaded", "no_pdf_link_found",
            ],
        },
        "optional_check": ["title", "doi"],
    },
    "fallback": {
        "primary_key": "canonical_id",
        "required": [
            "canonical_id", "final_retrieval_status",
            "abstract_source", "fallback_level",
        ],
        "enums": {
            "final_retrieval_status": [
                "fulltext_pdf", "fulltext_xml", "fulltext_html",
                "abstract_only", "metadata_only",
            ],
            "abstract_source": [
                "search_metadata", "local_raw", "pubmed_efetch",
                "europepmc", "none",
            ],
        },
        "optional_check": ["title", "doi"],
    },
    "enrich": {
        "primary_key": "canonical_id",
        "required": ["canonical_id", "metrics_status"],
        "enums": {
            "metrics_status": ["available", "partial", "unavailable"],
        },
        "optional_check": ["title", "doi"],
    },
    "output": {
        "primary_key": "canonical_id",
        "required": [
            "canonical_id", "first_seen_run", "last_seen_run",
            "appearance_count", "matched_strategy_count",
        ],
        "enums": {},
        "optional_check": ["title", "doi"],
    },
}

def _check_primary_key(df: pd.DataFrame, pk, errors: list[dict]) -> None:
    pk_cols = [pk] if isinstance(pk, str) else list(pk)
    pk_label = pk if isinstance(pk, str) else "+".join(pk_cols)

    for col in pk_cols:
        if col not in df.columns:
            errors.append({
                "check": "primary_key",
                "message": f"缺少主键列：{col}",
            })
            return

    if len(pk_cols) == 1:
        pk_series = df[pk_cols[0]].astype(str)
    else:
        pk_series = df[pk_cols].astype(str).agg("\x1f".join, axis=1)

    null_mask = df[pk_cols].isna().any(axis=1)
    for col in pk_cols:
        col_str = df[col].astype(str)
        null_mask = null_mask | (col_str == "") | (col_str == "nan")
    if null_mask.any():
        errors.append({
            "check": "primary_key",
            "message": f"主键 {pk_label} 存在空值（{int(null_mask.sum())} 行）",
        })

    dup_mask = pk_series.duplicated(keep=False) & ~null_mask
    if dup_mask.any():
        errors.append({
            "check": "primary_key",
            "message": f"主键 {pk_label} 存在重复（{int(dup_mask.sum())} 行）",
        })

def _check_required_fields(
    df: pd.DataFrame, required: list[str], errors: list[dict],
) -> None:
    for col in required:
        if col not in df.columns:
            errors.append({
                "check": "required",
                "message": f"缺少必填列：{col}",
            })
            continue
        col_as_str = df[col].astype(str)
        empty_mask = (
            df[col].isna()
            | (col_as_str == "")
            | (col_as_str == "nan")
        )
        if empty_mask.any():
            errors.append({
                "check": "required",
                "message": (
                    f"必填列 {col} 存在空值"
                    f"（{int(empty_mask.sum())} 行）"
                ),
            })

def _check_null_ratio(
    df: pd.DataFrame, columns: list[str], warnings: list[dict],
) -> None:
    if len(df) == 0:
        return
    for col in columns:
        if col not in df.columns:
            continue
        empty_ratio = (df[col].astype(str).str.strip() == "").mean()
        if empty_ratio > 0.9:
            warnings.append({
                "check": "null_ratio",
                "message": f"字段 {col} 空值比例过高（{empty_ratio:.2%}）",
            })

def _check_truncation(df: pd.DataFrame, warnings: list[dict]) -> None:
    if "title" not in df.columns or len(df) == 0:
        return
    titles = df["title"].astype(str)
    short_mask = (titles.str.len() < 5) & (titles.str.strip() != "")
    if short_mask.any():
        warnings.append({
            "check": "truncated",
            "message": f"存在过短标题（{int(short_mask.sum())} 行）",
        })
    ellipsis_mask = titles.str.endswith("...") | titles.str.endswith("…")
    if ellipsis_mask.any():
        warnings.append({
            "check": "truncated",
            "message": (
                f"存在以省略号结尾的标题（{int(ellipsis_mask.sum())} 行）"
            ),
        })

def _check_duplicate_rows(df: pd.DataFrame, warnings: list[dict]) -> None:
    if len(df) == 0:
        return
    dup_mask = df.astype(str).duplicated(keep=False)
    if dup_mask.any():
        warnings.append({
            "check": "duplicate_rows",
            "message": f"存在完全重复行（{int(dup_mask.sum())} 行）",
        })

def _check_enums(
    df: pd.DataFrame,
    enums: dict[str, list[str]],
    warnings: list[dict],
) -> None:
    if len(df) == 0:
        return
    for col, allowed in enums.items():
        if col not in df.columns:
            continue
        col_values = df[col].astype(str)
        invalid_mask = ~col_values.isin(allowed)
        if invalid_mask.any():
            warnings.append({
                "check": "enum",
                "message": (
                    f"字段 {col} 存在非法枚举值"
                    f"（{int(invalid_mask.sum())} 行）"
                ),
            })

def validate_stage_output(
    stage: str,
    df: pd.DataFrame,
    expected_count: int | None = None,
) -> dict:
    warnings: list[dict] = []
    errors: list[dict] = []

    if stage not in _SCHEMA_BY_STAGE:
        errors.append({
            "check": "stage",
            "message": f"未知 stage：{stage}",
        })
        return {"ok": False, "warnings": warnings, "errors": errors}

    if df is None:
        errors.append({"check": "dataframe", "message": "df 为 None"})
        return {"ok": False, "warnings": warnings, "errors": errors}

    schema = _SCHEMA_BY_STAGE[stage]

    if expected_count is not None and len(df) != expected_count:
        warnings.append({
            "check": "count",
            "message": f"行数 {len(df)} 与期望 {expected_count} 不一致",
        })

    _check_primary_key(df, schema["primary_key"], errors)
    _check_required_fields(df, schema["required"], errors)
    _check_null_ratio(df, schema.get("optional_check", []), warnings)
    _check_truncation(df, warnings)
    _check_duplicate_rows(df, warnings)
    _check_enums(df, schema.get("enums", {}), warnings)

    return {
        "ok": len(errors) == 0,
        "warnings": warnings,
        "errors": errors,
    }

def write_stage_done(
    run_root: Path,
    stage: str,
    output_path: Path,
    output_rows: int,
    checksum: str,
) -> None:
    path = Path(run_root) / _STAGE_DONE_DIR / f"{stage}{_DONE_SUFFIX}"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "completed_at": _now_iso(),
        "output_path": str(output_path),
        "output_rows": int(output_rows),
        "checksum": str(checksum),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)

def read_stage_done(run_root: Path, stage: str) -> dict | None:
    path = Path(run_root) / _STAGE_DONE_DIR / f"{stage}{_DONE_SUFFIX}"
    if not path.exists():
        return None

    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None

    if not text:
        return {"format": "legacy", "completed_at": ""}

    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or "completed_at" not in data:
            return None
        try:
            output_rows = int(data.get("output_rows", 0))
        except (ValueError, TypeError):
            output_rows = 0
        return {
            "format": "json",
            "completed_at": str(data.get("completed_at", "")),
            "output_path": str(data.get("output_path", "")),
            "output_rows": output_rows,
            "checksum": str(data.get("checksum", "")),
        }

    return {"format": "legacy", "completed_at": text}

def detect_api_change(source: str, payload: Any) -> bool:
    if not isinstance(payload, (dict, list)):
        return True

    if source == "pubmed":
        if not isinstance(payload, dict):
            return True
        result = payload.get("result")
        return not isinstance(result, dict)

    if source == "europepmc":
        if not isinstance(payload, dict):
            return True
        result_list = payload.get("resultList")
        if not isinstance(result_list, dict):
            return True
        return "result" not in result_list

    if source == "crossref":
        if not isinstance(payload, dict):
            return True
        return "message" not in payload

    if source == "openalex":
        if not isinstance(payload, dict):
            return True
        return "id" not in payload and "results" not in payload

    return True