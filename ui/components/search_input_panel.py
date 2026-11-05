"""检索输入面板（单输入框 + 模式选择 + 开始运行）。

多检索式：分号（; / ；）或换行分隔。strategy_name 由 MainWindow 派生。
智能设计按钮 v1.5 禁用"。"""

from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
    QComboBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
)

from PySide6.QtCore import Qt, Signal

from ui.theme.palette import Palette
from ui.components.glass_panel import GlassPanel

class SearchInputPanel(GlassPanel):
    OBJECT_NAME = "SearchInputPanel"

    CONTENT_MARGINS = (24, 16, 24, 8)
    CONTENT_SPACING = 8
    ROW_SPACING = 8

    EXPANDED_MIN_HEIGHT = 170
    COLLAPSED_HEIGHT = 120
    BOTTOM_ROW_HEIGHT = 42
    MODE_COMBO_MIN_WIDTH = 120
    RUN_BTN_MIN_WIDTH = 120

    MODE_INTENSIVE = "intensive"
    MODE_SCAN = "scan"
    MODE_LABELS = [
        ("精读（intensive）", MODE_INTENSIVE),
        ("泛读（scan）", MODE_SCAN),
    ]

    INPUT_PLACEHOLDER = "lupus AND biomarker; aspirin and Kawasaki disease"

    run_requested = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(self.OBJECT_NAME)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumHeight(self.EXPANDED_MIN_HEIGHT)

        self._collapsed = False
        self._build_ui()
        self._connect_signals()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*self.CONTENT_MARGINS)
        layout.setSpacing(self.CONTENT_SPACING)

        self.input_label = QLabel("检索式（多个用分号 ; 或换行分隔），连续词自动作为短语；支持中英文括号")
        self.input_label.setStyleSheet(
            f"color: {Palette.TEXT_MUTED};"
            f"font-size: 12px;"
            f"letter-spacing: 1px;"
        )
        self.input_label.setWordWrap(False)
        layout.addWidget(self.input_label)

        self.input_edit = QPlainTextEdit()
        self.input_edit.setPlaceholderText(self.INPUT_PLACEHOLDER)
        self.input_edit.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Minimum,
        )
        self.input_edit.setMinimumWidth(300)
        layout.addWidget(self.input_edit, stretch=1)

        bottom = QWidget()
        bottom.setFixedHeight(self.BOTTOM_ROW_HEIGHT)
        bottom.setLayout(self._build_bottom_row())
        layout.addWidget(bottom)

    def _build_bottom_row(self):
        row = QHBoxLayout()
        row.setSpacing(6)
        row.setContentsMargins(0, 0, 0, 0)

        # ---- 模式标签 ----
        mode_label = QLabel("模式")
        mode_label.setStyleSheet(
            f"color: {Palette.TEXT_MUTED};"
            f"font-size: 12px;"
            f"letter-spacing: 1px;"
        )
        row.addWidget(mode_label)

        # ---- 模式下拉 ----
        self.mode_combo = QComboBox()
        self.mode_combo.setMinimumWidth(self.MODE_COMBO_MIN_WIDTH)
        self.mode_combo.setCursor(Qt.PointingHandCursor)
        for text, mode in self.MODE_LABELS:
            self.mode_combo.addItem(text, mode)
        row.addWidget(self.mode_combo)

        row.addStretch()

        # ---- 智能设计（v1.5 禁用） ----
        self.smart_btn = QPushButton("智能设计")
        self.smart_btn.setCursor(Qt.PointingHandCursor)
        self.smart_btn.setEnabled(False)
        self.smart_btn.setToolTip("v1.6 将开放")
        row.addWidget(self.smart_btn)

        # ---- 开始运行 ----
        self.run_btn = QPushButton("开始运行")
        self.run_btn.setObjectName("PrimaryCTA")
        self.run_btn.setCursor(Qt.PointingHandCursor)
        self.run_btn.setMinimumWidth(self.RUN_BTN_MIN_WIDTH)
        row.addWidget(self.run_btn)

        return row

    def _connect_signals(self):
        self.run_btn.clicked.connect(self._on_run_clicked)

    def _on_run_clicked(self):
        if not self.is_ready():
            return
        self.run_requested.emit(self.run_config())

    def set_collapsed(self, collapsed):
        self._collapsed = bool(collapsed)
        self.input_label.setVisible(not collapsed)
        if collapsed:
            self.setMinimumHeight(90)
            self.setMaximumHeight(self.COLLAPSED_HEIGHT)
        else:
            self.setMinimumHeight(self.EXPANDED_MIN_HEIGHT)
            self.setMaximumHeight(16777215)

    def input_text(self) -> str:
        return self.input_edit.toPlainText()

    def pipeline_mode(self) -> str:
        return self.mode_combo.currentData() or self.MODE_INTENSIVE

    def run_config(self) -> dict:
        return {
            "queries_raw": self.input_text(),
            "pipeline_mode": self.pipeline_mode(),
        }

    def is_ready(self) -> bool:
        return bool(self.input_text().strip())

    def set_input_text(self, text) -> None:
        self.input_edit.setPlainText(str(text) if text else "")