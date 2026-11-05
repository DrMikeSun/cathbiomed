"""ui/controllers/screening_controller.py — 筛选中心控制器（A5）。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
from PySide6.QtCore import QObject, QTimer

if TYPE_CHECKING:
    from ui.views.main_window import MedLitFlowMainWindow
    from ui.controllers.config_controller import ConfigController
    from ui.controllers.library_controller import LibraryController
    from ui.controllers.strategy_controller import StrategyController

class ScreeningController(QObject):
    def __init__(
        self,
        main_window: "MedLitFlowMainWindow",
        config_ctrl: "ConfigController",
        library_ctrl: "LibraryController",
        strategy_ctrl: "StrategyController",
    ):
        super().__init__(main_window)
        self._mw = main_window
        self._config_ctrl = config_ctrl
        self._library_ctrl = library_ctrl
        self._strategy_ctrl = strategy_ctrl

        self._screening_draft_manager = None
        self._screening_migrated = False
        self._stats_pending_strategies: set[str] = set()
        self._pending_decisions_by_strategy: dict[str, dict[str, str]] = {}

        self._stats_debounce_timer = QTimer(self)
        self._stats_debounce_timer.setSingleShot(True)
        self._stats_debounce_timer.setInterval(300)
        self._stats_debounce_timer.timeout.connect(
            self._on_stats_debounce_timeout
        )

        self._storage_stats_timer = QTimer(self)
        self._storage_stats_timer.setSingleShot(True)
        self._storage_stats_timer.setInterval(200)
        self._storage_stats_timer.timeout.connect(
            self._refresh_storage_stats_impl
        )

    def cleanup(self) -> None:
        try:
            if self._stats_debounce_timer is not None:
                self._stats_debounce_timer.stop()
        except Exception:
            pass
        try:
            if self._storage_stats_timer is not None:
                self._storage_stats_timer.stop()
        except Exception:
            pass
        if self._screening_draft_manager is not None:
            try:
                self._screening_draft_manager.flush()
                self._screening_draft_manager.stop()
            except Exception:
                pass

    def ensure_screening_migrated(self) -> None:
        if self._screening_migrated:
            return

        try:
            from medlitflow import screening
            config = self._config_ctrl.get_config()
            library_root = Path(config["library"]["root"])
        except Exception as exc:
            self._mw._notify_status(
                "warning", f"筛选中心迁移跳过（配置不可用）：{exc}"
            )
            return

        def _ok(r):
            return (
                isinstance(r, dict)
                and r.get("status") in
                ("migrated", "already_migrated", "no_file")
            )

        all_ok = True

        try:
            r_dec = screening.migrate_decisions_schema(
                library_root, config,
            )
            if r_dec.get("status") == "migrated":
                self._mw._notify_status(
                    "info",
                    f"decisions schema 已迁移："
                    f"{r_dec.get('original_rows', 0)} 行 → "
                    f"{r_dec.get('migrated_rows', 0)} 行",
                )
            if not _ok(r_dec):
                all_ok = False
        except Exception as exc:
            all_ok = False
            self._mw._notify_status("warning", f"decisions 迁移失败：{exc}")

        try:
            r_fi = screening.migrate_file_index_schema(library_root)
            if r_fi.get("status") == "migrated":
                cols = ", ".join(r_fi.get("columns_added", []))
                self._mw._notify_status(
                    "info", f"file_index schema 已迁移（加列：{cols}）"
                )
            if not _ok(r_fi):
                all_ok = False
        except Exception as exc:
            all_ok = False
            self._mw._notify_status("warning", f"file_index 迁移失败：{exc}")

        if all_ok:
            self._screening_migrated = True

    def refresh_screening(self) -> None:
        records = self._library_ctrl._library_records
        if records is None:
            records = self._library_ctrl.load_library_records()

        strategies_set: set[str] = set()
        if records is not None and len(records) > 0:
            try:
                if "strategy_names" in records.columns:
                    s = records["strategy_names"].fillna("").astype(str)
                    names = s.str.split(";").explode().str.strip()
                    strategies_set.update({n for n in names if n})
            except Exception:
                pass

        running_strategies: set[str] = set()
        if (self._mw._has_running_worker()
                and self._mw._running_preview_records is not None
                and len(self._mw._running_preview_records) > 0):
            try:
                rp = self._mw._running_preview_records
                if "strategy_names" in rp.columns:
                    s = rp["strategy_names"].fillna("").astype(str)
                    names = s.str.split(";").explode().str.strip()
                    running_strategies = {n for n in names if n}
                    strategies_set.update(running_strategies)
            except Exception:
                pass

        alive_strategies, status_map = self._read_strategy_registry_once()

        if alive_strategies is not None:
            strategies_set = {
                s for s in strategies_set
                if s in alive_strategies or s in running_strategies
            }

        strategies = sorted(strategies_set)

        if self._screening_draft_manager is None and strategies:
            try:
                config = self._config_ctrl.get_config()
                library_root = Path(config["library"]["root"])
                from ui.components.screening_draft_manager import (
                    ScreeningDraftManager,
                )
                self._screening_draft_manager = ScreeningDraftManager(
                    library_root
                )
                self._mw.screening_view.set_draft_manager(
                    self._screening_draft_manager
                )
                for s in strategies:
                    try:
                        self._screening_draft_manager.load_from_disk(s)
                    except Exception:
                        pass
            except Exception as exc:
                self._screening_draft_manager = None
                self._screening_log(
                    "warning", f"DraftManager 初始化失败：{exc}"
                )

        groups: dict[str, list[dict]] = {
            "已筛选": [], "部分筛选": [], "采集中": [], "未筛选": [],
            "已归档": [],
        }
        for name in strategies:
            st = status_map.get(name, "unscreened")
            if name in running_strategies:
                group_name = "采集中"
            elif st == "confirmed":
                group_name = "已筛选"
            elif st == "partially_screened":
                group_name = "部分筛选"
            elif st == "collecting":
                group_name = "采集中"
            elif st == "archived_unprocessed":
                group_name = "已归档"
            else:
                group_name = "未筛选"
            count = 0
            try:
                if name in running_strategies:
                    rp = self._mw._running_preview_records
                    sub = rp[
                        rp["strategy_names"].astype(str).str.contains(
                            rf"(?:^|;)\s*{re.escape(name)}\s*(?:;|$)",
                            regex=True, na=False,
                        )
                    ]
                    count = int(len(sub))
                elif records is not None and len(records) > 0:
                    sub = records[
                        records["strategy_names"].astype(str).str.contains(
                            rf"(?:^|;)\s*{re.escape(name)}\s*(?:;|$)",
                            regex=True, na=False,
                        )
                    ]
                    count = int(len(sub))
            except Exception:
                count = 0
            has_draft = (
                self._screening_draft_manager is not None
                and self._screening_draft_manager.has_draft(name)
            )
            groups[group_name].append({
                "name": name, "status": st, "count": count,
                "has_draft": has_draft,
            })

        self._mw.screening_view.set_strategy_groups(groups)

        orphan_strategies: list[str] = []
        if alive_strategies is not None:
            try:
                if (records is not None and len(records) > 0
                        and "strategy_names" in records.columns):
                    _s = records["strategy_names"].fillna("").astype(str)
                    _names = _s.str.split(";").explode().str.strip()
                    _all_in_records = {n for n in _names if n}
                    _orphan_set = (
                        _all_in_records
                        - alive_strategies
                        - running_strategies
                    )
                    orphan_strategies = sorted(_orphan_set)
            except Exception:
                pass

        try:
            self._mw.literature_view.set_strategy_options(
                strategies, orphan_strategies,
            )
        except Exception:
            pass

        current = self._mw.screening_view.current_strategy()
        if not strategies:
            if current:
                self._mw.screening_view.set_current_strategy("")
            current = ""
        elif not current or current not in strategies:
            current = strategies[0]

        if current:
            self._mw.screening_view.set_current_strategy(current)

        if self._mw.screening_view.is_user_selected() and current:
            self._refresh_screening_table(current)
            self._update_screening_stats_from_table(current)
        else:
            self._refresh_screening_table("")
            self._update_screening_stats_from_table("")

        if self._storage_stats_timer is not None:
            self._storage_stats_timer.start()
        else:
            self._refresh_storage_stats()

    def on_screening_refresh_status(self, strategy_name) -> None:
        if not strategy_name:
            self._screening_log("warning", "未选择策略")
            return

        try:
            from medlitflow import screening
            config = self._config_ctrl.get_config()
            library_root = Path(config["library"]["root"])
        except Exception as exc:
            self._screening_log("error", f"配置加载失败：{exc}")
            return

        try:
            screening.update_strategy_status(strategy_name, library_root)
            self._screening_log(
                "info", f"策略 {strategy_name} 状态已刷新"
            )
        except Exception as exc:
            self._screening_log("error", f"刷新失败：{exc}")
            return

        self._library_ctrl.load_library_records()
        self._library_ctrl._refresh_literature()
        self._mw._refresh_dashboard()
        self.refresh_screening()

    def on_screening_strategy_changed(self, strategy_name) -> None:
        self._mw.screening_view.set_user_selected(True)
        self._refresh_screening_table(strategy_name)
        self._update_screening_stats_from_table(strategy_name)

    def on_screening_rows_filtered(self) -> None:
        current = self._mw.screening_view.current_strategy()
        if current:
            self._update_screening_stats_from_table(current)

    def on_screening_cleanup_trash(self) -> None:
        if self._mw._has_running_worker():
            self._mw._notify_status(
                "warning",
                "有任务运行中，请等待完成后再清理回收站",
                source="screening",
            )
            return

        try:
            from medlitflow import screening
            config = self._config_ctrl.get_config()
            library_root = Path(config["library"]["root"])
        except Exception as exc:
            self._screening_log("error", f"配置加载失败：{exc}")
            return

        try:
            result = screening.cleanup_trash(library_root, config)
        except Exception as exc:
            self._screening_log("error", f"清理失败：{exc}")
            return

        self._screening_log(
            "info",
            f"已清理 {result.get('deleted_count', 0)} 条，"
            f"释放 {result.get('freed_bytes', 0)} 字节",
        )

        self._library_ctrl.load_library_records()
        self._library_ctrl._refresh_literature()
        self._mw._refresh_dashboard()
        self.refresh_screening()

    def on_screening_decision_changed(
        self, cid, strategy, decision,
    ) -> None:
        if not strategy:
            return
        self._stats_pending_strategies.add(strategy)
        pending = self._pending_decisions_by_strategy.setdefault(
            strategy, {}
        )
        pending[str(cid)] = str(decision or "unscreened")
        self._stats_debounce_timer.start()

    def on_screening_save_decisions(self, strategy) -> None:
        if not strategy:
            return
        try:
            from medlitflow import screening
            config = self._config_ctrl.get_config()
            library_root = Path(config["library"]["root"])
        except Exception as exc:
            self._mw._notify_status(
                "error", f"配置加载失败：{exc}", source="screening"
            )
            return

        try:
            result = screening.save_draft_to_decisions(
                strategy, library_root, config,
            )
        except Exception as exc:
            self._mw._notify_status(
                "error", f"保存决策失败：{exc}", source="screening"
            )
            return

        if result.get("error"):
            detail = str(result.get("detail", "") or "")
            self._mw._notify_status(
                "error",
                f"保存决策失败（磁盘写入失败）：{detail}。"
                f"草稿文件保留，修复后可重试。",
                source="screening",
            )
            return

        merged = int(result.get("merged", 0) or 0)
        skipped = int(result.get("skipped", 0) or 0)
        self._mw._notify_status(
            "info",
            f"策略 {strategy} 决策已保存：合并 {merged} 条，跳过 {skipped} 条",
            source="screening",
        )

        if self._screening_draft_manager is not None:
            try:
                self._screening_draft_manager.clear(strategy)
            except Exception:
                pass

        self._library_ctrl.load_library_records(force=True)
        self._library_ctrl._refresh_literature()
        self._mw._refresh_dashboard()
        self.refresh_screening()
        self._strategy_ctrl.refresh_completed_strategies()
        try:
            current = self._mw.screening_view.current_strategy()
            if current == strategy:
                self._update_screening_stats_from_table(strategy)
        except Exception:
            pass

    def on_screening_confirm_archive(self, strategy) -> None:
        if not strategy:
            return

        try:
            from medlitflow import screening
            config = self._config_ctrl.get_config()
            library_root = Path(config["library"]["root"])
        except Exception as exc:
            self._mw._notify_status(
                "error", f"配置加载失败：{exc}", source="screening"
            )
            return

        # 0. 自动合并草稿到 decisions
        draft_path = screening._draft_path(library_root, strategy)
        has_disk_draft = draft_path.exists()
        has_memory_draft = (
            self._screening_draft_manager is not None
            and self._screening_draft_manager.has_draft(strategy)
        )
        if has_disk_draft or has_memory_draft:
            if (has_memory_draft
                    and self._screening_draft_manager is not None):
                try:
                    self._screening_draft_manager.flush(strategy)
                except Exception:
                    pass
                if self._screening_draft_manager.has_dirty_draft(strategy):
                    self._mw._notify_status(
                        "error",
                        f"草稿落盘失败（磁盘空间 / 权限问题），已中止归档。"
                        f"当前决策未丢失，请检查磁盘后重试。",
                        source="screening",
                    )
                    return
            try:
                save_result = screening.save_draft_to_decisions(
                    strategy, library_root, config,
                )
                if save_result.get("error"):
                    detail = str(save_result.get("detail", "") or "")
                    self._mw._notify_status(
                        "error",
                        f"自动保存决策失败（磁盘写入失败），已中止归档"
                        f"（避免基于旧数据结算）：{detail}。请修复后重新归档。",
                        source="screening",
                    )
                    return
                merged = int(save_result.get("merged", 0) or 0)
                if merged > 0:
                    self._mw._notify_status(
                        "info",
                        f"策略 {strategy} 决策已自动保存：合并 {merged} 条",
                        source="screening",
                    )
                if self._screening_draft_manager is not None:
                    try:
                        self._screening_draft_manager.clear(strategy)
                    except Exception:
                        pass
                self._library_ctrl.load_library_records(force=True)
            except Exception as exc:
                self._mw._notify_status(
                    "error",
                    f"自动保存决策失败，已中止归档（避免基于旧数据结算）："
                    f"{exc}。请修复后重新归档。",
                    source="screening",
                )
                return

        # 1. 检查 unscreened
        try:
            stats = screening.compute_progress_stats(strategy, library_root)
        except Exception as exc:
            self._mw._notify_status(
                "error", f"统计失败：{exc}", source="screening"
            )
            return
        unscreened = int(stats.get("unscreened", 0) or 0)
        if unscreened > 0:
            from PySide6.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                self._mw,
                "还有未筛选文献",
                f"策略 {strategy} 还有 {unscreened} 篇未筛选。\n\n"
                f"建议先在筛选中心完成决策再归档。\n\n"
                f"去筛选？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                try:
                    from ui.config import PAGE_SCREENING
                    self._mw.goto_page(PAGE_SCREENING)
                except Exception:
                    pass
            return

        # 2. 预扫文件缺失
        try:
            missing_map = screening.compute_file_missing_status(
                library_root, persist=False,
            )
        except Exception as exc:
            missing_map = {}
            try:
                self._screening_log(
                    "warning", f"文件缺失预扫失败：{exc}"
                )
            except Exception:
                pass
        missing_count = 0
        try:
            records_for_missing = pd.read_parquet(
                library_root / "records_final.parquet",
            )
            if "strategy_names" in records_for_missing.columns:
                pattern = rf"(?:^|;)\s*{re.escape(strategy)}\s*(?:;|$)"
                mask_missing = (
                    records_for_missing["strategy_names"]
                    .fillna("").astype(str)
                    .str.contains(pattern, regex=True, na=False)
                )
                cids_for_missing = (
                    records_for_missing.loc[mask_missing, "canonical_id"]
                    .astype(str)
                )
                missing_count = sum(
                    1 for cid in cids_for_missing
                    if missing_map.get(cid)
                )
        except Exception:
            pass

        # 3. 估算空间
        try:
            estimate = screening.estimate_archive_space_release(
                strategy, library_root, config,
            )
        except Exception:
            estimate = {"freed_gb": 0.0, "cid_count": 0}

        from PySide6.QtWidgets import QMessageBox
        _retention_mode = (config.get("screening", {}) or {}).get(
            "retention_mode", "release_space"
        )
        _exclude_desc = (
            "library/excluded/（PDF/HTML 进 trash）"
            if _retention_mode == "release_space"
            else "library/excluded/（全部文件保留）"
        )
        msg = (
            f"即将归档策略 {strategy}：\n\n"
            f"决策统计：\n"
            f"  · 纳入 {int(stats.get('include', 0))} 篇 → library/included/\n"
            f"  · 排除 {int(stats.get('exclude', 0))} 篇 → {_exclude_desc}\n"
            f"  · 待定 {int(stats.get('maybe', 0))} 篇 → library/maybe/\n\n"
        )
        if missing_count > 0:
            msg += (
                f"⚠️ 文件补足：\n"
                f"  · 需补足 {missing_count} 篇（从回收站恢复或重新下载）\n\n"
            )
        msg += (
            f"预计空间释放：{float(estimate.get('freed_gb', 0.0)):.2f} GB\n\n"
            f"[确认归档]  [取消]"
        )
        reply = QMessageBox.question(
            self._mw, "确认归档", msg,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        # 4. 执行归档
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtWidgets import QApplication as _QApp
        from medlitflow import robustness
        robustness.reset_shutdown()
        _QApp.setOverrideCursor(_Qt.WaitCursor)
        try:
            self._mw._notify_status(
                "info", f"正在归档策略 {strategy}，请稍候...",
                source="screening",
            )
            result = screening.apply_screening(
                strategy, library_root, config
            )
        except Exception as exc:
            self._mw._notify_status(
                "error", f"归档失败：{exc}", source="screening"
            )
            return
        finally:
            _QApp.restoreOverrideCursor()

        archive_errors = list(result.get("errors", []) or [])
        if archive_errors:
            err_text = "; ".join(str(e) for e in archive_errors)
            self._mw._notify_status(
                "error",
                f"归档失败（文件索引或写盘异常）：{err_text}",
                source="screening",
            )
            try:
                QMessageBox.warning(
                    self._mw, "归档失败",
                    f"归档过程出现错误：\n{err_text}\n\n"
                    f"数据可能未完全写入，请检查 library 后再试。",
                )
            except Exception:
                pass
            self._library_ctrl.load_library_records(force=True)
            self._library_ctrl._refresh_literature()
            self._mw._refresh_dashboard()
            self.refresh_screening()
            return

        # 5. 结果弹窗
        moved = int(result.get("moved", 0) or 0)
        skipped = int(result.get("skipped", 0) or 0)
        restored = int(result.get("restored", 0) or 0)
        redownloaded = int(result.get("redownloaded", 0) or 0)
        missing = int(result.get("missing", 0) or 0)
        space_freed = int(result.get("space_freed_bytes", 0) or 0)

        done_msg = (
            f"归档完成：策略 {strategy}\n\n"
            f"  · 移动 {moved} 篇\n"
            f"  · 跳过 {skipped} 篇（已就位）\n"
            f"  · 从回收站恢复 {restored} 篇\n"
            f"  · 重新下载 {redownloaded} 篇\n"
            f"  · 无法补足 {missing} 篇\n"
            f"  · 释放空间 {space_freed / 1024 / 1024:.2f} MB"
        )
        QMessageBox.information(self._mw, "归档完成", done_msg)
        self._mw._notify_status(
            "info", f"归档完成：{strategy}", source="screening"
        )

        # 6. 刷新
        self._mw.screening_view.set_current_strategy(strategy)
        self._mw.screening_view.set_user_selected(True)
        self._library_ctrl.load_library_records(force=True)
        self._library_ctrl._refresh_literature()
        self._mw._refresh_dashboard()
        self.refresh_screening()
        self._strategy_ctrl.refresh_completed_strategies()
        self._library_ctrl._refresh_literature_tabs()

    def on_screening_export_report(self, strategy, path) -> None:
        if not strategy:
            return
        try:
            from medlitflow import screening
            config = self._config_ctrl.get_config()
            library_root = Path(config["library"]["root"])
        except Exception as exc:
            self._mw._notify_status(
                "error", f"配置加载失败：{exc}", source="screening"
            )
            return

        output_dir = Path(path) if path else None
        try:
            result = screening.export_screening_report(
                strategy, library_root, output_dir, config,
            )
        except Exception as exc:
            self._mw._notify_status(
                "error", f"导出失败：{exc}", source="screening"
            )
            return

        if result.get("ok"):
            out = result.get("output_dir", "")
            self._mw._notify_status(
                "info", f"报告已导出：{out}", source="screening"
            )
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(
                self._mw, "报告已导出",
                f"输出目录：{out}\n\n"
                f"文件：prisma_report.md / included.csv / "
                f"excluded.csv / all_decisions.csv\n\n"
                f"提示：同一策略始终只保留最新一份报告；"
                f"历史日期的旧报告目录已在本次导出时清理。",
            )
        else:
            self._mw._notify_status(
                "warning",
                f"导出失败：{result.get('reason', '未知原因')}",
                source="screening",
            )

    def on_screening_row_selected(self, cid) -> None:
        if not cid:
            return
        cid_str = str(cid)

        model_row = None
        try:
            table = self._mw.screening_view.document_table
            model = table._model
            model_row = model.get_row_by_cid(cid_str)
        except Exception:
            model_row = None

        records = self._library_ctrl._library_records
        row_data = None
        if records is not None and len(records) > 0:
            try:
                mask = records["canonical_id"].astype(str) == cid_str
                if mask.any():
                    row_data = records[mask].iloc[0].to_dict()
            except Exception:
                row_data = None
        if row_data is None and model_row is None:
            return
        row_data = row_data or {}

        decision = (
            str(model_row.get("decision", "") or "").strip().lower()
            if model_row is not None else ""
        )
        if decision and decision != "unscreened":
            status = self._library_ctrl._map_detail_status(
                {"user_decision": decision}
            )
        else:
            status = self._library_ctrl._map_detail_status(row_data)
        status_text = self._library_ctrl._map_detail_status_text(
            row_data, override_decision=decision,
        )

        from ui.utils.formatting import first_source
        detail = {
            "source":   first_source(row_data.get("source_databases", "")),
            "title":    str(row_data.get("title", "") or ""),
            "authors":  str(row_data.get("authors", "") or ""),
            "journal":  str(row_data.get("journal", "") or ""),
            "year":     str(row_data.get("year", "") or ""),
            "doi":      str(row_data.get("doi", "") or ""),
            "pmid":     str(row_data.get("pmid", "") or ""),
            "status":      status,
            "status_text": status_text,
            "abstract":    str(row_data.get("abstract_final", "") or ""),
            "canonical_id":         cid_str,
            "current_file_path":    str(row_data.get("current_file_path", "") or ""),
            "downloaded_file_path": str(row_data.get("downloaded_file_path", "") or ""),
            "fulltext_url":         str(row_data.get("fulltext_url", "") or ""),
            "landing_page_url":     str(row_data.get("landing_page_url", "") or ""),
            "citation_count":             row_data.get("citation_count"),
            "reference_count":            row_data.get("reference_count"),
            "influential_citation_count": row_data.get("influential_citation_count"),
            "journal_impact_factor":      row_data.get("journal_impact_factor"),
            "journal_jcr_quartile":       row_data.get("journal_jcr_quartile"),
            "author_count":               row_data.get("author_count"),
            "institution_count":          row_data.get("institution_count"),
            "country_codes":              row_data.get("country_codes"),
            "publication_type_category":  row_data.get("publication_type_category"),
            "open_access_flag":           row_data.get("open_access_flag", False),
            "file_locked_by_strategy": (
                str(model_row.get("file_locked_by_strategy", "") or "")
                if model_row is not None else ""
            ),
            "file_locked_decision": (
                str(model_row.get("file_locked_decision", "") or "")
                if model_row is not None else ""
            ),
            "file_missing": (
                bool(model_row.get("file_missing", False))
                if model_row is not None else False
            ),
        }
        try:
            self._mw.screening_view.show_detail_panel(detail)
        except Exception:
            pass

    def on_screening_open_pdf(self, cid) -> None:
        if not cid:
            return
        try:
            from medlitflow import screening
            config = self._config_ctrl.get_config()
            library_root = Path(config["library"]["root"])
        except Exception as exc:
            self._mw._notify_status(
                "error", f"配置加载失败：{exc}", source="screening"
            )
            return
        try:
            opened = screening.open_literature_file(cid, library_root)
            if opened:
                self._mw._notify_status(
                    "info", "已打开文献文件", source="screening"
                )
            else:
                self._mw._notify_status(
                    "warning", "未找到可打开的文献文件", source="screening"
                )
        except Exception as exc:
            self._mw._notify_status(
                "warning", f"打开文件失败：{exc}", source="screening"
            )

    def on_screening_detail_action(self, action, data) -> None:
        if not isinstance(data, dict):
            return
        if action == "open":
            self._library_ctrl._detail_action_open(data)
        elif action == "copy_doi":
            self._library_ctrl._detail_action_copy_doi(data)
        elif action == "open_pdf":
            self._library_ctrl._detail_action_open_pdf(data)

    def on_screening_reopen_strategy(self, strategy) -> None:
        if not strategy:
            return

        try:
            from medlitflow import screening
            config = self._config_ctrl.get_config()
            library_root = Path(config["library"]["root"])
        except Exception as exc:
            self._mw._notify_status(
                "error", f"配置加载失败：{exc}", source="screening"
            )
            return

        current_status = self._strategy_ctrl._get_strategy_status(strategy) or ""
        if current_status == "confirmed":
            prompt = (
                f"策略 {strategy} 已确认归档。\n\n"
                f"继续修改后需重新确认归档。\n"
                f"文件位置保持不变，仅状态改为「部分筛选」。\n\n"
                f"确认重开？"
            )
        elif current_status == "archived_unprocessed":
            prompt = (
                f"策略 {strategy} 已「归档为未处理」。\n\n"
                f"重开后状态将根据当前决策完整度重新计算：\n"
                f"  · 全部决策完整 → 已确认\n"
                f"  · 部分决策      → 部分筛选\n"
                f"  · 无任何决策    → 未筛选\n\n"
                f"文献、文件位置、决策记录均保持不变。\n\n"
                f"确认重开？"
            )
        else:
            self._mw._notify_status(
                "warning",
                f"策略 {strategy} 当前状态 {current_status or '未知'} 不支持重开",
                source="screening",
            )
            return

        from PySide6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self._mw,
            "重开策略",
            prompt,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            result = screening.reopen_strategy(
                strategy, library_root, config=config,
            )
        except Exception as exc:
            self._mw._notify_status(
                "error", f"重开失败：{exc}", source="screening"
            )
            return

        if result.get("ok"):
            self._mw._notify_status(
                "info",
                f"策略 {strategy} 已重开："
                f"{result.get('from')} → {result.get('to')}",
                source="screening",
            )
            self._library_ctrl.load_library_records(force=True)
            self.refresh_screening()
            self._strategy_ctrl.refresh_completed_strategies()
        else:
            self._mw._notify_status(
                "warning",
                f"重开失败：{result.get('reason', '未知')}",
                source="screening",
            )

    def _on_stats_debounce_timeout(self) -> None:
        strategies = set(self._stats_pending_strategies)
        self._stats_pending_strategies.clear()
        if not strategies:
            return

        try:
            from medlitflow import screening as _screening
            config = self._config_ctrl.get_config()
            library_root = Path(config["library"]["root"])
        except Exception:
            library_root = None

        if library_root is not None:
            for strategy in list(strategies):
                decisions = self._pending_decisions_by_strategy.pop(
                    strategy, {}
                )
                if not decisions:
                    continue
                try:
                    _screening.apply_decisions_incremental(
                        strategy, decisions, library_root,
                    )
                except Exception as exc:
                    self._pending_decisions_by_strategy.setdefault(
                        strategy, {}
                    ).update(decisions)
                    try:
                        self._mw.logs_view.append_log(
                            "error",
                            f"决策落盘失败（策略 {strategy}）："
                            f"{type(exc).__name__}: {exc}",
                            source="screening",
                        )
                    except Exception:
                        pass

        current = self._mw.screening_view.current_strategy()
        if current and current in strategies:
            self._update_screening_stats_from_table(current)

    def _read_strategy_registry_once(self):
        try:
            from medlitflow import archive
            config = self._config_ctrl.get_config()
            archive._set_config(config)
            try:
                archive_root = archive._resolve_archive_root(config)
            finally:
                archive._set_config(None)
            registry_path = archive_root / "strategy_registry.parquet"
            if not registry_path.exists():
                return None, {}
            df = pd.read_parquet(registry_path)
            if len(df) == 0:
                return None, {}
            names = df["strategy_name"].astype(str)
            alive = set(names.tolist())
            status_map = dict(zip(
                names,
                df["strategy_status"].astype(str),
            ))
            return alive, status_map
        except Exception:
            return None, {}

    def _read_strategy_statuses(self) -> dict:
        try:
            from medlitflow import archive
            config = self._config_ctrl.get_config()
            archive._set_config(config)
            try:
                archive_root = archive._resolve_archive_root(config)
            finally:
                archive._set_config(None)
            registry_path = archive_root / "strategy_registry.parquet"
            if not registry_path.exists():
                return {}
            df = pd.read_parquet(registry_path)
            if len(df) == 0:
                return {}
            return dict(zip(
                df["strategy_name"].astype(str),
                df["strategy_status"].astype(str),
            ))
        except Exception:
            return {}

    def _refresh_storage_stats(self) -> None:
        if self._storage_stats_timer is not None:
            self._storage_stats_timer.start()
        else:
            self._refresh_storage_stats_impl()

    def _refresh_storage_stats_impl(self) -> None:
        try:
            from medlitflow import screening
            config = self._config_ctrl.get_config()
            library_root_raw = (config.get("library", {}) or {}).get(
                "root", ""
            )
            if not library_root_raw:
                return
            storage = screening.compute_storage_stats(
                Path(library_root_raw)
            )
            self._mw.screening_view.progress_bar.set_storage(storage)
        except Exception:
            pass

    def _refresh_screening_table(self, strategy_name) -> None:
        if not strategy_name:
            try:
                self._mw.screening_view.set_screening_rows([])
            except Exception:
                pass
            return
        try:
            from medlitflow import screening
            config = self._config_ctrl.get_config()
            library_root = Path(config["library"]["root"])
            rows = screening.load_screening_rows(strategy_name, library_root)
        except Exception as exc:
            self._screening_log(
                "warning", f"加载筛选表格失败：{exc}"
            )
            try:
                self._mw.screening_view.set_screening_rows([])
            except Exception:
                pass
            return
        try:
            self._mw.screening_view.set_screening_rows(rows)
        except Exception:
            pass

    def _update_screening_stats_from_table(self, strategy_name) -> None:
        if not strategy_name:
            self._mw.screening_view.set_stats({
                "unscreened": 0, "include": 0, "maybe": 0,
                "excluded": 0, "total": 0,
            })
            return
        try:
            table = self._mw.screening_view.document_table
            model = table._model
            counts = {
                "include": 0, "exclude": 0, "maybe": 0, "unscreened": 0,
            }
            rows = model.get_rows()
            for r in rows:
                d = str(
                    r.get("decision", "unscreened") or "unscreened"
                ).lower()
                if d in counts:
                    counts[d] += 1
                else:
                    counts["unscreened"] += 1
            self._mw.screening_view.set_stats({
                "include":    counts["include"],
                "excluded":   counts["exclude"],
                "maybe":      counts["maybe"],
                "unscreened": counts["unscreened"],
                "total":      len(rows),
            })
        except Exception:
            pass

    def _screening_log(self, level, message) -> None:
        try:
            self._mw.screening_view.append_log(str(level), str(message))
        except Exception:
            pass