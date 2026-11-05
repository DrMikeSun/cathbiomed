"""采集任务视图（PAGE_TASKS：已完成 / 进行中双 Tab）。

已完成=CompletedStrategyPanel；进行中=IncompleteRunPanel + RunningRunPanel。
用户手动切过 Tab 后不再自动切回（_user_switched_tab）。"""

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QStackedWidget,
)
from PySide6.QtCore import Signal

from ui.components.section_header import SectionHeader
from ui.components.segment_toggle import SegmentToggle
from ui.components.completed_strategy_panel import CompletedStrategyPanel
from ui.components.incomplete_run_panel import IncompleteRunPanel
from ui.components.running_run_panel import RunningRunPanel

class TasksView(QWidget):
    strategy_refresh_requested = Signal(str)
    strategy_report_requested = Signal(str)
    strategy_screening_requested = Signal(str)
    strategy_consolidate_requested = Signal(str)
    strategy_preview_requested = Signal(str)
    strategy_archive_requested = Signal(str)
    strategy_delete_requested = Signal(str)
    strategy_reopen_requested = Signal(str)
    completed_reload_requested = Signal()

    tab_changed = Signal(str)

    run_resume_requested = Signal(str)
    run_abandon_requested = Signal(str)
    run_refresh_requested = Signal()
    retry_all_requested = Signal()

    pause_requested = Signal()

    CONTENT_MARGINS = (40, 28, 40, 28)
    CONTENT_SPACING = 16

    TITLE = "采集任务"
    SUBTITLE = "管理已完成与进行中的采集任务"

    TAB_COMPLETED = "completed"
    TAB_IN_PROGRESS = "in_progress"

    INDEX_COMPLETED = 0
    INDEX_IN_PROGRESS = 1

    def __init__(self, parent=None):
        super().__init__(parent)

        self._user_switched_tab = False
        self._suppress_segment_signal = False

        self._build_ui()
        self._connect_signals()
        self._set_default_tab_if_needed()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*self.CONTENT_MARGINS)
        layout.setSpacing(self.CONTENT_SPACING)

        self.header = SectionHeader(self.TITLE, self.SUBTITLE)
        self.segment_toggle = SegmentToggle([
            ("已完成", self.TAB_COMPLETED),
            ("进行中", self.TAB_IN_PROGRESS),
        ])
        self.header.add_action(self.segment_toggle)
        layout.addWidget(self.header)

        self._stack = QStackedWidget()
        layout.addWidget(self._stack, stretch=1)

        self.completed_panel = CompletedStrategyPanel()
        self._stack.addWidget(self.completed_panel)

        self._stack.addWidget(self._build_in_progress_page())
        self._stack.setCurrentIndex(self.INDEX_COMPLETED)

    def _build_in_progress_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(self.CONTENT_SPACING)

        self.incomplete_run_panel = IncompleteRunPanel()
        v.addWidget(self.incomplete_run_panel, stretch=1)

        self.running_run_panel = RunningRunPanel()
        v.addWidget(self.running_run_panel)

        return page

    def _connect_signals(self):
        self.segment_toggle.changed.connect(self._on_segment_changed)

        self.completed_panel.refresh_requested.connect(
            self.strategy_refresh_requested.emit
        )
        self.completed_panel.report_requested.connect(
            self.strategy_report_requested.emit
        )
        self.completed_panel.screening_requested.connect(
            self.strategy_screening_requested.emit
        )
        self.completed_panel.consolidate_requested.connect(
            self.strategy_consolidate_requested.emit
        )
        self.completed_panel.preview_requested.connect(
            self.strategy_preview_requested.emit
        )
        self.completed_panel.archive_requested.connect(
            self.strategy_archive_requested.emit
        )
        self.completed_panel.delete_requested.connect(
            self.strategy_delete_requested.emit
        )
        self.completed_panel.reopen_requested.connect(
            self.strategy_reopen_requested.emit
        )
        self.completed_panel.data_reload_requested.connect(
            self.completed_reload_requested.emit
        )
        self.incomplete_run_panel.resume_requested.connect(
            self.run_resume_requested.emit
        )
        self.incomplete_run_panel.abandon_requested.connect(
            self.run_abandon_requested.emit
        )
        self.incomplete_run_panel.refresh_requested.connect(
            self.run_refresh_requested.emit
        )
        self.incomplete_run_panel.retry_all_requested.connect(
            self.retry_all_requested.emit
        )
        self.running_run_panel.pause_requested.connect(
            self.pause_requested.emit
        )

    def _on_segment_changed(self, key):
        if self._suppress_segment_signal:
            return
        self._user_switched_tab = True
        self._apply_tab(key)
        self.tab_changed.emit(key)

    def _apply_tab(self, key):
        if key == self.TAB_COMPLETED:
            self._stack.setCurrentIndex(self.INDEX_COMPLETED)
        elif key == self.TAB_IN_PROGRESS:
            self._stack.setCurrentIndex(self.INDEX_IN_PROGRESS)

    def _set_default_tab_if_needed(self):
        if self._user_switched_tab:
            return

        has_active = (
            self.incomplete_run_panel.run_count() > 0
            or self.running_run_panel.is_running()
        )

        default_key = (
            self.TAB_IN_PROGRESS if has_active else self.TAB_COMPLETED
        )
        self._suppress_segment_signal = True
        try:
            self.segment_toggle.set_current_key(default_key)
        finally:
            self._suppress_segment_signal = False
        self._apply_tab(default_key)

    def _update_tab_badge(self):
        n = self.incomplete_run_panel.run_count()
        if self.running_run_panel.is_running():
            n += 1
        text = f"进行中（{n}）" if n > 0 else "进行中"
        try:
            self.segment_toggle.set_item_text(
                self.TAB_IN_PROGRESS, text
            )
        except Exception:
            pass

    def set_completed_strategies(self, items):
        self.completed_panel.set_strategies(items)

    def completed_strategy_count(self):
        return self.completed_panel.strategy_count()

    def selected_completed_strategy(self):
        return self.completed_panel.selected_strategy()

    def set_incomplete_runs(self, runs):
        self.incomplete_run_panel.set_runs(runs)
        self._set_default_tab_if_needed()
        self._update_tab_badge()

    def incomplete_run_count(self):
        return self.incomplete_run_panel.run_count()

    def selected_incomplete_run(self):
        return self.incomplete_run_panel.selected_run()

    def set_running(
        self, run_name, strategy_name="", stage="",
        auto_switch: bool = False,
    ):
        self.running_run_panel.set_running(
            run_name, strategy_name, stage,
        )
        if auto_switch and not self._user_switched_tab:
            self.switch_to_tab(self.TAB_IN_PROGRESS)
        self._update_tab_badge()

    def set_running_stage(self, stage):
        self.running_run_panel.set_stage(stage)

    def clear_running(self):
        self.running_run_panel.clear()
        self._set_default_tab_if_needed()
        self._update_tab_badge()

    def switch_to_tab(self, key):
        if key not in (self.TAB_COMPLETED, self.TAB_IN_PROGRESS):
            return
        self._suppress_segment_signal = True
        try:
            self.segment_toggle.set_current_key(key)
        finally:
            self._suppress_segment_signal = False
        self._apply_tab(key)

    def current_tab(self):
        return self.segment_toggle.current_key()