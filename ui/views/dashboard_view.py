"""仪表盘视图（PAGE_DASHBOARD：工具栏 + 4 卡 + 最近文献表）。

所有按钮点击只发信号。数据经 set_stats / set_recent_tasks 注入。
tasks 卡光晕与 tag 分别由 set_running_glow / set_tasks_tag 控制。"""

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
)

from PySide6.QtCore import Signal

from ui.theme.palette import Palette
from ui.components.section_header import SectionHeader
from ui.components.toolbar import Toolbar
from ui.components.stat_card import StatCard
from ui.components.data_table import DataTable

class DashboardView(QWidget):
    create_requested = Signal()
    refresh_requested = Signal()
    export_requested = Signal()

    CONTENT_MARGINS = (40, 28, 40, 28)
    CONTENT_SPACING = 20
    CARDS_SPACING = 20

    CARD_DEFS = [
        ("total",                "总库",       "TOTAL", " 篇", False),
        ("fulltext_or_imported", "全文+导入",  "FULL",  " 篇", False),
        ("retrieval_rate",       "检索全文率", "RATE",  "%",   False),
        ("tasks",                "任务",       "TASK",  "",    True),
    ]

    TABLE_HEADERS = ["标题", "作者", "年份", "来源", "状态"]
    TABLE_MONO_COLUMNS = {2}
    TABLE_HIGHLIGHT_COLUMNS = {4}
    TABLE_COLUMN_WIDTHS = {
        0: None,   # 标题：Stretch
        1: 160,    # 作者
        2: 80,     # 年份
        3: 140,    # 来源
        4: 100,    # 状态
    }

    def __init__(self, parent=None):
        super().__init__(parent)

        self._cards = {}

        self._build_ui()
        self._connect_signals()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*self.CONTENT_MARGINS)
        layout.setSpacing(self.CONTENT_SPACING)

        # ---- 标题 ----
        self.header = SectionHeader(
            "文献采集驾驶舱",
            "总览采集管线的实时状态"
        )
        layout.addWidget(self.header)

        # ---- 工具栏 ----
        self.toolbar = Toolbar()
        self._refresh_btn = self.toolbar.add_button("刷新")
        self._create_btn = self.toolbar.add_button(
            "新建检索", primary=True
        )
        self.toolbar.add_stretch()
        self._export_btn = self.toolbar.add_button("导出")
        layout.addWidget(self.toolbar)

        # ---- 统计卡行 ----
        layout.addLayout(self._build_cards_row())

        # ---- 最近文献表 ----
        self.table = DataTable(
            headers=self.TABLE_HEADERS,
            mono_columns=self.TABLE_MONO_COLUMNS,
            highlight_columns=self.TABLE_HIGHLIGHT_COLUMNS,
            column_widths=self.TABLE_COLUMN_WIDTHS,
        )
        layout.addWidget(self.table, stretch=1)

    def _build_cards_row(self):
        row = QHBoxLayout()
        row.setSpacing(self.CARDS_SPACING)

        for key, title, tag, unit, glow in self.CARD_DEFS:
            card = StatCard(title, "—", tag, unit=unit, glow=glow)
            self._cards[key] = card
            row.addWidget(card, stretch=1)

        return row

    def _connect_signals(self):
        self._refresh_btn.clicked.connect(
            lambda checked=False: self.refresh_requested.emit()
        )
        self._create_btn.clicked.connect(
            lambda checked=False: self.create_requested.emit()
        )
        self._export_btn.clicked.connect(
            lambda checked=False: self.export_requested.emit()
        )

    def set_stats(self, data):
        if not isinstance(data, dict):
            return

        for key, value in data.items():
            card = self._cards.get(key)
            if card is not None:
                card.set_value(value)

    def set_recent_tasks(self, tasks):
        if tasks is None:
            tasks = []
        self.table.load_data(tasks)

    def clear_recent_tasks(self):
        self.table.clear_data()

    def set_running_glow(self, active, color=None):
        card = self._cards.get("tasks")
        if card is None:
            return
        try:
            if active:
                card.set_glow_color(
                    color if color is not None else Palette.CYAN
                )
                card.start_pulse()
            else:
                card.stop_pulse()
                card.reset_shadow()
        except Exception:
            pass

    def set_tasks_tag(self, text: str):
        card = self._cards.get("tasks")
        if card is None:
            return
        try:
            card.set_tag(str(text) if text else "")
        except Exception:
            pass

    def stats(self):
        return {
            key: card.value()
            for key, card in self._cards.items()
        }

    def task_count(self):
        return self.table.row_count()