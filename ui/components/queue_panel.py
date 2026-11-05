"""运行队列面板（检索页多策略进度，3 列 DataTable）。

set_queue 全部置 pending；mark_running/mark_done 单行更新。任务名支持
type 前缀（[检索]/[重试]/[指标]）。add_title_widget 允许向标题行插入按钮。"""

from PySide6.QtWidgets import (
    QHBoxLayout,
    QVBoxLayout,
    QLabel,
)

from ui.theme.palette import Palette
from ui.components.glass_panel import GlassPanel
from ui.components.data_table import DataTable

class QueuePanel(GlassPanel):
    OBJECT_NAME = "QueuePanel"

    CONTENT_MARGINS = (16, 4, 16, 6)
    CONTENT_SPACING = 4

    TABLE_HEADERS = ["序号", "任务名", "状态"]
    TABLE_MONO_COLUMNS = {0}
    TABLE_HIGHLIGHT_COLUMNS = {2}
    TABLE_COLUMN_WIDTHS = {
        0: 80,     # 序号
        1: None,   # 任务名：Stretch
        2: 110,    # 状态
    }

    PANEL_HEIGHT = 120
    TABLE_ROW_HEIGHT = 32
    TITLE_ROW_HEIGHT = 24
    TABLE_HEADER_HEIGHT_COMPACT = 24

    STATUS_TEXT = {
        "pending":     "待运行",
        "processing":  "运行中",
        "success":     "完成",
        "error":       "失败",
        "warning":     "中断",
        "neutral":     "—",
    }

    TYPE_PREFIX = {
        "search":  "[检索] ",
        "retry":   "[重试] ",
        "metrics": "[指标] ",
    }

    def __init__(self, parent=None):
        super().__init__(self.OBJECT_NAME)

        self.setMaximumHeight(self.PANEL_HEIGHT)
        self.setMinimumHeight(80)
        self._items = []
        self._build_ui()

    def _build_ui(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(*self.CONTENT_MARGINS)
        v.setSpacing(self.CONTENT_SPACING)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)

        self.title_label = QLabel("运行队列")
        self.title_label.setFixedHeight(self.TITLE_ROW_HEIGHT)
        self.title_label.setStyleSheet(
            f"color: {Palette.TEXT_MUTED};"
            f"font-size: 12px;"
            f"letter-spacing: 1px;"
        )
        title_row.addWidget(self.title_label)
        title_row.addStretch()

        v.addLayout(title_row)

        self._title_row = title_row

        self.table = DataTable(
            headers=self.TABLE_HEADERS,
            mono_columns=self.TABLE_MONO_COLUMNS,
            highlight_columns=self.TABLE_HIGHLIGHT_COLUMNS,
            column_widths=self.TABLE_COLUMN_WIDTHS,
        )
        self.table.verticalHeader().setDefaultSectionSize(self.TABLE_ROW_HEIGHT)
        self.table.horizontalHeader().setStyleSheet(
            "QHeaderView::section {"
            "  background: rgba(255, 255, 255, 22);"
            f"  color: {Palette.CYAN};"
            "  border: none;"
            "  border-bottom: 1px solid rgba(2, 6, 16, 90);"
            "  border-top: 1px solid rgba(255, 255, 255, 50);"
            "  padding: 4px 8px;"
            "  font-weight: bold;"
            "  letter-spacing: 1px;"
            "  font-size: 12px;"
            "}"
        )

        self.table.setStyleSheet(
            "QTableView::item { font-size: 12px; }"
        )

        self.table.horizontalHeader().setFixedHeight(
            self.TABLE_HEADER_HEIGHT_COMPACT
        )
        v.addWidget(self.table, stretch=1)

    def add_title_widget(self, widget):
        self._title_row.addWidget(widget)

    def set_title(self, text):
        self.title_label.setText(str(text) if text else "运行队列")

    def _format_name(self, item):
        type_key = str(item.get("type", "search") or "search")
        prefix = self.TYPE_PREFIX.get(type_key, self.TYPE_PREFIX["search"])
        name = str(item.get("name", "") or "")
        return prefix + name

    def set_queue(self, items):
        if items is None:
            items = []
        self._items = [dict(it) for it in items if isinstance(it, dict)]

        total = len(self._items)
        rows = [
            [f"{i + 1}/{total}",
             self._format_name(it),
             self.STATUS_TEXT["pending"]]
            for i, it in enumerate(self._items)
        ]
        self.table.load_data(rows)

        if total:
            self.title_label.setText(f"运行队列（{total} 个）")
        else:
            self.title_label.setText("运行队列")

    def mark_running(self, index):
        self._update_row(index, "processing")

    def mark_done(self, index, status):
        mapping = {
            "ok":          "success",
            "failed":      "error",
            "interrupted": "warning",
        }
        semantic = mapping.get(status, "neutral")
        self._update_row(index, semantic)

    def clear(self):
        self._items = []
        self.table.clear_data()
        self.title_label.setText("运行队列")

    def _update_row(self, index, semantic_status):
        if index < 0 or index >= len(self._items):
            return
        total = len(self._items)
        it = self._items[index]
        row = [
            f"{index + 1}/{total}",
            self._format_name(it),
            self.STATUS_TEXT.get(semantic_status, "—"),
        ]
        self.table.update_row(index, row)