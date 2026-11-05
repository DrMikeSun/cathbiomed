"""工具栏（QWidget + QHBoxLayout）。"""

from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QPushButton,
    QSizePolicy
)

from PySide6.QtCore import Qt

class Toolbar(QWidget):
    OBJECT_NAME = "Toolbar"
    OBJECT_NAME_PRIMARY = "PrimaryCTA"

    DEFAULT_SPACING = 8

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setObjectName(self.OBJECT_NAME)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        self._build_ui()

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(self.DEFAULT_SPACING)
        layout.setAlignment(Qt.AlignVCenter)

        self._layout = layout

    def add_widget(self, widget):
        self._layout.addWidget(widget)
        return widget

    def add_button(self, text, primary=False, on_click=None):
        btn = QPushButton(str(text))
        btn.setCursor(Qt.PointingHandCursor)

        if primary:
            btn.setObjectName(self.OBJECT_NAME_PRIMARY)

        if on_click is not None:
            btn.clicked.connect(on_click)

        self._layout.addWidget(btn)
        return btn

    def add_stretch(self):
        self._layout.addStretch(1)

    def clear(self):
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

    def count(self):
        return self._layout.count()

    def buttons(self):
        result = []
        for i in range(self._layout.count()):
            w = self._layout.itemAt(i).widget()
            if isinstance(w, QPushButton):
                result.append(w)
        return result