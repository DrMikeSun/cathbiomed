"""ui/controllers/zotero_controller.py — Zotero 业务控制器。

组合模式（parent = main_window）；依赖 5 个公开接口 + 3 个受控例外；
写操作统一走 zotero_import / zotero_bridge 公开 API；
缓存（stats / collections / missing / importable / item_alive）由本类维护。"""

from __future__ import annotations

import time as _time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
from PySide6.QtCore import QObject, QThread, QTimer, Signal

from medlitflow import core
from ui.utils.formatting import first_author, format_year

if TYPE_CHECKING:
    from ui.views.main_window import MedLitFlowMainWindow

class ZoteroSectionAliveWorker(QThread):
    """单条 item_key 存活检查（三态回传，纯只读）。

    status ∈ {"alive", "confirmed_missing", "unverified"}；"""
    checked = Signal(str, str)

    def __init__(
        self,
        item_key: str,
        library_type: str = "user",
        library_id: str = "0",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._item_key = str(item_key or "")
        self._library_type = str(library_type or "user")
        self._library_id = str(library_id or "0")

    def run(self) -> None:
        status = "unverified"
        try:
            import httpx
            from medlitflow import zotero_bridge
            url = (
                f"{zotero_bridge.ZOTERO_LOCAL_API}/"
                f"{self._library_type}s/{self._library_id}/items/"
                f"{self._item_key}"
            )
            with httpx.Client(timeout=5) as client:
                r = client.get(url)
            if r.status_code == 200:
                status = "alive"
            elif r.status_code in (404, 410):
                status = "confirmed_missing"
        except Exception:
            status = "unverified"
        try:
            self.checked.emit(self._item_key, status)
        except Exception:
            pass

class ZoteroController(QObject):
    """Zotero 业务控制器（组合模式）。"""
    stats_updated = Signal(dict)
    refresh_status_changed = Signal(str, str)
    activity_appended = Signal(str, str)
    list_data_ready = Signal(str, list, list)
    collections_loaded = Signal(list, list)
    connection_changed = Signal(bool, str, str)
    section_updated = Signal(str)
    detail_ready = Signal(dict)

    _ALIVE_CACHE_TTL = 60.0
    _ALIVE_CHECK_MAX_CONCURRENT = 5

    _VALID_STATES = frozenset({
        "not_synced", "synced", "missing", "retrieval_overridden",
        "checking", "unverified", "invalid",
    })
    _VALID_DATA_ORIGINS = frozenset({"retrieval", "zotero_import"})

    def __init__(self, main_window: "MedLitFlowMainWindow"):
        super().__init__(main_window)
        self._mw = main_window

        self._refresh_worker = None
        self._action_worker = None

        self._collections_cache: list[dict] = []
        self._stats_cache: dict = {
            "pending": 0, "synced": 0, "missing": 0,
            "unverified": 0, "importable": 0,
        }
        self._last_update: str = ""
        self._missing_cache: list[dict] = []
        self._importable_cache: pd.DataFrame | None = None

        self._item_alive_cache: dict[str, tuple[str, float]] = {}

        self._done_timer = QTimer(self)
        self._done_timer.setSingleShot(True)
        self._done_timer.setInterval(10000)
        self._done_timer.timeout.connect(self._on_done_timeout)

        self._pending_alive_checks: dict[str, "ZoteroSectionAliveWorker"] = {}
        self._current_detail_cid: str = ""
        self._alive_check_queue: list[tuple[str, str]] = []

        from medlitflow import zotero_bridge
        zotero_bridge.register_log_callback(self._on_bridge_log)
        self._current_list_key: str | None = None

        self._view_connected = False
        self._detail_connected = False
        self._zview_detail_connected = False

    def connect_view(self, view) -> None:
        if self._view_connected:
            return
        self._view_connected = True

        view.refresh_all_requested.connect(self.refresh_all)
        view.card_clicked.connect(self.on_card_clicked)
        view.open_zotero_item_requested.connect(
            self.on_open_zotero_item
        )
        view.sync_selected_requested.connect(self.on_sync_selected)
        view.prune_selected_requested.connect(self.on_prune_selected)
        view.import_selected_requested.connect(self.on_import_selected)
        view.open_zotero_library_requested.connect(
            self.on_open_zotero_library
        )
        view.collection_changed.connect(self.on_collection_changed)
        view.configure_collections_requested.connect(
            self.on_configure_collections
        )

        self.stats_updated.connect(view.set_stats)
        self.refresh_status_changed.connect(view.set_refresh_status)
        self.activity_appended.connect(view.append_activity)
        self.collections_loaded.connect(view.set_collections)
        self.connection_changed.connect(view.set_connection_status)

        view.row_clicked.connect(self.on_row_clicked)

        self.list_data_ready.connect(self._on_list_data_ready)
        self.detail_ready.connect(self._on_detail_ready)
        self.section_updated.connect(self._on_section_updated)

    def connect_detail_panel(self, dp) -> None:
        if self._detail_connected:
            return
        self._detail_connected = True

        dp.zotero_sync_requested.connect(self.on_detail_sync)
        dp.zotero_resync_requested.connect(self.on_detail_resync)
        dp.zotero_withdraw_requested.connect(self.on_detail_withdraw)
        dp.zotero_clear_links_requested.connect(
            self.on_detail_clear_links
        )
        dp.zotero_open_requested.connect(self.on_detail_open)
        dp.zotero_reverify_requested.connect(self.on_detail_reverify)

    def connect_zotero_view_detail(self, dp) -> None:
        if self._zview_detail_connected:
            return
        self._zview_detail_connected = True

        dp.zotero_sync_requested.connect(self.on_detail_sync)
        dp.zotero_resync_requested.connect(self.on_detail_resync)
        dp.zotero_withdraw_requested.connect(self.on_detail_withdraw)
        dp.zotero_clear_links_requested.connect(
            self.on_detail_clear_links
        )
        dp.zotero_open_requested.connect(self.on_detail_open)
        dp.zotero_reverify_requested.connect(self.on_detail_reverify)

    def _on_list_data_ready(
        self, card_key: str, rows: list, item_keys: list,
    ) -> None:
        try:
            self._mw.zotero_view.show_list(card_key)
            self._mw.zotero_view.set_list_data(
                card_key, rows, item_keys
            )
        except Exception:
            pass

    def _on_detail_ready(self, detail) -> None:
        if not isinstance(detail, dict) or not detail:
            return
        try:
            self._mw.zotero_view.set_detail(detail)
        except Exception:
            pass

    def _on_section_updated(self, cid: str) -> None:
        if not cid:
            return
        try:
            cid_str = str(cid)
            record = None
            display = self._mw._library_ctrl._literature_display_records
            if display is not None and len(display) > 0:
                mask = display["canonical_id"].astype(str) == cid_str
                if mask.any():
                    record = display[mask].iloc[0]
            if record is None:
                zdisplay = (
                    self._mw._library_ctrl._zotero_import_display_records
                )
                if zdisplay is not None and len(zdisplay) > 0:
                    zmask = (
                        zdisplay["canonical_id"].astype(str) == cid_str
                    )
                    if zmask.any():
                        record = zdisplay[zmask].iloc[0]
            if record is None:
                return
            zdata = self.get_zotero_section_data(cid_str, record)
            self._mw.literature_view.detail_panel.set_zotero_status(zdata)
        except Exception:
            pass

    def on_page_entered(self) -> None:
        self.refresh_all()

    def _build_rows_for_card(
        self, card_key: str,
    ) -> tuple[list[list], list[str]] | None:
        library_root = self._mw.get_library_root()
        if card_key == "pending":
            return self._build_pending_rows(library_root)
        if card_key == "synced":
            return self._build_synced_rows(library_root)
        if card_key == "missing":
            return self._build_missing_rows()
        if card_key == "importable":
            if self._importable_cache is None:
                return None
            return self._build_importable_rows()
        return None

    def on_card_clicked(self, card_key: str) -> None:
        result = self._build_rows_for_card(card_key)
        if result is None:
            if card_key == "importable":
                self._activity_appended("warning", "请先配置观察分类并刷新")
            return
        rows, keys = result
        self._current_list_key = card_key
        self.list_data_ready.emit(card_key, rows, keys)

    def _refresh_current_list_if_open(self) -> None:
        if self._current_list_key is None:
            return
        try:
            card_key = self._current_list_key
            result = self._build_rows_for_card(card_key)
            if result is None:
                if card_key == "importable":
                    self.list_data_ready.emit(card_key, [], [])
                return
            rows, keys = result
            self.list_data_ready.emit(card_key, rows, keys)
        except Exception:
            pass

    def on_row_clicked(
        self, card_key: str, item_key_or_cid: str,
    ) -> None:
        if not card_key or not item_key_or_cid:
            return

        try:
            record = self._resolve_zotero_row_record(
                card_key, item_key_or_cid,
            )
        except Exception:
            record = None

        if record is None:
            self._activity_appended(
                "warning",
                f"无法解析列表行数据（{card_key} / {item_key_or_cid}）",
            )
            return

        try:
            detail = self._mw.get_detail_dict_for_record(record)
        except Exception:
            return
        if not detail:
            return

        cid = str(detail.get("canonical_id", "") or "")
        try:
            self._mw.zotero_view.detail_panel.clear_zotero_status()
        except Exception:
            pass
        self.detail_ready.emit(detail)
        if cid:
            try:
                zdata = self.get_zotero_section_data(cid, record)
                if zdata:
                    self._mw.zotero_view.detail_panel.set_zotero_status(
                        zdata
                    )
            except Exception:
                pass

    def _resolve_zotero_row_record(
        self, card_key: str, item_key_or_cid: str,
    ):
        library_root = self._mw.get_library_root()
        records_path = library_root / "records_final.parquet"
        if not records_path.exists():
            return None
        try:
            records = pd.read_parquet(records_path)
        except Exception:
            return None
        if "canonical_id" not in records.columns:
            return None

        cid = ""
        if card_key in ("pending", "missing"):
            cid = str(item_key_or_cid)
        elif card_key == "synced":
            links_path = library_root / "zotero_links.parquet"
            if not links_path.exists():
                return None
            try:
                links = pd.read_parquet(links_path)
            except Exception:
                return None
            if "zotero_item_key" not in links.columns:
                return None
            m = links["zotero_item_key"].astype(str) == str(item_key_or_cid)
            if not m.any():
                return None
            cid = str(links.loc[m, "canonical_id"].iloc[0])
        elif card_key == "importable":
            if self._importable_cache is None:
                return None
            if "zotero_item_key" not in self._importable_cache.columns:
                return None
            m = (
                self._importable_cache["zotero_item_key"]
                .astype(str) == str(item_key_or_cid)
            )
            if not m.any():
                return None
            cid = str(
                self._importable_cache.loc[m, "canonical_id"].iloc[0]
            )

        if not cid:
            return None

        mask = records["canonical_id"].astype(str) == cid
        if not mask.any():
            return None
        return records[mask].iloc[0]

    def _has_action_worker_running(self) -> bool:
        w = self._action_worker
        if w is None:
            return False
        try:
            return w.isRunning()
        except RuntimeError:
            self._action_worker = None
            return False

    def refresh_all(self) -> None:
        if self._refresh_worker is not None:
            try:
                if self._refresh_worker.isRunning():
                    return
            except RuntimeError:
                self._refresh_worker = None

        self._done_timer.stop()
        display_time = self._last_update or "更新中"
        self.refresh_status_changed.emit("loading", display_time)

        try:
            config = self._mw.get_config()
            library_root = self._mw.get_library_root()
        except Exception as exc:
            self.refresh_status_changed.emit("error", self._last_update)
            self._activity_appended("error", f"刷新失败：{exc}")
            return

        current_names = list(
            (config.get("zotero", {}) or {}).get(
                "import_collection_names", []
            ) or []
        )

        try:
            from ui.bridge.zotero_worker import ZoteroRefreshWorker
            worker = ZoteroRefreshWorker(
                config, library_root, current_names,
                previous_stats=self._stats_cache,
                previous_missing_cache=self._missing_cache,
                parent=self,
            )
            worker.finished_ok.connect(self._on_refresh_done)
            worker.failed.connect(self._on_refresh_failed)
            worker.interrupted.connect(self._on_refresh_interrupted)
            self._refresh_worker = worker
            worker.start()
        except Exception as exc:
            self.refresh_status_changed.emit("error", self._last_update)
            self._activity_appended("error", f"刷新启动失败：{exc}")

    def _on_refresh_done(self, result: dict) -> None:
        self._refresh_worker = None
        try:
            self._connected = bool(result.get("connected", False))
            self._missing_cache = list(result.get("missing_cache") or [])
            self._importable_cache = result.get("importable_df")
            self._stats_cache = dict(result.get("stats") or {})
            current_names = list(result.get("current_names") or [])

            verify_ok = bool(result.get("verify_ok", False))
            unverified_count = int(
                result.get("unverified_count", 0) or 0
            )
            stats_source = str(result.get("stats_source", "fresh"))
            verify_errors = list(result.get("verify_errors") or [])

            link_mode = str(result.get("link_mode", "imported_file"))
            self.connection_changed.emit(
                self._connected, "localhost:23119", link_mode,
            )
            new_collections = list(result.get("collections") or [])
            self._collections_cache = new_collections
            self.collections_loaded.emit(
                new_collections,
                current_names,
            )
            self.stats_updated.emit(self._stats_cache)
            try:
                self._refresh_current_list_if_open()
            except Exception:
                pass

            if stats_source == "fresh":
                self._last_update = datetime.now().strftime("%H:%M")
                self.refresh_status_changed.emit(
                    "done", self._last_update,
                )
            else:
                self.refresh_status_changed.emit(
                    "cached", self._last_update,
                )

            if verify_ok:
                if unverified_count > 0:
                    self._activity_appended(
                        "warning",
                        f"刷新完成（{unverified_count} 条暂无法验证）："
                        f"{self._stats_cache}"
                    )
                else:
                    self._activity_appended(
                        "info", f"刷新完成：{self._stats_cache}"
                    )
            else:
                err_text = (
                    "; ".join(verify_errors)
                    if verify_errors else "未知原因"
                )
                self._activity_appended(
                    "warning",
                    f"Zotero 校验失败，已保留上次缓存（{err_text}）"
                )

            self._item_alive_cache = {
                k: v for k, v in self._item_alive_cache.items()
                if v[0] == "alive"
            }
            self._done_timer.start()
        except Exception as exc:
            self.refresh_status_changed.emit("error", self._last_update)
            self._activity_appended(
                "error", f"刷新完成但应用失败：{exc}"
            )

    def _on_refresh_failed(self, error: str) -> None:
        self._refresh_worker = None
        self.refresh_status_changed.emit("error", self._last_update)
        self._activity_appended("error", f"刷新失败：{error}")

    def _on_refresh_interrupted(self) -> None:
        self._refresh_worker = None
        self.refresh_status_changed.emit("idle", self._last_update)
        self._activity_appended("info", "Zotero 刷新已取消")

    def _on_action_done(self, result: dict) -> None:
        self._action_worker = None
        if not isinstance(result, dict):
            result = {}
        action = str(result.get("action", ""))

        if action == "sync":
            count = int(result.get("count", 0))
            self._activity_appended("info", f"同步 {count} 条完成")
        elif action == "import":
            imported = int(result.get("count", 0))
            skipped = int(result.get("skipped", 0))
            errors = list(result.get("errors", []))
            self._activity_appended(
                "info",
                f"引入 {imported} 条，跳过 {skipped} 条，"
                f"错误 {len(errors)} 条",
            )
            self._importable_cache = None
        elif action == "resync":
            cid = str(result.get("cid", "") or "")
            err = result.get("error")
            if err:
                self._activity_appended(
                    "error",
                    f"重新同步 {cid} 失败：{err}",
                )
            else:
                count = int(result.get("count", 0))
                self._activity_appended(
                    "info",
                    f"重新同步 {cid} 完成（{count} 条）",
                )

        self.refresh_status_changed.emit("idle", self._last_update)

        self.on_data_changed()
        self._refresh_current_list_if_open()
        if action == "import":
            try:
                self._mw.refresh_after_data_change()
            except Exception as exc:
                self._activity_appended(
                    "error",
                    f"引入成功但文献库刷新失败：{type(exc).__name__}: {exc}",
                )

    def _on_action_failed(self, error: str) -> None:
        self._action_worker = None
        self.refresh_status_changed.emit("error", self._last_update)
        self._activity_appended("error", f"操作失败：{error}")

    def _on_action_interrupted(self) -> None:
        self._action_worker = None
        self.refresh_status_changed.emit("idle", self._last_update)
        self._activity_appended("info", "操作已取消")

    def _on_sync_progress(self, current: int, total: int) -> None:
        if total <= 0:
            return
        try:
            self.refresh_status_changed.emit(
                "loading", f"同步 {current}/{total}"
            )
        except Exception:
            pass

    def shutdown(self) -> None:
        w = self._refresh_worker
        self._refresh_worker = None
        if w is not None:
            try:
                if w.isRunning():
                    w.request_interrupt()
                    if not w.wait(2000):
                        w.terminate()
                        w.wait(500)
            except RuntimeError:
                pass

        for key, worker in list(self._pending_alive_checks.items()):
            try:
                if worker.isRunning():
                    if not worker.wait(500):
                        worker.terminate()
                        worker.wait(200)
            except RuntimeError:
                pass
            self._pending_alive_checks.pop(key, None)

        aw = self._action_worker
        self._action_worker = None
        if aw is not None:
            try:
                if aw.isRunning():
                    aw.request_interrupt()
                    if not aw.wait(10000):
                        aw.terminate()
                        aw.wait(500)
            except RuntimeError:
                pass

        self._item_alive_cache.clear()
        self._alive_check_queue.clear()

    def on_data_changed(self) -> None:
        try:
            library_root = self._mw.get_library_root()
            config = self._mw.get_config()

            include_cids = self._load_include_cids(library_root)
            linked_cids = self._load_linked_cids(library_root)
            self._stats_cache["pending"] = len(include_cids - linked_cids)

            linked_set = set(linked_cids)
            self._missing_cache = [
                m for m in self._missing_cache
                if str(m.get("canonical_id", "") or "") in linked_set
            ]

            self._stats_cache["missing"] = len(self._missing_cache)
            self._stats_cache["synced"] = max(
                0,
                len(linked_cids) - len(self._missing_cache),
            )

            names = list(
                (config.get("zotero", {}) or {}).get(
                    "import_collection_names", []
                ) or []
            )
            if not names:
                self._importable_cache = None
                self._stats_cache["importable"] = 0
            elif self._importable_cache is None:
                self._stats_cache["importable"] = 0
            else:
                self._stats_cache["importable"] = int(
                    len(self._importable_cache)
                )

            self.stats_updated.emit(self._stats_cache)
        except Exception:
            pass

    def on_sync_selected(self, cids: list) -> None:
        if not cids:
            return
        if not self._zotero_desktop_alive():
            self._activity_appended("error", "Zotero 桌面端未运行")
            return
        if self._has_action_worker_running():
            self._activity_appended(
                "warning", "同步 / 引入任务进行中，请稍候"
            )
            return
        if self._mw.is_busy():
            self._mw.notify_user("warning", "有任务运行中，请稍后")
            return
        from PySide6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self._mw, "确认同步",
            f"即将同步 {len(cids)} 条记录到 Zotero。\n\n确认？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            library_root = self._mw.get_library_root()
            config = self._mw.get_config()
            included_df = self._zotero_build_included_df(library_root, cids)
            if len(included_df) == 0:
                self._activity_appended("info", "无待同步记录")
                return
        except Exception as exc:
            self._activity_appended("error", f"同步准备失败：{exc}")
            return

        from ui.bridge.zotero_worker import ZoteroActionWorker
        worker = ZoteroActionWorker(
            action="sync",
            payload={"included_df": included_df},
            config=config,
            library_root=library_root,
            parent=self,
        )
        worker.finished_ok.connect(self._on_action_done)
        worker.failed.connect(self._on_action_failed)
        worker.interrupted.connect(self._on_action_interrupted)
        worker.progressed.connect(self._on_sync_progress)
        self._action_worker = worker
        self.refresh_status_changed.emit(
            "loading", f"同步 {len(included_df)} 条…"
        )
        worker.start()

    def on_prune_selected(self, cids: list) -> None:
        if not cids:
            return
        if self._has_action_worker_running():
            self._activity_appended(
                "warning", "同步 / 引入任务进行中，请稍候"
            )
            return
        from PySide6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self._mw, "确认清理",
            f"即将清理 {len(cids)} 条失效记录的 Zotero 关联。\n\n"
            f"  · 从 zotero_links 删除\n"
            f"  · 保留 records_final 记录\n\n确认？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            from medlitflow import zotero_import
            library_root = self._mw.get_library_root()
            result = zotero_import.clear_zotero_links(cids, library_root)
            self._activity_appended(
                "info",
                f"清理 {result['cleared']} 条，"
                f"跳过 {len(result['skipped'])} 条",
            )
            cid_set = set(str(c) for c in cids if c)
            self._missing_cache = [
                m for m in self._missing_cache
                if str(m.get("canonical_id", "") or "") not in cid_set
            ]
        except Exception as exc:
            self._activity_appended("error", f"清理失败：{exc}")
        self.on_data_changed()
        self._refresh_current_list_if_open()

    def on_import_selected(self, item_keys: list) -> None:
        if not item_keys:
            return
        if self._importable_cache is None:
            return
        if self._has_action_worker_running():
            self._activity_appended(
                "warning", "同步 / 引入任务进行中，请稍候"
            )
            return
        from PySide6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self._mw, "确认引入",
            f"即将引入 {len(item_keys)} 条记录到文献库。\n\n"
            f"  · 将追加到 records_final（data_origin=zotero_import）\n"
            f"  · 不复制 PDF 文件\n\n确认？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        candidates = self._importable_cache[
            self._importable_cache["zotero_item_key"].isin(item_keys)
        ].copy()
        if len(candidates) == 0:
            self._activity_appended("info", "无可引入记录")
            return
        try:
            library_root = self._mw.get_library_root()
            config = self._mw.get_config()
        except Exception as exc:
            self._activity_appended("error", f"配置加载失败：{exc}")
            return

        from ui.bridge.zotero_worker import ZoteroActionWorker
        worker = ZoteroActionWorker(
            action="import",
            payload={"candidates": candidates},
            config=config,
            library_root=library_root,
            parent=self,
        )
        worker.finished_ok.connect(self._on_action_done)
        worker.failed.connect(self._on_action_failed)
        worker.interrupted.connect(self._on_action_interrupted)
        self._action_worker = worker
        self.refresh_status_changed.emit(
            "loading", f"引入 {len(candidates)} 条…"
        )
        worker.start()

    def on_open_zotero_library(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl("zotero://"))

    def on_open_zotero_item(self, item_key: str) -> None:
        if not item_key:
            self._activity_appended("warning", "item_key 为空")
            return
        if not self._zotero_desktop_alive():
            self._activity_appended("warning", "Zotero 未运行")
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl(f"zotero://select/items/{item_key}"))

    def on_detail_sync(self, cid: str) -> None:
        self.on_sync_selected([cid])

    def on_detail_resync(self, cid: str) -> None:
        if not cid:
            return
        if not self._zotero_desktop_alive():
            self._activity_appended("error", "Zotero 桌面端未运行")
            return
        if self._mw.is_busy():
            self._mw.notify_user("warning", "有任务运行中，请稍后")
            return
        if self._has_action_worker_running():
            self._activity_appended(
                "warning", "同步 / 引入任务进行中，请稍候"
            )
            return

        library_root = self._mw.get_library_root()
        config = self._mw.get_config()
        included_df = self._zotero_build_included_df(library_root, [cid])
        if len(included_df) == 0:
            self._activity_appended(
                "info", "无可同步记录（records 已不存在）"
            )
            return

        from ui.bridge.zotero_worker import ZoteroActionWorker
        worker = ZoteroActionWorker(
            action="resync",
            payload={"cid": cid, "included_df": included_df},
            config=config,
            library_root=library_root,
            parent=self,
        )
        worker.finished_ok.connect(self._on_action_done)
        worker.failed.connect(self._on_action_failed)
        worker.interrupted.connect(self._on_action_interrupted)
        self._action_worker = worker
        self.refresh_status_changed.emit(
            "loading", f"重新同步 {cid}…"
        )
        worker.start()

    def on_detail_withdraw(self, cid: str) -> None:
        if not cid:
            return
        from PySide6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self._mw, "确认撤回",
            f"即将撤回该记录（cid={cid}）。\n\n"
            f"  · 从 records_final 删除\n"
            f"  · 从 zotero_links 删除\n"
            f"  · 不删除 Zotero 中的条目\n\n"
            f"此操作不可逆。确认？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            from medlitflow import zotero_import
            library_root = self._mw.get_library_root()
            result = zotero_import.withdraw_imported_items([cid], library_root)
            self._activity_appended("info", f"撤回 {result['withdrawn']} 条")
            cid_str = str(cid)
            self._missing_cache = [
                m for m in self._missing_cache
                if str(m.get("canonical_id", "") or "") != cid_str
            ]
        except Exception as exc:
            self._activity_appended("error", f"撤回失败：{exc}")
        self._mw.literature_view.close_detail()
        self._mw.refresh_after_data_change()
        self._refresh_current_list_if_open()

    def on_detail_clear_links(self, cid: str) -> None:
        if not cid:
            return
        from PySide6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self._mw, "确认清理关联",
            f"即将清理该记录的 Zotero 关联。\n\n"
            f"  · 从 zotero_links 删除\n"
            f"  · 保留 records_final 中的记录\n"
            f"  · 不删除 Zotero 中的条目\n\n确认？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            from medlitflow import zotero_import
            library_root = self._mw.get_library_root()
            result = zotero_import.clear_zotero_links([cid], library_root)
            self._activity_appended("info", f"清理关联 {result['cleared']} 条")
            cid_str = str(cid)
            self._missing_cache = [
                m for m in self._missing_cache
                if str(m.get("canonical_id", "") or "") != cid_str
            ]
        except Exception as exc:
            self._activity_appended("error", f"清理失败：{exc}")
        self._mw.refresh_after_data_change()
        self._refresh_current_list_if_open()

    def on_detail_open(self, item_key: str) -> None:
        self.on_open_zotero_item(item_key)

    def get_zotero_section_data(self, cid: str, record) -> dict:
        if not cid:
            return {}

        self._current_detail_cid = str(cid)

        library_root = self._mw.get_library_root()
        links_path = library_root / "zotero_links.parquet"

        raw_origin = (
            record.get("data_origin") if record is not None else None
        )
        if (
            raw_origin is None
            or (isinstance(raw_origin, float) and raw_origin != raw_origin)
            or str(raw_origin).strip().lower() in ("", "none", "nan")
        ):
            origin = "retrieval"
        else:
            origin = str(raw_origin).strip()
        if origin not in self._VALID_DATA_ORIGINS:
            origin = "retrieval"

        if not links_path.exists():
            return {
                "state": "not_synced", "data_origin": origin,
                "canonical_id": cid,
            }
        try:
            links = pd.read_parquet(links_path)
        except Exception:
            return {
                "state": "not_synced", "data_origin": origin,
                "canonical_id": cid,
            }
        match = links[links["canonical_id"].astype(str) == cid]
        if len(match) == 0:
            return {
                "state": "not_synced", "data_origin": origin,
                "canonical_id": cid,
            }

        link_row = match.iloc[0]
        item_key = str(link_row.get("zotero_item_key", "") or "")

        if not item_key:
            return {
                "state": "invalid",
                "data_origin": origin,
                "item_key": "",
                "synced_at": str(link_row.get("zotero_synced_at", "") or ""),
                "link_mode": str(link_row.get("zotero_link_mode", "") or ""),
                "import_collection": str(link_row.get("zotero_collection", "") or ""),
                "canonical_id": cid,
            }

        cached = self._item_alive_cache.get(item_key)
        now = _time.monotonic()
        if cached is not None and (now - cached[1]) < self._ALIVE_CACHE_TTL:
            cached_status = cached[0]
            if cached_status == "alive":
                state = (
                    "synced" if origin == "zotero_import"
                    else "retrieval_overridden"
                )
            elif cached_status == "confirmed_missing":
                state = "missing"
            else:
                state = "unverified"
        else:
            self._schedule_alive_check(item_key, cid)
            state = "checking"

        if state not in self._VALID_STATES:
            state = "not_synced"

        return {
            "state": state,
            "data_origin": origin,
            "item_key": item_key,
            "synced_at": str(link_row.get("zotero_synced_at", "") or ""),
            "link_mode": str(link_row.get("zotero_link_mode", "") or ""),
            "import_collection": str(link_row.get("zotero_collection", "") or ""),
            "canonical_id": cid,
        }

    def _schedule_alive_check(self, item_key: str, cid: str) -> None:
        if not item_key:
            return
        if item_key in self._pending_alive_checks:
            return
        for qkey, _qcid in self._alive_check_queue:
            if qkey == item_key:
                return

        if len(self._pending_alive_checks) >= self._ALIVE_CHECK_MAX_CONCURRENT:
            self._alive_check_queue.append((item_key, cid))
            return

        config = self._mw.get_config() if hasattr(self._mw, "get_config") else {}
        zc = (config.get("zotero", {}) or {}) if config else {}
        lt = core.clean_text(zc.get("library_type", "user")) or "user"
        lid = core.clean_text(str(zc.get("library_id", "0"))) or "0"

        worker = ZoteroSectionAliveWorker(
            item_key, lt, lid, parent=self,
        )
        self._pending_alive_checks[item_key] = worker
        worker.checked.connect(
            lambda key, status, c=cid:
                self._on_alive_checked(key, status, c)
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_alive_checked(
        self, item_key: str, status: str, cid: str,
    ) -> None:
        if status not in ("alive", "confirmed_missing", "unverified"):
            status = "unverified"
        self._item_alive_cache[item_key] = (status, _time.monotonic())
        self._pending_alive_checks.pop(item_key, None)
        if str(cid) == self._current_detail_cid:
            try:
                self.section_updated.emit(str(cid))
            except Exception:
                pass
        if self._alive_check_queue:
            next_key, next_cid = self._alive_check_queue.pop(0)
            self._schedule_alive_check(next_key, next_cid)

    def invalidate_alive_cache(self, item_key: str) -> None:
        if not item_key:
            return
        self._item_alive_cache.pop(item_key, None)
        self._pending_alive_checks.pop(item_key, None)

    def on_detail_reverify(self, cid: str) -> None:
        if not cid:
            return
        library_root = self._mw.get_library_root()
        links_path = library_root / "zotero_links.parquet"
        if not links_path.exists():
            return
        try:
            links = pd.read_parquet(links_path)
            match = links[links["canonical_id"].astype(str) == cid]
            if len(match) == 0:
                return
            item_key = str(match.iloc[0].get("zotero_item_key", "") or "")
            if not item_key:
                return

            self.invalidate_alive_cache(item_key)
            self._current_detail_cid = str(cid)
            self._schedule_alive_check(item_key, cid)
            self.section_updated.emit(str(cid))
        except Exception:
            pass


    def set_current_detail_cid(self, cid: str) -> None:
        self._current_detail_cid = str(cid or "")

    def _zotero_desktop_alive(self) -> bool:
        try:
            import httpx
            from medlitflow import zotero_bridge
            config = self._mw.get_config()
            zc = config.get("zotero", {}) or {}
            lt = core.clean_text(zc.get("library_type", "user")) or "user"
            lid = core.clean_text(str(zc.get("library_id", "0"))) or "0"
            url = f"{zotero_bridge.ZOTERO_LOCAL_API}/{lt}s/{lid}/items"
            with httpx.Client(timeout=0.5) as client:
                r = client.get(url, params={"limit": 1})
            return r.status_code == 200
        except Exception:
            return False

    def _zotero_load_collections(self) -> list[dict]:
        try:
            from medlitflow import zotero_import
            collections = zotero_import.list_collections(
                self._mw.get_library_root(), self._mw.get_config(),
            )
            self._collections_cache = collections
            return collections
        except Exception:
            return self._collections_cache

    def _activity_appended(self, level: str, message: str) -> None:
        self.activity_appended.emit(str(level), str(message))

    def _on_bridge_log(self, level: str, message: str) -> None:
        self._activity_appended(str(level), str(message))

    def _on_done_timeout(self) -> None:
        self.refresh_status_changed.emit("done_empty", self._last_update)

    def _load_include_cids(self, library_root: Path) -> set[str]:
        path = library_root / "screening_decisions.parquet"
        if not path.exists():
            return set()
        try:
            df = pd.read_parquet(path)
        except Exception:
            return set()
        if "user_decision" not in df.columns or "canonical_id" not in df.columns:
            return set()
        return set(
            df.loc[
                df["user_decision"].astype(str) == "include",
                "canonical_id",
            ].astype(str).tolist()
        )

    def _load_linked_cids(self, library_root: Path) -> set[str]:
        path = library_root / "zotero_links.parquet"
        if not path.exists():
            return set()
        try:
            df = pd.read_parquet(path)
        except Exception:
            return set()
        if "canonical_id" not in df.columns:
            return set()
        return set(df["canonical_id"].astype(str).tolist())

    def _zotero_build_included_df(
        self, library_root: Path, cids: list[str],
    ) -> pd.DataFrame:
        if not cids:
            return pd.DataFrame()
        records_path = library_root / "records_final.parquet"
        if not records_path.exists():
            return pd.DataFrame()
        try:
            records = pd.read_parquet(records_path)
        except Exception:
            return pd.DataFrame()
        if "canonical_id" not in records.columns:
            return pd.DataFrame()
        cid_set = set(str(c) for c in cids if c)
        sub = records[
            records["canonical_id"].astype(str).isin(cid_set)
        ]
        if len(sub) == 0:
            return pd.DataFrame()
        keep = ["canonical_id", "title", "authors", "doi", "journal", "year"]
        for c in keep:
            if c not in sub.columns:
                sub[c] = ""
        return sub[keep].reset_index(drop=True)

    def _build_pending_rows(
        self, library_root: Path,
    ) -> tuple[list[list], list[str]]:
        rows: list[list] = []
        item_keys: list[str] = []
        include_cids = self._load_include_cids(library_root)
        linked_cids = self._load_linked_cids(library_root)
        pending_cids = include_cids - linked_cids
        if not pending_cids:
            return rows, item_keys
        records_path = library_root / "records_final.parquet"
        if not records_path.exists():
            return rows, item_keys
        try:
            records = pd.read_parquet(records_path)
        except Exception:
            return rows, item_keys
        sub = records[
            records["canonical_id"].astype(str).isin(pending_cids)
        ]
        for r in sub.to_dict("records"):
            cid = str(r.get("canonical_id", "") or "")
            rows.append([
                str(r.get("title", "") or ""),
                first_author(r.get("authors", "")),
                format_year(r.get("year")),
                str(r.get("doi", "") or ""),
            ])
            item_keys.append(cid)
        return rows, item_keys

    def _build_synced_rows(
        self, library_root: Path,
    ) -> tuple[list[list], list[str]]:
        """已同步卡（排除 missing）：[标题, 作者, 年份, 同步时间, 分类]。"""
        rows: list[list] = []
        item_keys: list[str] = []
        links_path = library_root / "zotero_links.parquet"
        if not links_path.exists():
            return rows, item_keys
        try:
            links = pd.read_parquet(links_path)
        except Exception:
            return rows, item_keys
        if len(links) == 0:
            return rows, item_keys
        records_path = library_root / "records_final.parquet"
        record_map: dict[str, dict] = {}
        if records_path.exists():
            try:
                rdf = pd.read_parquet(records_path)
                for r in rdf.to_dict("records"):
                    cid = str(r.get("canonical_id", "") or "")
                    if cid:
                        record_map[cid] = r
            except Exception:
                pass
        missing_cids = {
            str(m.get("canonical_id", "") or "")
            for m in self._missing_cache
        }
        for link in links.to_dict("records"):
            cid = str(link.get("canonical_id", "") or "")
            if not cid or cid in missing_cids:
                continue
            rec = record_map.get(cid, {})
            rows.append([
                str(rec.get("title", "") or ""),
                first_author(rec.get("authors", "")),
                format_year(rec.get("year")),
                str(link.get("zotero_synced_at", "") or ""),
                str(link.get("zotero_collection", "") or ""),
            ])
            item_keys.append(str(link.get("zotero_item_key", "") or ""))
        return rows, item_keys

    def _build_missing_rows(self) -> tuple[list[list], list[str]]:
        rows: list[list] = []
        item_keys: list[str] = []
        if not self._missing_cache:
            return rows, item_keys

        library_root = self._mw.get_library_root()
        records_path = library_root / "records_final.parquet"
        record_map: dict[str, dict] = {}
        if records_path.exists():
            try:
                rdf = pd.read_parquet(records_path)
                for r in rdf.to_dict("records"):
                    cid = str(r.get("canonical_id", "") or "")
                    if cid:
                        record_map[cid] = r
            except Exception:
                pass

        for m in self._missing_cache:
            cid = str(m.get("canonical_id", "") or "")
            http_status = m.get("http_status", 0)
            rec = record_map.get(cid, {})
            rows.append([
                str(rec.get("title", "") or ""),
                first_author(rec.get("authors", "")),
                format_year(rec.get("year")),
                str(http_status),
            ])
            item_keys.append(cid)
        return rows, item_keys

    def _build_importable_rows(self) -> tuple[list[list], list[str]]:
        """可引入卡：[标题, 作者, 年份, DOI, 分类]，item_keys 存 item_key。"""
        rows: list[list] = []
        item_keys: list[str] = []
        if self._importable_cache is None:
            return rows, item_keys
        for r in self._importable_cache.to_dict("records"):
            rows.append([
                str(r.get("title", "") or ""),
                first_author(r.get("authors", "")),
                format_year(r.get("year")),
                str(r.get("doi", "") or ""),
                str(r.get("import_collection", "") or ""),
            ])
            item_keys.append(str(r.get("zotero_item_key", "") or ""))
        return rows, item_keys

    def on_configure_collections(self) -> None:
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QCheckBox,
            QPushButton, QLabel, QScrollArea, QWidget, QFrame,
        )
        from PySide6.QtCore import Qt

        config = self._mw.get_config()
        current_names = list(
            (config.get("zotero", {}) or {}).get(
                "import_collection_names", []
            ) or []
        )
        collections = self._zotero_load_collections()
        if not collections:
            self._mw.notify_user(
                "warning", "未获取到 Zotero 分类列表（请确认桌面端运行）"
            )
            return

        dlg = QDialog(self._mw)
        dlg.setWindowTitle("配置观察分类")
        dlg.setMinimumWidth(480)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(10)
        v.addWidget(QLabel("勾选要扫描的 Zotero 分类（用于\"可引入\"）："))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        list_w = QWidget()
        list_v = QVBoxLayout(list_w)
        list_v.setContentsMargins(0, 0, 0, 0)
        list_v.setSpacing(4)
        checkboxes: dict[str, QCheckBox] = {}
        for col in collections:
            name = col["name"]
            cb = QCheckBox(f"{name}  —  {col['item_count']} 条")
            cb.setChecked(name in current_names)
            checkboxes[name] = cb
            list_v.addWidget(cb)
        list_v.addStretch()
        scroll.setWidget(list_w)
        scroll.setMinimumHeight(200)
        v.addWidget(scroll, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        confirm_btn = QPushButton("确定")
        confirm_btn.setObjectName("PrimaryCTA")
        confirm_btn.setCursor(Qt.PointingHandCursor)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(confirm_btn)
        v.addLayout(btn_row)
        cancel_btn.clicked.connect(dlg.reject)
        confirm_btn.clicked.connect(dlg.accept)

        if dlg.exec() != QDialog.Accepted:
            return
        selected = [
            name for name, cb in checkboxes.items() if cb.isChecked()
        ]
        self.on_collection_changed(selected)

    def on_collection_changed(self, names: list) -> None:
        names = [str(n) for n in (names or []) if n]
        try:
            self._persist_import_collection_names(names)
        except Exception as exc:
            self._activity_appended("error", f"保存观察分类失败：{exc}")
            return
        self._activity_appended("info", f"观察分类已更新：{len(names)} 个")

        self.collections_loaded.emit(self._collections_cache, names)
        self._importable_cache = None
        self.on_data_changed()

    def _persist_import_collection_names(self, names: list) -> None:
        import os
        import yaml
        user_yaml = self._mw._config_ctrl.user_yaml_path()
        existing: dict = {}
        if user_yaml.exists():
            try:
                existing = yaml.safe_load(
                    user_yaml.read_text(encoding="utf-8")
                ) or {}
            except Exception:
                existing = {}
        if not isinstance(existing, dict):
            existing = {}
        existing.setdefault("zotero", {})[
            "import_collection_names"
        ] = list(names)
        user_yaml.parent.mkdir(parents=True, exist_ok=True)
        tmp = user_yaml.with_name(user_yaml.name + ".tmp")
        tmp.write_text(
            yaml.safe_dump(existing, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        os.replace(tmp, user_yaml)

        self._mw._config_ctrl._config_cache = None
        self._mw._config_ctrl._config_cache_mtime = None