"""日志面板（QPlainTextEdit + 级别过滤 + 自动滚动 + 清空/复制）。

级别着色用 QTextCharFormat（不依赖 QSS）。内部维护 entry 列表供过滤切换时重建；QPlainTextEdit 内置 maximumBlockCount 自动裁剪。"""

from datetime import datetime

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QCheckBox,
    QPlainTextEdit,
    QApplication,
)

from PySide6.QtGui import (
    QTextCursor,
    QTextCharFormat,
)

from PySide6.QtCore import Qt

from ui.config import FONT_MONO_FAMILY, LOG_MAX_LINES
from ui.theme.palette import Palette, qcolor
from ui.components.glass_panel import GlassPanel

class LogPanel(GlassPanel):
    OBJECT_NAME = "LogPanel"
    OBJECT_NAME_TOOLBAR = "LogToolbar"
    OBJECT_NAME_VIEW = "LogView"

    LEVELS = ["debug", "info", "warning", "error"]
    LEVEL_ORDER = {"debug": 0, "info": 1, "warning": 2, "error": 3}

    LEVEL_LABELS = {
        "debug":   "DEBUG",
        "info":    "INFO",
        "warning": "WARN",
        "error":   "ERROR",
    }

    LEVEL_COLORS = {
        "debug":   Palette.TEXT_MUTED,
        "info":    Palette.TEXT_SECONDARY,
        "warning": Palette.RISK_ORANGE,
        "error":   Palette.DANGER_RED,
    }

    TIMESTAMP_COLOR = Palette.TEXT_MUTED
    SOURCE_COLOR = Palette.CYAN
    MESSAGE_COLOR = Palette.TEXT_PRIMARY

    _FORMAT_CACHE: dict[str, QTextCharFormat] = {}

    @classmethod
    def _get_format(cls, key: str, color: str) -> QTextCharFormat:
        fmt = cls._FORMAT_CACHE.get(key)
        if fmt is None:
            fmt = QTextCharFormat()
            fmt.setForeground(qcolor(color))
            cls._FORMAT_CACHE[key] = fmt
        return fmt

    MIN_HEIGHT = 80
    TOOLBAR_SPACING = 8
    TRIM_HYSTERESIS = 500
    CONTENT_MARGINS = (16, 10, 16, 10)
    CONTENT_SPACING = 8

    def __init__(
        self,
        max_lines=None,
        auto_scroll=True,
        level_filter=None,
        parent=None
    ):
        super().__init__(self.OBJECT_NAME)

        self.setMinimumHeight(self.MIN_HEIGHT)

        self._entries = []                 # list[dict]
        self._max_lines = max_lines if max_lines else LOG_MAX_LINES
        self._auto_scroll = bool(auto_scroll)
        self._level_filter = level_filter  # None 表示不过滤
        self._scroll_pending = False       # scroll 延后防重入标记

        self._build_ui()
        self._connect_signals()

        self.auto_scroll_check.setChecked(self._auto_scroll)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*self.CONTENT_MARGINS)
        layout.setSpacing(self.CONTENT_SPACING)

        layout.addLayout(self._build_toolbar())
        layout.addWidget(self._build_view(), stretch=1)

    def _build_toolbar(self):
        row = QHBoxLayout()
        row.setSpacing(self.TOOLBAR_SPACING)

        self.level_combo = QComboBox()
        self.level_combo.addItem("全部", None)
        for level in self.LEVELS:
            self.level_combo.addItem(
                self.LEVEL_LABELS[level],
                level
            )
        if self._level_filter in self.LEVELS:
            self.level_combo.setCurrentIndex(
                self.LEVELS.index(self._level_filter) + 1
            )
        row.addWidget(self.level_combo)

        self.auto_scroll_check = QCheckBox("自动滚动")
        self.auto_scroll_check.setCursor(Qt.PointingHandCursor)
        row.addWidget(self.auto_scroll_check)

        row.addStretch()

        self.clear_btn = QPushButton("清空")
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        row.addWidget(self.clear_btn)

        self.copy_btn = QPushButton("复制")
        self.copy_btn.setCursor(Qt.PointingHandCursor)
        row.addWidget(self.copy_btn)

        return row

    def _build_view(self):
        self.view = QPlainTextEdit()
        self.view.setObjectName(self.OBJECT_NAME_VIEW)
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.setUndoRedoEnabled(False)
        self.view.setStyleSheet(self._view_style())

        self.view.setMaximumBlockCount(self._max_lines)

        return self.view

    def _view_style(self):
        return (
            f"QPlainTextEdit {{"
            f"  background: rgba(255, 255, 255, 10);"
            f"  border: 1px solid rgba(255, 255, 255, 20);"
            f"  border-radius: 10px;"
            f"  padding: 8px 10px;"
            f"  color: {Palette.TEXT_PRIMARY};"
            f"  font-family: {FONT_MONO_FAMILY};"
            f"  font-size: 12px;"
            f"  selection-background-color: rgba(0, 133, 255, 120);"
            f"}}"
        )

    def _connect_signals(self):
        self.level_combo.currentIndexChanged.connect(
            self._on_level_filter_changed
        )
        self.auto_scroll_check.toggled.connect(self._on_auto_scroll_toggled)
        self.clear_btn.clicked.connect(self.clear)
        self.copy_btn.clicked.connect(self._copy_to_clipboard)

    def _on_level_filter_changed(self, index):
        level = self.level_combo.itemData(index)
        self.set_level_filter(level)

    def _on_auto_scroll_toggled(self, checked):
        self._auto_scroll = bool(checked)

    def _passes_filter(self, level):
        if self._level_filter is None:
            return True
        return (
            self.LEVEL_ORDER.get(level, 0)
            >= self.LEVEL_ORDER.get(self._level_filter, 0)
        )

    def _insert_entry(self, cursor, entry):
        cursor.insertText(
            f"[{entry['time']}] ",
            self._get_format("_ts", self.TIMESTAMP_COLOR),
        )

        level = entry["level"]
        level_color = self.LEVEL_COLORS.get(level, Palette.TEXT_SECONDARY)
        cursor.insertText(
            f"[{self.LEVEL_LABELS.get(level, level.upper())}] ",
            self._get_format(f"_lv_{level}", level_color),
        )

        if entry.get("source"):
            cursor.insertText(
                f"[{entry['source']}] ",
                self._get_format("_src", self.SOURCE_COLOR),
            )

        cursor.insertText(
            entry["message"] + "\n",
            self._get_format("_msg", self.MESSAGE_COLOR),
        )

    def _append_entry_to_view(self, entry):
        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.End)
        self._insert_entry(cursor, entry)

        if self._auto_scroll and not self._scroll_pending:
            self._scroll_pending = True
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self._scroll_to_bottom_deferred)


    def _scroll_to_bottom_deferred(self):
        self._scroll_pending = False
        self._scroll_to_bottom()

    def _rebuild_view(self):
        if len(self._entries) > self._max_lines:
            self._entries = self._entries[-self._max_lines:]
        self.view.clear()

        cursor = self.view.textCursor()
        cursor.beginEditBlock()
        try:
            for entry in self._entries:
                if self._passes_filter(entry["level"]):
                    cursor.movePosition(QTextCursor.End)
                    self._insert_entry(cursor, entry)
        finally:
            cursor.endEditBlock()

        if self._auto_scroll:
            self._scroll_to_bottom()

    def _scroll_to_bottom(self):
        try:
            scrollbar = self.view.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
        except RuntimeError:
            pass

    def append(self, level, message, source=""):
        level = (level or "info").lower()
        if level not in self.LEVEL_ORDER:
            level = "info"

        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "source": str(source) if source else "",
            "message": str(message) if message is not None else "",
        }
        self._entries.append(entry)

        if len(self._entries) > self._max_lines + self.TRIM_HYSTERESIS:
            self._entries = self._entries[-self._max_lines:]
            self._rebuild_view()
            return

        if self._passes_filter(level):
            self._append_entry_to_view(entry)

    def clear(self):
        self._entries.clear()
        self.view.clear()

    def set_auto_scroll(self, enabled):
        self._auto_scroll = bool(enabled)
        self.auto_scroll_check.setChecked(self._auto_scroll)

    def set_level_filter(self, level):
        if level is not None and level not in self.LEVEL_ORDER:
            level = None

        self._level_filter = level

        if level is None:
            target_index = 0
        else:
            target_index = self.LEVELS.index(level) + 1

        if self.level_combo.currentIndex() != target_index:
            self.level_combo.blockSignals(True)
            self.level_combo.setCurrentIndex(target_index)
            self.level_combo.blockSignals(False)

        self._rebuild_view()

    def set_max_lines(self, n):
        try:
            n = int(n)
        except (TypeError, ValueError):
            return
        if n <= 0:
            return

        self._max_lines = n
        self.view.setMaximumBlockCount(n)

        if len(self._entries) > n:
            self._entries = self._entries[-n:]
            self._rebuild_view()

    def _copy_to_clipboard(self):
        text = self.view.toPlainText()
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)

    def entry_count(self):
        return len(self._entries)

    def visible_text(self):
        return self.view.toPlainText()

    def level_filter(self):
        return self._level_filter

    def auto_scroll(self):
        return self._auto_scroll