"""运行日志页（SectionHeader + LogPanel；capture LogPanel 清空/复制按钮）。

视图缓存 _entries 与 LogPanel 同步（供按 run 裁剪 + 过滤切换重建）。"""

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
)

from PySide6.QtCore import Signal, QTimer

from ui.config import LOG_MAX_LINES
from ui.components.section_header import SectionHeader
from ui.components.log_panel import LogPanel

class LogsView(QWidget):
    clear_requested = Signal()
    copy_requested = Signal(str)

    CONTENT_MARGINS = (40, 28, 40, 28)
    CONTENT_SPACING = 20

    TITLE = "运行日志"
    SUBTITLE = "采集管线运行日志，可过滤、清空、复制"

    VALID_LEVELS = ("debug", "info", "warning", "error")

    BUFFER_INTERVAL_MS = 100
    RUN_THRESHOLD = 500           # 超此条数触发"按 run 裁剪"
    KEEP_RUNS_HIGH = 2            # 超阈值保留 run 数（前一个 + 当前）
    KEEP_RUNS_LOW = 20            # 未超阈值保留 run 数
    HARD_LIMIT = 5000
    MAX_ENTRIES = LOG_MAX_LINES

    TRIM_HYSTERESIS = 500

    def __init__(self, parent=None):
        super().__init__(parent)

        self._entries = []
        self._run_boundaries = []
        self._pending_entries = []

        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(self.BUFFER_INTERVAL_MS)
        self._flush_timer.timeout.connect(self._flush_pending)

        self._build_ui()
        self._connect_signals()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*self.CONTENT_MARGINS)
        layout.setSpacing(self.CONTENT_SPACING)

        self.header = SectionHeader(self.TITLE, self.SUBTITLE)
        layout.addWidget(self.header)

        self.log_panel = LogPanel()
        layout.addWidget(self.log_panel, stretch=1)

    def _connect_signals(self):
        """
        捕获 LogPanel 工具栏按钮的点击。

        LogPanel 内部已将 clear_btn.clicked 连接到 self.clear，
        将 copy_btn.clicked 连接到 self._copy_to_clipboard。
        本视图在其之后连接，两者都会执行：

        清空：
            1. LogPanel.clear() 清空面板自身
            2. LogsView._on_clear_clicked() 清空缓存并发信号

        复制：
            1. LogPanel._copy_to_clipboard() 复制文本到剪贴板
            2. LogsView._on_copy_clicked() 读取可见文本并发信号
        """
        self.log_panel.clear_btn.clicked.connect(self._on_clear_clicked)
        self.log_panel.copy_btn.clicked.connect(self._on_copy_clicked)

    def _on_clear_clicked(self):
        """
        用户点击 LogPanel 的「清空」按钮：
            - LogPanel 已经清空
            - 停止缓冲定时器并清空待 flush 队列
            - 同步清空视图内部缓存
            - 发出 clear_requested 信号
        """
        self._flush_timer.stop()
        self._pending_entries.clear()
        self._entries.clear()
        self._run_boundaries.clear()
        self.clear_requested.emit()

    def _on_copy_clicked(self):
        """
        用户点击 LogPanel 的「复制」按钮：
            - LogPanel 已经把文本复制到系统剪贴板
            - 读取当前可见文本（尊重过滤状态）并发出信号
        """
        text = self.log_panel.visible_text()
        self.copy_requested.emit(text)

    def append_log(self, level, message, source=""):
        level = (level or "info").lower()
        if level not in self.VALID_LEVELS:
            level = "info"

        entry = {
            "level": level,
            "message": str(message) if message is not None else "",
            "source": str(source) if source else "",
        }

        self._entries.append(entry)
        self._pending_entries.append(entry)

        if (self._run_boundaries and len(self._entries) > self.RUN_THRESHOLD) \
                or (len(self._entries) > self.MAX_ENTRIES):
            self._trim_by_runs()

        if self._pending_entries and not self._flush_timer.isActive():
            self._flush_timer.start()

    def set_logs(self, entries):
        self._flush_timer.stop()
        self._pending_entries.clear()
        self._entries.clear()
        self._run_boundaries.clear()
        self.log_panel.clear()

        if not entries:
            return

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            level = (entry.get("level") or "info").lower()
            if level not in self.VALID_LEVELS:
                level = "info"
            message = str(entry.get("message", ""))
            source = str(entry.get("source", ""))

            self._entries.append({
                "level": level,
                "message": message,
                "source": source,
            })
            self.log_panel.append(level, message, source)

    def clear_logs(self):
        self._flush_timer.stop()
        self._pending_entries.clear()
        self._entries.clear()
        self._run_boundaries.clear()
        self.log_panel.clear()

    def _flush_pending(self):
        if not self._pending_entries:
            return

        for entry in self._pending_entries:
            self.log_panel.append(
                entry["level"], entry["message"], entry["source"]
            )
        self._pending_entries.clear()

    def flush(self):
        if self._flush_timer.isActive():
            self._flush_timer.stop()
        self._flush_pending()

    def start_new_run(self, run_name=""):
        self.flush()
        self._run_boundaries.append(len(self._entries))
        self._trim_by_runs()

    def _trim_by_runs(self):
        if not self._run_boundaries:
            if len(self._entries) > self.MAX_ENTRIES:
                self._entries = self._entries[-self.MAX_ENTRIES:]
                self._rebuild_log_panel()
            return

        total = len(self._entries)
        keep_runs = (
            self.KEEP_RUNS_HIGH if total > self.RUN_THRESHOLD
            else self.KEEP_RUNS_LOW
        )

        if len(self._run_boundaries) > keep_runs:
            drop_count = len(self._run_boundaries) - keep_runs
            drop_index = self._run_boundaries[drop_count]

            self._entries = self._entries[drop_index:]
            self._run_boundaries = [
                b - drop_index
                for b in self._run_boundaries[drop_count:]
            ]
            self._rebuild_log_panel()

        if len(self._entries) > self.HARD_LIMIT:
            keep = self.HARD_LIMIT - self.TRIM_HYSTERESIS
            self._entries = self._entries[-keep:]
            self._run_boundaries = [0]
            self._rebuild_log_panel()

    def _rebuild_log_panel(self):
        self._pending_entries.clear()
        if self._flush_timer.isActive():
            self._flush_timer.stop()

        self.log_panel.clear()
        for entry in self._entries:
            self.log_panel.append(
                entry["level"], entry["message"], entry["source"]
            )

    def set_auto_scroll(self, enabled):
        self.log_panel.set_auto_scroll(enabled)

    def auto_scroll(self):
        return self.log_panel.auto_scroll()

    def set_level_filter(self, level):
        self.log_panel.set_level_filter(level)

    def level_filter(self):
        return self.log_panel.level_filter()

    def entry_count(self):
        return len(self._entries)

    def visible_text(self):
        return self.log_panel.visible_text()

    def entries(self):
        return [dict(e) for e in self._entries]