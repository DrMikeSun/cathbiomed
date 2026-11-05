"""侧栏（Logo + 菜单按钮组 + 系统状态面板）。"""

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QButtonGroup,
    QFrame,
    QScrollArea,
    QSizePolicy,
)

from PySide6.QtCore import (
    Signal,
    Qt
)

from ui.config import (
    SIDEBAR_MENUS,
    SIDEBAR_WIDTH_EXPANDED,
)
from ui.theme.palette import Palette

class Sidebar(QWidget):
    page_changed = Signal(int)

    SYSTEM_BOX_HEIGHT = 44                    # 与菜单按钮等高
    INITIAL_STATUS_TEXT = "待检测"           # 初始文本
    INITIAL_STATUS_COLOR = "TEXT_MUTED"      # 初始颜色（灰）

    def __init__(self):
        super().__init__()

        self.setObjectName("Sidebar")

        self.current_index = 0

        self.button_group = QButtonGroup(self)
        self.button_group.setExclusive(True)
        self.buttons = []
        self._page_indices = []

        self._status_label = None
        self._status_dot = None

        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        logo = QLabel("⚕\nCath\nBiomed")
        logo.setObjectName("SideLogo")
        logo.setAlignment(Qt.AlignCenter)
        layout.addWidget(logo)

        menu_container = QWidget()
        menu_layout = QVBoxLayout(menu_container)
        menu_layout.setContentsMargins(0, 0, 0, 0)
        menu_layout.setSpacing(8)

        for btn_pos, (icon, name, page_index) in enumerate(SIDEBAR_MENUS):

            full_text = f"{icon}   {name}"

            btn = QPushButton(full_text)
            btn.setCheckable(True)
            btn.setMinimumHeight(44)
            btn.setObjectName("MenuButton")
            btn.setCursor(Qt.PointingHandCursor)

            self.button_group.addButton(btn, btn_pos)

            btn.clicked.connect(
                lambda checked=False, pi=page_index: self.change_page(pi)
            )

            self.buttons.append(btn)
            self._page_indices.append(page_index)

            menu_layout.addWidget(btn)

        menu_layout.addStretch()

        self._menu_scroll = QScrollArea()
        self._menu_scroll.setWidgetResizable(True)
        self._menu_scroll.setFrameShape(QFrame.NoFrame)
        self._menu_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._menu_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._menu_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        self._menu_scroll.setWidget(menu_container)
        layout.addWidget(self._menu_scroll, stretch=1)

        if self.buttons:
            self.buttons[0].setChecked(True)

        self.system_box = self._build_system_box()
        layout.addWidget(self.system_box)

        self.setLayout(layout)
        self.setFixedWidth(SIDEBAR_WIDTH_EXPANDED)

    def _build_system_box(self):
        box = QFrame()
        box.setObjectName("SystemBox")
        box.setFixedHeight(self.SYSTEM_BOX_HEIGHT)
        box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        hbox = QHBoxLayout(box)
        hbox.setContentsMargins(10, 0, 10, 0)
        hbox.setSpacing(6)

        dot_color = getattr(
            Palette, self.INITIAL_STATUS_COLOR, Palette.TEXT_MUTED
        )
        self._status_dot = QLabel("●")
        self._status_dot.setFixedWidth(14)
        self._status_dot.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._status_dot.setStyleSheet(
            f"color: {dot_color};"
            f"font-size: 10px;"
        )
        hbox.addWidget(self._status_dot)

        self._status_label = QLabel(self.INITIAL_STATUS_TEXT)
        self._status_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._status_label.setStyleSheet(
            f"color: {Palette.TEXT_SECONDARY};"
            f"font-size: 12px;"
            f"letter-spacing: 1px;"
        )
        hbox.addWidget(self._status_label, stretch=1)

        return box

    def change_page(self, page_index):
        self.current_index = page_index
        self.page_changed.emit(page_index)

    def set_current(self, page_index):
        if page_index in self._page_indices:
            btn_pos = self._page_indices.index(page_index)
            self.buttons[btn_pos].setChecked(True)
            self.current_index = page_index

    def set_system_status(self, key=None, text=None, color_key=None):
        if text is not None and self._status_label is not None:
            self._status_label.setText(str(text))

        if color_key is not None and self._status_dot is not None:
            color = getattr(Palette, color_key, Palette.TEXT_SECONDARY)
            self._status_dot.setStyleSheet(
                f"color: {color};"
                f"font-size: 10px;"
            )