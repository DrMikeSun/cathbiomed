"""当前运行 run 面板（采集任务页次分区）。

纯会话内存（MainWindow set_running/clear 同步），不读磁盘；孤儿 run
归 IncompleteRunPanel。无进度条（进度在检索页展示）。"""

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
)

from PySide6.QtCore import Qt, Signal

from ui.config import FONT_MONO_FAMILY
from ui.theme.palette import Palette
from ui.components.glass_panel import GlassPanel

class RunningRunPanel(GlassPanel):
    OBJECT_NAME = "RunningRunPanel"

    PANEL_HEIGHT = 150
    CONTENT_MARGINS = (24, 16, 24, 16)
    CONTENT_SPACING = 8
    ROW_SPACING = 8

    pause_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(self.OBJECT_NAME)

        self.setFixedHeight(self.PANEL_HEIGHT)

        self._running = False
        self._run_name = ""
        self._strategy_name = ""
        self._stage = ""

        self._build_ui()
        self._connect_signals()
        self._refresh_ui()

    def _build_ui(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(*self.CONTENT_MARGINS)
        v.setSpacing(self.CONTENT_SPACING)

        v.addLayout(self._build_title_row())
        v.addLayout(self._build_info_row())
        v.addStretch()
        v.addLayout(self._build_button_row())

    def _build_title_row(self):
        row = QHBoxLayout()
        row.setSpacing(self.ROW_SPACING)

        self.title_label = QLabel("当前无正在运行的 run")
        self.title_label.setStyleSheet(self._title_style())
        row.addWidget(self.title_label)

        row.addStretch()
        return row

    def _build_info_row(self):
        row = QHBoxLayout()
        row.setSpacing(self.ROW_SPACING)

        self.run_name_label = QLabel("—")
        self.run_name_label.setStyleSheet(self._info_style())
        row.addWidget(self.run_name_label)

        row.addStretch()

        self.stage_label = QLabel("")
        self.stage_label.setStyleSheet(self._info_style())
        row.addWidget(self.stage_label)

        return row

    def _build_button_row(self):
        row = QHBoxLayout()
        row.setSpacing(self.ROW_SPACING)

        row.addStretch()

        self.pause_btn = QPushButton("暂停")
        self.pause_btn.setCursor(Qt.PointingHandCursor)
        row.addWidget(self.pause_btn)

        return row

    def _connect_signals(self):
        self.pause_btn.clicked.connect(
            lambda checked=False: self.pause_requested.emit()
        )

    def set_running(self, run_name, strategy_name="", stage=""):
        self._running = True
        self._run_name = str(run_name) if run_name else ""
        self._strategy_name = str(strategy_name) if strategy_name else ""
        self._stage = str(stage) if stage else ""
        self._refresh_ui()

    def set_stage(self, stage):
        self._stage = str(stage) if stage else ""
        self._refresh_ui()

    def clear(self):
        self._running = False
        self._run_name = ""
        self._strategy_name = ""
        self._stage = ""
        self._refresh_ui()

    def is_running(self):
        return self._running

    def _refresh_ui(self):
        if self._running:
            if self._strategy_name:
                self.title_label.setText(
                    f"当前正在运行：{self._strategy_name}"
                )
            else:
                self.title_label.setText("当前正在运行")
            self.run_name_label.setText(self._run_name or "—")
            self.stage_label.setText(self._stage or "")
            self.pause_btn.setEnabled(True)
        else:
            self.title_label.setText("当前无正在运行的 run")
            self.run_name_label.setText("—")
            self.stage_label.setText("")
            self.pause_btn.setEnabled(False)

    def _title_style(self):
        return (
            f"color: {Palette.TEXT_PRIMARY};"
            f"font-size: 14px;"
            f"font-weight: bold;"
            f"letter-spacing: 1px;"
        )

    def _info_style(self):
        return (
            f"color: {Palette.TEXT_SECONDARY};"
            f"font-size: 12px;"
            f"font-family: {FONT_MONO_FAMILY};"
        )