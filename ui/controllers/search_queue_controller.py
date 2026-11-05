"""ui/controllers/search_queue_controller.py — 检索队列控制器。"""

from __future__ import annotations

import json
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
from PySide6.QtCore import QObject, QTimer

from ui.controllers.search_run_launcher import SearchRunLauncher

if TYPE_CHECKING:
    from ui.views.main_window import MedLitFlowMainWindow
    from ui.controllers.config_controller import ConfigController
    from ui.controllers.library_controller import LibraryController
    from ui.controllers.strategy_controller import StrategyController

_ACTIVE_WINDOW_SECONDS = 200.0

class SearchQueueController(QObject):
    """检索队列 / 执行链 / 未完成 run 扫描 / 临时配置。"""
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

        self._search_queue: list = []
        self._search_queue_total = 0
        self._search_queue_results: list = []
        self._search_current_config = None
        self._running_kind = "search"
        self._ui_tmp_files: list = []
        self._user_cancelled_queue = False
        self._search_worker = None

    def is_busy(self) -> bool:
        if self._search_worker is None:
            return False
        try:
            return self._search_worker.isRunning()
        except RuntimeError:
            self._search_worker = None
            return False

    def cleanup(self) -> None:
        self._search_queue.clear()
        self._search_queue_results.clear()
        self._search_current_config = None
        SearchRunLauncher.cleanup_tmp_files(self._ui_tmp_files)

    def _build_queue(self, queries_raw, pipeline_mode) -> list:
        return SearchRunLauncher.build_queue(queries_raw, pipeline_mode)

    def _refresh_queue_panel(self) -> None:
        items = []
        if self._search_current_config:
            cur = dict(self._search_current_config)
            cur["_display_status"] = "running"
            items.append(cur)
        for cfg in self._search_queue:
            item = dict(cfg)
            item["_display_status"] = str(
                cfg.get("status", "pending") or "pending"
            )
            items.append(item)
        try:
            self._mw.search_view.queue_panel.set_queue(items)
            for i, item in enumerate(items):
                st = item.get("_display_status", "pending")
                if st == "running":
                    self._mw.search_view.queue_panel.mark_running(i)
                elif st == "interrupted":
                    self._mw.search_view.queue_panel.mark_done(i, "interrupted")
        except Exception:
            pass

    def _get_queue_display_items(self) -> list:
        items = []
        if self._search_current_config:
            items.append(self._search_current_config)
        items.extend(self._search_queue)
        return items

    def _is_selected_running(self, selected_name) -> bool:
        if not selected_name:
            return False
        if self._search_current_config is None:
            return False
        return str(
            self._search_current_config.get("name", "") or ""
        ) == selected_name

    def on_search_run_requested(self, run_config) -> None:
        queries_raw = str(run_config.get("queries_raw", "") or "")
        pipeline_mode = str(
            run_config.get("pipeline_mode", "intensive") or "intensive"
        )
        queries = SearchRunLauncher.parse_search_queries(queries_raw)
        if not queries:
            self._report_search_error("请输入至少一个检索式。")
            return
        if self._mw._has_running_worker():
            try:
                self._mw.search_view.log_panel.append(
                    "warning", "已有运行中的任务，请先取消或等待完成",
                )
            except Exception:
                pass
            return
        queue = self._build_queue(queries_raw, pipeline_mode)
        self._search_queue = list(queue)
        self._search_queue_total = len(queue)
        self._search_queue_results = []
        self._search_current_config = None
        self._mw._running_preview_records = None
        try:
            self._mw.search_view.queue_panel.set_queue(queue)
        except Exception:
            pass
        try:
            self._mw.search_view.log_panel.append(
                "info", f"队列已就绪：{self._search_queue_total} 个检索式",
            )
        except Exception:
            pass
        self._launch_next_in_queue()

    def on_search_cancel_requested(self) -> None:
        selected_row = -1
        try:
            selected_row = (
                self._mw.search_view.queue_panel.table.selected_row()
            )
        except Exception:
            selected_row = -1
        if selected_row >= 0:
            display_items = self._get_queue_display_items()
            if selected_row < len(display_items):
                item = display_items[selected_row]
                selected_name = str(item.get("name", "") or "")
                if not self._is_selected_running(selected_name):
                    for i, cfg in enumerate(self._search_queue):
                        if cfg.get("name") != selected_name:
                            continue
                        status = str(cfg.get("status", "pending") or "pending")
                        if status in ("pending", "interrupted", "failed"):
                            cfg["status"] = "interrupted"
                            self._refresh_queue_panel()
                            try:
                                self._mw.logs_view.append_log(
                                    "info",
                                    f"暂停任务：{selected_name}",
                                    source="队列",
                                )
                            except Exception:
                                pass
                            if self._search_worker is None:
                                return
                            break
                        return
        if self._search_worker is None:
            return
        try:
            self._user_cancelled_queue = True
            self._search_worker.request_interrupt()
            self._mw.sync_run_state("cancelling")
            self._mw.search_view.log_panel.append(
                "warning",
                "取消请求已发出，pipeline 将在下一个检查点响应",
            )
        except Exception as exc:
            try:
                self._mw.search_view.log_panel.append(
                    "error", f"取消失败：{exc}",
                )
            except Exception:
                pass

    def on_search_continue_requested(self) -> None:
        selected_row = -1
        try:
            selected_row = (
                self._mw.search_view.queue_panel.table.selected_row()
            )
        except Exception:
            selected_row = -1
        if selected_row >= 0:
            display_items = self._get_queue_display_items()
            if selected_row < len(display_items):
                item = display_items[selected_row]
                selected_name = str(item.get("name", "") or "")
                if self._is_selected_running(selected_name):
                    try:
                        self._mw.search_view.log_panel.append(
                            "info",
                            f"任务 {selected_name} 正在运行中",
                        )
                    except Exception:
                        pass
                    return
                for idx, cfg in enumerate(self._search_queue):
                    if cfg.get("name") != selected_name:
                        continue
                    status = str(cfg.get("status", "pending") or "pending")
                    if status in ("pending", "interrupted", "failed"):
                        cfg["status"] = "pending"
                        self._search_queue.pop(idx)
                        self._search_queue.insert(0, cfg)
                        try:
                            self._mw.logs_view.append_log(
                                "info",
                                f"插队继续任务：{selected_name}",
                                source="队列",
                            )
                        except Exception:
                            pass
                        self._refresh_queue_panel()
                        if not self._mw._has_running_worker():
                            self._launch_next_in_queue()
                        else:
                            try:
                                self._mw.notify_user(
                                    "info", 
                                    f"任务 {selected_name} 已排入队首，当前任务结束后立即执行"
                                )
                            except Exception:
                                pass
                        return
                    return
                return
        if self._mw._has_running_worker():
            try:
                self._mw.search_view.log_panel.append(
                    "warning", "已有运行中的任务，无法继续",
                )
            except Exception:
                pass
            return
        if not self._search_queue:
            try:
                self._mw.search_view.log_panel.append(
                    "info", "队列已空，无剩余任务",
                )
            except Exception:
                pass
            return
        has_pending = any(
            str(c.get("status", "pending") or "pending") == "pending"
            for c in self._search_queue
        )
        if not has_pending:
            try:
                self._mw.search_view.log_panel.append(
                    "info",
                    "无可继续的任务（全部中断，请选中任务后点「继续」）",
                )
            except Exception:
                pass
            return
        self._launch_next_in_queue()

    def _launch_next_in_queue(self) -> None:
        if self._search_current_config is not None:
            return
        if self._search_worker is not None and self._search_worker.isRunning():
            return
        pending_idx = -1
        for i, c in enumerate(self._search_queue):
            if str(c.get("status", "pending") or "pending") == "pending":
                pending_idx = i
                break
        if pending_idx < 0:
            self._refresh_queue_panel()
            if self._search_queue:
                self._mw.search_view.set_toolbar_visible(True)
                self._mw.sync_run_state("interrupted")
                try:
                    self._mw.notify_user(
                        "info",
                        f"队列已暂停，{len(self._search_queue)} 项中断"
                        f"等待处理；选中任务点「继续」可恢复",
                    )
                except Exception:
                    pass
            else:
                self._mw.search_view.set_toolbar_visible(False)
                self._mw.sync_run_state("finished")
            self._notify_queue_summary()
            return
        cfg = self._search_queue.pop(pending_idx)
        cfg["status"] = "running"
        self._search_current_config = cfg
        self._running_kind = "search"
        self._refresh_queue_panel()
        try:
            self._mw.search_view.set_toolbar_visible(True)
        except Exception:
            pass
        if cfg.get("resume") and cfg.get("run_name"):
            run_name = str(cfg["run_name"])
            started = self._resume_one_run(run_name, running_kind="search")
            if started:
                try:
                    self._mw.logs_view.append_log(
                        "info",
                        f"恢复中断任务 {run_name}",
                        source="队列",
                    )
                except Exception:
                    pass
                return
            self._record_queue_failure(
                cfg, pending_idx, f"恢复失败（run={run_name}）",
            )
            QTimer.singleShot(0, self._launch_next_in_queue)
            return
        try:
            base_config = self._config_ctrl.get_config()
        except Exception as exc:
            self._record_queue_failure(
                cfg, pending_idx, f"配置加载失败：{exc}",
            )
            QTimer.singleShot(0, self._launch_next_in_queue)
            return
        project = base_config.setdefault("project", {})
        project["strategy_name"] = cfg["name"]
        project["run_name"] = ""
        base_config["queries"] = [
            {"name": cfg["name"], "pubmed": cfg["pubmed"]}
        ]
        pipeline_mode = str(cfg.get("pipeline_mode", "intensive"))
        base_config.setdefault("pipeline", {})["mode"] = pipeline_mode
        try:
            from medlitflow import pipeline as _pipeline
            run_name = _pipeline._resolve_run_name(
                base_config, cfg["name"], resume=False
            )
        except Exception as exc:
            self._record_queue_failure(
                cfg, pending_idx, f"run_name 生成失败：{exc}",
            )
            QTimer.singleShot(0, self._launch_next_in_queue)
            return
        project["run_name"] = run_name
        cfg["run_name"] = run_name
        try:
            tmp_cfg_path = SearchRunLauncher.write_ui_run_config(
                base_config, self._ui_tmp_files,
            )
        except Exception as exc:
            self._record_queue_failure(
                cfg, pending_idx, f"临时配置写入失败：{exc}",
            )
            QTimer.singleShot(0, self._launch_next_in_queue)
            return
        try:
            worker = SearchRunLauncher.prepare_search_worker(
                tmp_cfg_path,
                resume=False,
                main_window=self._mw,
                on_finished=self._on_search_worker_finished,
                on_failed=self._on_search_worker_failed,
                on_interrupted=self._on_search_worker_interrupted,
            )
        except Exception as exc:
            self._record_queue_failure(
                cfg, pending_idx, f"Worker 构造/绑定失败：{exc}",
            )
            QTimer.singleShot(0, self._launch_next_in_queue)
            return
        try:
            self._mw.logs_view.append_log(
                "info", f"启动 run {cfg['name']}", source="队列",
            )
        except Exception:
            pass
        self._search_worker = worker
        self._mw.sync_run_state("running")
        try:
            self.refresh_incomplete_runs()
        except Exception:
            pass
        try:
            self._mw.tasks_view.set_running(
                cfg["name"], cfg["name"], "准备中", auto_switch=True,
            )
        except Exception:
            pass
        try:
            self._mw.search_view.set_current_run_name(run_name)
        except Exception:
            pass
        try:
            self._mw.search_view.log_panel.append(
                "info", f"启动 {cfg['name']}",
            )
        except Exception:
            pass
        try:
            self._mw.logs_view.start_new_run(cfg["name"])
        except Exception:
            pass
        self._mw._spawn_worker(worker)

    def _resume_one_run(self, run_name, running_kind="retry") -> bool:
        self._running_kind = running_kind
        runs = self._collect_incomplete_runs()
        target = next(
            (r for r in runs if r["run_name"] == run_name), None,
        )
        if target is None:
            try:
                self._mw.notify_user("warning", f"未找到 run：{run_name}")
            except Exception:
                pass
            return False
        strategy_name = target.get("strategy_name", "")
        if not strategy_name:
            try:
                self._mw.notify_user(
                    "warning",
                    f"run {run_name} 缺少 strategy_name，无法恢复",
                )
            except Exception:
                pass
            return False
        pubmed_query = str(target.get("pubmed_query", "") or "").strip()
        if not pubmed_query:
            try:
                self._mw.notify_user(
                    "warning",
                    f"run {run_name} 由旧版本创建，缺少原始检索式，"
                    f"无法自动恢复。请到「检索」页输入相同检索式后重新运行。",
                )
            except Exception:
                pass
            return False
        try:
            base_config = self._config_ctrl.get_config()
        except Exception as exc:
            try:
                self._mw.notify_user("error", f"配置加载失败：{exc}")
            except Exception:
                pass
            return False
        project = base_config.setdefault("project", {})
        project["strategy_name"] = strategy_name
        project["run_name"] = run_name
        base_config["queries"] = [
            {"name": strategy_name, "pubmed": pubmed_query}
        ]
        try:
            tmp_cfg_path = SearchRunLauncher.write_ui_run_config(
                base_config, self._ui_tmp_files,
            )
        except Exception as exc:
            try:
                self._mw.notify_user("error", f"临时配置写入失败：{exc}")
            except Exception:
                pass
            return False
        try:
            worker = SearchRunLauncher.prepare_search_worker(
                tmp_cfg_path,
                resume=True,
                main_window=self._mw,
                on_finished=self._on_search_worker_finished,
                on_failed=self._on_search_worker_failed,
                on_interrupted=self._on_search_worker_interrupted,
            )
        except Exception as exc:
            try:
                self._mw.notify_user("error", f"Worker 构造/绑定失败：{exc}")
            except Exception:
                pass
            return False
        try:
            self._mw.logs_view.append_log(
                "info",
                f"从断点恢复 run {run_name}",
                source="队列",
            )
        except Exception:
            pass
        self._search_worker = worker
        self._search_current_config = {
            "name": run_name,
            "run_name": run_name,
            "pubmed": pubmed_query,
            "resume": True,
            "status": "running",
        }
        try:
            self._mw.sync_run_state("running")
        except Exception:
            pass
        try:
            self._mw.tasks_view.set_running(
                run_name, strategy_name, "准备中", auto_switch=True,
            )
        except Exception:
            pass
        try:
            self._mw.notify_user(
                "info",
                f"从断点恢复：run {run_name} / 策略 {strategy_name}",
            )
        except Exception:
            pass
        try:
            self._mw.logs_view.start_new_run(run_name)
        except Exception:
            pass
        self._mw._spawn_worker(worker)
        return True

    def _record_queue_failure(self, cfg, index, error_msg) -> None:
        name = str(cfg.get("name", "") or "")
        self._search_queue_results.append({
            "name": name, "status": "failed", "error": str(error_msg),
        })
        self._search_current_config = None
        self._refresh_queue_panel()
        try:
            self._mw.search_view.log_panel.append(
                "error", f"队列失败：{name} - {error_msg}",
            )
        except Exception:
            pass

    def _notify_queue_summary(self) -> None:
        total = len(self._search_queue_results)
        if total == 0:
            return
        ok = sum(
            1 for r in self._search_queue_results if r["status"] == "ok"
        )
        failed = sum(
            1 for r in self._search_queue_results if r["status"] == "failed"
        )
        interrupted = total - ok - failed
        msg = f"队列完成：{ok}/{total} 成功"
        if failed:
            msg += f"，{failed} 失败"
        if interrupted:
            msg += f"，{interrupted} 中断"
        try:
            self._mw.search_view.log_panel.append("info", msg)
        except Exception:
            pass

    def _on_search_worker_finished(self, run_root) -> None:
        sender = self.sender()
        if sender is not None and sender is not self._search_worker:
            return
        self._search_worker = None
        self._mw._running_preview_records = None
        cfg = self._search_current_config or {}
        name = str(cfg.get("name", "") or "")
        self._search_queue_results.append({
            "name": name, "status": "ok", "run_root": run_root,
        })
        self._search_current_config = None
        if run_root and run_root != "OK":
            try:
                run_name = Path(run_root).name
                self._mw.search_view.set_current_run_name(run_name)
            except Exception:
                pass
        if not self._search_queue:
            try:
                self._library_ctrl.load_library_records(force=True)
                self._library_ctrl._refresh_literature()
                self._library_ctrl._refresh_literature_tabs()
                self._mw._refresh_dashboard()
                self._mw._screening_ctrl.refresh_screening()
                self._strategy_ctrl.refresh_completed_strategies()
            except Exception:
                pass
            self._mw.sync_run_state("finished")
            self._refresh_queue_panel()
            self._notify_queue_summary()
        else:
            self._launch_next_in_queue()

    def _on_search_worker_failed(self, error) -> None:
        sender = self.sender()
        if sender is not None and sender is not self._search_worker:
            return
        self._search_worker = None
        self._mw._running_preview_records = None
        cfg = self._search_current_config or {}
        name = str(cfg.get("name", "") or "")
        self._search_queue_results.append({
            "name": name, "status": "failed", "error": str(error),
        })
        self._search_current_config = None
        self.refresh_incomplete_runs()
        try:
            self._mw.tasks_view.clear_running()
        except Exception:
            pass
        if not self._search_queue:
            self._mw.sync_run_state("failed")
            self._refresh_queue_panel()
            self._notify_queue_summary()
        else:
            self._launch_next_in_queue()

    def _on_search_worker_interrupted(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        sender = self.sender()
        if sender is not None and sender is not self._search_worker:
            return
        self._search_worker = None
        self._mw._running_preview_records = None
        cfg = self._search_current_config or {}
        name = str(cfg.get("name", "") or "")
        run_name = str(cfg.get("run_name", "") or "")
        self._search_queue_results.append({
            "name": name, "status": "interrupted",
        })
        self.refresh_incomplete_runs()
        has_pending = any(
            str(c.get("status", "pending") or "pending") == "pending"
            for c in self._search_queue
        )
        self._search_current_config = None
        if cfg and run_name:
            resume_cfg = dict(cfg)
            if self._run_dir_exists(run_name):
                resume_cfg["resume"] = True
            else:
                resume_cfg.pop("resume", None)
                resume_cfg.pop("run_name", None)
            resume_cfg["status"] = "interrupted"
            self._search_queue.append(resume_cfg)
        user_cancelled = self._user_cancelled_queue
        self._user_cancelled_queue = False
        try:
            self._mw.tasks_view.clear_running()
        except Exception:
            pass
        self._refresh_queue_panel()
        if user_cancelled:
            try:
                self._mw.notify_user(
                    "info", "队列已暂停；选中任务点「继续」可恢复",
                )
            except Exception:
                pass
            self._mw.sync_run_state("interrupted")
            self._notify_queue_summary()
            return
        if has_pending:
            pending_count = sum(
                1 for c in self._search_queue
                if str(c.get("status", "pending") or "pending") == "pending"
            )
            reply = QMessageBox.question(
                self._mw,
                "队列中断",
                f"当前任务已中断（已排队尾）。\n"
                f"剩余 {pending_count} 个任务待运行。\n\n"
                f"继续剩余任务？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self._launch_next_in_queue()
                return
            try:
                self._mw.notify_user(
                    "info",
                    f"队列已暂停，{pending_count} 项待运行 + "
                    f"{len(self._search_queue) - pending_count} 项中断；"
                    f"选中任务点「继续」可恢复",
                )
            except Exception:
                pass
        else:
            try:
                self._mw.notify_user(
                    "info", "当前任务已中断；选中任务点「继续」可恢复",
                )
            except Exception:
                pass
        self._mw.sync_run_state("interrupted")
        self._notify_queue_summary()

    def on_tasks_retry_all(self) -> None:
        if self._mw._has_running_worker():
            try:
                self._mw.notify_user(
                    "warning", "已有运行中的任务，请先暂停或等待完成",
                )
            except Exception:
                pass
            return
        runs = self._collect_incomplete_runs()
        if not runs:
            try:
                self._mw.notify_user("info", "无未完成 run")
            except Exception:
                pass
            return
        from PySide6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self._mw,
            "确认全部重试",
            f"即将依次重试 {len(runs)} 个未完成 run：\n\n"
            f"  · 有 pubmed_query 的 run 会自动恢复\n"
            f"  · 缺少 pubmed_query 的旧 run 会被跳过\n\n"
            f"确认继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            try:
                self._mw.notify_user("info", "已取消全部重试")
            except Exception:
                pass
            return
        self._search_queue.clear()
        self._search_queue_results.clear()
        self._search_current_config = None
        for r in runs:
            run_name = r.get("run_name")
            if run_name:
                self._search_queue.append({
                    "name": run_name,
                    "pubmed": "",
                    "type": "retry",
                    "resume": True,
                    "run_name": run_name,
                    "status": "pending"
                })
        self._search_queue_total = len(self._search_queue)
        self._refresh_queue_panel()
        self._launch_next_in_queue()

    def _show_resume_failed_dialog(self, run_name, detail="") -> None:
        from PySide6.QtWidgets import QMessageBox
        msg = (
            f"无法恢复 run：\n{run_name}\n\n"
            f"原因：{detail or '缺少原始检索式'}\n\n"
            f"处理方式：\n"
            f"  1. 到「检索」页输入相同检索式后重新运行\n"
            f"  2. 到「采集任务」页点「放弃所选」清掉此 run"
        )
        QMessageBox.warning(self._mw, "无法恢复 run", msg)

    def on_tasks_pause(self) -> None:
        self.on_search_cancel_requested()

    def _run_dir_exists(self, run_name: str) -> bool:
        if not run_name:
            return False
        try:
            config = self._config_ctrl.get_config()
        except Exception:
            return False
        output_root = str(
            (config.get("project", {}) or {}).get("output_root", "") or ""
        )
        if not output_root:
            return False
        try:
            return (Path(output_root) / "runs" / run_name).is_dir()
        except OSError:
            return False

    def _load_interim_for_active_run(self):
        try:
            config = self._config_ctrl.get_config()
            output_root = Path(config["project"]["output_root"])
            runs_dir = output_root / "runs"
            if not runs_dir.exists():
                return None
            active_run = None
            latest_mtime = 0.0
            for run_dir in runs_dir.iterdir():
                if not run_dir.is_dir():
                    continue
                state_path = run_dir / ".run_state.json"
                if not state_path.exists():
                    continue
                try:
                    state = json.loads(
                        state_path.read_text(encoding="utf-8")
                    )
                except Exception:
                    continue
                if not isinstance(state, dict):
                    continue
                if state.get("state") != "running":
                    continue
                try:
                    mtime = state_path.stat().st_mtime
                except OSError:
                    continue
                if mtime > latest_mtime:
                    latest_mtime = mtime
                    active_run = run_dir
            if active_run is None:
                return None
            candidates = [
                active_run / "output" / "records_final.parquet",
                active_run / "interim" / "records_finalized.parquet",
                active_run / "interim" / "retrieval.partial.parquet",
                active_run / "interim" / "records_deduplicated.parquet",
                active_run / "interim" / "records_collected.parquet",
            ]
            for path in candidates:
                if path.exists() and path.stat().st_size > 0:
                    try:
                        df = pd.read_parquet(path)
                        if len(df) > 0:
                            return df
                    except Exception:
                        continue
            return None
        except Exception:
            return None

    def refresh_literature_from_interim(self) -> None:
        df = self._load_interim_for_active_run()
        if df is None or len(df) == 0:
            return
        df = df.copy()
        if "user_decision" not in df.columns:
            df["user_decision"] = ""
        if "final_retrieval_status" not in df.columns:
            df["final_retrieval_status"] = ""
        self._mw._running_preview_records = df
        try:
            current_filters = self._mw.literature_view.filter_values()
        except Exception:
            current_filters = {}
        current_filters["special_tab"] = ""
        self._library_ctrl._apply_literature_filter(current_filters)

    def check_schema_compat(self) -> None:
        try:
            config = self._config_ctrl.get_config()
        except Exception:
            return
        try:
            project_cfg = config.get("project", {}) or {}
            output_root_raw = str(
                project_cfg.get("output_root", "") or ""
            )
            if not output_root_raw:
                return
            runs_root = Path(output_root_raw) / "runs"
            if not runs_root.exists():
                return
        except Exception:
            return
        too_new: list = []
        try:
            for run_dir in runs_root.iterdir():
                if not run_dir.is_dir():
                    continue
                state_path = run_dir / ".run_state.json"
                if not state_path.exists():
                    continue
                try:
                    state = json.loads(
                        state_path.read_text(encoding="utf-8")
                    )
                except (json.JSONDecodeError, OSError):
                    continue
                if not isinstance(state, dict):
                    continue
                try:
                    version = int(state.get("schema_version", 1))
                except (ValueError, TypeError):
                    version = 1
                if version > 1:
                    too_new.append((run_dir.name, version))
        except Exception:
            return
        if not too_new:
            return
        for name, version in too_new:
            try:
                self._mw.logs_view.append_log(
                    "warning",
                    f"run {name} 由更新版本创建"
                    f"（schema_version={version}），"
                    f"当前 UI 无法解析，请升级 UI",
                )
            except Exception:
                pass

    def refresh_incomplete_runs(self) -> int:
        runs = self._collect_incomplete_runs()
        count = len(runs)
        try:
            self._mw.notification_banner.set_incomplete_runs(count)
        except Exception:
            pass
        try:
            self._mw.tasks_view.set_incomplete_runs(runs)
        except Exception:
            pass
        try:
            running_n = 1 if self._mw._has_running_worker() else 0
            self._mw.dashboard_view.set_stats(
                {"tasks": str(count + running_n)}
            )
            self._mw.dashboard_view.set_tasks_tag(
                f"运行 {running_n} / 中断 {count}"
            )
        except Exception:
            pass
        return count

    def _collect_incomplete_runs(self) -> list:
        runs: list = []
        try:
            config = self._config_ctrl.get_config()
        except Exception:
            return runs
        try:
            project_cfg = config.get("project", {}) or {}
            output_root_raw = str(
                project_cfg.get("output_root", "") or ""
            )
            if not output_root_raw:
                return runs
            runs_root = Path(output_root_raw) / "runs"
            if not runs_root.exists():
                return runs
            for run_dir in runs_root.iterdir():
                if not run_dir.is_dir():
                    continue
                state_path = run_dir / ".run_state.json"
                if not state_path.exists():
                    continue
                try:
                    state = json.loads(
                        state_path.read_text(encoding="utf-8")
                    )
                except (json.JSONDecodeError, OSError):
                    continue
                if not isinstance(state, dict):
                    continue
                try:
                    version = int(state.get("schema_version", 1))
                except (ValueError, TypeError):
                    version = 1
                if version > 1:
                    continue
                if state.get("state") != "running":
                    continue
                interrupt_at = str(state.get("interrupt_at", "") or "")
                interrupt_detail = str(
                    state.get("interrupt_detail", "") or ""
                )
                if not interrupt_at:
                    latest_mtime = 0.0
                    for probe in (
                        state_path,
                        run_dir / "interim",
                        run_dir / "papers" / "inbox",
                        run_dir / "output",
                        run_dir / "audit",
                    ):
                        try:
                            m = probe.stat().st_mtime
                            if m > latest_mtime:
                                latest_mtime = m
                        except OSError:
                            continue
                    age = (
                        _time.time() - latest_mtime
                        if latest_mtime > 0 else float("inf")
                    )
                    if age < _ACTIVE_WINDOW_SECONDS:
                        continue
                    from datetime import datetime as _dt
                    try:
                        interrupt_at = _dt.fromtimestamp(
                            latest_mtime
                        ).astimezone().isoformat()
                    except (OSError, ValueError):
                        interrupt_at = ""
                    if not interrupt_detail:
                        interrupt_detail = (
                            "运行异常终止（无中断记录，可能被强杀）"
                        )
                post_hook = str(
                    state.get("post_hook_stage", "none") or "none"
                )
                last_stage = str(state.get("last_stage", "") or "")
                if post_hook and post_hook != "none":
                    stage_display = f"post_hooks: {post_hook}"
                elif last_stage:
                    stage_display = f"阶段: {last_stage}"
                else:
                    stage_display = "—"
                try:
                    mtime = run_dir.stat().st_mtime
                except OSError:
                    mtime = 0.0
                runs.append({
                    "run_name": str(
                        state.get("run_name", run_dir.name) or run_dir.name
                    ),
                    "strategy_name": str(
                        state.get("strategy_name", "") or ""
                    ),
                    "pubmed_query": str(
                        state.get("pubmed_query", "") or ""
                    ),
                    "stage_display": stage_display,
                    "interrupt_at": str(interrupt_at or ""),
                    "interrupt_detail": str(interrupt_detail or ""),
                    "mtime": mtime,
                })
        except Exception:
            return runs
        runs.sort(key=lambda r: r.get("mtime", 0.0), reverse=True)
        return runs

    def on_incomplete_run_refresh(self) -> None:
        count = self.refresh_incomplete_runs()
        try:
            self._mw.notify_user(
                "info", f"已刷新未完成 run 列表（{count} 个）",
            )
        except Exception:
            pass

    def on_incomplete_run_resume(self, run_name) -> None:
        if not run_name:
            return
        if self._mw._has_running_worker():
            try:
                self._mw.notify_user(
                    "warning",
                    "已有运行中的任务，请先取消或等待完成",
                )
            except Exception:
                pass
            return
        try:
            items = [{"name": run_name, "pubmed": ""}]
            self._mw.search_view.queue_panel.set_queue(items)
            self._mw.search_view.queue_panel.mark_running(0)
            self._mw.search_view.set_toolbar_visible(True)
        except Exception:
            pass
        started = self._resume_one_run(run_name)
        if started:
            try:
                from ui.config import PAGE_SEARCH
                self._mw.goto_page(PAGE_SEARCH)
            except Exception:
                pass
        else:
            self._show_resume_failed_dialog(run_name)

    def on_incomplete_run_abandon(self, run_name) -> None:
        if not run_name:
            return
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication, QMessageBox
        if self._mw._has_running_worker():
            try:
                self._mw.notify_user(
                    "warning", "pipeline 正在运行，请先取消再放弃",
                )
            except Exception:
                pass
            return
        reply = QMessageBox.question(
            self._mw,
            "确认放弃 run",
            f"即将放弃 run：{run_name}\n\n"
            f"此操作会回滚 library 与 archive 副作用，"
            f"并删除 run 目录，不可恢复。\n\n"
            f"确认继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            try:
                self._mw.notify_user(
                    "info", f"已取消放弃 run：{run_name}",
                )
            except Exception:
                pass
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            from medlitflow import pipeline
            pipeline.abandon_run(
                self._config_ctrl.app_config_path, run_name,
            )
        except Exception as exc:
            try:
                self._mw.notify_user("error", f"放弃失败：{exc}")
            except Exception:
                pass
            return
        finally:
            QApplication.restoreOverrideCursor()
        try:
            self._mw.notify_user("info", f"已放弃 run：{run_name}")
        except Exception:
            pass
        try:
            self._library_ctrl.load_library_records()
            self._library_ctrl._refresh_literature()
            self._mw._refresh_dashboard()
            self._mw._screening_ctrl.refresh_screening()
            self.refresh_incomplete_runs()
        except Exception:
            pass

    def _report_search_error(self, message) -> None:
        try:
            self._mw.search_view.set_run_state(
                self._mw.search_view.STATE_FAILED
            )
        except Exception:
            pass
        try:
            self._mw.search_view.log_panel.append("error", str(message))
        except Exception:
            pass