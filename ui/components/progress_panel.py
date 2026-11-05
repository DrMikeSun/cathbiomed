"""任务进度面板（名称 + 状态徽章 + 进度条 + 阶段 + 耗时）。

set_progress(v)：v<0 进入 busy（QProgressBar 内置动画），否则 0-100。
set_task(dict) 批量填充；缺失键保持当前值不变（区别于 None）。"""

from PySide6.QtWidgets import (
    QLabel,
    QVBoxLayout,
    QHBoxLayout,
    QProgressBar,
)

from PySide6.QtCore import Qt

from ui.config import FONT_MONO_FAMILY
from ui.theme.palette import Palette
from ui.components.glass_panel import GlassPanel
from ui.components.status_badge import StatusBadge

class ProgressPanel(GlassPanel):
    # ---- objectName ----
    OBJECT_NAME = "ProgressPanel"
    OBJECT_NAME_NAME = "ProgressTaskName"
    OBJECT_NAME_STAGE = "ProgressStage"
    OBJECT_NAME_ELAPSED = "ProgressElapsed"

    # ---- 尺寸参数 ----
    MIN_HEIGHT = 48
    BAR_HEIGHT = 10
    ELAPSED_MIN_WIDTH = 80

    # ---- 内容边距 ----
    CONTENT_MARGINS = (20, 16, 20, 16)
    CONTENT_SPACING = 10
    ROW_SPACING = 12

    # ---- 进度模式 ----
    BUSY = -1

    def __init__(
        self,
        name="",
        status="neutral",
        progress=0,
        stage="",
        elapsed="",
        parent=None
    ):
        super().__init__(self.OBJECT_NAME)

        self.setMinimumHeight(self.MIN_HEIGHT)

        self._build_ui()

        self.set_name(name)
        self.set_status(status)
        self.set_progress(progress)
        self.set_stage(stage)
        self.set_elapsed(elapsed)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*self.CONTENT_MARGINS)
        layout.setSpacing(self.CONTENT_SPACING)

        layout.addLayout(self._build_top_row())

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(self.BAR_HEIGHT)
        layout.addWidget(self.progress_bar)

        layout.addLayout(self._build_bottom_row())

    def _build_top_row(self):
        row = QHBoxLayout()
        row.setSpacing(self.ROW_SPACING)

        self.name_label = QLabel("—")
        self.name_label.setObjectName(self.OBJECT_NAME_NAME)
        self.name_label.setWordWrap(True)
        self.name_label.setStyleSheet(self._name_style())
        row.addWidget(self.name_label, stretch=1)

        self.status_badge = StatusBadge("neutral")
        row.addWidget(self.status_badge, alignment=Qt.AlignTop | Qt.AlignRight)

        return row

    def _build_bottom_row(self):
        row = QHBoxLayout()
        row.setSpacing(self.ROW_SPACING)

        self.stage_label = QLabel("")
        self.stage_label.setObjectName(self.OBJECT_NAME_STAGE)
        self.stage_label.setStyleSheet(self._stage_style())
        row.addWidget(self.stage_label, stretch=1)

        self.elapsed_label = QLabel("")
        self.elapsed_label.setObjectName(self.OBJECT_NAME_ELAPSED)
        self.elapsed_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.elapsed_label.setMinimumWidth(self.ELAPSED_MIN_WIDTH)
        self.elapsed_label.setStyleSheet(self._elapsed_style())
        row.addWidget(self.elapsed_label)

        return row

    def _name_style(self):
        return (
            f"color: {Palette.TEXT_PRIMARY};"
            f"font-size: 14px;"
            f"font-weight: bold;"
            f"letter-spacing: 1px;"
        )

    def _stage_style(self):
        return (
            f"color: {Palette.TEXT_SECONDARY};"
            f"font-size: 12px;"
        )

    def _elapsed_style(self):
        return (
            f"color: {Palette.TEXT_SECONDARY};"
            f"font-family: {FONT_MONO_FAMILY};"
            f"font-size: 12px;"
        )

    def set_name(self, name):
        text = str(name).strip() if name else ""
        self.name_label.setText(text or "—")

    def set_status(self, status):
        self.status_badge.set_status(status)

    def set_progress(self, value):
        try:
            v = int(value)
        except (TypeError, ValueError):
            v = 0

        if v < 0:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(max(0, min(100, v)))

    def set_stage(self, text):
        self.stage_label.setText(str(text) if text else "")

    def set_elapsed(self, text):
        self.elapsed_label.setText(str(text) if text else "")

    def set_task(self, data):
        if not isinstance(data, dict):
            return

        if "name" in data:
            self.set_name(data["name"])
        if "status" in data:
            self.set_status(data["status"])
        if "progress" in data:
            self.set_progress(data["progress"])
        if "stage" in data:
            self.set_stage(data["stage"])
        if "elapsed" in data:
            self.set_elapsed(data["elapsed"])

    def reset(self):
        self.set_name("")
        self.set_status("neutral")
        self.set_progress(0)
        self.set_stage("")
        self.set_elapsed("")

    def name(self):
        return self.name_label.text()

    def stage(self):
        return self.stage_label.text()

    def elapsed(self):
        return self.elapsed_label.text()

    def progress_value(self):
        if self.progress_bar.maximum() == 0 and self.progress_bar.minimum() == 0:
            return self.BUSY
        return self.progress_bar.value()