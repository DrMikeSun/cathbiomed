"""state.py — 运行状态管理。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from medlitflow import core

__all__ = [
    "init_run",
    "mark_stage_done",
    "is_stage_done",
    "write_stage_audit",
    "write_http_audit",
]

_STAGE_DONE_DIR = ".stage_done"
_AUDIT_DIR = "audit"
_PIPELINE_AUDIT_FILE = "pipeline_audit.jsonl"
_HTTP_AUDIT_FILE = "http_audit.jsonl"

_RUN_SUBDIRS = (
    ".stage_done",
    "audit",
    "interim",
    "output",
    "output/entities",
    "output/relations",
    "papers/inbox",
)

_SAFE_RUN_NAME_RE = re.compile(r"[^0-9A-Za-z._-]+")

def _iso_now() -> str:
    return core.now_iso()

_MAX_RUN_NAME_LEN = 100

def _sanitize_run_name(run_name: str) -> str:
    if not run_name:
        raise ValueError("run_name 不能为空")
    safe = _SAFE_RUN_NAME_RE.sub("_", run_name).strip("._")
    if not safe:
        raise ValueError(f"run_name 无有效字符：{run_name!r}")
    if len(safe) > _MAX_RUN_NAME_LEN:
        digest = hashlib.sha256(run_name.encode("utf-8")).hexdigest()[:8]
        safe = f"{safe[:_MAX_RUN_NAME_LEN - 9]}_{digest}"
    return safe

def init_run(run_name: str, config: dict) -> Path:
    safe_name = _sanitize_run_name(run_name)

    output_root = Path(config["project"]["output_root"])
    run_root = (output_root / "runs" / safe_name).resolve()

    for sub in _RUN_SUBDIRS:
        (run_root / sub).mkdir(parents=True, exist_ok=True)

    core._register_http_audit_sink(run_root)

    return run_root

def mark_stage_done(run_root: Path, stage: str) -> None:
    stage_done_dir = Path(run_root) / _STAGE_DONE_DIR
    stage_done_dir.mkdir(parents=True, exist_ok=True)
    marker = stage_done_dir / f"{stage}.done"
    marker.write_text(_iso_now(), encoding="utf-8")

def is_stage_done(run_root: Path, stage: str) -> bool:
    marker = Path(run_root) / _STAGE_DONE_DIR / f"{stage}.done"
    return marker.exists()

def write_stage_audit(
    run_root: Path,
    stage: str,
    input_count: int,
    output_count: int,
    elapsed: float,
    errors: int,
) -> None:
    audit_dir = Path(run_root) / _AUDIT_DIR
    audit_dir.mkdir(parents=True, exist_ok=True)

    entry = {
        "stage": stage,
        "input_count": int(input_count),
        "output_count": int(output_count),
        "elapsed_seconds": float(elapsed),
        "error_count": int(errors),
        "timestamp": _iso_now(),
    }

    log_path = audit_dir / _PIPELINE_AUDIT_FILE
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

def write_http_audit(run_root: Path, entry: dict) -> None:
    audit_dir = Path(run_root) / _AUDIT_DIR
    audit_dir.mkdir(parents=True, exist_ok=True)

    record = {
        "url": str(entry.get("url", "")),
        "method": str(entry.get("method", "GET")),
        "status_code": int(entry.get("status_code", 0)),
        "elapsed_ms": int(entry.get("elapsed_ms", 0)),
        "retry_count": int(entry.get("retry_count", 0)),
        "timestamp": entry.get("timestamp") or _iso_now(),
    }

    log_path = audit_dir / _HTTP_AUDIT_FILE
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")