"""顶部通知横幅（条件显示，不阻断操作）。

4 级语义：info / warning / error / success。dismissed 后按_dismissed_count 抑制同数量重复出现。"""

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
)

from PySide6.QtCore import Qt, Signal

from ui.theme.palette import Palette
from ui.components.glass_panel import GlassPanel

class NotificationBanner(GlassPanel):
    OBJECT_NAME = "NotificationBanner"
    radius = 10
    CONTENT_MARGINS = (6, 1, 6, 1)
    CONTENT_SPACING = 4
    ICON_WIDTH = 16
    DISMISS_BTN_SIZE = 18

    LEVEL_ICONS = {
        "info":    "●",
        "warning": "⚠",
        "error":   "✕",
        "success": "✓",
    }

    LEVEL_COLORS = {
        "info":    Palette.CYAN,
        "warning": Palette.RISK_ORANGE,
        "error":   Palette.DANGER_RED,
        "success": Palette.AI_GREEN,
    }

    clicked = Signal()
    dismissed = Signal()

    def __init__(self, parent=None):
        super().__init__(self.OBJECT_NAME, parent)

        self._level = "info"
        self._message = ""
        self._action_text = "查看"
        self._last_count = 0
        self._dismissed_count = 0

        self._build_ui()
        self._connect_signals()
        self.hide()

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(*self.CONTENT_MARGINS)
        layout.setSpacing(self.CONTENT_SPACING)

        self.icon_label = QLabel(self.LEVEL_ICONS["info"])
        self.icon_label.setFixedWidth(self.ICON_WIDTH)
        self.icon_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.icon_label)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setMinimumHeight(0)
        self._scroll.setMaximumHeight(16777215)
        self._scroll.setStyleSheet(self._scroll_style())

        self.message_label = QLabel("")
        self.message_label.setWordWrap(True)
        self.message_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.message_label.setTextFormat(Qt.PlainText)
        self.message_label.setStyleSheet(
            f"color: {Palette.TEXT_PRIMARY};"
            f"font-size: 12px;"
            f"letter-spacing: 0.5px;"
            f"background: transparent;"
            f"padding: 0;"
        )
        self._scroll.setWidget(self.message_label)
        layout.addWidget(self._scroll, stretch=1)

        self.action_btn = QPushButton(self._action_text)
        self.action_btn.setObjectName("PrimaryCTA")
        self.action_btn.setCursor(Qt.PointingHandCursor)
        self.action_btn.setFixedHeight(20)
        self.action_btn.setStyleSheet(
            f"QPushButton {{"
            f"  padding: 0 6px;"
            f"}}"
        )
        layout.addWidget(self.action_btn)

        self.dismiss_btn = QPushButton("✕")
        self.dismiss_btn.setFixedSize(
            self.DISMISS_BTN_SIZE, self.DISMISS_BTN_SIZE
        )
        self.dismiss_btn.setCursor(Qt.PointingHandCursor)
        self.dismiss_btn.setStyleSheet(self._dismiss_btn_style())
        layout.addWidget(self.dismiss_btn)

        self._apply_style()

    def _connect_signals(self):
        self.action_btn.clicked.connect(
            lambda checked=False: self.clicked.emit()
        )
        self.dismiss_btn.clicked.connect(
            lambda checked=False: self._on_dismiss()
        )

    def _on_dismiss(self):
        self._dismissed_count = self._last_count
        self.hide()
        self.dismissed.emit()

    def _apply_style(self):
        color = self.LEVEL_COLORS.get(self._level, Palette.CYAN)
        self.icon_label.setStyleSheet(
            f"color: {color};"
            f"font-size: 12px;"
            f"font-weight: bold;"
        )
        self.message_label.setStyleSheet(
            f"color: {Palette.TEXT_PRIMARY};"
            f"font-size: 12px;"
            f"letter-spacing: 0.5px;"
            f"background: transparent;"
            f"padding: 0;"
        )

        try:
            if self._level in ("info", "success"):
                self.reset_shadow()
            else:
                self.set_glow(color, alpha=90, blur=12)
        except Exception:
            pass

    def _scroll_style(self):
        return (
            f"QScrollArea {{"
            f"  background: transparent;"
            f"  border: none;"
            f"}}"
            f"QScrollBar:vertical {{"
            f"  width: 4px;"
            f"  background: transparent;"
            f"  margin: 0;"
            f"}}"
            f"QScrollBar::handle:vertical {{"
            f"  background: rgba(255, 255, 255, 60);"
            f"  border-radius: 2px;"
            f"  min-height: 6px;"
            f"}}"
            f"QScrollBar::handle:vertical:hover {{"
            f"  background: rgba(255, 255, 255, 110);"
            f"}}"
            f"QScrollBar::add-line:vertical,"
            f"QScrollBar::sub-line:vertical {{"
            f"  height: 0;"
            f"}}"
        )

    def _dismiss_btn_style(self):
        return (
            f"QPushButton {{"
            f"  background: transparent;"
            f"  border: 1px solid rgba(255, 255, 255, 40);"
            f"  border-radius: {self.DISMISS_BTN_SIZE // 2}px;"
            f"  color: {Palette.TEXT_SECONDARY};"
            f"  font-size: 11px;"
            f"  padding: 0;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: rgba(255, 255, 255, 30);"
            f"  color: {Palette.TEXT_PRIMARY};"
            f"}}"
        )

    def set_message(self, text, level="info", action_text="查看"):
        self._level = level if level in self.LEVEL_ICONS else "info"
        self._message = str(text) if text else ""
        self._action_text = str(action_text) if action_text else ""

        self.icon_label.setText(self.LEVEL_ICONS[self._level])
        self.message_label.setText(self._message)
        self.action_btn.setText(self._action_text)
        self.action_btn.setVisible(bool(self._action_text))

        self._apply_style()

        try:
            self._scroll.verticalScrollBar().setValue(0)
        except (AttributeError, RuntimeError):
            pass

    def set_incomplete_runs(self, count, action_text="查看"):
        try:
            n = int(count)
        except (TypeError, ValueError):
            n = 0

        n = max(0, n)
        self._last_count = n

        if n <= 0:
            self._dismissed_count = 0
            self.hide()
            return

        if n == self._dismissed_count:
            return

        self.set_message(
            f"存在 {n} 个未完成的 run",
            level="warning",
            action_text=action_text,
        )
        self.show_banner()

    def show_banner(self):
        self.show()

    def hide_banner(self):
        self.hide()

    def clear(self):
        self._message = ""
        self.message_label.setText("")
        self.hide()

    def message(self):
        return self._message

    def level(self):
        return self._level