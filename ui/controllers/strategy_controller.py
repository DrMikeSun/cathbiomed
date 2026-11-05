"""ui/controllers/strategy_controller.py — 策略控制器（A3）。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
from PySide6.QtCore import QObject

from ui.config import PAGE_SCREENING, PAGE_SEARCH
from ui.utils.formatting import safe_int

if TYPE_CHECKING:
    from ui.views.main_window import MedLitFlowMainWindow
    from ui.controllers.config_controller import ConfigController
    from ui.controllers.library_controller import LibraryController

class StrategyController(QObject):
    def __init__(
        self,
        main_window: "MedLitFlowMainWindow",
        config_ctrl: "ConfigController",
        library_ctrl: "LibraryController",
    ):
        super().__init__(main_window)
        self._mw = main_window
        self._config_ctrl = config_ctrl
        self._library_ctrl = library_ctrl
        self._metrics_worker = None
        self._current_metrics_strategy = ""
        self._strategy_delete_worker = None
        self._completed_strategies_cache: list | None = None
        self._completed_strategies_mtime: tuple | None = None
        self._strategy_records_cache: dict = {}
        self._strategy_records_cache_mtime = None
        self._strategy_cids_map_cache: dict | None = None
        self._strategy_cids_map_mtime = None

    def has_running_worker(self) -> bool:
        for w in (self._metrics_worker, self._strategy_delete_worker):
            if w is not None:
                try:
                    if w.isRunning():
                        return True
                except RuntimeError:
                    pass
        return False

    def refresh_completed_strategies(self) -> None:
        self._refresh_completed_strategies()

    def launch_cleanup_orphans_worker(self, selected, config, library_root):
        from ui.bridge.worker import StrategyDeleteWorker
        worker = StrategyDeleteWorker(
            action="cleanup_orphans",
            payload={"strategies": selected},
            config=config,
            library_root=library_root,
            parent=self._mw,
        )
        worker.finished_ok.connect(self._on_cleanup_orphans_worker_done)
        worker.failed.connect(self._on_cleanup_orphans_worker_failed)
        worker.interrupted.connect(self._on_cleanup_orphans_worker_interrupted)
        self._strategy_delete_worker = worker
        self._mw.notify_user(
            "info",
            f"正在清理 {len(selected)} 个孤儿策略，请稍候...",
        )
        self._mw._spawn_worker(worker)

    def _get_strategy_status(self, strategy_name):
        if not strategy_name:
            return ""
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
                return ""
            registry = pd.read_parquet(registry_path)
            if "strategy_name" not in registry.columns:
                return ""
            if "strategy_status" not in registry.columns:
                return ""
            mask = registry["strategy_name"].astype(str) == str(strategy_name)
            if not mask.any():
                return ""
            return str(registry.loc[mask, "strategy_status"].iloc[0])
        except Exception:
            return ""

    @staticmethod
    def _status_to_chinese(status):
        return {
            "collecting":           "采集中",
            "unscreened":           "未筛选",
            "partially_screened":   "部分筛选",
            "confirmed":            "已确认",
            "archived_unprocessed": "已归档",
        }.get(str(status or ""), "未知")

    def _get_strategy_records(self, strategy_name):
        if not strategy_name:
            return None
        records = self._library_ctrl.load_library_records()
        if records is None or len(records) == 0:
            return None
        if "strategy_names" not in records.columns:
            return None
        current_mtime = self._library_ctrl._library_records_mtime
        if self._strategy_records_cache_mtime != current_mtime:
            self._strategy_records_cache.clear()
            self._strategy_records_cache_mtime = current_mtime
        if strategy_name in self._strategy_records_cache:
            return self._strategy_records_cache[strategy_name]
        try:
            pattern = rf"(?:^|;)\s*{re.escape(strategy_name)}\s*(?:;|$)"
            mask = (
                records["strategy_names"].fillna("").astype(str)
                .str.contains(pattern, regex=True, na=False)
            )
            sub = records[mask]
            result = sub if len(sub) > 0 else None
            self._strategy_records_cache[strategy_name] = result
            return result
        except Exception:
            self._strategy_records_cache[strategy_name] = None
            return None

    def _get_strategy_cids_for_confirmed(self, strategy_name):
        sub = self._get_strategy_records(strategy_name)
        if sub is None:
            return []
        if "user_decision" not in sub.columns:
            return []
        try:
            decision = (
                sub["user_decision"].fillna("").astype(str)
                .str.strip().str.lower()
            )
            include_maybe = sub[decision.isin(["include", "maybe"])]
            return include_maybe["canonical_id"].astype(str).tolist()
        except Exception:
            return []

    def _get_strategy_cids_for_all(self, strategy_name):
        sub = self._get_strategy_records(strategy_name)
        if sub is None:
            return []
        if "user_decision" not in sub.columns:
            return sub["canonical_id"].astype(str).tolist()
        try:
            decision = (
                sub["user_decision"].fillna("").astype(str)
                .str.strip().str.lower()
            )
            not_excluded = ~decision.isin(["exclude"])
            return sub[not_excluded]["canonical_id"].astype(str).tolist()
        except Exception:
            return []

    @staticmethod
    def _build_strategy_cids_map(records):
        result: dict[str, set] = {}
        if records is None or len(records) == 0:
            return result
        if "strategy_names" not in records.columns:
            return result
        try:
            s = records["strategy_names"].fillna("").astype(str)
            cids = records["canonical_id"].astype(str)
            counts = s.str.count(";") + 1
            exploded_strategy = s.str.split(";").explode().str.strip()
            exploded_cid = cids.repeat(counts).reset_index(drop=True)
            n = min(len(exploded_strategy), len(exploded_cid))
            exploded_strategy = exploded_strategy.iloc[:n].reset_index(drop=True)
            exploded_cid = exploded_cid.iloc[:n]
            mask = exploded_strategy.ne("") & exploded_cid.ne("")
            exploded_strategy = exploded_strategy[mask]
            exploded_cid = exploded_cid[mask]
            if len(exploded_strategy) == 0:
                return result
            tmp = pd.DataFrame({
                "strategy": exploded_strategy.values,
                "cid": exploded_cid.values,
            })
            grouped = tmp.groupby("strategy")["cid"].agg(set)
            for name, cid_set in grouped.items():
                result[str(name)] = set(cid_set)
        except Exception:
            return result
        return result

    @staticmethod
    def _count_decision(records, cids, decision):
        if records is None or len(records) == 0 or not cids:
            return 0
        if "user_decision" not in records.columns:
            return 0
        try:
            mask = (
                records["canonical_id"].astype(str).isin(cids)
                & (
                    records["user_decision"]
                    .fillna("").astype(str)
                    .str.strip().str.lower()
                    == decision
                )
            )
            return int(mask.sum())
        except Exception:
            return 0

    @staticmethod
    def _compute_metric_coverage(records, cids):
        if records is None or len(records) == 0 or not cids:
            return "—"
        try:
            sub = records[
                records["canonical_id"].astype(str).isin(cids)
            ]
            if len(sub) == 0:
                return "—"
            c = int(sub["citation_count"].notna().sum()) \
                if "citation_count" in sub.columns else 0
            j = int(sub["journal_impact_factor"].notna().sum()) \
                if "journal_impact_factor" in sub.columns else 0
            i = int(
                (pd.to_numeric(
                    sub["institution_count"], errors="coerce"
                ).fillna(0) > 0).sum()
            ) if "institution_count" in sub.columns else 0
            if c == 0 and j == 0 and i == 0:
                return "—"
            return f"{c}/{j}/{i}"
        except Exception:
            return "—"

    @staticmethod
    def _count_strategy_runs(archive_root, strategy_name):
        runs_root = (
            Path(archive_root) / "strategies" / strategy_name / "runs"
        )
        if not runs_root.exists():
            return 0
        try:
            count = 0
            for date_dir in runs_root.iterdir():
                if not date_dir.is_dir():
                    continue
                for rdir in date_dir.iterdir():
                    if rdir.is_dir():
                        count += 1
            return count
        except Exception:
            return 0

    @staticmethod
    def _latest_strategy_run_date(archive_root, strategy_name):
        runs_root = (
            Path(archive_root) / "strategies" / strategy_name / "runs"
        )
        if not runs_root.exists():
            return ""
        try:
            dates = [d.name for d in runs_root.iterdir() if d.is_dir()]
            return max(dates) if dates else ""
        except Exception:
            return ""

    def _refresh_completed_strategies(self):
        import os
        try:
            from medlitflow import archive
            config = self._config_ctrl.get_config()
            archive._set_config(config)
            try:
                archive_root = archive._resolve_archive_root(config)
            finally:
                archive._set_config(None)
            library_root = Path(config["library"]["root"])
        except Exception:
            try:
                self._mw.tasks_view.set_completed_strategies([])
            except Exception:
                pass
            return
        registry_path = archive_root / "strategy_registry.parquet"
        records_path = library_root / "records_final.parquet"
        decisions_path = library_root / "screening_decisions.parquet"

        def _safe_mtime(p):
            try:
                return os.path.getmtime(p) if p.exists() else None
            except OSError:
                return None

        current_mtimes = (
            _safe_mtime(records_path),
            _safe_mtime(decisions_path),
            _safe_mtime(registry_path),
        )
        if (self._completed_strategies_cache is not None
                and self._completed_strategies_mtime == current_mtimes):
            items = self._completed_strategies_cache
        else:
            try:
                items = self._collect_completed_strategies()
            except Exception:
                items = []
            self._completed_strategies_cache = items
            self._completed_strategies_mtime = current_mtimes
        try:
            self._mw.tasks_view.set_completed_strategies(items)
        except Exception:
            pass

    def _collect_completed_strategies(self):
        try:
            from medlitflow import archive
            config = self._config_ctrl.get_config()
            archive._set_config(config)
            try:
                archive_root = archive._resolve_archive_root(config)
            finally:
                archive._set_config(None)
        except Exception as exc:
            try:
                self._mw.logs_view.append_log(
                    "warning",
                    f"已完成策略加载失败（archive config）：{exc}",
                    source="tasks",
                )
            except Exception:
                pass
            return []
        registry_path = archive_root / "strategy_registry.parquet"
        if not registry_path.exists():
            return []
        try:
            registry = pd.read_parquet(registry_path)
        except Exception as exc:
            try:
                self._mw.logs_view.append_log(
                    "error",
                    f"strategy_registry 读取失败：{exc}",
                    source="tasks",
                )
            except Exception:
                pass
            return []
        if len(registry) == 0:
            return []
        records = self._library_ctrl.load_library_records()
        current_records_mtime = (
            self._library_ctrl._library_records_mtime[0]
            if self._library_ctrl._library_records_mtime
            else None
        )
        if (
            self._strategy_cids_map_cache is not None
            and self._strategy_cids_map_mtime == current_records_mtime
        ):
            strategy_cids = self._strategy_cids_map_cache
        else:
            strategy_cids = self._build_strategy_cids_map(records)
            self._strategy_cids_map_cache = strategy_cids
            self._strategy_cids_map_mtime = current_records_mtime
        items: list[dict] = []
        for _, row in registry.iterrows():
            sname = str(row.get("strategy_name", "") or "")
            if not sname:
                continue
            cids = strategy_cids.get(sname, set())
            items.append({
                "strategy_name": sname,
                "strategy_status": str(row.get("strategy_status", "") or ""),
                "record_count": len(cids),
                "run_count": self._count_strategy_runs(archive_root, sname),
                "metric_coverage": self._compute_metric_coverage(records, cids),
                "last_run_at": self._latest_strategy_run_date(archive_root, sname),
                "unscreened_count": safe_int(row.get("unscreened_count", 0)),
                "include_count": self._count_decision(records, cids, "include"),
                "maybe_count": safe_int(row.get("maybe_count", 0)),
            })
        return items

    def on_strategy_refresh(self, strategy_name):
        from PySide6.QtWidgets import QMessageBox
        if not strategy_name:
            return
        if self._mw._has_running_worker():
            self._mw.notify_user("warning", "已有任务运行中，请等待完成")
            return
        status = self._get_strategy_status(strategy_name)
        status_cn = self._status_to_chinese(status)
        if status == "confirmed":
            target_cids = self._get_strategy_cids_for_confirmed(strategy_name)
            if not target_cids:
                QMessageBox.information(
                    self._mw, "更新指标",
                    f"策略 {strategy_name} 已确认，但无 include/maybe 文献。\n\n"
                    f"如需补全全部文献，请在筛选中心调整决策后重试。",
                )
                return
        else:
            box = QMessageBox(self._mw)
            box.setWindowTitle("更新指标")
            box.setIcon(QMessageBox.Question)
            box.setText(
                f"策略 {strategy_name} 当前状态：{status_cn}。\n\n"
                f"更新指标可补全文献的引用数、影响因子、机构等信息，"
                f"有助于筛选决策。\n\n"
                f"建议先筛选再更新（更精准，更省时间）。"
            )
            btn_screening = box.addButton("先筛选", QMessageBox.YesRole)
            btn_metrics = box.addButton("先更新指标", QMessageBox.NoRole)
            box.setDefaultButton(btn_screening)
            box.exec()
            clicked = box.clickedButton()
            if clicked == btn_screening:
                self._mw.notify_user(
                    "info",
                    f"策略 {strategy_name}：用户选择先筛选，已跳转",
                )
                self._mw.goto_page(PAGE_SCREENING)
                return
            if clicked != btn_metrics:
                return
            self._mw.notify_user(
                "info",
                f"策略 {strategy_name}：用户选择先更新指标（全部文献）",
            )
            target_cids = self._get_strategy_cids_for_all(strategy_name)
        if not target_cids:
            self._mw.notify_user(
                "warning",
                f"策略 {strategy_name} 暂无可更新的文献",
            )
            return
        self._launch_metrics_task(strategy_name, target_cids)

    def on_strategy_report(self, strategy_name):
        if not strategy_name:
            return
        report = self._resolve_latest_summary_report(strategy_name)
        if report is None:
            self._mw.notify_user(
                "warning",
                f"策略 {strategy_name} 无可用报告"
                f"（无 run 或 summary_report.md 缺失）",
            )
            return
        try:
            from medlitflow import core
            opened = core.open_file_with_default_app(report)
            if opened:
                self._mw.notify_user(
                    "info",
                    f"已打开报告：{report.name}"
                    f"（run 目录：{report.parent.name}）",
                )
            else:
                self._mw.notify_user("warning", f"打开失败：{report}")
        except Exception as exc:
            self._mw.notify_user("error", f"打开报告失败：{exc}")

    def on_strategy_screening(self, strategy_name):
        if not strategy_name:
            return
        self._mw.goto_page(PAGE_SCREENING)
        self._mw.screening_view.set_user_selected(True)
        self._mw.screening_view.set_current_strategy(strategy_name)
        self._mw._screening_ctrl._refresh_screening_table(strategy_name)
        self._mw._screening_ctrl._update_screening_stats_from_table(strategy_name)
        self._mw.notify_user(
            "info",
            f"已跳转到筛选中心；已定位策略 {strategy_name}",
        )

    def on_strategy_consolidate(self, strategy_name):
        if not strategy_name:
            return
        from PySide6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self._mw, "确认合并",
            f"即将合并策略 {strategy_name} 的所有 run。\n\n"
            f"此操作会覆盖 consolidated/ 下的现有文件，"
            f"不可恢复。\n\n"
            f"确认继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            from medlitflow import archive
            config = self._config_ctrl.get_config()
            archive._set_config(config)
            try:
                result = archive.consolidate(strategy_name)
            finally:
                archive._set_config(None)
            status = result.get("status", "")
            rows = result.get("rows_total", 0)
            self._mw.notify_user(
                "info",
                f"合并完成：status={status}, rows={rows}",
            )
            self.refresh_completed_strategies()
        except Exception as exc:
            self._mw.notify_user("error", f"合并失败：{exc}")

    def on_strategy_preview(self, strategy_name):
        if not strategy_name:
            return
        try:
            from medlitflow import archive
            config = self._config_ctrl.get_config()
            archive._set_config(config)
            try:
                path = archive.preview_archive(strategy_name)
            finally:
                archive._set_config(None)
            self._mw.notify_user("info", f"预览已生成：{path}")
        except Exception as exc:
            self._mw.notify_user("error", f"预览失败：{exc}")

    def on_strategy_archive(self, strategy_name):
        if not strategy_name:
            return
        from PySide6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self._mw, "确认归档",
            f"即将把策略 {strategy_name} 归档为未处理。\n\n"
            f"归档后策略状态变为 archived_unprocessed，\n"
            f"不会再参与 enrich / 筛选流程。\n\n"
            f"确认继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            from medlitflow import archive
            config = self._config_ctrl.get_config()
            archive._set_config(config)
            try:
                archive.archive_strategy_as_unprocessed(strategy_name)
            finally:
                archive._set_config(None)
            self._mw.notify_user("info", f"策略 {strategy_name} 已归档")
            self.refresh_completed_strategies()
        except Exception as exc:
            self._mw.notify_user("error", f"归档失败：{exc}")

    def on_strategy_delete(self, strategy_name):
        if not strategy_name:
            return
        if self._mw._has_running_worker():
            self._mw.notify_user("warning", "有任务运行中，请等待完成")
            return
        if any(c in strategy_name for c in ("/", "\\", "..")):
            self._mw.notify_user("error", f"策略名非法：{strategy_name}")
            return
        try:
            from medlitflow import archive, screening as _screening
            config = self._config_ctrl.get_config()
            library_root = Path(config["library"]["root"])
        except Exception as exc:
            self._mw.notify_user("error", f"配置加载失败：{exc}")
            return
        try:
            from medlitflow import robustness as _rb
            _out_root = str(
                (config.get("project", {}) or {}).get("output_root", "") or ""
            )
            if _out_root:
                _lock = Path(_out_root) / ".pipeline.lock"
                if _lock.exists() and not _rb.is_lock_stale(_lock):
                    self._mw.notify_user(
                        "warning",
                        "检测到 pipeline 正在运行，删除操作已拒绝。"
                        "请停止后重试。",
                    )
                    return
        except Exception:
            pass
        try:
            pre = _screening.prescan_delete_strategy(strategy_name, library_root)
        except Exception as exc:
            self._mw.notify_user("error", f"预扫描失败：{exc}")
            return
        if not pre.get("ok"):
            errs = pre.get("errors", [])
            err_text = "; ".join(str(e) for e in errs) if errs else "未知原因"
            self._mw.notify_user("error", f"预扫描失败：{err_text}")
            return
        exclusive_n = int(pre.get("exclusive_count", 0) or 0)
        shared_n = int(pre.get("shared_count", 0) or 0)
        if exclusive_n == 0 and shared_n == 0:
            if not self._show_empty_strategy_confirm_dialog(strategy_name):
                self._mw.notify_user("info", "已取消删除")
                return
            mode = "remove"
        else:
            mode, confirmed = self._show_strategy_delete_mode_dialog(
                strategy_name, exclusive_n, shared_n, pre,
            )
            if not confirmed:
                self._mw.notify_user("info", "已取消删除")
                return
        from PySide6.QtWidgets import QApplication, QMessageBox
        from PySide6.QtCore import Qt
        if mode == "purge":
            from ui.bridge.worker import StrategyDeleteWorker
            worker = StrategyDeleteWorker(
                action="purge",
                payload={
                    "strategy_name": strategy_name,
                    "prescan_result": pre,
                },
                config=config,
                library_root=library_root,
                parent=self._mw,
            )
            worker.finished_ok.connect(
                lambda r, n=strategy_name: self._on_purge_worker_done(n, r)
            )
            worker.failed.connect(self._on_purge_worker_failed)
            worker.interrupted.connect(self._on_purge_worker_interrupted)
            self._strategy_delete_worker = worker
            self._mw.notify_user(
                "info",
                f"正在彻底删除策略 {strategy_name}，请稍候...",
            )
            self._mw._spawn_worker(worker)
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        info_msg = ""
        try:
            self._mw.notify_user(
                "info",
                f"正在移除策略 {strategy_name}...",
            )
            archive._set_config(config)
            try:
                result = archive.delete_strategy(strategy_name)
            finally:
                archive._set_config(None)
            op_status = str(result.get("status", ""))
            if op_status == "error":
                self._mw.notify_user(
                    "error",
                    f"删除失败：{result.get('message', '未知原因')}",
                )
                return
            real_run_count = int(result.get("run_count", 0) or 0)
            real_freed_mb = int(result.get("freed_bytes", 0) or 0) / 1024 / 1024
            info_msg = (
                f"已移除策略 {strategy_name}"
                f"（{real_run_count} 个 run，"
                f"释放 {real_freed_mb:.1f} MB）；"
                f"文献库数据已完整保留"
            )
        except Exception as exc:
            self._mw.notify_user("error", f"删除失败：{exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        self._mw.notify_user("info", info_msg)
        try:
            QMessageBox.information(
                self._mw, "移除完成",
                f"策略 {strategy_name} 已从面板移除。\n\n"
                f"· 文献库中 {exclusive_n} 篇文献**完整保留**"
                f"（含所有字段）\n"
                f"· 文献的「检索策略」列仍显示 {strategy_name}\n"
                f"· 已完成面板和筛选中心不再显示此策略\n"
                f"· archive 快照已删除（下次同策略检索从全新开始）\n\n"
                f"提示：\n"
                f"  · 若之后用相同检索式重跑，这些文献会被复用，"
                f"不会重复下载\n"
                f"  · 如需彻底清空这些文献，请点击文献库"
                f"「清理孤儿文献」按钮",
            )
        except Exception:
            pass
        try:
            self._library_ctrl.load_library_records(force=True)
            self._library_ctrl._refresh_literature()
            self._mw._refresh_dashboard()
            self._mw._screening_ctrl.refresh_screening()
            self.refresh_completed_strategies()
        except Exception:
            pass

    def on_completed_reload(self):
        self.refresh_completed_strategies()
        self._mw.notify_user("info", "已刷新已完成策略列表")

    def on_tasks_tab_changed(self, key):
        try:
            if key == self._mw.tasks_view.TAB_COMPLETED:
                self.refresh_completed_strategies()
        except Exception:
            pass

    def _show_empty_strategy_confirm_dialog(self, strategy_name):
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
        )
        from PySide6.QtCore import Qt
        dlg = QDialog(self._mw)
        dlg.setWindowTitle("删除空策略")
        dlg.setMinimumWidth(480)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(12)
        info = QLabel(
            f"策略 <b>{strategy_name}</b> 在 library 中无任何文献影响"
            f"（独占 0 篇 / 共享 0 篇）。\n\n"
            f"archive 中的 run 快照将一并删除"
            f"（纯空策略无 PRISMA 追溯价值）。\n"
            f"library 数据不受影响。"
        )
        info.setWordWrap(True)
        info.setTextFormat(Qt.RichText)
        v.addWidget(info)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.setMinimumWidth(90)
        cancel_btn.setCursor(Qt.PointingHandCursor)
        confirm_btn = QPushButton("确认删除")
        confirm_btn.setObjectName("PrimaryCTA")
        confirm_btn.setMinimumWidth(110)
        confirm_btn.setCursor(Qt.PointingHandCursor)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(confirm_btn)
        v.addLayout(btn_row)
        cancel_btn.clicked.connect(lambda checked=False: dlg.reject())
        confirm_btn.clicked.connect(lambda checked=False: dlg.accept())
        return dlg.exec() == QDialog.Accepted

    def _show_strategy_delete_mode_dialog(
        self, strategy_name, exclusive_n, shared_n, pre,
    ):
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
            QPushButton, QRadioButton, QButtonGroup, QWidget,
        )
        from PySide6.QtCore import Qt as _Qt
        from ui.theme.palette import Palette
        dlg = QDialog(self._mw)
        dlg.setWindowTitle(f"⚠ 删除策略 {strategy_name}")
        dlg.setMinimumWidth(600)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(24, 18, 24, 18)
        v.setSpacing(12)
        size_mb = int(pre.get("size_bytes", 0) or 0) / 1024 / 1024
        info = QLabel(
            f"策略 <b>{strategy_name}</b> 当前涉及：\n"
            f"  · 独占文献 {exclusive_n} 篇（只属于此策略）\n"
            f"  · 共享文献 {shared_n} 篇（同时属于其他策略）\n"
            f"  · 预计文件大小约 {size_mb:.1f} MB"
        )
        info.setTextFormat(_Qt.RichText)
        info.setWordWrap(True)
        v.addWidget(info)
        v.addWidget(QLabel("请选择删除方式："))
        rb_remove = QRadioButton(
            f"仅移除策略（保留 {exclusive_n} 篇文献在文献库）"
        )
        rb_remove.setChecked(True)
        rb_purge = QRadioButton(
            f"彻底删除（独占 {exclusive_n} 篇文献一并删除，不可恢复）"
        )
        group = QButtonGroup(dlg)
        group.addButton(rb_remove, 0)
        group.addButton(rb_purge, 1)
        v.addWidget(rb_remove)
        v.addWidget(rb_purge)
        desc = QLabel("")
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"color: {Palette.TEXT_SECONDARY};"
            f"font-size: 12px;"
            f"background: rgba(255,255,255,8);"
            f"border-radius: 6px;"
            f"padding: 8px 10px;"
            f"margin-top: 4px;"
        )
        v.addWidget(desc)

        def _update_desc():
            if rb_remove.isChecked():
                desc.setText(
                    "✓ 文献库保留所有文献及字段"
                    "（title/abstract/doi/文件路径等）\n"
                    "✓ 文献的「检索策略」列仍显示此策略名\n"
                    "✓ 已完成面板和筛选中心不再显示此策略\n"
                    "✓ archive 快照删除（下次同策略检索从全新开始）\n"
                    "✓ 重跑相同检索式时，文献会被复用（不重复下载）\n"
                    "✓ 可在文献库「清理孤儿文献」按钮中后续彻底清空"
                )
            else:
                desc.setText(
                    "⚠ 独占文献及其文件将被彻底删除（不可恢复）\n"
                    "· 共享文献保留，从 strategy_names 移除 + 重算位置\n"
                    "· 决策记录删除该策略的行\n"
                    "· archive 快照删除\n"
                    "· 下次同策略检索从零开始（文献重新采集）"
                )
        rb_remove.toggled.connect(lambda c: _update_desc() if c else None)
        rb_purge.toggled.connect(lambda c: _update_desc() if c else None)
        _update_desc()
        input_container = QWidget()
        input_v = QVBoxLayout(input_container)
        input_v.setContentsMargins(0, 8, 0, 0)
        input_v.setSpacing(4)
        input_v.addWidget(QLabel("请输入策略名确认（仅「彻底删除」需要）："))
        name_row = QHBoxLayout()
        name_row.setSpacing(4)
        name_edit = QLineEdit(strategy_name)
        name_edit.setReadOnly(True)
        name_edit.setCursor(_Qt.IBeamCursor)
        name_edit.setStyleSheet(
            f"QLineEdit {{"
            f"  background: rgba(255,255,255,20);"
            f"  border: 1px solid rgba(0,133,255,90);"
            f"  border-radius: 6px;"
            f"  color: {Palette.CYAN};"
            f"  padding: 5px 8px;"
            f"  font-size: 13px;"
            f"  font-family: Consolas, monospace;"
            f"}}"
        )
        name_row.addWidget(name_edit, stretch=1)
        copy_btn = QPushButton("📋 复制")
        copy_btn.setFixedWidth(90)
        copy_btn.setCursor(_Qt.PointingHandCursor)
        copy_btn.setToolTip("一键复制策略名")

        def _on_copy():
            from PySide6.QtWidgets import QApplication as _QApp
            from PySide6.QtCore import QTimer as _QTimer
            try:
                _QApp.clipboard().setText(strategy_name)
                copy_btn.setText("✓ 已复制")
                timer = _QTimer(copy_btn)
                timer.setSingleShot(True)
                timer.timeout.connect(lambda: copy_btn.setText("📋 复制"))
                timer.start(1500)
            except Exception:
                copy_btn.setText("❌ 失败")
        copy_btn.clicked.connect(lambda checked=False: _on_copy())
        name_row.addWidget(copy_btn)
        input_v.addLayout(name_row)
        input_edit = QLineEdit()
        input_edit.setPlaceholderText(f"输入 {strategy_name}")
        input_v.addWidget(input_edit)
        v.addWidget(input_container)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.setMinimumWidth(90)
        cancel_btn.setCursor(_Qt.PointingHandCursor)
        confirm_btn = QPushButton("确认删除")
        confirm_btn.setObjectName("PrimaryCTA")
        confirm_btn.setMinimumWidth(110)
        confirm_btn.setCursor(_Qt.PointingHandCursor)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(confirm_btn)
        v.addLayout(btn_row)

        def _update_state():
            if rb_remove.isChecked():
                input_container.setVisible(False)
                input_edit.clear()
                confirm_btn.setEnabled(True)
            else:
                input_container.setVisible(True)
                confirm_btn.setEnabled(
                    input_edit.text().strip() == strategy_name
                )
        rb_remove.toggled.connect(lambda c: _update_state() if c else None)
        rb_purge.toggled.connect(lambda c: _update_state() if c else None)
        input_edit.textChanged.connect(lambda _: _update_state())
        _update_state()
        cancel_btn.clicked.connect(dlg.reject)
        confirm_btn.clicked.connect(dlg.accept)
        if dlg.exec() != QDialog.Accepted:
            return "", False
        if rb_remove.isChecked():
            return "remove", True
        if input_edit.text().strip() != strategy_name:
            return "", False
        return "purge", True

    def _launch_metrics_task(self, strategy_name, cids):
        if not cids:
            return
        try:
            config = self._config_ctrl.get_config()
        except Exception as exc:
            self._mw.notify_user("error", f"配置加载失败：{exc}")
            return
        try:
            from ui.bridge.worker import EnrichmentMetricsWorker
            worker = EnrichmentMetricsWorker(config, cids, strategy_name)
        except Exception as exc:
            self._mw.notify_user("error", f"Worker 构造失败：{exc}")
            return
        try:
            interactor = worker.get_interactor()
            interactor.notified.connect(self._mw._on_interactor_notified)
            interactor.progressed.connect(self._mw._on_running_progressed)
            interactor.progressed.connect(self._mw._on_interactor_progressed)
            worker.finished_ok.connect(self._on_metrics_worker_finished)
            worker.failed.connect(self._on_metrics_worker_failed)
            worker.interrupted.connect(self._on_metrics_worker_interrupted)
        except Exception as exc:
            self._mw.notify_user("error", f"信号绑定失败：{exc}")
            return
        self._metrics_worker = worker
        self._current_metrics_strategy = strategy_name
        self._mw._logs_progress_stage = ""
        self._mw._logs_progress_milestone = 0
        self._mw.notify_user(
            "info",
            f"开始更新指标：策略 {strategy_name}，共 {len(cids)} 条",
        )
        try:
            self._mw.tasks_view.set_running(
                f"更新指标-{strategy_name}", strategy_name, "准备中",
                auto_switch=True,
            )
        except Exception:
            pass
        try:
            self._mw.tasks_view.completed_panel.set_refresh_state("running")
        except Exception:
            pass
        self._mw.sync_run_state("running")
        try:
            self._mw.search_view.bind_interactor(interactor)
            self._mw.search_view.queue_panel.set_queue([
                {"name": strategy_name, "type": "metrics"},
            ])
            self._mw.search_view.queue_panel.set_title("指标更新队列")
            self._mw.search_view.queue_panel.mark_running(0)
            self._mw.search_view.set_toolbar_visible(True)
        except Exception:
            pass
        try:
            self._mw.goto_page(PAGE_SEARCH)
        except Exception:
            pass
        try:
            self._mw.logs_view.start_new_run(f"metrics-{strategy_name}")
        except Exception:
            pass
        self._mw._spawn_worker(worker)

    def _on_metrics_worker_finished(self, stats):
        from PySide6.QtWidgets import QMessageBox
        self._metrics_worker = None
        strategy = self._current_metrics_strategy or ""
        self._current_metrics_strategy = ""
        try:
            self._mw.tasks_view.clear_running()
        except Exception:
            pass
        try:
            self._mw.tasks_view.completed_panel.set_refresh_state("idle")
        except Exception:
            pass
        self._mw.sync_run_state("finished")
        try:
            self._mw.search_view.unbind_interactor()
        except Exception:
            pass
        if not isinstance(stats, dict):
            stats = {}
        total = int(stats.get("total", 0) or 0)
        increased = int(stats.get("increased", 0) or 0)
        s2_hits = int(stats.get("s2_hits", 0) or 0)
        jcr_hits = int(stats.get("jcr_hits", 0) or 0)
        openalex_hits = int(stats.get("openalex_hits", 0) or 0)
        if increased > 0:
            msg = (
                f"更新完成\n"
                f"· 处理 {total} 条\n"
                f"· 补全 {increased} 条"
            )
        else:
            msg = (
                f"更新完成\n"
                f"· 处理 {total} 条\n"
                f"· 补全 0 条\n\n"
                f"未获得新数据，可能原因：\n"
                f"· 数据源未收录该文献\n"
                f"· API 限流或短暂故障\n"
                f"· 原有数据已完整\n\n"
                f"详见 library/enrich_citation_log.jsonl。"
            )
        if total > 0 and (s2_hits + jcr_hits + openalex_hits) == 0:
            diag: list[str] = []
            if s2_hits == 0:
                diag.append("引用数：S2 未命中")
            if jcr_hits == 0:
                diag.append("影响因子：JCR 表未命中")
            if openalex_hits == 0:
                diag.append("机构数据：S2 作者缺失或 OpenAlex 未命中")
            if diag:
                msg += (
                    "\n\n维度诊断（数据源问题）：\n"
                    + "\n".join(f"  · {d}" for d in diag)
                )
        try:
            QMessageBox.information(self._mw, "更新指标", msg)
        except Exception:
            pass
        self._mw.notify_user("info", msg.split("\n")[0])
        self._library_ctrl.load_library_records(force=True)
        self._library_ctrl._refresh_literature()
        self._mw._refresh_dashboard()
        self.refresh_completed_strategies()
        try:
            self._mw.search_view.queue_panel.clear()
            self._mw.search_view.queue_panel.set_title("运行队列")
            self._mw.search_view.set_toolbar_visible(False)
        except Exception:
            pass
        try:
            self._update_latest_report_metrics(strategy, stats)
        except Exception:
            self._mw.notify_user(
                "warning",
                "报告更新失败（不影响指标数据，详见日志）",
            )

    def _on_metrics_worker_failed(self, error):
        self._metrics_worker = None
        self._current_metrics_strategy = ""
        try:
            self._mw.tasks_view.clear_running()
        except Exception:
            pass
        try:
            self._mw.tasks_view.completed_panel.set_refresh_state("idle")
        except Exception:
            pass
        self._mw.sync_run_state("failed")
        try:
            self._mw.search_view.unbind_interactor()
        except Exception:
            pass
        self._mw.notify_user("error", f"更新指标失败：{error}")
        try:
            self._mw.search_view.queue_panel.clear()
            self._mw.search_view.queue_panel.set_title("运行队列")
            self._mw.search_view.set_toolbar_visible(False)
        except Exception:
            pass

    def _on_metrics_worker_interrupted(self):
        self._metrics_worker = None
        self._current_metrics_strategy = ""
        try:
            self._mw.tasks_view.clear_running()
        except Exception:
            pass
        try:
            self._mw.tasks_view.completed_panel.set_refresh_state("idle")
        except Exception:
            pass
        self._mw.sync_run_state("interrupted")
        try:
            self._mw.search_view.unbind_interactor()
        except Exception:
            pass
        self._mw.notify_user("warning", "更新指标已中断")
        self._library_ctrl.load_library_records(force=True)
        self._library_ctrl._refresh_literature()
        self._mw._refresh_dashboard()
        self.refresh_completed_strategies()
        try:
            self._mw.search_view.queue_panel.clear()
            self._mw.search_view.queue_panel.set_title("运行队列")
            self._mw.search_view.set_toolbar_visible(False)
        except Exception:
            pass

    def _on_purge_worker_done(self, strategy_name, result):
        self._strategy_delete_worker = None
        from PySide6.QtWidgets import QMessageBox
        if not isinstance(result, dict):
            result = {}
        cascade_result = result.get("result", {}) or {}
        if not cascade_result.get("ok"):
            errs = list(cascade_result.get("errors", []) or [])
            err_text = (
                "; ".join(str(e) for e in errs) if errs else "未知原因"
            )
            partial = bool(cascade_result.get("partial", False))
            txn_dir = str(
                cascade_result.get("txn_retained_at", "") or ""
            )
            if partial:
                try:
                    QMessageBox.warning(
                        self._mw, "级联删除部分失败",
                        f"错误：{err_text}\n"
                        f"事务备份：{txn_dir}\n"
                        f"请检查 .txn 目录",
                    )
                except Exception:
                    pass
            self._mw.notify_user("error", f"删除失败：{err_text}")
            try:
                self._library_ctrl.load_library_records(force=True)
                self._library_ctrl._refresh_literature()
                self._mw._screening_ctrl.refresh_screening()
                self.refresh_completed_strategies()
            except Exception:
                pass
            return
        exclusive_deleted = int(cascade_result.get("exclusive_deleted", 0) or 0)
        shared_updated = int(cascade_result.get("shared_updated", 0) or 0)
        freed_mb = int(cascade_result.get("freed_bytes", 0) or 0) / 1024 / 1024
        info_msg = (
            f"已彻底删除策略 {strategy_name}："
            f"独占 {exclusive_deleted} / "
            f"共享 {shared_updated}，"
            f"释放 {freed_mb:.1f} MB"
        )
        self._mw.notify_user("info", info_msg)
        try:
            QMessageBox.information(
                self._mw, "删除完成",
                f"策略 {strategy_name} 已彻底删除。\n\n"
                f"· 独占文献及其文件已删除\n"
                f"· 共享文献保留并重算位置\n"
                f"· archive 快照已删除\n"
                f"· 下次同策略检索从零开始",
            )
        except Exception:
            pass
        try:
            self._library_ctrl.load_library_records(force=True)
            self._library_ctrl._refresh_literature()
            self._mw._refresh_dashboard()
            self._mw._screening_ctrl.refresh_screening()
            self.refresh_completed_strategies()
        except Exception:
            pass

    def _on_purge_worker_failed(self, error):
        self._strategy_delete_worker = None
        self._mw.notify_user("error", f"删除失败：{error}")

    def _on_purge_worker_interrupted(self):
        self._strategy_delete_worker = None
        self._mw.notify_user(
            "warning",
            "彻底删除已取消（删除操作本身不可回滚，请检查 library）",
        )
        try:
            self._library_ctrl.load_library_records(force=True)
            self._library_ctrl._refresh_literature()
            self._mw._screening_ctrl.refresh_screening()
            self.refresh_completed_strategies()
        except Exception:
            pass

    def _on_cleanup_orphans_worker_done(self, result):
        self._strategy_delete_worker = None
        if not isinstance(result, dict):
            result = {}
        ok_count = int(result.get("ok_count", 0) or 0)
        total = int(result.get("total", 0) or 0)
        failed = list(result.get("failed", []) or [])
        if ok_count > 0:
            self._mw.notify_user(
                "info", f"已清理 {ok_count}/{total} 个孤儿策略",
            )
        if failed:
            def _fmt(f):
                name = str(f.get("name", "?") or "?")
                if "errors" in f:
                    return f"{name}: {f['errors']}"
                return f"{name}: {f.get('error', '未知')}"
            self._mw.notify_user(
                "warning",
                f"清理失败 {len(failed)} 个："
                + "; ".join(_fmt(f) for f in failed),
            )
        try:
            self._library_ctrl.load_library_records(force=True)
            self._library_ctrl._refresh_literature()
            self._mw._refresh_dashboard()
            self._mw._screening_ctrl.refresh_screening()
            self.refresh_completed_strategies()
        except Exception:
            pass

    def _on_cleanup_orphans_worker_failed(self, error):
        self._strategy_delete_worker = None
        self._mw.notify_user("error", f"清理失败：{error}")

    def _on_cleanup_orphans_worker_interrupted(self):
        self._strategy_delete_worker = None
        self._mw.notify_user(
            "warning", "孤儿清理已取消（已完成部分不撤销）",
        )
        try:
            self._library_ctrl.load_library_records(force=True)
            self._library_ctrl._refresh_literature()
            self._mw._screening_ctrl.refresh_screening()
            self.refresh_completed_strategies()
        except Exception:
            pass

    def _resolve_latest_summary_report(self, strategy_name):
        if not strategy_name:
            return None
        try:
            from medlitflow import archive
            config = self._config_ctrl.get_config()
            archive._set_config(config)
            try:
                archive_root = archive._resolve_archive_root(config)
                is_iso_date = archive._is_iso_date
            finally:
                archive._set_config(None)
        except Exception:
            return None
        runs_root = archive_root / "strategies" / strategy_name / "runs"
        if not runs_root.exists():
            return None
        try:
            date_dirs = [
                d for d in runs_root.iterdir()
                if d.is_dir() and is_iso_date(d.name)
            ]
            if not date_dirs:
                return None
            date_dirs.sort(reverse=True)
            run_dirs = [d for d in date_dirs[0].iterdir() if d.is_dir()]
            if not run_dirs:
                return None
            run_dirs.sort(reverse=True)
            report = run_dirs[0] / "summary_report.md"
            if not report.exists():
                report = run_dirs[0] / "output" / "summary_report.md"
            return report if report.exists() else None
        except Exception:
            return None

    def _update_latest_report_metrics(self, strategy_name, stats):
        report_path = self._resolve_latest_summary_report(strategy_name)
        if report_path is None:
            return
        sub = self._get_strategy_records(strategy_name)
        if sub is None or len(sub) == 0:
            return
        coverage = {
            "total": int(len(sub)),
            "citation": int(sub["citation_count"].notna().sum())
                if "citation_count" in sub.columns else 0,
            "jcr": int(sub["journal_impact_factor"].notna().sum())
                if "journal_impact_factor" in sub.columns else 0,
            "authors": int(
                (pd.to_numeric(sub["author_count"], errors="coerce")
                 .fillna(0) > 0).sum()
            ) if "author_count" in sub.columns else 0,
            "institutions": int(
                (pd.to_numeric(sub["institution_count"], errors="coerce")
                 .fillna(0) > 0).sum()
            ) if "institution_count" in sub.columns else 0,
            "country_codes": int(
                sub["country_codes"].fillna("").astype(str)
                .str.strip().ne("").sum()
            ) if "country_codes" in sub.columns else 0,
        }
        from medlitflow import annotator
        from medlitflow import core as _core
        annotator.update_report_metrics_section(
            report_path,
            coverage,
            _core.now_iso(),
            int(stats.get("total", 0) or 0),
            int(stats.get("filled_before", 0) or 0),
            int(stats.get("filled_after", 0) or 0),
        )