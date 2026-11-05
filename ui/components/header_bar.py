"""顶栏（Logo + 副标题 + 状态项 + 时钟）。

状态项从 HEADER_STATUS_ITEMS 读取；文字颜色由 QSS 控制，仅分隔点
用 Palette 内联（无 QSS 选择器）。
"""

from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QLabel
)

from PySide6.QtCore import Qt, QTimer

from datetime import datetime

from ui.config import (
    APP_NAME,
    HEADER_STATUS_ITEMS,
    HEADER_HEIGHT,
)
from ui.theme.palette import Palette

class HeaderBar(QWidget):
    def __init__(self):
        super().__init__()

        self.setObjectName("Header")
        self.setMinimumHeight(HEADER_HEIGHT)

        self._status_labels = {}

        self.init_ui()
        self.start_clock()

    def init_ui(self):

        layout = QHBoxLayout()
        layout.setContentsMargins(25, 14, 25, 14)
        layout.setSpacing(16)

        # ---- 左侧：Logo + 副标题 ----
        logo = QLabel(f"⚕ {APP_NAME}")
        logo.setObjectName("Logo")

        title = QLabel("Precision Research Navigator")
        title.setObjectName("HeaderTitle")

        layout.addWidget(logo)
        layout.addWidget(title)

        layout.addStretch()

        # ---- 右侧：状态项（从 config 读取） ----
        for i, (obj_name, text) in enumerate(HEADER_STATUS_ITEMS):
            if i > 0:
                layout.addWidget(self._separator())

            label = QLabel(text)
            label.setObjectName(obj_name)
            layout.addWidget(label)

            self._status_labels[obj_name] = label

        # ---- 分隔点 + 系统时间 ----
        layout.addWidget(self._separator())

        self.time_label = QLabel("--:--:--")
        self.time_label.setObjectName("SystemClock")
        self.time_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.time_label)

        self.setLayout(layout)

    def _separator(self):
        sep = QLabel("·")
        sep.setStyleSheet(
            f"color: {Palette.TEXT_MUTED};"
            f"font-size: 16px;"
            f"font-weight: bold;"
        )
        return sep

    def start_clock(self):
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_time)
        self.timer.start(1000)
        self.update_time()

    def update_time(self):
        now = datetime.now()
        self.time_label.setText(now.strftime("%H:%M:%S"))

    def set_status(self, key, text, color_key=None):
        label = self._status_labels.get(key)
        if label is None:
            return
        label.setText(text)
        if color_key is not None:
            color = getattr(Palette, color_key, Palette.TEXT_SECONDARY)
            label.setStyleSheet(f"color: {color};")