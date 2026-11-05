"""分组标题（主标题 + 副标题 + 右侧操作插槽）。

主标题复用 #PageTitle；副标题内联样式对齐左侧。"""

from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QLabel
)

from PySide6.QtCore import Qt

from ui.theme.palette import Palette

class SectionHeader(QWidget):
    OBJECT_NAME_TITLE = "PageTitle"
    OBJECT_NAME_SUBTITLE = "SectionSubtitle"

    TITLE_PADDING = 2

    def __init__(
        self,
        title="",
        subtitle="",
        parent=None
    ):
        super().__init__(parent)

        self._build_ui()

        if title:
            self.set_title(title)
        if subtitle:
            self.set_subtitle(subtitle)

    def _build_ui(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(0)

        self.title_label = QLabel("")
        self.title_label.setObjectName(self.OBJECT_NAME_TITLE)

        self.subtitle_label = QLabel("")
        self.subtitle_label.setObjectName(self.OBJECT_NAME_SUBTITLE)
        self.subtitle_label.setStyleSheet(self._subtitle_style())
        self.subtitle_label.setVisible(False)

        left.addWidget(self.title_label)

        subtitle_row = QHBoxLayout()
        subtitle_row.setContentsMargins(0, 0, 0, 0)
        subtitle_row.setSpacing(8)
        subtitle_row.addWidget(self.subtitle_label)
        subtitle_row.addStretch()
        self._subtitle_actions = QHBoxLayout()
        self._subtitle_actions.setContentsMargins(0, 0, 0, 0)
        self._subtitle_actions.setSpacing(6)
        self._subtitle_actions.setAlignment(Qt.AlignVCenter)
        subtitle_row.addLayout(self._subtitle_actions)
        left.addLayout(subtitle_row)

        outer.addLayout(left)
        outer.addStretch()

        self._actions = QHBoxLayout()
        self._actions.setContentsMargins(0, 0, 0, 0)
        self._actions.setSpacing(8)
        self._actions.setAlignment(Qt.AlignVCenter)

        outer.addLayout(self._actions)

    def _subtitle_style(self):
        return (
            f"color: {Palette.TEXT_SECONDARY};"
            f"font-size: 13px;"
            f"letter-spacing: 1px;"
            f"padding-left: {self.TITLE_PADDING}px;"
        )

    def set_title(self, title):
        self.title_label.setText(str(title))

    def set_subtitle(self, subtitle):
        text = str(subtitle or "")
        self.subtitle_label.setText(text)
        self.subtitle_label.setVisible(bool(text))

    def add_action(self, widget):
        self._actions.addWidget(widget)

    def add_stretch(self):
        self._actions.addStretch()

    def add_subtitle_action(self, widget):
        self._subtitle_actions.addWidget(widget)

    def title(self):
        return self.title_label.text()

    def subtitle(self):
        return self.subtitle_label.text()