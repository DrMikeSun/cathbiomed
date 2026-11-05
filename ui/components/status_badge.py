"""状态徽章（QLabel 内联样式，6 语义色：success/warning/error/
processing/info/neutral）。

未识别或 None 规范化为 neutral。set_status 清除文本覆盖；
set_status_and_text(s, t) 同时设置。"""

from PySide6.QtWidgets import QLabel, QSizePolicy
from PySide6.QtCore import Qt

from ui.theme.palette import Palette, rgba_str

class StatusBadge(QLabel):
    STATUS_COLORS = {
        "success":    "STATUS_SUCCESS",
        "warning":    "STATUS_WARNING",
        "error":      "STATUS_ERROR",
        "processing": "STATUS_PROCESSING",
        "info":       "STATUS_INFO",
        "neutral":    "TEXT_SECONDARY",
    }

    STATUS_TEXTS = {
        "success":    "成功",
        "warning":    "警告",
        "error":      "错误",
        "processing": "处理中",
        "info":       "信息",
        "neutral":    "—",
    }

    DEFAULT_STATUS = "neutral"

    BORDER_RADIUS = 6
    PADDING_V = 1
    PADDING_H = 6
    FONT_SIZE = 11
    LETTER_SPACING = 1

    BG_ALPHA = 40           # 背景透明度
    BORDER_ALPHA = 120      # 边框透明度

    def __init__(self, status=None, text=None, parent=None):
        super().__init__(parent)

        self._status = self._normalize_status(status)
        self._text_override = text

        self._build_ui()
        self._apply_text()
        self._apply_style()

    def _build_ui(self):
        self.setObjectName("StatusBadge")
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def _normalize_status(self, status):
        if not status:
            return self.DEFAULT_STATUS
        if status in self.STATUS_COLORS:
            return status
        return self.DEFAULT_STATUS

    def _resolve_color(self, status):
        key = self.STATUS_COLORS.get(
            status,
            self.STATUS_COLORS[self.DEFAULT_STATUS]
        )
        return getattr(Palette, key, Palette.TEXT_SECONDARY)

    def _apply_text(self):
        if self._text_override is not None:
            self.setText(str(self._text_override))
        else:
            self.setText(self.STATUS_TEXTS.get(self._status, self._status))

    def _apply_style(self):
        color = self._resolve_color(self._status)
        bg = rgba_str(color, self.BG_ALPHA)
        border = rgba_str(color, self.BORDER_ALPHA)

        self.setStyleSheet(
            f"background: {bg};"
            f"border: 1px solid {border};"
            f"border-radius: {self.BORDER_RADIUS}px;"
            f"color: {color};"
            f"padding: {self.PADDING_V}px {self.PADDING_H}px;"
            f"font-size: {self.FONT_SIZE}px;"
            f"font-weight: bold;"
            f"letter-spacing: {self.LETTER_SPACING}px;"
        )

    def set_status(self, status):
        self._status = self._normalize_status(status)
        self._text_override = None
        self._apply_text()
        self._apply_style()

    def set_text(self, text):
        self._text_override = text
        self._apply_text()

    def set_status_and_text(self, status, text=None):
        self._status = self._normalize_status(status)
        self._text_override = text
        self._apply_text()
        self._apply_style()

    def status(self):
        return self._status

    def text_value(self):
        return self.text()