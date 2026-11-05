
"""筛选中心进度条（搜索框 + 进度 + 4 态统计 + 空间）。

纯 UI 不读盘；stats/storage 由外部注入（compute_progress_stats /compute_storage_stats）。"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
)
from PySide6.QtCore import Qt, Signal

from ui.config import FONT_MONO_FAMILY
from ui.theme.palette import Palette
from ui.components.glass_panel import GlassPanel

class ScreeningProgressBar(GlassPanel):
    OBJECT_NAME = "ScreeningProgressBar"

    CONTENT_MARGINS = (16, 12, 16, 12)
    CONTENT_SPACING = 6
    SEARCH_WIDTH = 240
    PROGRESS_HEIGHT = 10

    search_text_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(self.OBJECT_NAME)
        self._build_ui()
        self._connect_signals()
        self.set_stats({})
        self.set_storage({})

    def _build_ui(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(*self.CONTENT_MARGINS)
        v.setSpacing(self.CONTENT_SPACING)

        row1 = QHBoxLayout()
        row1.setSpacing(10)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索标题 / 作者...")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedWidth(self.SEARCH_WIDTH)
        row1.addWidget(self.search_edit)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(self.PROGRESS_HEIGHT)
        row1.addWidget(self.progress_bar, stretch=1)

        self.pct_label = QLabel("0%")
        self.pct_label.setStyleSheet(self._mono_style())
        self.pct_label.setFixedWidth(50)
        row1.addWidget(self.pct_label)

        row1.addSpacing(16)

        self.storage_label = QLabel("")
        self.storage_label.setStyleSheet(self._muted_style())
        row1.addWidget(self.storage_label)

        v.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(16)
        row2.addStretch()

        self._stat_labels: dict[str, QLabel] = {}
        for key, label in (
            ("include",    "纳入"),
            ("exclude",    "排除"),
            ("maybe",      "待定"),
            ("unscreened", "未筛"),
        ):
            row2.addWidget(self._make_stat_widget(key, label))

        self._total_label = QLabel("")
        self._total_label.setStyleSheet(self._muted_style())
        row2.addWidget(self._total_label)

        row2.addStretch()
        v.addLayout(row2)

    def _make_stat_widget(self, key, label_text):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)

        value = QLabel("0")
        value.setStyleSheet(self._stat_value_style())
        h.addWidget(value)

        name = QLabel(label_text)
        name.setStyleSheet(self._muted_style())
        h.addWidget(name)

        self._stat_labels[key] = value
        return w

    def _connect_signals(self):
        self.search_edit.textChanged.connect(
            lambda t: self.search_text_changed.emit(str(t or ""))
        )

    def set_stats(self, stats):
        stats = stats or {}
        total = int(stats.get("total", 0) or 0)
        decided = sum(
            int(stats.get(k, 0) or 0)
            for k in ("include", "exclude", "maybe")
        )
        pct = int(decided / total * 100) if total > 0 else 0
        self.progress_bar.setValue(pct)
        self.pct_label.setText(f"{pct}%")

        for key in ("include", "exclude", "maybe", "unscreened"):
            lbl = self._stat_labels.get(key)
            if lbl is not None:
                lbl.setText(str(int(stats.get(key, 0) or 0)))

        total_label = getattr(self, "_total_label", None)
        if total_label is not None:
            total_label.setText(f"（共 {total}）")

    def set_storage(self, storage):
        storage = storage or {}
        lib = float(storage.get("library_gb", 0) or 0)
        trash = float(storage.get("trash_gb", 0) or 0)
        self.storage_label.setText(
            f"库 {lib:.1f}G · 回收站 {trash:.1f}G"
        )

    def set_search_text(self, text):
        self.search_edit.setText(str(text or ""))

    def search_text(self):
        return self.search_edit.text().strip()

    def _mono_style(self):
        return (
            f"color: {Palette.CYAN};"
            f"font-family: {FONT_MONO_FAMILY};"
            f"font-size: 13px;"
            f"font-weight: bold;"
        )

    def _muted_style(self):
        return (
            f"color: {Palette.TEXT_MUTED};"
            f"font-size: 12px;"
            f"letter-spacing: 1px;"
        )

    def _stat_value_style(self):
        return (
            f"color: {Palette.CYAN};"
            f"font-family: {FONT_MONO_FAMILY};"
            f"font-size: 16px;"
            f"font-weight: bold;"
        )