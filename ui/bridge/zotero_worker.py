"""Zotero 后台 Worker：刷新（ZoteroRefreshWorker）+ 批量写（ZoteroActionWorker）。

不访问 UI，只读 config/parquet + HTTP；结果通过 Signal 自动 queue 到主线程。
中断粗粒度（verify 内部不打断），每阶段开始前 check_shutdown。
"""

from __future__ import annotations

import copy
from pathlib import Path

import httpx
import pandas as pd
from PySide6.QtCore import QThread, Signal

from medlitflow import core, robustness

class ZoteroRefreshWorker(QThread):
    """后台执行 Zotero 页刷新。"""
    finished_ok = Signal(dict)
    failed = Signal(str)
    interrupted = Signal()

    def __init__(
        self,
        config: dict,
        library_root: Path,
        current_names: list[str],
        previous_stats: dict | None = None,
        previous_missing_cache: list[dict] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._config = copy.deepcopy(config) if config else {}
        self._library_root = Path(library_root)
        self._current_names = list(current_names or [])
        self._previous_stats = dict(previous_stats or {})
        self._previous_missing = list(previous_missing_cache or [])

    def request_interrupt(self) -> None:
        try:
            from medlitflow import robustness
            robustness.request_shutdown(detail="Zotero 刷新取消")
        except Exception:
            pass

    def run(self) -> None:
        robustness.reset_shutdown()
        try:
            result = self._do_refresh()
            self.finished_ok.emit(result)
        except robustness.InterruptRequested:
            self.interrupted.emit()
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")

    def _do_refresh(self) -> dict:
        from medlitflow import robustness, zotero_bridge, zotero_import

        zc = self._config.get("zotero", {}) or {}
        link_mode = core.clean_text(
            zc.get("link_mode", "imported_file")
        ) or "imported_file"

        stats = {
            "pending": 0,
            "synced": int(self._previous_stats.get("synced", 0)),
            "missing": int(self._previous_stats.get("missing", 0)),
            "unverified": int(self._previous_stats.get("unverified", 0)),
            "importable": 0,
        }

        include_cids = self._load_include_cids()
        linked_cids = self._load_linked_cids()
        stats["pending"] = len(include_cids - linked_cids)

        connected = self._desktop_alive()

        collections: list[dict] = []
        try:
            collections = zotero_import.list_collections(
                self._library_root, self._config,
            )
        except Exception:
            collections = []

        verify_ok = False
        verify_errors: list[str] = []
        unverified_count = 0

        stats_source = (
            "cached"
            if (
                self._previous_stats.get("synced", 0) > 0
                or self._previous_stats.get("missing", 0) > 0
            )
            else "fresh"
        )

        missing_cache: list[dict] = list(self._previous_missing)

        if connected:
            robustness.check_shutdown()
            try:
                verify_result = zotero_bridge.verify_zotero_links(
                    self._library_root, self._config,
                )
                stats["synced"] = int(verify_result.get("alive", 0))
                missing_cache = list(
                    verify_result.get("confirmed_missing") or []
                )
                stats["missing"] = len(missing_cache)
                unverified_count = len(
                    verify_result.get("unverified") or []
                )
                stats["unverified"] = unverified_count
                verify_ok = True
                stats_source = "fresh"
            except robustness.InterruptRequested:
                raise
            except Exception as exc:
                verify_errors.append(
                    f"{type(exc).__name__}: {exc}"
                )
                stats_source = "cached"
        else:
            stats_source = "cached"

        importable_df: pd.DataFrame | None = None
        if self._current_names:
            robustness.check_shutdown()
            try:
                importable_df = zotero_import.scan_collections_for_import(
                    self._current_names,
                    self._library_root,
                    self._config,
                )
                stats["importable"] = int(len(importable_df))
            except robustness.InterruptRequested:
                raise
            except Exception as exc:
                stats["importable"] = 0
                verify_errors.append(
                    f"扫描可引入失败：{type(exc).__name__}: {exc}"
                )
        else:
            stats["importable"] = 0

        return {
            "connected": connected,
            "link_mode": link_mode,
            "collections": collections,
            "current_names": list(self._current_names),
            "stats": stats,
            "missing_cache": missing_cache,
            "importable_df": importable_df,
            "verify_ok": verify_ok,
            "verify_errors": verify_errors,
            "unverified_count": unverified_count,
            "stats_source": stats_source,
        }

    def _desktop_alive(self) -> bool:
        try:
            from medlitflow import zotero_bridge
            zc = self._config.get("zotero", {}) or {}
            lt = core.clean_text(zc.get("library_type", "user")) or "user"
            lid = core.clean_text(
                str(zc.get("library_id", "0"))
            ) or "0"
            url = (
                f"{zotero_bridge.ZOTERO_LOCAL_API}/{lt}s/{lid}/items"
            )
            with httpx.Client(timeout=3) as client:
                r = client.get(url, params={"limit": 1})
            return r.status_code == 200
        except Exception:
            return False

    def _load_include_cids(self) -> set[str]:
        path = self._library_root / "screening_decisions.parquet"
        if not path.exists():
            return set()
        try:
            df = pd.read_parquet(path)
        except Exception:
            return set()
        if (
            "user_decision" not in df.columns
            or "canonical_id" not in df.columns
        ):
            return set()
        return set(
            df.loc[
                df["user_decision"].astype(str) == "include",
                "canonical_id",
            ].astype(str).tolist()
        )

    def _load_linked_cids(self) -> set[str]:
        path = self._library_root / "zotero_links.parquet"
        if not path.exists():
            return set()
        try:
            df = pd.read_parquet(path)
        except Exception:
            return set()
        if "canonical_id" not in df.columns:
            return set()
        return set(df["canonical_id"].astype(str).tolist())

class ZoteroActionWorker(QThread):
    """后台执行 Zotero 批量写（sync / import / resync）。"""
    finished_ok = Signal(dict)
    failed = Signal(str)
    interrupted = Signal()
    progressed = Signal(int, int)

    def __init__(
        self,
        action: str,
        payload: dict,
        config: dict,
        library_root: Path,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._action = str(action or "")
        self._payload = dict(payload or {})
        self._config = copy.deepcopy(config) if config else {}
        self._library_root = Path(library_root)

    def request_interrupt(self) -> None:
        robustness.request_shutdown(
            detail=f"Zotero {self._action} 取消"
        )

    def run(self) -> None:
        robustness.reset_shutdown()
        try:
            result = self._do_action()
            self.finished_ok.emit(result)
        except robustness.InterruptRequested:
            self.interrupted.emit()
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")

    def _do_action(self) -> dict:
        if self._action == "sync":
            return self._do_sync()
        if self._action == "import":
            return self._do_import()
        if self._action == "resync":
            return self._do_resync()
        raise ValueError(f"未知 action：{self._action!r}")

    def _do_sync(self) -> dict:
        from medlitflow import zotero_bridge
        included_df = self._payload.get("included_df")
        if included_df is None or len(included_df) == 0:
            return {"action": "sync", "count": 0}

        def _progress(current: int, total: int) -> None:
            if current == total or current % 10 == 0:
                self.progressed.emit(int(current), int(total))

        zotero_bridge.sync_to_zotero(
            included_df, self._config, progress_callback=_progress,
        )
        return {
            "action": "sync",
            "count": int(len(included_df)),
        }

    def _do_import(self) -> dict:
        from medlitflow import zotero_import
        candidates = self._payload.get("candidates")
        if candidates is None or len(candidates) == 0:
            return {
                "action": "import", "count": 0,
                "skipped": 0, "errors": [],
            }
        result = zotero_import.import_zotero_items(
            candidates, self._library_root, self._config,
        )
        return {
            "action": "import",
            "count": int(result.get("imported", 0)),
            "skipped": int(result.get("skipped", 0)),
            "errors": list(result.get("errors", [])),
        }

    def _do_resync(self) -> dict:
        from medlitflow import zotero_bridge, zotero_import
        cid = str(self._payload.get("cid", "") or "")
        included_df = self._payload.get("included_df")

        if not cid:
            return {"action": "resync", "cid": "", "count": 0,
                    "error": "empty_cid"}
        if included_df is None or len(included_df) == 0:
            return {"action": "resync", "cid": cid, "count": 0,
                    "error": "empty_included_df"}

        try:
            zotero_import.clear_zotero_links([cid], self._library_root)
        except robustness.InterruptRequested:
            raise
        except Exception as exc:
            return {"action": "resync", "cid": cid, "count": 0,
                    "error": f"clear_links_failed: {exc}"}

        try:
            zotero_bridge.sync_to_zotero(included_df, self._config)
        except robustness.InterruptRequested:
            raise
        except Exception as exc:
            return {"action": "resync", "cid": cid, "count": 0,
                    "error": f"sync_failed: {exc}"}

        return {"action": "resync", "cid": cid,
                "count": int(len(included_df))}