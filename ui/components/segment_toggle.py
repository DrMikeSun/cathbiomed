"""分段切换控件（单选，QButtonGroup）。"""

from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QPushButton,
    QButtonGroup,
    QSizePolicy,
)

from PySide6.QtCore import Qt, Signal

from ui.theme.palette import Palette, rgba_str

class SegmentToggle(QWidget):
    DEFAULT_HEIGHT = 32
    DEFAULT_PADDING_H = 14
    BUTTON_MIN_WIDTH = 80
    BORDER_RADIUS = 8
    FONT_SIZE = 13
    CONTENT_SPACING = 0

    changed = Signal(str)

    def __init__(
        self,
        items,
        button_height=None,
        button_padding_h=None,
        parent=None,
    ):
        super().__init__(parent)

        self.setObjectName("SegmentToggle")
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        self._items = list(items) if items else []
        self._keys = [key for _, key in self._items]
        self._buttons = {}       # key → QPushButton
        self._current_key = ""
        self._button_height = (
            int(button_height)
            if button_height is not None
            else self.DEFAULT_HEIGHT
        )
        self._button_padding_h = (
            int(button_padding_h)
            if button_padding_h is not None
            else self.DEFAULT_PADDING_H
        )

        self._build_ui()
        self._connect_signals()

        if self._keys:
            self._set_current_key_internal(self._keys[0])

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(self.CONTENT_SPACING)

        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(True)

        count = len(self._items)
        for index, (text, key) in enumerate(self._items):
            btn = QPushButton(str(text))
            btn.setCheckable(True)
            btn.setMinimumWidth(self.BUTTON_MIN_WIDTH)
            btn.setFixedHeight(self._button_height)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setProperty("segment_key", key)
            btn.setStyleSheet(self._segment_style(index, count))

            self._button_group.addButton(btn)
            layout.addWidget(btn)

            self._buttons[key] = btn

    def _segment_style(self, index, count):
        if count <= 1:
            r_tl = r_tr = r_br = r_bl = f"{self.BORDER_RADIUS}px"
        elif index == 0:
            r_tl = f"{self.BORDER_RADIUS}px"
            r_tr = "0"
            r_br = "0"
            r_bl = f"{self.BORDER_RADIUS}px"
        elif index == count - 1:
            r_tl = "0"
            r_tr = f"{self.BORDER_RADIUS}px"
            r_br = f"{self.BORDER_RADIUS}px"
            r_bl = "0"
        else:
            r_tl = r_tr = r_br = r_bl = "0"

        return (
            f"QPushButton {{"
            f"  background: rgba(255, 255, 255, 12);"
            f"  border: 1px solid rgba(255, 255, 255, 30);"
            f"  border-top-left-radius: {r_tl};"
            f"  border-top-right-radius: {r_tr};"
            f"  border-bottom-right-radius: {r_br};"
            f"  border-bottom-left-radius: {r_bl};"
            f"  color: {Palette.TEXT_SECONDARY};"
            f"  font-size: {self.FONT_SIZE}px;"
            f"  padding: 0 {self._button_padding_h}px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: {rgba_str(Palette.CYAN, 40)};"
            f"  color: {Palette.TEXT_ON_ACCENT};"
            f"}}"
            f"QPushButton:checked {{"
            f"  background: {rgba_str(Palette.CYAN, 70)};"
            f"  border: 1px solid {rgba_str(Palette.CYAN, 120)};"
            f"  color: {Palette.TEXT_ON_ACCENT};"
            f"  font-weight: bold;"
            f"}}"
            f"QPushButton:disabled {{"
            f"  color: {Palette.TEXT_MUTED};"
            f"  background: rgba(255, 255, 255, 6);"
            f"}}"
        )

    def _connect_signals(self):
        self._button_group.buttonClicked.connect(self._on_button_clicked)

    def _on_button_clicked(self, btn):
        key = btn.property("segment_key")
        if key is None:
            return
        key = str(key)
        if key == self._current_key:
            return
        self._current_key = key
        self.changed.emit(key)

    def current_key(self):
        return self._current_key

    def set_current_key(self, key):
        if key not in self._buttons:
            return
        if key == self._current_key:
            return
        self._set_current_key_internal(key)

    def set_item_text(self, key, text):
        btn = self._buttons.get(key)
        if btn is None:
            return

        if text is None or str(text) == "":
            return
        btn.setText(str(text))

    def _set_current_key_internal(self, key):
        self._current_key = key
        btn = self._buttons.get(key)
        if btn is not None:
            btn.setChecked(True)

    def set_item_enabled(self, key, enabled):
        btn = self._buttons.get(key)
        if btn is None:
            return
        btn.setEnabled(bool(enabled))

    def item_count(self):
        return len(self._items)

    def keys(self):
        return list(self._keys)