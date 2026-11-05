"""主窗口（玻璃背景 + 顶栏 + 通知横幅 + 侧栏 + 8 个 SlidingStackedWidget 视图）。"""

import json
import random
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import time as _time

from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
)
from PySide6.QtGui import (
    QPainter,
    QColor,
    QLinearGradient,
    QRadialGradient,
    QPixmap,
    QImage,
)
from PySide6.QtCore import Qt, QTimer

from ui.config import (
    APP_NAME,
    DEFAULT_WINDOW_SIZE,
    PAGE_DASHBOARD,
    PAGE_SEARCH,
    PAGE_LITERATURE,
    PAGE_SCREENING,
    PAGE_ZOTERO,
    PAGE_TASKS,
    PAGE_LOGS,
    PAGE_SETTINGS,
)
from ui.theme.palette import Palette, qcolor
from ui.utils.formatting import first_source

from ui.components.header_bar import HeaderBar
from ui.components.sidebar import Sidebar
from ui.components.notification_banner import NotificationBanner
from ui.animations.sliding_stack import SlidingStackedWidget

from ui.views.dashboard_view import DashboardView
from ui.views.search_view import SearchView
from ui.views.literature_view import LiteratureView
from ui.views.screening_view import ScreeningView
from ui.views.tasks_view import TasksView
from ui.views.logs_view import LogsView
from ui.views.settings_view import SettingsView
from ui.views.zotero_view import ZoteroView

_ACTIVE_WINDOW_SECONDS = 300.0

class GlassBackground(QWidget):
    FLOODLIGHT_COLOR = qcolor(Palette.FLOODLIGHT)
    REFLECT_COLOR = qcolor(Palette.REFLECT_BLUE)
    AMBIENT_COLOR = qcolor(Palette.AMBIENT_BLUE)

    FLOODLIGHT_ALPHA = 70
    REFLECT_ALPHA = 90
    AMBIENT_ALPHA = 35
    NOISE_ALPHA = 18

    def __init__(self, parent=None):
        super().__init__(parent)
        self._noise_tile = self._build_noise_tile(size=256)
        self._gradients_cache: tuple | None = None
        self._gradients_size: tuple[int, int] = (-1, -1)

    def _build_noise_tile(self, size=256):
        image = QImage(size, size, QImage.Format_ARGB32)
        image.fill(Qt.transparent)

        total_pixels = size * size
        target_count = int(total_pixels * 0.05)

        for pos in random.sample(range(total_pixels), target_count):
            x = pos % size
            y = pos // size
            v = random.randint(0, 255)
            image.setPixelColor(x, y, QColor(v, v, v, self.NOISE_ALPHA))

        return QPixmap.fromImage(image)

    def _build_gradients(self, w: int, h: int) -> tuple:
        base = QLinearGradient(0, 0, w, h)
        base.setColorAt(0.0, qcolor(Palette.PITCH_DEEP))
        base.setColorAt(0.5, qcolor(Palette.PITCH_RAISED))
        base.setColorAt(1.0, qcolor(Palette.PITCH_DARK))

        # ---- L1: 泛光灯主光源（左上） ----
        flood = QRadialGradient(w * 0.15, h * 0.10, max(w, h) * 0.70)
        c = self.FLOODLIGHT_COLOR
        flood.setColorAt(0.0, QColor(c.red(), c.green(), c.blue(),
                                     self.FLOODLIGHT_ALPHA))
        flood.setColorAt(0.5, QColor(c.red(), c.green(), c.blue(),
                                     int(self.FLOODLIGHT_ALPHA * 0.25)))
        flood.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), 0))

        # ---- L2: 深钢蓝反射光（底部中央） ----
        reflect = QRadialGradient(w * 0.50, h * 1.00, max(w, h) * 0.55)
        c = self.REFLECT_COLOR
        reflect.setColorAt(0.0, QColor(c.red(), c.green(), c.blue(),
                                       self.REFLECT_ALPHA))
        reflect.setColorAt(0.6, QColor(c.red(), c.green(), c.blue(),
                                       int(self.REFLECT_ALPHA * 0.20)))
        reflect.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), 0))

        # ---- L3: 环境氛围光（右上） ----
        ambient = QRadialGradient(w * 0.88, h * 0.20, max(w, h) * 0.35)
        c = self.AMBIENT_COLOR
        ambient.setColorAt(0.0, QColor(c.red(), c.green(), c.blue(),
                                       self.AMBIENT_ALPHA))
        ambient.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), 0))

        return (base, flood, reflect, ambient)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = self.rect()
        w = rect.width()
        h = rect.height()

        size = (w, h)
        if self._gradients_cache is None or self._gradients_size != size:
            self._gradients_cache = self._build_gradients(w, h)
            self._gradients_size = size
        base, flood, reflect, ambient = self._gradients_cache

        painter.fillRect(rect, base)
        painter.fillRect(rect, flood)
        painter.fillRect(rect, reflect)
        painter.fillRect(rect, ambient)

        painter.drawTiledPixmap(rect, self._noise_tile)

        painter.end()

class MedLitFlowMainWindow(QMainWindow):
    """ 应用主窗口 """
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle(APP_NAME)

        from PySide6.QtWidgets import QApplication as _QApp

        _screen = _QApp.primaryScreen()
        if _screen is not None:
            _avail = _screen.availableGeometry()
            _avail_w = _avail.width()
            _avail_h = _avail.height()

            _min_w = min(1200, int(_avail_w * 0.85))
            _min_h = min(760, int(_avail_h * 0.85))
            self.setMinimumSize(_min_w, _min_h)

            _init_w = min(DEFAULT_WINDOW_SIZE[0], int(_avail_w * 0.85))
            _init_h = min(DEFAULT_WINDOW_SIZE[1], int(_avail_h * 0.85))
            _init_w = max(_init_w, _min_w)
            _init_h = max(_init_h, _min_h)
            self.resize(_init_w, _init_h)
        else:
            self.setMinimumSize(1200, 760)
            self.resize(*DEFAULT_WINDOW_SIZE)

        self._worker = None
        self._running_preview_records = None

        self._worker_pool: list = []
        self._logs_progress_stage: str = ""
        self._logs_progress_milestone: int = 0
        self._config_cache: dict | None = None
        self._config_cache_mtime = None

        self._special_actions = None
        self._zotero_controller = None

        self._build_ui()
        from ui.controllers.config_controller import ConfigController
        self._config_ctrl = ConfigController(self)
        from ui.controllers.library_controller import LibraryController
        self._library_ctrl = LibraryController(self)
        from ui.controllers.strategy_controller import StrategyController
        self._strategy_ctrl = StrategyController(
            self, self._config_ctrl, self._library_ctrl,
        )
        from ui.controllers.search_queue_controller import SearchQueueController
        self._queue_ctrl = SearchQueueController(
            self, self._config_ctrl, self._library_ctrl, self._strategy_ctrl,
        )
        from ui.controllers.screening_controller import ScreeningController
        self._screening_ctrl = ScreeningController(
            self, self._config_ctrl, self._library_ctrl, self._strategy_ctrl,
        )
        self._connect_signals()
        self._init_special_actions()
        self._init_zotero_controller()

    def closeEvent(self, event):
        workers_to_stop = list(self._worker_pool)

        needs_mark = False
        for w in workers_to_stop:
            try:
                if w.isRunning():
                    w.request_interrupt()
                    try:
                        from ui.bridge.worker import StartupCleanupWorker
                        is_startup = isinstance(w, StartupCleanupWorker)
                    except Exception:
                        is_startup = False
                    timeout_ms = 1500 if is_startup else 5000
                    if not w.wait(timeout_ms):
                        needs_mark = True
                        w.terminate()
                        w.wait(1000)
            except RuntimeError:
                pass

        if needs_mark:
            self._mark_running_runs_as_interrupted(
                "UI 关闭时强制退出（worker 5 秒内未响应中断）"
            )

        self._worker_pool.clear()
        try:
            self._queue_ctrl.cleanup()
        except Exception:
            pass

        try:
            self._screening_ctrl.cleanup()
        except Exception:
            pass

        if self._zotero_controller is not None:
            try:
                from medlitflow import zotero_bridge
                zotero_bridge.register_log_callback(None)
            except Exception:
                pass

        if self._zotero_controller is not None:
            try:
                self._zotero_controller.shutdown()
            except Exception:
                pass

        if self._zotero_controller is not None:
            try:
                self._zotero_controller.set_current_detail_cid("")
            except Exception:
                pass

        super().closeEvent(event)

    def _mark_running_runs_as_interrupted(self, detail: str = ""):
        import os as _os
        try:
            config = self.get_config()
            output_root = Path(config["project"]["output_root"])
        except Exception:
            return

        runs_root = output_root / "runs"
        if not runs_root.exists():
            return

        from medlitflow import core as _core
        now = _core.now_iso()
        detail_text = detail or "UI 关闭时强制退出"

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
            except Exception:
                continue
            if not isinstance(state, dict):
                continue
            if state.get("state") != "running":
                continue
            if state.get("interrupt_at"):
                continue
            state["interrupt_at"] = now
            state["interrupt_detail"] = detail_text
            try:
                tmp = state_path.with_name(state_path.name + ".tmp")
                tmp.write_text(
                    json.dumps(state, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                _os.replace(tmp, state_path)
            except OSError:
                pass

    def _build_ui(self):
        self.background = GlassBackground()
        self.setCentralWidget(self.background)

        outer = QVBoxLayout(self.background)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.header = HeaderBar()
        outer.addWidget(self.header)

        self.notification_banner = NotificationBanner(self.background)
        self.notification_banner.hide()

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self.sidebar = Sidebar()
        body.addWidget(self.sidebar)

        self.stack = SlidingStackedWidget()
        self._create_pages()
        body.addWidget(self.stack, stretch=1)

        outer.addLayout(body, stretch=1)

    def _create_pages(self):
        self.dashboard_view = DashboardView()
        self.search_view = SearchView()
        self.literature_view = LiteratureView()
        self.screening_view = ScreeningView()
        self.zotero_view = ZoteroView()
        self.tasks_view = TasksView()
        self.logs_view = LogsView()
        self.settings_view = SettingsView()

        for view in (
            self.dashboard_view,
            self.search_view,
            self.literature_view,
            self.screening_view,
            self.zotero_view,
            self.tasks_view,
            self.logs_view,
            self.settings_view,
        ):
            self.stack.addWidget(view)

    def _connect_signals(self):
        # ---- 侧栏 → 页面切换 ----
        self.sidebar.page_changed.connect(self._on_page_changed)

        # ---- 通知横幅 → 跳转任务页 ----
        self.notification_banner.clicked.connect(
            self._on_banner_clicked
        )

        # ---- 仪表盘「新建检索」 → 跳转检索页 ----
        self.dashboard_view.create_requested.connect(
            self._on_dashboard_create_clicked
        )

        # ---- 文献库：行点击 → 填充详情 ----
        self.literature_view.row_clicked.connect(
            self._library_ctrl.on_row_clicked
        )

        # ---- 检索页：桥接到 PipelineWorker ----
        self.search_view.run_requested.connect(self._queue_ctrl.on_search_run_requested)
        self.search_view.cancel_requested.connect(self._queue_ctrl.on_search_cancel_requested)
        self.search_view.continue_queue_requested.connect(self._queue_ctrl.on_search_continue_requested)

        # ---- 仪表盘：刷新 ----
        self.dashboard_view.refresh_requested.connect(
            self._on_dashboard_refresh_requested
        )

        # ---- 文献库：筛选 / 重置 / 打开筛选中心 ----
        self.literature_view.filter_requested.connect(
            self._library_ctrl.on_filter_requested
        )
        self.literature_view.reset_requested.connect(
            self._library_ctrl.on_reset_requested
        )
        self.literature_view.open_screening_requested.connect(
            self._library_ctrl.on_open_screening
        )

        self.literature_view.cleanup_orphans_requested.connect(
            self._library_ctrl.on_cleanup_orphans
        )

        self.literature_view.detail_action_requested.connect(
            self._library_ctrl.on_detail_action
        )

        try:
            self.literature_view.special_tab_changed.connect(
                self._library_ctrl.on_special_tab_changed
            )
        except Exception as exc:
            import sys
            print(
                f"[MainWindow] 连接 literature_view.special_tab_changed "
                f"失败：{exc}",
                file=sys.stderr,
            )

        self.screening_view.strategy_changed.connect(
            self._screening_ctrl.on_screening_strategy_changed
        )
        self.screening_view.refresh_status_requested.connect(
            self._screening_ctrl.on_screening_refresh_status
        )
        self.screening_view.cleanup_trash_requested.connect(
            self._screening_ctrl.on_screening_cleanup_trash
        )
        self.screening_view.decision_changed.connect(
            self._screening_ctrl.on_screening_decision_changed
        )
        self.screening_view.save_decisions_requested.connect(
            self._screening_ctrl.on_screening_save_decisions
        )
        self.screening_view.confirm_archive_requested.connect(
            self._screening_ctrl.on_screening_confirm_archive
        )
        self.screening_view.export_report_requested.connect(
            self._screening_ctrl.on_screening_export_report
        )
        self.screening_view.row_selected.connect(
            self._screening_ctrl.on_screening_row_selected
        )
        self.screening_view.open_pdf_requested.connect(
            self._screening_ctrl.on_screening_open_pdf
        )
        self.screening_view.rows_filtered.connect(
            self._screening_ctrl.on_screening_rows_filtered
        )
        self.screening_view.detail_action_requested.connect(
            self._screening_ctrl.on_screening_detail_action
        )
        self.screening_view.reopen_strategy_requested.connect(
            self._screening_ctrl.on_screening_reopen_strategy
        )

        self.screening_view.log_message.connect(
            lambda level, msg: self.logs_view.append_log(
                level, msg, source="screening"
            )
        )

        # ---- 采集任务：未完成 run 面板 ----
        self.tasks_view.run_resume_requested.connect(self._queue_ctrl.on_incomplete_run_resume)
        self.tasks_view.run_abandon_requested.connect(self._queue_ctrl.on_incomplete_run_abandon)
        self.tasks_view.run_refresh_requested.connect(self._queue_ctrl.on_incomplete_run_refresh)
        self.tasks_view.retry_all_requested.connect(self._queue_ctrl.on_tasks_retry_all)
        self.tasks_view.pause_requested.connect(self._queue_ctrl.on_tasks_pause)

        # ---- 采集任务：已完成策略面板 ----
        self.tasks_view.strategy_refresh_requested.connect(
            self._strategy_ctrl.on_strategy_refresh
        )
        self.tasks_view.strategy_report_requested.connect(
            self._strategy_ctrl.on_strategy_report
        )
        self.tasks_view.strategy_screening_requested.connect(
            self._strategy_ctrl.on_strategy_screening
        )
        self.tasks_view.strategy_consolidate_requested.connect(
            self._strategy_ctrl.on_strategy_consolidate
        )
        self.tasks_view.strategy_preview_requested.connect(
            self._strategy_ctrl.on_strategy_preview
        )
        self.tasks_view.strategy_archive_requested.connect(
            self._strategy_ctrl.on_strategy_archive
        )
        self.tasks_view.strategy_delete_requested.connect(
            self._strategy_ctrl.on_strategy_delete
        )
        self.tasks_view.strategy_reopen_requested.connect(
            self._screening_ctrl.on_screening_reopen_strategy
        )
        self.tasks_view.completed_reload_requested.connect(
            self._strategy_ctrl.on_completed_reload
        )
        self.tasks_view.tab_changed.connect(
            self._strategy_ctrl.on_tasks_tab_changed
        )
        # ---- 仪表盘：导出 → 跳转设置页 ----
        self.dashboard_view.export_requested.connect(
            self._on_dashboard_export_requested
        )
        self.literature_view.tab_changed.connect(
            self._library_ctrl.on_tab_changed
        )
        # ---- 设置页：保存 / 重置 / 浏览 ----
        self.settings_view.save_requested.connect(
            self._config_ctrl.on_settings_save
        )
        self.settings_view.reset_requested.connect(
            self._config_ctrl.on_settings_reset
        )
        self.settings_view.browse_requested.connect(
            self._config_ctrl.on_settings_browse
        )

    def _init_special_actions(self):
        try:
            from ui.controllers.special_actions_controller import (
                SpecialActionsController,
            )
            self._special_actions = SpecialActionsController(self)
        except Exception as exc:  # noqa: BLE001
            self._special_actions = None
            try:
                self._notify_status(
                    "warning",
                    f"删除功能初始化失败：{exc}（不影响其他功能）",
                )
            except Exception:
                pass

    def _init_zotero_controller(self):
        try:
            from ui.controllers.zotero_controller import ZoteroController
            self._zotero_controller = ZoteroController(self)
        except Exception as exc:
            self._zotero_controller = None
            try:
                self._notify_status(
                    "warning",
                    f"Zotero 控制器初始化失败：{exc}（不影响其他功能）",
                )
            except Exception:
                pass
            return

        ctrl = self._zotero_controller

        try:
            ctrl.connect_view(self.zotero_view)
        except Exception:
            pass

        try:
            ctrl.connect_detail_panel(self.literature_view.detail_panel)
        except Exception:
            pass

        try:
            ctrl.connect_zotero_view_detail(
                self.zotero_view.detail_panel
            )
        except Exception:
            pass

        self.zotero_view.detail_action_requested.connect(
            self._on_zotero_detail_action
        )

        ctrl.activity_appended.connect(
            lambda lv, msg: self.logs_view.append_log(
                lv, f"[Zotero] {msg}", source="zotero",
            )
        )

    def get_config(self) -> dict:
        return self._config_ctrl.get_config()

    def get_library_root(self) -> Path:
        return self._config_ctrl.get_library_root()

    def reload_library_records(self, force=False):
        return self._library_ctrl.load_library_records(force=force)

    def is_busy(self) -> bool:
        return self._has_running_worker()

    def notify_user(self, level: str, message: str) -> None:
        self._notify_status(str(level), str(message))

    def _safe_refresh(self, name: str, fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            try:
                self.logs_view.append_log(
                    "error",
                    f"{name} 失败：{type(exc).__name__}: {exc}",
                    source="refresh",
                )
            except Exception:
                pass

    def refresh_after_data_change(self) -> None:
        self._safe_refresh(
            "load_library_records",
            lambda: self._library_ctrl.load_library_records(force=True),
        )
        self._safe_refresh(
            "refresh_literature", self._library_ctrl._refresh_literature,
        )
        self._safe_refresh(
            "refresh_literature_tabs",
            self._library_ctrl._refresh_literature_tabs,
        )
        self._safe_refresh("refresh_dashboard", self._refresh_dashboard)
        self._safe_refresh(
            "refresh_screening", self._screening_ctrl.refresh_screening,
        )
        self._safe_refresh(
            "refresh_completed_strategies",
            self._strategy_ctrl.refresh_completed_strategies,
        )

        if self._zotero_controller is not None:
            self._safe_refresh(
                "zotero_on_data_changed",
                self._zotero_controller.on_data_changed,
            )

    def _on_page_changed(self, page_index):
        current = self.stack.currentIndex()
        if page_index == current:
            return
        direction = 1 if page_index > current else -1
        self.stack.slide_to_index(page_index, direction=direction)
        self._after_page_switched(page_index)

    def _on_banner_clicked(self):
        self.goto_page(PAGE_TASKS)

    def _on_dashboard_create_clicked(self):
        self.goto_page(PAGE_SEARCH)

    def get_detail_dict_for_record(self, record) -> dict:
        return self._library_ctrl.get_detail_dict_for_record(record)

    def _on_zotero_detail_action(self, action, data) -> None:
        if not isinstance(data, dict):
            return
        if action == "open":
            self._library_ctrl._detail_action_open(data)
        elif action == "copy_doi":
            self._library_ctrl._detail_action_copy_doi(data)
        elif action == "open_pdf":
            self._library_ctrl._detail_action_open_pdf(data)

    def _on_dashboard_refresh_requested(self):
        self._refresh_dashboard()
        self._queue_ctrl.refresh_incomplete_runs()

    def _on_dashboard_export_requested(self):
        self.goto_page(PAGE_SETTINGS)
        try:
            self.settings_view.scroll_to_section("导出配置")
        except Exception:
            pass
        self._notify_status(
            "info",
            "导出配置位于设置页；勾选「运行后自动导出」即生效",
        )

    def _locate_screening_cid(self, cid):
        if not cid:
            return
        cid_str = str(cid)

        found = False
        try:
            table = self.screening_view.document_table
            model = table._model
            found = model.get_row_by_cid(cid_str) is not None
        except Exception:
            found = False

        if not found:
            records = self._library_ctrl._library_records
            if records is not None and len(records) > 0:
                try:
                    mask = records["canonical_id"].astype(str) == cid_str
                    if mask.any():
                        row = records[mask].iloc[0]
                        names = str(row.get("strategy_names", "") or "")
                        strategies = [
                            s.strip() for s in names.split(";") if s.strip()
                        ]
                        if strategies:
                            status_map = self._screening_ctrl._read_strategy_statuses()
                            _pri = {
                                "confirmed": 4,
                                "partially_screened": 3,
                                "collecting": 2,
                                "unscreened": 1,
                                "archived_unprocessed": 0,
                            }
                            ordered = sorted(
                                strategies,
                                key=lambda s: (
                                    -_pri.get(
                                        status_map.get(s, "unscreened"), 0
                                    ),
                                    s,
                                ),
                            )
                            target = ordered[0]
                            self.screening_view.set_user_selected(True)
                            self.screening_view.set_current_strategy(target)
                            self._screening_ctrl._refresh_screening_table(target)
                            self._screening_ctrl._update_screening_stats_from_table(target)
                            found = True
                except Exception:
                    pass

        if not found:
            self._notify_status(
                "warning",
                f"未能定位到 cid={cid_str}（无对应策略或记录）",
            )
            return

        try:
            ok = self.screening_view.locate_cid(cid_str)
        except Exception:
            ok = False
        if ok:
            self._notify_status("info", f"已定位到筛选中心 cid={cid_str}")
        else:
            self._notify_status(
                "warning",
                f"定位失败 cid={cid_str}（可能被搜索框过滤）",
            )

    def _notify_status(self, level, message, source=""):
        try:
            self.logs_view.append_log(
                str(level), f"[操作] {message}", source=source,
            )
        except Exception:
            pass

    def sync_run_state(self, state_name):
        self._sync_run_state(state_name)

    def _sync_run_state(self, state_name):
        """state → (sidebar_text, header_text, color_key, glowing, glow_color)。

        同步侧栏 SystemBox / 顶栏 RunState / 仪表盘 running_tasks 卡脉冲。
        """
        state_map = {
            "idle":        ("待检测",   "IDLE",
                            "TEXT_MUTED",   False, None),
            "running":     ("检测中",   "RUNNING",
                            "CYAN",         True,  None),
            "cancelling":  ("取消中",   "STOPPING",
                            "RISK_ORANGE",  True,
                            Palette.RISK_ORANGE),
            "finished":    ("全部正常", "DONE",
                            "AI_GREEN",     False, None),
            "failed":      ("运行异常", "FAILED",
                            "DANGER_RED",   True,
                            Palette.DANGER_RED),
            "interrupted": ("已中断",   "STOPPED",
                            "RISK_ORANGE",  False, None),
        }
        entry = state_map.get(state_name, state_map["idle"])
        sidebar_text, header_text, color_key, glowing, glow_color = entry

        try:
            self.sidebar.set_system_status("pipeline", sidebar_text, color_key)
        except Exception:
            pass

        try:
            self.header.set_status("RunState", header_text, color_key)
        except Exception:
            pass

        try:
            if glowing:
                self.dashboard_view.set_running_glow(
                    True, color=glow_color
                )
            else:
                self.dashboard_view.set_running_glow(False)
        except Exception:
            pass

    def _has_running_worker(self):
        if self._queue_ctrl is not None and self._queue_ctrl.is_busy():
            return True
        if self._strategy_ctrl is not None and self._strategy_ctrl.has_running_worker():
            return True
        try:
            from ui.bridge.worker import StartupCleanupWorker
            for w in self._worker_pool:
                if isinstance(w, StartupCleanupWorker):
                    try:
                        if w.isRunning():
                            return True
                    except RuntimeError:
                        pass
        except Exception:
            pass
        return False

    def _on_running_progressed(self, stage, current, total):
        try:
            text = (
                f"{stage} ({current}/{total})"
                if total > 0 else str(stage)
            )
            self.tasks_view.set_running_stage(text)
        except Exception:
            pass

    def _on_interactor_notified(self, level, message):
        try:
            self.logs_view.append_log(
                str(level), str(message), source="pipeline",
            )
        except Exception:
            pass
        _msg_str = str(message or "").rstrip()
        if _msg_str.endswith((": dedup", "：dedup")):
            self._queue_ctrl.refresh_literature_from_interim()

    def _on_interactor_progressed(self, stage, current, total):
        if not stage:
            return

        if stage != self._logs_progress_stage:
            self._logs_progress_stage = str(stage)
            self._logs_progress_milestone = 0
            return

        if total is None or total <= 0:
            return

        try:
            pct = int(current / total * 100)
        except (TypeError, ZeroDivisionError):
            return

        if pct >= 100:
            self._logs_progress_milestone = 100
            return
        crossed = [
            m for m in (25, 50, 75)
            if pct >= m > self._logs_progress_milestone
        ]
        if not crossed:
            return
        highest = max(crossed)
        self._logs_progress_milestone = highest
        try:
            self.logs_view.append_log(
                "info",
                f"{stage} 进度 {highest}%",
                source="pipeline",
            )
        except Exception:
            pass

    def _on_prompt_requested(
        self, interactor, text: str, choices: list,
    ) -> None:
        from PySide6.QtWidgets import QInputDialog
        if not choices:
            interactor.respond_prompt("")
            return
        items = [str(c) for c in choices]
        try:
            choice, ok = QInputDialog.getItem(
                self, "请选择", str(text or ""), items, 0, False,
            )
        except RuntimeError:
            interactor.respond_prompt("")
            return
        if not ok or not choice:
            interactor.respond_prompt("")
            return
        interactor.respond_prompt(str(choice))

    def _spawn_worker(self, worker):
        """统一启动 worker（持引用防 GC，并接 finished → _release_worker）。

        信号顺序依赖：finished_ok（run 内先 emit）先于 QThread.finished（run 返回后
        emit）。即使平台反转也不崩——deleteLater 延迟销毁 + _release_worker 不访问
        业务态。
        ⚠️ 若改 finished 为 DirectConnection 或在 finished_ok 前主动 deleteLater，
        必须复查此处。
        """
        self._worker_pool.append(worker)
        try:
            worker.finished.connect(
                lambda w=worker: self._release_worker(w)
            )
        except Exception:
            pass
        worker.start()

    def _release_worker(self, worker):
        try:
            if worker in self._worker_pool:
                self._worker_pool.remove(worker)
        except (ValueError, RuntimeError):
            pass
        try:
            worker.deleteLater()
        except RuntimeError:
            pass

    def _refresh_dashboard(self):
        records = self._library_ctrl._library_records
        if records is None:
            records = self._library_ctrl.load_library_records()

        if records is None or len(records) == 0:
            self.dashboard_view.set_stats({
                "total": "0",
                "fulltext_or_imported": "0",
                "retrieval_rate": "0.0",
            })
            self.dashboard_view.clear_recent_tasks()
            return

        total = int(len(records))

        final_status = records.get(
            "final_retrieval_status",
            pd.Series([""] * len(records), index=records.index),
        ).fillna("").astype(str)
        fulltext_wide = {"fulltext_pdf", "fulltext_xml", "fulltext_html"}
        fulltext_count = int(final_status.isin(fulltext_wide).sum())

        if "data_origin" not in records.columns:
            data_origin = pd.Series(
                ["retrieval"] * len(records), index=records.index,
            )
        else:
            data_origin = records["data_origin"].fillna(
                "retrieval"
            ).astype(str)

        imported_count = int((data_origin == "zotero_import").sum())
        fulltext_or_imported = fulltext_count + imported_count

        fulltext_narrow = {"fulltext_pdf", "fulltext_xml"}
        retrieval_mask = (data_origin == "retrieval")
        retrieval_total = int(retrieval_mask.sum())
        retrieval_fulltext = int(
            (retrieval_mask & final_status.isin(fulltext_narrow)).sum()
        )
        rate = (
            f"{retrieval_fulltext / retrieval_total * 100:.1f}"
            if retrieval_total > 0 else "0.0"
        )

        self.dashboard_view.set_stats({
            "total": f"{total:,}",
            "fulltext_or_imported": f"{fulltext_or_imported:,}",
            "retrieval_rate": rate,
        })

        try:
            recent = records.tail(20).iloc[::-1]
            rows = [
                self._library_ctrl._record_to_table_row(r)
                for _, r in recent.iterrows()
            ]
            self.dashboard_view.set_recent_tasks(rows)
        except Exception:
            self.dashboard_view.clear_recent_tasks()

    def views(self):
        return {
            PAGE_DASHBOARD: self.dashboard_view,
            PAGE_SEARCH: self.search_view,
            PAGE_LITERATURE: self.literature_view,
            PAGE_SCREENING: self.screening_view,
            PAGE_ZOTERO: self.zotero_view,
            PAGE_TASKS: self.tasks_view,
            PAGE_LOGS: self.logs_view,
            PAGE_SETTINGS: self.settings_view,
        }

    _BANNER_TOP_OVERLAP = 8
    _BANNER_HEIGHT = 36

    def _reposition_banner(self):
        if not hasattr(self, "notification_banner"):
            return
        if not hasattr(self, "background"):
            return
        if not hasattr(self, "header"):
            return
        if self.notification_banner.isHidden():
            return

        bg_w = self.background.width()
        header_h = self.header.height()
        if bg_w <= 0 or header_h <= 0:
            return

        y = header_h - self._BANNER_TOP_OVERLAP
        self.notification_banner.setGeometry(
            0, y, bg_w, self._BANNER_HEIGHT
        )
        self.notification_banner.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition_banner()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._reposition_banner)

    def goto_page(self, page_index, direction=None):
        if not (0 <= page_index < self.stack.count()):
            return
        current = self.stack.currentIndex()
        if direction is None:
            direction = 1 if page_index > current else -1
        self.stack.slide_to_index(page_index, direction=direction)
        # 同步侧栏选中
        self.sidebar.set_current(page_index)
        self._after_page_switched(page_index)

    def _after_page_switched(self, page_index):
        if page_index == PAGE_ZOTERO and self._zotero_controller is not None:
            from PySide6.QtCore import QTimer as _QTimer
            _QTimer.singleShot(0, self._trigger_zotero_refresh)

        if page_index in (PAGE_LITERATURE, PAGE_DASHBOARD):
            from PySide6.QtCore import QTimer as _QTimer
            _QTimer.singleShot(50, self._library_ctrl.maybe_reload_if_stale)

    def _trigger_zotero_refresh(self):
        if self._zotero_controller is None:
            return
        try:
            self._zotero_controller.on_page_entered()
        except Exception as exc:
            try:
                self._notify_status(
                    "warning", f"Zotero 页刷新失败：{exc}"
                )
            except Exception:
                pass

    def _connect_bridge(self, worker):
        try:
            from medlitflow import robustness
            robustness.register_signal_handler()
        except Exception:
            pass

        try:
            from ui.theme import loader as _qss_loader
            _qss_loader.register_log_callback(
                lambda level, msg: self.logs_view.append_log(level, msg)
            )
        except Exception:
            pass

        QTimer.singleShot(500, self._schedule_startup_cleanup)

        self._queue_ctrl.refresh_incomplete_runs()
        self._queue_ctrl.check_schema_compat()
        QTimer.singleShot(0, self._reposition_banner)
        self._config_ctrl.check_jcr_warning()
        self._library_ctrl.load_library_records()
        self._library_ctrl._refresh_literature()
        self._refresh_dashboard()

        try:
            self.literature_view.set_current_tab("retrieval")
        except Exception:
            pass

        try:
            self._library_ctrl._refresh_literature_tabs()
        except Exception:
            pass

        self._screening_ctrl.ensure_screening_migrated()
        self._screening_ctrl.refresh_screening()
        self._config_ctrl.load_config_into_settings_view()

        self._sync_run_state("idle")

        self._strategy_ctrl.refresh_completed_strategies()

        try:
            from PySide6.QtWidgets import QApplication as _QApp
            _screen = _QApp.primaryScreen()
            if _screen is not None:
                _h = _screen.availableGeometry().height()
                if _h < 800:
                    self.search_view._main_layout.setStretchFactor(
                        self.search_view.input_panel, 1
                    )
                    self.search_view._main_layout.setStretchFactor(
                        self.search_view.log_panel, 1
                    )
        except Exception:
            pass

        self._worker = worker

        if worker is None:
            return

        try:
            interactor = worker.get_interactor()
            self.search_view.bind_interactor(interactor)
        except Exception:
            pass

        try:
            worker.finished_ok.connect(self._on_worker_finished)
            worker.failed.connect(self._on_worker_failed)
            worker.interrupted.connect(self._on_worker_interrupted)
        except Exception:
            pass

    def _schedule_startup_cleanup(self) -> None:
        try:
            if self._has_running_worker():
                return
        except Exception:
            return

        try:
            config = self.get_config()
            library_root_raw = (config.get("library", {}) or {}).get(
                "root", ""
            )
            if not library_root_raw:
                return
            library_root = Path(library_root_raw)
            if not library_root.exists():
                return
        except Exception:
            return

        try:
            from ui.bridge.worker import StartupCleanupWorker
            worker = StartupCleanupWorker(library_root, parent=self)
            worker.finished_ok.connect(self._on_startup_cleanup_done)
            worker.failed.connect(self._on_startup_cleanup_failed)
            self._spawn_worker(worker)
        except Exception:
            pass

    def _on_startup_cleanup_done(self, result) -> None:
        if not isinstance(result, dict):
            return
        try:
            cleaned = int(result.get("cleaned", 0) or 0)
        except (TypeError, ValueError):
            cleaned = 0
        try:
            backfilled = int(result.get("backfilled", 0) or 0)
        except (TypeError, ValueError):
            backfilled = 0
        if cleaned <= 0 and backfilled <= 0:
            return
        try:
            self._library_ctrl.load_library_records(force=True)
            self._library_ctrl._refresh_literature_tabs()
            self._library_ctrl._refresh_literature()
        except Exception:
            pass

    def _on_startup_cleanup_failed(self, error) -> None:
        try:
            self.logs_view.append_log(
                "debug",
                f"启动清理跳过（{error}）",
                source="zotero",
            )
        except Exception:
            pass

    def _on_worker_finished(self, run_root):
        self._queue_ctrl.refresh_incomplete_runs()
        self._reposition_banner()

    def _on_worker_failed(self, error):
        self._queue_ctrl.refresh_incomplete_runs()
        self._reposition_banner()

    def _on_worker_interrupted(self):
        self._queue_ctrl.refresh_incomplete_runs()
        self._reposition_banner()