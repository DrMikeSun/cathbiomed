"""Zotero 统计卡（比 StatCard 更紧凑：140×120 常规，120×80 compact）。

支持 selected（高亮光晕）/ disabled（"可引入"卡配置为空时灰显）。
点击通过 clicked(str) 回传 card_key，不触发业务。"""

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout

from ui.components.glass_panel import GlassPanel
from ui.theme.palette import Palette

class ZoteroStatCard(GlassPanel):
    OBJECT_NAME = "ZoteroStatCard"

    MIN_WIDTH = 140
    MIN_HEIGHT = 120
    COMPACT_WIDTH = 120
    COMPACT_HEIGHT = 80

    NAME_FONT_SIZE = 14
    VALUE_FONT_SIZE = 32
    COMPACT_NAME_FONT_SIZE = 12
    COMPACT_VALUE_FONT_SIZE = 22

    clicked = Signal(str)

    def __init__(self, card_key: str, name: str, parent=None):
        super().__init__(self.OBJECT_NAME)
        self._card_key = card_key
        self._name = name
        self._value: Any = 0
        self._disabled = False
        self._selected = False
        self._compact = False
        self._build_ui()
        self.setCursor(Qt.PointingHandCursor)

    def _build_ui(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(4)

        self._name_label = QLabel(self._name)
        self._name_label.setAlignment(Qt.AlignCenter)

        self._value_label = QLabel("—")
        self._value_label.setAlignment(Qt.AlignCenter)

        v.addStretch()
        v.addWidget(self._name_label)
        v.addWidget(self._value_label)
        v.addStretch()

        self._apply_style()

    def _apply_style(self):
        name_size = (
            self.COMPACT_NAME_FONT_SIZE if self._compact
            else self.NAME_FONT_SIZE
        )
        value_size = (
            self.COMPACT_VALUE_FONT_SIZE if self._compact
            else self.VALUE_FONT_SIZE
        )
        if self._disabled:
            color_name = Palette.TEXT_MUTED
            color_value = Palette.TEXT_MUTED
        else:
            color_name = Palette.TEXT_SECONDARY
            color_value = Palette.CYAN

        self._name_label.setStyleSheet(
            f"color: {color_name};"
            f" font-size: {name_size}px;"
            f" letter-spacing: 1px;"
        )
        self._value_label.setStyleSheet(
            f"color: {color_value};"
            f" font-size: {value_size}px;"
            f" font-weight: bold;"
        )

        if self._selected:
            self.set_glow(Palette.CYAN, alpha=90, blur=35)
        else:
            self.reset_shadow()

    def set_value(self, value):
        self._value = value
        if self._disabled:
            self._value_label.setText("—")
            return
        self._value_label.setText(str(value))

    def set_disabled(self, disabled: bool):
        self._disabled = bool(disabled)
        self.setCursor(
            Qt.ArrowCursor if self._disabled else Qt.PointingHandCursor
        )
        if self._disabled:
            self._value_label.setText("—")
        else:
            self._value_label.setText(str(self._value))
        self._apply_style()

    def set_selected(self, selected: bool):
        self._selected = bool(selected)
        self._apply_style()

    def set_compact(self, compact: bool):
        self._compact = bool(compact)
        if self._compact:
            self.setFixedSize(self.COMPACT_WIDTH, self.COMPACT_HEIGHT)
        else:
            self.setMinimumSize(self.MIN_WIDTH, self.MIN_HEIGHT)
            self.setMaximumSize(16777215, 16777215)
        self._apply_style()

    def card_key(self) -> str:
        return self._card_key

    def mousePressEvent(self, event):
        if self._disabled:
            super().mousePressEvent(event)
            return
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._card_key)
        super().mousePressEvent(event)