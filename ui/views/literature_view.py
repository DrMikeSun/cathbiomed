"""文献库视图（PAGE_LITERATURE：双 Tab + 筛选 + 表格 + 详情）。

单击状态机：首次打开面板 + 高亮；再次点同一行关闭；点不同行切换。
_detail_open 是内部逻辑状态（不依赖 QWidget.isVisible()，后者受父级影响）。

双 Tab 缓存 _retrieval_rows/_retrieval_cids / _zotero_import_rows；
切 Tab 不丢数据，切回由 main_window 重新注入或从缓存恢复。"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QAbstractItemView, QMenu,
)
from PySide6.QtCore import Qt, QTimer, Signal
from ui.theme.palette import Palette
from ui.components.section_header import SectionHeader
from ui.components.filter_bar import FilterBar
from ui.components.data_table import DataTable
from ui.components.detail_panel import DetailPanel
from ui.components.segment_toggle import SegmentToggle

class LiteratureView(QWidget):
    filter_requested = Signal(dict)
    reset_requested = Signal()
    row_clicked = Signal(int)
    detail_action_requested = Signal(str, dict)
    open_screening_requested = Signal()
    tab_changed = Signal(str)
    mark_noise_requested = Signal(list)
    mark_deleted_requested = Signal(list)
    restore_special_requested = Signal(list)
    hard_delete_requested = Signal(list)
    special_tab_changed = Signal(str)
    cleanup_orphans_requested = Signal()

    CONTENT_MARGINS = (40, 28, 40, 28)
    OUTER_SPACING = 8      # 垂直间距
    INNER_SPACING = 20      # 水平间距

    TITLE = "文献库"
    SUBTITLE = "管理已采集的文献，点击行查看详情，再次点击同一行关闭"

    TAB_RETRIEVAL = "retrieval"
    TAB_ZOTERO_IMPORT = "zotero_import"

    TABLE_HEADERS_RETRIEVAL = [
        "检索策略", "标题", "作者", "期刊", "年份", "DOI", "来源", "状态",
    ]
    TABLE_MONO_COLUMNS_RETRIEVAL = {4}
    TABLE_HIGHLIGHT_COLUMNS_RETRIEVAL = {7}
    TABLE_COLUMN_WIDTHS_RETRIEVAL = {
        0: 120,   # 检索策略
        1: 450,   # 标题
        2: 120,   # 作者
        3: 130,   # 期刊
        4: 60,    # 年份
        5: 150,   # DOI
        6: 180,   # 来源
        7: 90,    # 状态
    }

    TABLE_HEADERS_ZOTERO_IMPORT = [
        "标题", "作者", "期刊", "年份", "DOI", "zotero", "Zotero分类",
    ]
    TABLE_MONO_COLUMNS_ZOTERO_IMPORT = {3}
    TABLE_HIGHLIGHT_COLUMNS_ZOTERO_IMPORT = {6}
    TABLE_COLUMN_WIDTHS_ZOTERO_IMPORT = {
        0: 450,   # 标题
        1: 140,   # 作者
        2: 160,   # 期刊
        3: 70,    # 年份
        4: 180,   # DOI
        5: 80,    # zotero
        6: 140,   # Zotero分类
    }

    SEARCH_PLACEHOLDER = "搜索标题 / 作者 / DOI"
    FILTERS = [
        ("strategy", "策略", ["全部"]),
        ("source", "来源",
         ["含 PubMed", "含 PMC", "含 Europe PMC",
          "含 Crossref", "含 OpenAlex"]),
        ("year",   "年份",
         ["2025", "2024", "2023", "2022", "2021", "2020"]),
        ("status", "状态",
         ["未筛选", "已纳入", "待定", "已排除"]),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)

        self._current_row = -1
        self._pending_current_row = -1
        self._detail_open = False

        self._row_cids: list[str] = []
        self._retrieval_rows: list = []
        self._retrieval_cids: list[str] = []
        self._zotero_import_rows: list = []

        self._build_ui()
        self._connect_signals()
        self._apply_tab_visibility(self.TAB_RETRIEVAL)

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*self.CONTENT_MARGINS)
        outer.setSpacing(self.OUTER_SPACING)

        self.header = SectionHeader(self.TITLE, self.SUBTITLE)

        self._cleanup_orphans_btn = QPushButton("清理孤儿文献")
        self._cleanup_orphans_btn.setCursor(Qt.PointingHandCursor)
        self._cleanup_orphans_btn.setEnabled(False)
        self._cleanup_orphans_btn.setFixedHeight(22)
        self._cleanup_orphans_btn.setToolTip(
            "清理已删除策略残留的文献（模式R残留）"
        )
        self._cleanup_orphans_btn.clicked.connect(
            lambda checked=False: self.cleanup_orphans_requested.emit()
        )
        self.header.add_action(self._cleanup_orphans_btn)

        self._screening_btn = QPushButton("筛选中心")
        self._screening_btn.setObjectName("PrimaryCTA")
        self._screening_btn.setCursor(Qt.PointingHandCursor)
        self._screening_btn.setFixedHeight(22)
        self._screening_btn.clicked.connect(
            lambda checked=False: self.open_screening_requested.emit()
        )
        self.header.add_action(self._screening_btn)

        self._filter_count_label = QLabel("")
        self._filter_count_label.setStyleSheet(
            self._filter_count_style()
        )
        self._filter_count_label.setAlignment(
            Qt.AlignRight | Qt.AlignVCenter
        )
        self._filter_count_label.setVisible(False)
        self.header.add_subtitle_action(self._filter_count_label)

        outer.addWidget(self.header)

        self.main_toggle = SegmentToggle([
            ("📚 检索文献", self.TAB_RETRIEVAL),
            ("📥 Zotero 导入", self.TAB_ZOTERO_IMPORT),
        ])
        self.main_toggle.changed.connect(self._on_main_tab_changed)
        outer.addWidget(self.main_toggle)

        self.filter_bar = FilterBar(
            search_placeholder=self.SEARCH_PLACEHOLDER
        )
        for key, label, options in self.FILTERS:
            self.filter_bar.add_filter(key, label, options)
        outer.addWidget(self.filter_bar)

        self.special_toggle = SegmentToggle(
            [
                ("📚 全部", ""),
                ("🗑 检索噪声", "retrieval_noise"),
                ("⚠ 数据源删除", "deleted_from_source"),
            ],
            button_height=26,
            button_padding_h=6,
        )
        self.special_toggle.changed.connect(self._on_special_tab_changed)
        outer.addWidget(self.special_toggle)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(self.INNER_SPACING)

        self.table = DataTable(
            headers=self.TABLE_HEADERS_RETRIEVAL,
            mono_columns=self.TABLE_MONO_COLUMNS_RETRIEVAL,
            highlight_columns=self.TABLE_HIGHLIGHT_COLUMNS_RETRIEVAL,
            column_widths=self.TABLE_COLUMN_WIDTHS_RETRIEVAL,
            selection_mode=QAbstractItemView.ExtendedSelection,
        )
        bottom.addWidget(self.table, stretch=1)

        self._setup_context_menu()

        self.detail_panel = DetailPanel()
        bottom.addWidget(self.detail_panel)

        outer.addLayout(bottom, stretch=1)

    def _filter_count_style(self):
        return (
            f"color: {Palette.TEXT_SECONDARY};"
            f"font-size: 12px;"
            f"letter-spacing: 1px;"
        )

    def _connect_signals(self):
        self.filter_bar.filter_requested.connect(
            self.filter_requested.emit
        )
        self.filter_bar.reset_requested.connect(
            self.reset_requested.emit
        )
        self.table.row_clicked.connect(self._on_row_clicked)
        self.detail_panel.close_requested.connect(
            self._on_detail_close
        )
        self.detail_panel.action_requested.connect(
            self.detail_action_requested.emit
        )

        try:
            sel_model = self.table.selectionModel()
            if sel_model is not None:
                sel_model.currentChanged.connect(
                    self._on_table_current_changed
                )
        except Exception:
            pass

    def _on_table_current_changed(self, current, previous):
        if not self._detail_open:
            return
        if not current.isValid():
            return
        try:
            if len(self.table.selected_rows()) > 1:
                return
        except Exception:
            pass
        r = current.row()
        if r < 0 or r == self._current_row:
            return
        self._pending_current_row = r
        QTimer.singleShot(0, self._flush_pending_current)

    def _flush_pending_current(self):
        r = self._pending_current_row
        self._pending_current_row = -1
        if r < 0 or r == self._current_row:
            return
        if not self._detail_open:
            return
        self._current_row = r
        self.table.set_row_highlight(r)
        self.row_clicked.emit(r)

    def _on_row_clicked(self, r):
        self._pending_current_row = -1
        try:
            if len(self.table.selected_rows()) > 1:
                return
        except Exception:
            pass

        # ---- 命中已打开行 → 关闭 ----
        if self._detail_open and self._current_row == r:
            self.close_detail()
            return

        # ---- 记录当前行 + 设置高亮 ----
        self._current_row = r
        self.table.set_row_highlight(r)

        # ---- 若面板未打开，滑入 ----
        if not self._detail_open:
            self._detail_open = True
            self.detail_panel.slide_in()

        # ---- 通知父组件填充数据 ----
        self.row_clicked.emit(r)

    def _on_detail_close(self):
        self.close_detail()

    def _apply_tab_visibility(self, tab: str) -> None:
        is_retrieval = (tab == self.TAB_RETRIEVAL)
        self.special_toggle.setVisible(is_retrieval)
        self.filter_bar.setVisible(is_retrieval)

    def _on_main_tab_changed(self, key):
        tab = str(key or self.TAB_RETRIEVAL)
        if tab not in (self.TAB_RETRIEVAL, self.TAB_ZOTERO_IMPORT):
            tab = self.TAB_RETRIEVAL
        self._apply_tab_visibility(tab)
        self.close_detail()
        self.tab_changed.emit(tab)

    def current_tab(self) -> str:
        try:
            key = str(self.main_toggle.current_key() or self.TAB_RETRIEVAL)
        except Exception:
            return self.TAB_RETRIEVAL
        if key not in (self.TAB_RETRIEVAL, self.TAB_ZOTERO_IMPORT):
            return self.TAB_RETRIEVAL
        return key

    def set_current_tab(self, tab_key: str) -> None:
        if tab_key not in (self.TAB_RETRIEVAL, self.TAB_ZOTERO_IMPORT):
            return
        prev = self.current_tab()
        if tab_key == prev:
            return
        self.main_toggle.set_current_key(tab_key)
        self._apply_tab_visibility(tab_key)
        self.close_detail()
        if tab_key == self.TAB_RETRIEVAL:
            self._show_retrieval()
        else:
            self._show_zotero_import()
        self.tab_changed.emit(tab_key)

    def _apply_table_headers(self, tab: str) -> None:
        if tab == self.TAB_ZOTERO_IMPORT:
            self.table.set_headers(
                headers=self.TABLE_HEADERS_ZOTERO_IMPORT,
                mono_columns=self.TABLE_MONO_COLUMNS_ZOTERO_IMPORT,
                highlight_columns=self.TABLE_HIGHLIGHT_COLUMNS_ZOTERO_IMPORT,
                column_widths=self.TABLE_COLUMN_WIDTHS_ZOTERO_IMPORT,
            )
        else:
            self.table.set_headers(
                headers=self.TABLE_HEADERS_RETRIEVAL,
                mono_columns=self.TABLE_MONO_COLUMNS_RETRIEVAL,
                highlight_columns=self.TABLE_HIGHLIGHT_COLUMNS_RETRIEVAL,
                column_widths=self.TABLE_COLUMN_WIDTHS_RETRIEVAL,
            )

    def _show_retrieval(self) -> None:
        self._apply_table_headers(self.TAB_RETRIEVAL)
        self._row_cids = list(self._retrieval_cids)
        self.table.load_data(self._retrieval_rows)

    def _show_zotero_import(self) -> None:
        self._apply_table_headers(self.TAB_ZOTERO_IMPORT)
        self._row_cids = []
        self.table.load_data(self._zotero_import_rows)

    def set_literature(self, data, cids=None):
        self.set_retrieval_records(data, cids)

    def set_retrieval_records(self, rows, cids=None):
        self._retrieval_rows = list(rows) if rows else []
        self._retrieval_cids = list(cids) if cids else []
        if self.current_tab() == self.TAB_RETRIEVAL:
            self._show_retrieval()

    def set_zotero_import_records(self, rows):
        self._zotero_import_rows = list(rows) if rows else []
        if self.current_tab() == self.TAB_ZOTERO_IMPORT:
            self._show_zotero_import()

    def clear_literature(self):
        self.table.clear_data()
        self.close_detail()
        self._retrieval_rows = []
        self._retrieval_cids = []
        self._zotero_import_rows = []
        self._row_cids = []

    def set_detail(self, data):
        self.detail_panel.set_literature(data)
        if not self._detail_open:
            self._detail_open = True
            self.detail_panel.slide_in()

    def close_detail(self):
        self.detail_panel.slide_out()
        self._current_row = -1
        self._detail_open = False
        self.table.set_row_highlight(-1)
        try:
            self.table.horizontalScrollBar().setValue(0)
        except Exception:
            pass

    def set_year_options(self, years):
        if not years:
            return
        options = [str(y) for y in years]
        self.filter_bar.set_filter_options("year", options)

    def set_filter_count(self, matched, total):
        try:
            matched = int(matched)
            total = int(total)
        except (ValueError, TypeError):
            self._filter_count_label.setText("")
            self._filter_count_label.setVisible(False)
            return

        if total <= 0:
            self._filter_count_label.setText("")
            self._filter_count_label.setVisible(False)
            return

        if matched == total:
            text = f"全部 {total} 条"
        else:
            pct = f"{matched / total * 100:.1f}%"
            text = f"筛选：{matched} / {total}（{pct}）"

        self._filter_count_label.setText(text)
        self._filter_count_label.setVisible(True)

    def set_strategy_options(self, strategies, orphan_strategies=None):
        strategies_list = list(strategies or [])
        orphan_list = list(orphan_strategies or [])

        if not strategies_list and not orphan_list:
            return

        options: list = []
        strategy_set: set[str] = set()
        for s in strategies_list:
            s_str = str(s) if s else ""
            if s_str:
                options.append(s_str)
                strategy_set.add(s_str)
        for s in orphan_list:
            s_str = str(s) if s else ""
            if s_str and s_str not in strategy_set:
                options.append((f"{s_str}（已删除）", s_str))

        if not options:
            return
        self.filter_bar.set_filter_options("strategy", options)

    def set_orphan_strategies(self, strategies):
        if not strategies:
            self._cleanup_orphans_btn.setText("清理孤儿文献")
            self._cleanup_orphans_btn.setEnabled(False)
            self._cleanup_orphans_btn.setToolTip(
                "无孤儿策略文献"
            )
            return

        try:
            total = sum(
                int(s.get("record_count", 0) or 0) for s in strategies
            )
        except (TypeError, ValueError):
            total = 0

        self._cleanup_orphans_btn.setText(
            f"清理孤儿文献（{len(strategies)} 策略 / {total} 篇）"
        )
        self._cleanup_orphans_btn.setEnabled(True)

        try:
            detail = "\n".join(
                f"{s.get('name', '')}: {s.get('record_count', 0)} 篇"
                for s in strategies
            )
            self._cleanup_orphans_btn.setToolTip(detail)
        except Exception:
            pass

    def current_row(self):
        return self._current_row

    def is_detail_open(self):
        return self._detail_open

    def literature_count(self):
        return self.table.row_count()

    def filter_values(self):
        return self.filter_bar.values()

    def _on_special_tab_changed(self, key):
        self.special_tab_changed.emit(str(key or ""))

    def current_special_tab(self):
        try:
            return str(self.special_toggle.current_key() or "")
        except Exception:
            return ""

    def set_special_tab_counts(self, counts):
        if not isinstance(counts, dict):
            return
        try:
            n_all = int(counts.get("", 0) or 0)
            n_noise = int(counts.get("retrieval_noise", 0) or 0)
            n_deleted = int(counts.get("deleted_from_source", 0) or 0)
        except (TypeError, ValueError):
            return
        try:
            self.special_toggle.set_item_text("", f"📚 全部 ({n_all})")
            self.special_toggle.set_item_text(
                "retrieval_noise", f"🗑 检索噪声 ({n_noise})"
            )
            self.special_toggle.set_item_text(
                "deleted_from_source", f"⚠ 数据源删除 ({n_deleted})"
            )
        except Exception:
            pass

    def _setup_context_menu(self):
        try:
            self.table.setContextMenuPolicy(Qt.CustomContextMenu)
            self.table.customContextMenuRequested.connect(
                self._on_context_menu
            )
        except Exception:
            pass

    def _row_to_cid(self, row):
        if 0 <= row < len(self._row_cids):
            return self._row_cids[row]
        return ""

    def _on_context_menu(self, pos):
        if self._row_cids is None or len(self._row_cids) == 0:
            return

        index = self.table.indexAt(pos)
        if not index.isValid():
            return

        try:
            selected_rows = self.table.selected_rows()
        except Exception:
            selected_rows = []
        if not selected_rows:
            selected_rows = [index.row()]

        cids = [
            cid for cid in (self._row_to_cid(r) for r in selected_rows)
            if cid
        ]
        if not cids:
            return

        current_tab = self.current_special_tab()
        menu = QMenu(self)
        n = len(cids)

        if current_tab == "":
            menu.addAction(
                f"🗑 标记为检索噪声（{n} 篇）",
                lambda checked=False, c=list(cids):
                    self.mark_noise_requested.emit(c),
            )
            menu.addAction(
                f"⚠ 标记为数据源已删除（{n} 篇）",
                lambda checked=False, c=list(cids):
                    self.mark_deleted_requested.emit(c),
            )
        elif current_tab == "retrieval_noise":
            menu.addAction(
                f"恢复（{n} 篇，需重下全文）",
                lambda checked=False, c=list(cids):
                    self.restore_special_requested.emit(c),
            )
        elif current_tab == "deleted_from_source":
            menu.addAction(
                f"恢复（{n} 篇）",
                lambda checked=False, c=list(cids):
                    self.restore_special_requested.emit(c),
            )
            menu.addSeparator()
            menu.addAction(
                f"⚠ 彻底删除（{n} 篇，不可恢复）",
                lambda checked=False, c=list(cids):
                    self.hard_delete_requested.emit(c),
            )

        if menu.actions():
            try:
                menu.exec(self.table.viewport().mapToGlobal(pos))
            except Exception:
                pass