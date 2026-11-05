"""列表页筛选栏（搜索框 + 动态下拉 + 筛选/重置）。

add/remove_filter / set_filter_options 动态管理。set_filter_visible
仅隐藏不删（避免 Tab 切换布局跳动）。视觉由 QSS 控制。
"""

from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QComboBox,
    QPushButton,
)

from PySide6.QtCore import Qt, Signal

from typing import Any

from ui.theme.palette import Palette
from ui.components.glass_panel import GlassPanel

class FilterBar(GlassPanel):

    # ---- objectName ----
    OBJECT_NAME = "FilterBar"
    OBJECT_NAME_SEARCH = "FilterSearch"
    OBJECT_NAME_COMBO = "FilterCombo"
    OBJECT_NAME_LABEL = "FilterLabel"

    # ---- 尺寸参数 ----
    MIN_HEIGHT = 56
    CONTENT_MARGINS = (12, 8, 12, 8)
    CONTENT_SPACING = 8
    SEARCH_WIDTH = 180
    COMBO_MIN_WIDTH = 80

    # ---- 特殊值：表示「全部」/ 未筛选 ----
    ALL_VALUE = None

    # ---- 信号 ----
    filter_requested = Signal(dict)
    reset_requested = Signal()

    def __init__(
        self,
        search_placeholder="搜索...",
        search_width=None,
        parent=None
    ):
        super().__init__(self.OBJECT_NAME)

        self.setMinimumHeight(self.MIN_HEIGHT)

        self._search_placeholder = search_placeholder
        self._search_width = search_width or self.SEARCH_WIDTH

        self._filters = {}

        self._build_ui()
        self._connect_signals()

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(*self.CONTENT_MARGINS)
        layout.setSpacing(self.CONTENT_SPACING)
        self._layout = layout

        # ---- 搜索框 ----
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName(self.OBJECT_NAME_SEARCH)
        self.search_edit.setPlaceholderText(self._search_placeholder)
        self.search_edit.setFixedWidth(self._search_width)
        self.search_edit.setClearButtonEnabled(True)
        layout.addWidget(self.search_edit)

        # ---- 弹性空间 ----
        layout.addStretch()

        # ---- 按钮 ----
        self.apply_btn = QPushButton("筛选")
        self.apply_btn.setObjectName("PrimaryCTA")
        self.apply_btn.setCursor(Qt.PointingHandCursor)
        layout.addWidget(self.apply_btn)

        self.reset_btn = QPushButton("重置")
        self.reset_btn.setCursor(Qt.PointingHandCursor)
        layout.addWidget(self.reset_btn)

    def _connect_signals(self):
        self.apply_btn.clicked.connect(self._on_apply)
        self.reset_btn.clicked.connect(self._on_reset)
        self.search_edit.returnPressed.connect(self._on_apply)

    def _next_insert_index(self):
        for i in range(self._layout.count()):
            item = self._layout.itemAt(i)
            if item is not None and item.spacerItem() is not None:
                return i
        return self._layout.count()

    def add_filter(self, key, label, options):
        if not key or key in self._filters:
            return
        if not options:
            return

        opts = list(options)
        all_label = "全部"
        if opts[0] != all_label:
            opts.insert(0, all_label)

        # ---- 文字标签（可选） ----
        label_widget = None
        if label:
            label_widget = QLabel(label)
            label_widget.setObjectName(self.OBJECT_NAME_LABEL)
            label_widget.setStyleSheet(self._label_style())
            self._layout.insertWidget(
                self._next_insert_index(), label_widget
            )

        # ---- 下拉框 ----
        combo = QComboBox()
        combo.setObjectName(self.OBJECT_NAME_COMBO)
        combo.setMinimumWidth(self.COMBO_MIN_WIDTH)
        combo.setCursor(Qt.PointingHandCursor)
        for opt in opts:
            combo.addItem(str(opt), str(opt) if opt != all_label else self.ALL_VALUE)

        self._layout.insertWidget(
            self._next_insert_index(), combo
        )

        self._filters[key] = (label_widget, combo, all_label)

    def remove_filter(self, key):
        item = self._filters.pop(key, None)
        if item is None:
            return

        label_widget, combo, _ = item

        try:
            self._layout.removeWidget(combo)
            combo.setParent(None)
            combo.deleteLater()
        except RuntimeError:
            pass

        if label_widget is not None:
            try:
                self._layout.removeWidget(label_widget)
                label_widget.setParent(None)
                label_widget.deleteLater()
            except RuntimeError:
                pass

    def clear_filters(self):
        for key in list(self._filters.keys()):
            self.remove_filter(key)

    def set_filter_options(self, key, options):
        item = self._filters.get(key)
        if item is None:
            return

        label_widget, combo, all_label = item

        current_value = combo.currentData()

        opts_normalized: list[tuple[str, Any]] = []
        for opt in (options or []):
            if isinstance(opt, tuple) and len(opt) == 2:
                display, value = opt
                opts_normalized.append((str(display), value))
            else:
                opts_normalized.append((
                    str(opt),
                    str(opt) if opt != all_label else self.ALL_VALUE,
                ))

        if not opts_normalized:
            opts_normalized = [(all_label, self.ALL_VALUE)]
        elif opts_normalized[0][0] != all_label:
            opts_normalized.insert(0, (all_label, self.ALL_VALUE))

        if (current_value is not None
                and not any(v == current_value for _, v in opts_normalized)):
            opts_normalized.insert(
                1, (f"（失效：{current_value}）", current_value)
            )

        # 重建选项（阻塞信号）
        combo.blockSignals(True)
        combo.clear()
        for display, value in opts_normalized:
            combo.addItem(display, value)

        # 恢复选中
        if current_value is not None:
            idx = combo.findData(current_value)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
        else:
            combo.setCurrentIndex(0)
        combo.blockSignals(False)

    def set_filter_visible(self, key, visible):
        item = self._filters.get(key)
        if item is None:
            return
        label_widget, combo, _ = item
        if label_widget is not None:
            label_widget.setVisible(visible)
        combo.setVisible(visible)

    def set_search_visible(self, visible: bool) -> None:
        self.search_edit.setVisible(bool(visible))

    def values(self):
        result = {}
        result["_search"] = self.search_edit.text().strip()

        for key, (_, combo, _) in self._filters.items():
            result[key] = combo.currentData()

        return result

    def search_text(self):
        return self.search_edit.text().strip()

    def set_values(self, data):
        if not isinstance(data, dict):
            return

        # 搜索框
        if "_search" in data:
            self.search_edit.setText(str(data["_search"] or ""))

        # 下拉框
        for key, value in data.items():
            if key == "_search":
                continue
            item = self._filters.get(key)
            if item is None:
                continue
            _, combo, _ = item
            idx = combo.findData(value)
            if idx >= 0:
                combo.setCurrentIndex(idx)

    def reset(self):
        self.search_edit.clear()
        for _, combo, _ in self._filters.values():
            combo.setCurrentIndex(0)

    def _on_apply(self):
        self.filter_requested.emit(self.values())

    def _on_reset(self):
        self.reset()
        self.reset_requested.emit()

    def _label_style(self):
        return (
            f"color: {Palette.TEXT_MUTED};"
            f"font-size: 11px;"
            f"letter-spacing: 1px;"
        )

    def filter_keys(self):
        return list(self._filters.keys())

    def filter_count(self):
        return len(self._filters)