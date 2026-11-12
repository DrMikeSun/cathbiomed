"""medlitflow scan_cache.py — scan 结果缓存（原子写 + 报告生成）。"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pandas as pd

from medlitflow import core

__all__ = [
    "save_scan_cache",
    "load_scan_cache",
    "get_scan_meta",
    "clear_scan_cache",
]


def _cache_dir(config: dict) -> Path:
    library_root = Path(config["library"]["root"])
    return library_root / "scan_cache"


def _generate_summary_md(records: pd.DataFrame, meta: dict) -> str:
    lines: list[str] = []
    lines.append(f"# 扫描报告：{meta.get('strategy_name', '')}")
    lines.append("")
    lines.append(f"**扫描时间**：{meta.get('scan_at', '')}")
    lines.append(f"**检索式**：{meta.get('pubmed_query', '')}")
    lines.append(f"**命中数**：{int(meta.get('hit_count', 0) or 0)}")
    lines.append("")

    lines.append("## 数据来源分布")
    lines.append("")
    lines.append("| 数据源 | 数量 |")
    lines.append("|---|---|")
    if "source_databases" in records.columns and len(records) > 0:
        src_counter: dict[str, int] = {}
        for s in records["source_databases"].fillna("").astype(str):
            for part in s.split(";"):
                k = part.strip().lower()
                if k:
                    src_counter[k] = src_counter.get(k, 0) + 1
        for k in ("pubmed", "pmc", "europepmc", "crossref", "openalex"):
            lines.append(f"| {k} | {src_counter.get(k, 0)} |")
    else:
        lines.append("| （无数据） | 0 |")
    lines.append("")

    lines.append("## 出版年份分布")
    lines.append("")
    lines.append("| 年份 | 数量 |")
    lines.append("|---|---|")
    if "year" in records.columns and len(records) > 0:
        year_counter: dict[str, int] = {}
        for y in records["year"]:
            try:
                if y is None:
                    continue
                yi = int(float(y))
                year_counter[str(yi)] = year_counter.get(str(yi), 0) + 1
            except (TypeError, ValueError):
                continue
        for y in sorted(year_counter.keys(), key=int, reverse=True):
            lines.append(f"| {y} | {year_counter[y]} |")
    else:
        lines.append("| （无数据） | 0 |")
    lines.append("")

    lines.append("## 期刊 IF 覆盖")
    lines.append("")
    total = len(records)
    if "journal_impact_factor" in records.columns and total > 0:
        jif_count = int(
            pd.to_numeric(
                records["journal_impact_factor"], errors="coerce"
            ).notna().sum()
        )
    else:
        jif_count = 0
    pct = f"{jif_count / total * 100:.1f}%" if total > 0 else "0.0%"
    lines.append(f"- 有 IF：{jif_count} / {total}（{pct}）")
    lines.append("")

    lines.append("## 文献类型分布")
    lines.append("")
    lines.append("| 类型 | 数量 |")
    lines.append("|---|---|")
    if "publication_type_category" in records.columns and total > 0:
        pt_counter: dict[str, int] = {}
        for pt in records["publication_type_category"]:
            key = str(pt or "").strip().lower() or "other"
            pt_counter[key] = pt_counter.get(key, 0) + 1
        for k in ("research", "review", "editorial", "other"):
            lines.append(f"| {k} | {pt_counter.get(k, 0)} |")
    else:
        lines.append("| （无数据） | 0 |")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("扫描结果仅供初筛参考，不计入 PRISMA 流程图。")
    lines.append("如需精读，请使用「再次检索」按钮。")
    lines.append("")
    return "\n".join(lines)


def save_scan_cache(
    run_root: Path, config: dict, strategy_name: str,
) -> None:
    run_root = Path(run_root).resolve()
    cache_dir = _cache_dir(config)
    cache_dir.mkdir(parents=True, exist_ok=True)

    records_path = run_root / "output" / "records_final.parquet"
    query_path = run_root / "output" / "query_execution.json"
    meta_path = run_root / "output" / "run_metadata.json"

    records = pd.read_parquet(records_path)

    original_pubmed_query = ""
    if query_path.exists():
        try:
            qe = json.loads(query_path.read_text(encoding="utf-8"))
            if isinstance(qe, dict):
                original_pubmed_query = str(
                    qe.get("original_pubmed_query", "") or ""
                )
        except (json.JSONDecodeError, OSError):
            original_pubmed_query = ""

    started_at = ""
    if meta_path.exists():
        try:
            rm = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(rm, dict):
                started_at = str(rm.get("started_at", "") or "")
        except (json.JSONDecodeError, OSError):
            started_at = ""

    meta = {
        "scan_at": core.now_iso(),
        "strategy_name": str(strategy_name),
        "pubmed_query": original_pubmed_query,
        "hit_count": int(len(records)),
        "started_at": started_at,
        "scan_run_root": str(run_root),
    }

    old_run_root = ""
    old_meta_path = cache_dir / "scan_meta.json"
    if old_meta_path.exists():
        try:
            old_meta = json.loads(old_meta_path.read_text(encoding="utf-8"))
            if isinstance(old_meta, dict):
                old_run_root = str(old_meta.get("scan_run_root", "") or "")
        except (json.JSONDecodeError, OSError):
            old_run_root = ""

    tmp_records = cache_dir / "records.parquet.tmp"
    records.to_parquet(tmp_records, index=False)
    os.replace(tmp_records, cache_dir / "records.parquet")

    tmp_meta = cache_dir / "scan_meta.json.tmp"
    tmp_meta.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp_meta, cache_dir / "scan_meta.json")

    try:
        summary_text = _generate_summary_md(records, meta)
        tmp_summary = cache_dir / "scan_summary.md.tmp"
        tmp_summary.write_text(summary_text, encoding="utf-8")
        os.replace(tmp_summary, cache_dir / "scan_summary.md")
    except Exception:  # noqa: BLE001
        pass

    if old_run_root:
        try:
            old_path = Path(old_run_root).resolve()
            if old_path != run_root:
                shutil.rmtree(old_path, ignore_errors=True)
        except (OSError, ValueError):
            pass


def load_scan_cache(
    config: dict,
) -> tuple[pd.DataFrame | None, dict | None]:
    cache_dir = _cache_dir(config)
    records_path = cache_dir / "records.parquet"
    meta_path = cache_dir / "scan_meta.json"
    if not records_path.exists() or not meta_path.exists():
        return None, None
    records = pd.read_parquet(records_path)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        meta = None
    return records, meta


def get_scan_meta(config: dict) -> dict | None:
    meta_path = _cache_dir(config) / "scan_meta.json"
    if not meta_path.exists():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def clear_scan_cache(config: dict) -> None:
    cache_dir = _cache_dir(config)
    meta_path = cache_dir / "scan_meta.json"
    old_run_root = ""
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                old_run_root = str(meta.get("scan_run_root", "") or "")
        except (json.JSONDecodeError, OSError):
            old_run_root = ""
    if old_run_root:
        try:
            shutil.rmtree(Path(old_run_root), ignore_errors=True)
        except (OSError, ValueError):
            pass
    shutil.rmtree(cache_dir, ignore_errors=True)