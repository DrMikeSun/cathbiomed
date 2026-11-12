"""已完成策略面板（采集任务页"已完成"Tab：DataTable + EmptyState）。

纯 UI，数据由 MainWindow 注入。菜单惰性持有为成员变量（防 QMenu 被GC 回收）。"""

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QMenu,
)

from PySide6.QtCore import Qt, Signal

from ui.config import FONT_MONO_FAMILY
from ui.theme.palette import Palette
from ui.components.glass_panel import GlassPanel
from ui.components.data_table import DataTable
from ui.components.empty_state import EmptyState

class CompletedStrategyPanel(GlassPanel):
    """ 已完成策略面板。 """
    OBJECT_NAME = "CompletedStrategyPanel"
    CONTENT_MARGINS = (24, 20, 24, 20)
    CONTENT_SPACING = 12
    ROW_SPACING = 8

    TABLE_HEADERS = [
        "策略名", "状态", "文献数", "指标覆盖(引/IF/机构)",
        "最近运行", "纳入/待定/未筛",
    ]
    TABLE_MONO_COLUMNS = {2, 3, 4}
    TABLE_HIGHLIGHT_COLUMNS = {1}
    TABLE_COLUMN_WIDTHS = {
        0: None,    # 策略名：Stretch
        1: 100,     # 状态
        2: 80,      # 文献数
        3: 130,     # 指标覆盖(C/J/I) —— 原"运行"列
        4: 160,     # 最近运行
        5: 140,     # 纳入/待定/未筛
    }

    TABLE_MIN_HEIGHT = 240
    STATUS_DISPLAY = {
        "collecting":            "采集中",
        "unscreened":            "未筛选",
        "partially_screened":    "部分筛选",
        "confirmed":             "已确认",
        "archived_unprocessed":  "已归档",
        "scan":                  "扫描（缓存）",
    }

    REFRESH_BTN_TEXT = "更新指标"
    REFRESH_BTN_RUNNING_TEXT = "更新中..."
    REFRESH_BTN_QUEUED_TEXT = "已排队"

    refresh_requested = Signal(str)
    report_requested = Signal(str)
    screening_requested = Signal(str)
    consolidate_requested = Signal(str)
    preview_requested = Signal(str)
    archive_requested = Signal(str)
    delete_requested = Signal(str)
    reopen_requested = Signal(str)
    data_reload_requested = Signal()

    retrieval_again_requested = Signal(str)
    view_scan_requested = Signal()
    clear_scan_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(self.OBJECT_NAME)

        self._strategies = []          # list[dict]
        self._more_menu = None         # QMenu，惰性构建
        self._refresh_state = "idle"   # idle / running / queued

        self._build_ui()
        self._connect_signals()
        self._update_buttons()

    def _build_ui(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(*self.CONTENT_MARGINS)
        v.setSpacing(self.CONTENT_SPACING)

        v.addLayout(self._build_top_row())
        v.addWidget(self._build_content(), stretch=1)
        v.addLayout(self._build_button_row())

    def _build_top_row(self):
        row = QHBoxLayout()
        row.setSpacing(self.ROW_SPACING)

        self.count_label = QLabel("暂无已完成策略")
        self.count_label.setStyleSheet(self._count_label_style())
        row.addWidget(self.count_label)

        row.addStretch()

        self.reload_btn = QPushButton("刷新")
        self.reload_btn.setCursor(Qt.PointingHandCursor)
        row.addWidget(self.reload_btn)

        return row

    def _build_content(self):
        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self.table = DataTable(
            headers=self.TABLE_HEADERS,
            mono_columns=self.TABLE_MONO_COLUMNS,
            highlight_columns=self.TABLE_HIGHLIGHT_COLUMNS,
            column_widths=self.TABLE_COLUMN_WIDTHS,
        )
        self.table.setMinimumHeight(self.TABLE_MIN_HEIGHT)
        self.table.setToolTip(
            "指标覆盖 = 有引用数 / 有影响因子 / 有机构数的记录数\n"
            "格式：引/IF/机构（全部为 0 时显示 —）\n"
            "例：42/38/35 表示 42 篇有引用数、38 篇有 IF、35 篇有机构数"
        )
        v.addWidget(self.table, stretch=1)

        self.empty_state = EmptyState(
            title="暂无已完成策略",
            subtitle="策略完成筛选后会自动出现在这里",
            symbol="★",
        )
        self.empty_state.setVisible(False)
        v.addWidget(self.empty_state, stretch=1)

        return container

    def _build_button_row(self):
        row = QHBoxLayout()
        row.setSpacing(self.ROW_SPACING)

        row.addStretch()

        self.view_scan_btn = QPushButton("查看报告")
        self.view_scan_btn.setCursor(Qt.PointingHandCursor)
        self.view_scan_btn.setVisible(False)
        row.addWidget(self.view_scan_btn)

        self.retrieval_again_btn = QPushButton("再次检索")
        self.retrieval_again_btn.setObjectName("PrimaryCTA")
        self.retrieval_again_btn.setCursor(Qt.PointingHandCursor)
        self.retrieval_again_btn.setVisible(False)
        row.addWidget(self.retrieval_again_btn)

        self.clear_scan_btn = QPushButton("删除")
        self.clear_scan_btn.setCursor(Qt.PointingHandCursor)
        self.clear_scan_btn.setVisible(False)
        row.addWidget(self.clear_scan_btn)

        self.refresh_btn = QPushButton(self.REFRESH_BTN_TEXT)
        self.refresh_btn.setObjectName("PrimaryCTA")
        self.refresh_btn.setCursor(Qt.PointingHandCursor)
        row.addWidget(self.refresh_btn)

        self.report_btn = QPushButton("查看报告")
        self.report_btn.setCursor(Qt.PointingHandCursor)
        row.addWidget(self.report_btn)

        self.screening_btn = QPushButton("筛选中心")
        self.screening_btn.setCursor(Qt.PointingHandCursor)
        row.addWidget(self.screening_btn)

        self.more_btn = QPushButton("更多▾")
        self.more_btn.setCursor(Qt.PointingHandCursor)
        row.addWidget(self.more_btn)

        return row

    def _ensure_more_menu(self):
        if self._more_menu is not None:
            return
        self._more_menu = QMenu(self)
        self._more_menu.addAction("合并", self._on_consolidate)
        self._more_menu.addAction("预览归档", self._on_preview)
        self._more_menu.addSeparator()
        self._more_menu.addAction("归档为未处理", self._on_archive)
        self._more_menu.addAction("重开策略", self._on_reopen)
        self._more_menu.addSeparator()
        self._more_menu.addAction(
            "⚠ 删除策略", self._on_delete,
        )

    def _connect_signals(self):
        self.reload_btn.clicked.connect(
            lambda checked=False: self.data_reload_requested.emit()
        )
        self.refresh_btn.clicked.connect(self._on_refresh_clicked)
        self.report_btn.clicked.connect(self._on_report_clicked)
        self.screening_btn.clicked.connect(self._on_screening_clicked)
        self.more_btn.clicked.connect(self._on_more_clicked)
        self.table.row_clicked.connect(self._on_row_clicked)

        self.view_scan_btn.clicked.connect(
            lambda checked=False: self.view_scan_requested.emit()
        )
        self.retrieval_again_btn.clicked.connect(
            self._on_retrieval_again_clicked
        )
        self.clear_scan_btn.clicked.connect(
            lambda checked=False: self.clear_scan_requested.emit()
        )

        try:
            sel_model = self.table.selectionModel()
            if sel_model is not None:
                sel_model.selectionChanged.connect(
                    lambda *_args: self._update_buttons()
                )
        except Exception:
            pass

    def _on_refresh_clicked(self):
        s = self.selected_strategy()
        if s:
            self.refresh_requested.emit(s)

    def _on_report_clicked(self):
        s = self.selected_strategy()
        if s:
            self.report_requested.emit(s)

    def _on_screening_clicked(self):
        s = self.selected_strategy()
        if s:
            self.screening_requested.emit(s)

    def _on_more_clicked(self):
        s = self.selected_strategy()
        if not s:
            return
        self._ensure_more_menu()
        pos = self.more_btn.mapToGlobal(
            self.more_btn.rect().bottomLeft()
        )
        self._more_menu.exec(pos)

    def _on_row_clicked(self, _row):
        self._update_buttons()

    def _on_retrieval_again_clicked(self):
        s = self.selected_strategy()
        if s:
            self.retrieval_again_requested.emit(s)

    def _on_consolidate(self):
        s = self.selected_strategy()
        if s:
            self.consolidate_requested.emit(s)

    def _on_preview(self):
        s = self.selected_strategy()
        if s:
            self.preview_requested.emit(s)

    def _on_archive(self):
        s = self.selected_strategy()
        if s:
            self.archive_requested.emit(s)

    def _on_reopen(self):
        s = self.selected_strategy()
        if s:
            self.reopen_requested.emit(s)

    def _on_delete(self):
        s = self.selected_strategy()
        if s:
            self.delete_requested.emit(s)

    def set_strategies(self, items):
        if items is None:
            items = []

        self._strategies = [
            dict(r) for r in items if isinstance(r, dict)
        ]

        if not self._strategies:
            self.count_label.setText("暂无已完成策略")
            self.table.clear_data()
            self.table.setVisible(False)
            self.empty_state.setVisible(True)
            self._update_buttons()
            return

        self.count_label.setText(
            f"存在 {len(self._strategies)} 个已完成的策略"
        )

        rows = [self._strategy_to_row(r) for r in self._strategies]
        self.table.load_data(rows)
        self.table.setVisible(True)
        self.empty_state.setVisible(False)
        self._update_buttons()

    def selected_strategy(self):
        row = self.table.selected_row()
        if row < 0 or row >= len(self._strategies):
            return None
        return str(
            self._strategies[row].get("strategy_name", "") or ""
        ) or None

    def strategy_count(self):
        return len(self._strategies)

    def clear(self):
        self.set_strategies([])

    def _strategy_to_row(self, item):
        status = str(item.get("strategy_status", "") or "")
        status_text = self.STATUS_DISPLAY.get(status, "—")

        include_count = self._safe_int(item.get("include_count", 0))
        maybe_count = self._safe_int(item.get("maybe_count", 0))
        unscreened_count = self._safe_int(
            item.get("unscreened_count", 0)
        )

        return [
            str(item.get("strategy_name", "") or ""),
            status_text,
            str(self._safe_int(item.get("record_count", 0))),
            str(item.get("metric_coverage", "—") or "—"),
            str(item.get("last_run_at", "") or "—"),
            f"{include_count}/{maybe_count}/{unscreened_count}",
        ]

    @staticmethod
    def _safe_int(value):
        try:
            if value is None:
                return 0
            return int(value)
        except (ValueError, TypeError):
            return 0

    def _update_buttons(self):
        row = self.table.selected_row()
        has_sel = 0 <= row < len(self._strategies)
        is_scan = False
        if has_sel:
            item = self._strategies[row]
            is_scan = bool(item.get("is_scan", False))

        scan_visible = has_sel and is_scan
        self.view_scan_btn.setVisible(scan_visible)
        self.clear_scan_btn.setVisible(scan_visible)

        self.retrieval_again_btn.setVisible(has_sel)
        self.retrieval_again_btn.setEnabled(has_sel)

        intensive_visible = has_sel and not is_scan
        self.refresh_btn.setVisible(intensive_visible)
        self.report_btn.setVisible(intensive_visible)
        self.screening_btn.setVisible(intensive_visible)
        self.more_btn.setVisible(intensive_visible)
        if self._refresh_state == "idle":
            self.refresh_btn.setEnabled(intensive_visible)
        else:
            self.refresh_btn.setEnabled(False)
        self.report_btn.setEnabled(intensive_visible)
        self.screening_btn.setEnabled(intensive_visible)
        self.more_btn.setEnabled(intensive_visible)

    def set_refresh_state(self, state):
        state = str(state or "idle")
        if state not in ("idle", "running", "queued"):
            state = "idle"

        self._refresh_state = state

        if state == "running":
            self.refresh_btn.setText(self.REFRESH_BTN_RUNNING_TEXT)
            self.refresh_btn.setCursor(Qt.ArrowCursor)
        elif state == "queued":
            self.refresh_btn.setText(self.REFRESH_BTN_QUEUED_TEXT)
            self.refresh_btn.setCursor(Qt.ArrowCursor)
        else:
            self.refresh_btn.setText(self.REFRESH_BTN_TEXT)
            self.refresh_btn.setCursor(Qt.PointingHandCursor)

        self._update_buttons()

    def _count_label_style(self):
        return (
            f"color: {Palette.TEXT_SECONDARY};"
            f"font-size: 13px;"
            f"letter-spacing: 1px;"
            f"font-family: {FONT_MONO_FAMILY};"
        )