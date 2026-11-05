"""检索视图（PAGE_SEARCH：输入 + 队列 + 进度 + 日志）。

唯一订阅 UIBridgeInteractor 的视图；worker 由 MainWindow 构造。"""

from PySide6.QtWidgets import (
    QWidget,
    QPushButton,
    QVBoxLayout,
)

from PySide6.QtCore import Qt, Signal, QTimer

from ui.components.section_header import SectionHeader
from ui.components.search_input_panel import SearchInputPanel
from ui.components.queue_panel import QueuePanel
from ui.components.progress_panel import ProgressPanel
from ui.components.log_panel import LogPanel

class SearchView(QWidget):
    CONTENT_MARGINS = (40, 28, 40, 28)
    CONTENT_SPACING = 16

    TITLE = "检索"
    SUBTITLE = "设计检索式并启动采集管线"

    BUFFER_INTERVAL_MS = 100

    STATE_IDLE = "idle"
    STATE_RUNNING = "running"
    STATE_CANCELLING = "cancelling"
    STATE_FINISHED = "finished"
    STATE_FAILED = "failed"
    STATE_INTERRUPTED = "interrupted"

    run_requested = Signal(dict)
    cancel_requested = Signal()
    abandon_requested = Signal(str)
    smart_index_requested = Signal(str)
    continue_queue_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        # ---- 桥接层 ----
        self._interactor = None

        # ---- 运行状态 ----
        self._run_state = self.STATE_IDLE
        self._current_run_name = ""

        # ---- 日志节流缓冲 ----
        self._pending_logs = []
        self._log_timer = QTimer(self)
        self._log_timer.setSingleShot(True)
        self._log_timer.setInterval(self.BUFFER_INTERVAL_MS)
        self._log_timer.timeout.connect(self._flush_logs)

        self._build_ui()
        self._connect_signals()
        self.set_toolbar_visible(False)
        self._update_buttons()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*self.CONTENT_MARGINS)
        layout.setSpacing(8)

        # ---- 标题 ----
        self.header = SectionHeader(self.TITLE, self.SUBTITLE)
        layout.addWidget(self.header)

        # ---- 输入面板 ----
        self.input_panel = SearchInputPanel()
        layout.addWidget(self.input_panel, stretch=3)

        # ---- 运行队列 ----
        self.queue_panel = QueuePanel()
        self._build_inline_toolbar_buttons()
        layout.addWidget(self.queue_panel)

        # ---- 进度面板 ----
        layout.addWidget(self._build_status_block())

        # ---- 日志 ----
        self.log_panel = LogPanel()
        layout.addWidget(self.log_panel, stretch=1)

        self._main_layout = layout

    def _build_inline_toolbar_buttons(self):
        btn_style = (
            "QPushButton {"
            "  padding: 1px 8px;"
            "  font-size: 12px;"
            "  min-height: 0;"
            "}"
            "QPushButton:hover {"
            "  background: rgba(0, 133, 255, 55);"
            "}"
        )

        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.setCursor(Qt.PointingHandCursor)
        self._cancel_btn.setFixedHeight(24)
        self._cancel_btn.setStyleSheet(btn_style)
        self._cancel_btn.clicked.connect(
            lambda checked=False: self._on_cancel_clicked()
        )
        self.queue_panel.add_title_widget(self._cancel_btn)

        self._continue_btn = QPushButton("继续")
        self._continue_btn.setCursor(Qt.PointingHandCursor)
        self._continue_btn.setFixedHeight(24)
        self._continue_btn.setStyleSheet(btn_style)
        self._continue_btn.setToolTip(
            "继续执行剩余队列（当前无运行中任务时可用）"
        )
        self._continue_btn.clicked.connect(
            lambda checked=False: self.continue_queue_requested.emit()
        )
        self.queue_panel.add_title_widget(self._continue_btn)

        self._refresh_btn = QPushButton("刷新状态")
        self._refresh_btn.setCursor(Qt.PointingHandCursor)
        self._refresh_btn.setFixedHeight(24)
        self._refresh_btn.setStyleSheet(btn_style)
        self._refresh_btn.clicked.connect(
            lambda checked=False: self._update_buttons()
        )
        self.queue_panel.add_title_widget(self._refresh_btn)

    def set_toolbar_visible(self, visible: bool) -> None:
        self._cancel_btn.setVisible(bool(visible))
        self._continue_btn.setVisible(bool(visible))
        self._refresh_btn.setVisible(bool(visible))

    def _build_status_block(self):
        self.progress_panel = ProgressPanel()
        self.progress_panel.set_name("IDLE")
        return self.progress_panel

    def _connect_signals(self):
        self.input_panel.run_requested.connect(self._on_run_requested)

    def _on_run_requested(self, config):
        merged = dict(config or {})
        merged.setdefault("stage", None)
        merged.setdefault("resume", False)
        merged.setdefault("pipeline_mode", "intensive")
        self.run_requested.emit(merged)

    def _on_cancel_clicked(self):
        if self._run_state in (self.STATE_RUNNING, self.STATE_CANCELLING):
            self.cancel_requested.emit()
            self.set_run_state(self.STATE_CANCELLING)
            self.progress_panel.set_name("取消中...")
            self.progress_panel.set_stage(
                "等待 pipeline 响应中断（网络请求完成后生效）"
            )
            self.progress_panel.set_status("warning")
            self._append_log(
                "warning", "取消请求已发出，等待中断响应..."
            )

    def bind_interactor(self, interactor):
        if self._interactor is interactor:
            return
        self.unbind_interactor()
        if interactor is None:
            return

        self._interactor = interactor
        interactor.notified.connect(self._on_notified)
        interactor.progressed.connect(self._on_progressed)
        interactor.logged.connect(self._on_logged)
        interactor.started.connect(self._on_started)
        interactor.finished.connect(self._on_finished)
        interactor.failed.connect(self._on_failed)
        interactor.interrupted.connect(self._on_interrupted)

    def unbind_interactor(self):
        if self._interactor is None:
            return
        try:
            self._interactor.notified.disconnect(self._on_notified)
            self._interactor.progressed.disconnect(self._on_progressed)
            self._interactor.logged.disconnect(self._on_logged)
            self._interactor.started.disconnect(self._on_started)
            self._interactor.finished.disconnect(self._on_finished)
            self._interactor.failed.disconnect(self._on_failed)
            self._interactor.interrupted.disconnect(self._on_interrupted)
        except (RuntimeError, TypeError):
            pass
        self._interactor = None

    def _on_notified(self, level, message):
        self._append_log(level, message)
        try:
            self.progress_panel.set_stage(str(message))
        except Exception:
            pass

    def _on_progressed(self, stage, current, total):
        if total <= 0:
            self.progress_panel.set_progress(-1)
            self.progress_panel.set_stage(str(stage))
        else:
            pct = int(current / total * 100)
            self.progress_panel.set_progress(pct)
            self.progress_panel.set_stage(f"{stage} {current}/{total}")

    def _on_logged(self, message):
        self._append_log("debug", message)

    def _on_started(self):
        self.set_run_state(self.STATE_RUNNING)
        self.progress_panel.set_name("运行中")
        self.progress_panel.set_status("processing")
        self.progress_panel.set_progress(-1)  # busy
        self._append_log("info", "运行已启动")
        try:
            self.input_panel.set_collapsed(True)
            self._main_layout.setStretchFactor(self.input_panel, 0)
            self._main_layout.setStretchFactor(self.log_panel, 4)
        except Exception:
            pass
        
    def _on_finished(self, run_root):
        self.set_run_state(self.STATE_FINISHED)
        self.progress_panel.set_name(f"完成：{run_root}")
        self.progress_panel.set_status("success")
        self.progress_panel.set_progress(100)
        self.progress_panel.set_stage("")
        self._append_log("info", f"运行完成：{run_root}")
        try:
            self.input_panel.set_collapsed(False)
            self._main_layout.setStretchFactor(self.input_panel, 3)
            self._main_layout.setStretchFactor(self.log_panel, 1)
        except Exception:
            pass
        
    def _on_failed(self, error):
        self.set_run_state(self.STATE_FAILED)
        self.progress_panel.set_name(f"失败：{error}")
        self.progress_panel.set_status("error")
        self.progress_panel.set_progress(0)
        self.progress_panel.set_stage("")
        self._append_log("error", f"运行失败：{error}")
        try:
            self.input_panel.set_collapsed(False)
            self._main_layout.setStretchFactor(self.input_panel, 3)
            self._main_layout.setStretchFactor(self.log_panel, 1)
        except Exception:
            pass
        
    def _on_interrupted(self):
        self.set_run_state(self.STATE_INTERRUPTED)
        self.progress_panel.set_name("已中断")
        self.progress_panel.set_status("warning")
        self.progress_panel.set_progress(0)
        self.progress_panel.set_stage("")
        self._append_log("warning", "运行已中断")
        self._append_log(
            "info", "如需重试，请修改检索式后点击「开始运行」"
        )
        try:
            self.input_panel.set_collapsed(False)
            self._main_layout.setStretchFactor(self.input_panel, 3)
            self._main_layout.setStretchFactor(self.log_panel, 1)
        except Exception:
            pass    

    def _append_log(self, level, message, source=""):
        self._pending_logs.append((level, message, source))
        if not self._log_timer.isActive():
            self._log_timer.start()

    def _flush_logs(self):
        if not self._pending_logs:
            return
        for level, message, source in self._pending_logs:
            self.log_panel.append(level, message, source)
        self._pending_logs.clear()

    def flush(self):
        if self._log_timer.isActive():
            self._log_timer.stop()
        self._flush_logs()

    def set_run_state(self, state):
        self._run_state = state
        if state == self.STATE_IDLE:
            try:
                self.progress_panel.set_name("IDLE")
            except Exception:
                pass
        self._update_buttons()

    def set_current_run_name(self, name):
        self._current_run_name = str(name) if name else ""
        self._update_buttons()

    def current_run_name(self):
        return self._current_run_name

    def run_state(self):
        return self._run_state

    def _update_buttons(self):
        running = self._run_state == self.STATE_RUNNING
        cancelling = self._run_state == self.STATE_CANCELLING

        self._cancel_btn.setEnabled(running or cancelling)
        self._continue_btn.setEnabled(not running and not cancelling)

        try:
            self.input_panel.run_btn.setEnabled(
                not running and not cancelling
            )
        except (AttributeError, RuntimeError):
            pass