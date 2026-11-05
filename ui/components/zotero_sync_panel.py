"""Zotero 页核心面板（4 卡总览 ↔ 列表视图）。

item_keys 语义：pending/missing→cid；synced/importable→item_key。
列表 row 点击：两次点击打开详情，再次点击当前行关闭（与筛选中心一致）。"""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ui.components.data_table import DataTable
from ui.components.glass_panel import GlassPanel
from ui.components.zotero_stat_card import ZoteroStatCard
from ui.theme.palette import Palette

class ZoteroSyncPanel(GlassPanel):
    OBJECT_NAME = "ZoteroSyncPanel"

    refresh_all_requested = Signal()
    card_clicked = Signal(str)
    open_zotero_item_requested = Signal(str)
    sync_selected_requested = Signal(list)
    prune_selected_requested = Signal(list)
    import_selected_requested = Signal(list)
    open_zotero_library_requested = Signal()
    collection_changed = Signal(list)
    configure_collections_requested = Signal()

    row_clicked = Signal(str, str)

    detail_close_requested = Signal()

    _LOADING_FRAMES = ["◐", "◓", "◑", "◒"]
    _LOADING_INTERVAL_MS = 200

    _LIST_CARD_KEYS = ("pending", "synced", "missing", "importable")
    _LIST_CARD_LABELS = {
        "pending": "未同步", "synced": "已同步",
        "missing": "失效", "importable": "可从Zotero导入",
    }
    _LIST_TABLE_HEADERS = {
        "pending":    ["标题", "作者", "年份", "DOI"],
        "synced":     ["标题", "作者", "年份", "同步时间", "Zotero分类"],
        "missing":    ["标题", "作者", "年份", "HTTP状态"],
        "importable": ["标题", "作者", "年份", "DOI", "分类"],
    }
    _LIST_COLUMN_WIDTHS = {
        "pending":    {0: None, 1: 140, 2: 80, 3: 200},
        "synced":     {0: None, 1: 140, 2: 80, 3: 160, 4: 140},
        "missing":    {0: None, 1: 140, 2: 80, 3: 100},
        "importable": {0: None, 1: 140, 2: 80, 3: 200, 4: 140},
    }
    _LIST_MONO_COLUMNS = {
        "pending":    {2, 3},
        "synced":     {2, 3},
        "missing":    {2, 3},
        "importable": {2, 3},
    }

    _LIST_BATCH_ACTIONS = {
        "pending":    [("全选", "select_all"), ("同步所选", "sync")],
        "synced":     [("打开 Zotero 库", "open_library")],
        "missing":    [("全选", "select_all"), ("清理所选", "prune")],
        "importable": [("全选", "select_all"), ("引入所选", "import")],
    }

    CONTENT_MARGINS = (24, 20, 24, 20)
    CONTENT_SPACING = 12
    TOP_ROW_HEIGHT = 40
    BOTTOM_ROW_HEIGHT = 28
    CHIPS_SCROLL_HEIGHT = 28

    def __init__(self, parent=None):
        super().__init__(self.OBJECT_NAME)

        self._stat_cards: dict[str, ZoteroStatCard] = {}
        self._stat_cards_list: dict[str, ZoteroStatCard] = {}
        self._current_list_key: str | None = None
        self._list_item_keys: list[str] = []
        self._list_table: DataTable | None = None
        self._loading_index = 0

        self._armed_row: int = -1
        self._current_row: int = -1
        self._pending_current_row: int = -1
        self._detail_open: bool = False

        self._collections: list = []
        self._current_names: list = []

        self._connected: bool = False
        self._host: str = "localhost:23119"
        self._link_mode: str = "imported_file"

        self._refresh_status: str = "idle"
        self._last_update: str = ""

        self._loading_timer = QTimer(self)
        self._loading_timer.setInterval(self._LOADING_INTERVAL_MS)
        self._loading_timer.timeout.connect(self._on_loading_tick)

        self._build_ui()
        self._connect_signals()

        self._apply_connection_style()
        self._apply_status_style("idle", "")

    def _build_ui(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(*self.CONTENT_MARGINS)
        v.setSpacing(self.CONTENT_SPACING)

        v.addWidget(self._build_top_row())

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_cards_page())
        self._stack.addWidget(self._build_list_page())
        self._stack.setCurrentIndex(0)
        v.addWidget(self._stack, stretch=1)

        v.addWidget(self._build_bottom_row())

    def _build_top_row(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(self.TOP_ROW_HEIGHT)
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)

        self._conn_dot = QLabel("○")
        self._conn_dot.setFixedWidth(16)
        self._conn_dot.setAlignment(Qt.AlignCenter)
        h.addWidget(self._conn_dot)

        self._conn_label = QLabel("localhost:23119")
        h.addWidget(self._conn_label)

        h.addSpacing(16)

        self._chips_scroll = QScrollArea()
        self._chips_scroll.setWidgetResizable(True)
        self._chips_scroll.setFrameShape(QFrame.NoFrame)
        self._chips_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._chips_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._chips_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        self._chips_scroll.setFixedHeight(self.CHIPS_SCROLL_HEIGHT)
        self._chips_container = QWidget()
        self._chips_layout = QHBoxLayout(self._chips_container)
        self._chips_layout.setContentsMargins(0, 0, 0, 0)
        self._chips_layout.setSpacing(6)
        self._chips_scroll.setWidget(self._chips_container)
        h.addWidget(self._chips_scroll, stretch=1)

        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.setCursor(Qt.PointingHandCursor)
        h.addWidget(self._refresh_btn)

        self._config_btn = QPushButton("⚙ 配置")
        self._config_btn.setCursor(Qt.PointingHandCursor)
        h.addWidget(self._config_btn)

        return w

    def _build_cards_page(self) -> QWidget:
        page = QWidget()
        h = QHBoxLayout(page)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(16)
        for key in self._LIST_CARD_KEYS:
            card = ZoteroStatCard(key, self._LIST_CARD_LABELS[key])
            card.clicked.connect(self.card_clicked.emit)
            self._stat_cards[key] = card
            h.addWidget(card)
        return page

    def _build_list_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        # ---- 上部：4 张 compact 卡（靠左） ----
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(12)
        for key in self._LIST_CARD_KEYS:
            card = ZoteroStatCard(key, self._LIST_CARD_LABELS[key])
            card.set_compact(True)
            card.clicked.connect(self.card_clicked.emit)
            self._stat_cards_list[key] = card
            top.addWidget(card)
        top.addStretch()
        v.addLayout(top)

        # ---- 中部：DataTable（批量操作需 ExtendedSelection） ----
        self._list_table = DataTable(
            headers=self._LIST_TABLE_HEADERS["pending"],
            mono_columns=self._LIST_MONO_COLUMNS["pending"],
            highlight_columns=set(),
            column_widths=self._LIST_COLUMN_WIDTHS["pending"],
            selection_mode=QAbstractItemView.ExtendedSelection,
        )
        v.addWidget(self._list_table, stretch=1)

        # ---- 下部：批量操作按钮行 ----
        self._batch_row = QHBoxLayout()
        self._batch_row.setContentsMargins(0, 0, 0, 0)
        self._batch_row.setSpacing(8)
        self._batch_row.addStretch()
        v.addLayout(self._batch_row)

        self._list_table.row_clicked.connect(self._on_list_row_clicked)
        try:
            sel_model = self._list_table.selectionModel()
            if sel_model is not None:
                sel_model.currentChanged.connect(
                    self._on_list_current_changed
                )
        except Exception:
            pass

        return page

    def _on_list_row_clicked(self, row: int) -> None:
        self._pending_current_row = -1
        if self._current_list_key is None:
            return
        if not self._list_item_keys:
            return
        if row < 0 or row >= len(self._list_item_keys):
            return

        if self._detail_open and self._current_row == row:
            self.detail_close_requested.emit()
            return

        if self._detail_open:
            self._current_row = row
            self._armed_row = -1
            self.row_clicked.emit(
                self._current_list_key, self._list_item_keys[row]
            )
            return

        if self._armed_row == row:
            self._armed_row = -1
            self._current_row = row
            self._detail_open = True
            self.row_clicked.emit(
                self._current_list_key, self._list_item_keys[row]
            )
        else:
            self._armed_row = row

    def _on_list_current_changed(self, current, previous) -> None:
        if not self._detail_open:
            return
        if not current.isValid():
            return
        if self._current_list_key is None:
            return
        if not self._list_item_keys:
            return
        try:
            if len(self._list_table.selectionModel().selectedRows()) > 1:
                return
        except Exception:
            return
        row = current.row()
        if row < 0 or row == self._current_row:
            return
        if row >= len(self._list_item_keys):
            return
        self._pending_current_row = row
        QTimer.singleShot(0, self._flush_pending_current)

    def _flush_pending_current(self) -> None:
        row = self._pending_current_row
        self._pending_current_row = -1
        if row < 0 or row == self._current_row:
            return
        if not self._detail_open:
            return
        if self._current_list_key is None:
            return
        if row >= len(self._list_item_keys):
            return
        self._current_row = row
        self.row_clicked.emit(
            self._current_list_key, self._list_item_keys[row]
        )

    def mark_detail_closed(self) -> None:
        self._detail_open = False
        self._current_row = -1
        self._armed_row = -1

    def _build_bottom_row(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(self.BOTTOM_ROW_HEIGHT)
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)

        self._status_dot = QLabel("○")
        self._status_dot.setFixedWidth(20)
        self._status_dot.setAlignment(Qt.AlignCenter)
        h.addWidget(self._status_dot)

        self._status_text = QLabel("待检测")
        h.addWidget(self._status_text)

        self._status_time = QLabel("")
        h.addWidget(self._status_time)

        h.addStretch()

        self._activity_label = QLabel("")
        h.addWidget(self._activity_label)

        return w

    def _connect_signals(self):
        self._refresh_btn.clicked.connect(
            lambda checked=False: self.refresh_all_requested.emit()
        )
        self._config_btn.clicked.connect(
            lambda checked=False: self.configure_collections_requested.emit()
        )

    def _apply_connection_style(self):
        if self._connected:
            self._conn_dot.setText("●")
            self._conn_dot.setStyleSheet(
                f"color: {Palette.AI_GREEN}; font-size: 14px;"
            )
            self._conn_label.setText(self._host)
            self._conn_label.setStyleSheet(
                f"color: {Palette.TEXT_SECONDARY}; font-size: 12px;"
            )
        else:
            self._conn_dot.setText("○")
            self._conn_dot.setStyleSheet(
                f"color: {Palette.DANGER_RED}; font-size: 14px;"
            )
            self._conn_label.setText("Zotero 未运行")
            self._conn_label.setStyleSheet(
                f"color: {Palette.DANGER_RED}; font-size: 12px;"
            )

    def set_connection_status(self, connected: bool, host: str, link_mode: str):
        self._connected = bool(connected)
        self._host = str(host) if host else "localhost:23119"
        self._link_mode = str(link_mode) if link_mode else "imported_file"
        self._apply_connection_style()

    def _apply_status_style(self, status: str, last_update: str):
        self._refresh_status = status
        self._last_update = last_update or ""

        color_map = {
            "idle":       Palette.TEXT_MUTED,
            "loading":    Palette.CYAN,
            "done":       Palette.AI_GREEN,
            "done_empty": Palette.TEXT_MUTED,
            "cached":     Palette.RISK_ORANGE,
            "error":      Palette.DANGER_RED,
        }
        text_map = {
            "idle":       "待检测",
            "loading":    "更新中",
            "done":       "已更新",
            "done_empty": "已更新",
            "cached":     "已更新（缓存）",
            "error":      "异常",
        }

        if status == "loading":
            frame = self._LOADING_FRAMES[
                self._loading_index % len(self._LOADING_FRAMES)
            ]
            self._status_dot.setText(frame)
        else:
            symbol_map = {
                "idle": "○", "done": "●", "done_empty": "◎",
                "cached": "◐", "error": "⚠",
            }
            self._status_dot.setText(symbol_map.get(status, "○"))

        color = color_map.get(status, Palette.TEXT_MUTED)
        self._status_dot.setStyleSheet(
            f"color: {color}; font-size: 14px; font-weight: bold;"
        )
        self._status_text.setText(text_map.get(status, "待检测"))
        self._status_text.setStyleSheet(
            f"color: {color}; font-size: 12px;"
        )
        self._status_time.setText(self._last_update)

    def _on_loading_tick(self):
        self._loading_index += 1
        self._apply_status_style("loading", self._last_update)

    def set_refresh_status(self, status: str, last_update: str):
        if status == "loading":
            self._loading_index = 0
            self._loading_timer.start()
        else:
            self._loading_timer.stop()
        self._apply_status_style(status, last_update)

    def _rebuild_chips(self):
        while self._chips_layout.count():
            item = self._chips_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not self._current_names:
            hint = QLabel("未配置观察分类")
            hint.setStyleSheet(
                f"color: {Palette.TEXT_MUTED}; font-size: 12px;"
            )
            self._chips_layout.addWidget(hint)
            return

        for name in self._current_names:
            chip = QPushButton(f"📁 {name} ✕")
            chip.setCursor(Qt.PointingHandCursor)
            chip.setStyleSheet(
                f"QPushButton {{"
                f"  background: {Palette.CYAN}30;"
                f"  border: 1px solid {Palette.CYAN}80;"
                f"  border-radius: 10px;"
                f"  color: {Palette.TEXT_SECONDARY};"
                f"  font-size: 11px;"
                f"  padding: 2px 8px;"
                f"}}"
                f"QPushButton:hover {{"
                f"  background: {Palette.CYAN}50;"
                f"  color: {Palette.TEXT_PRIMARY};"
                f"}}"
            )
            chip.clicked.connect(
                lambda checked=False, n=name: self._on_chip_removed(n)
            )
            self._chips_layout.addWidget(chip)

    def _on_chip_removed(self, name: str):
        new_names = [n for n in self._current_names if n != name]
        self.collection_changed.emit(new_names)

    def set_collections(self, collections: list, current_names: list):
        self._collections = list(collections or [])
        self._current_names = list(current_names or [])
        self._rebuild_chips()

    def set_stats(self, stats: dict):
        if not isinstance(stats, dict):
            return
        for key in self._LIST_CARD_KEYS:
            value = stats.get(key, 0)
            if key in self._stat_cards:
                self._stat_cards[key].set_value(value)
            if key in self._stat_cards_list:
                self._stat_cards_list[key].set_value(value)

        names_empty = len(self._current_names or []) == 0
        if "importable" in self._stat_cards:
            self._stat_cards["importable"].set_disabled(names_empty)
        if "importable" in self._stat_cards_list:
            self._stat_cards_list["importable"].set_disabled(names_empty)

    def append_activity(self, level: str, message: str):
        msg = str(message) if message else ""
        self._activity_label.setText(msg)
        self._activity_label.setToolTip(msg)
        color_map = {
            "info":    Palette.TEXT_MUTED,
            "warning": Palette.RISK_ORANGE,
            "error":   Palette.DANGER_RED,
        }
        color = color_map.get(str(level), Palette.TEXT_MUTED)
        self._activity_label.setStyleSheet(
            f"color: {color}; font-size: 11px;"
        )

    def clear_activity(self):
        self._activity_label.setText("")
        self._activity_label.setToolTip("")

    def show_list(self, card_key: str):
        if card_key not in self._LIST_CARD_KEYS:
            return
        self._current_list_key = card_key
        for key, card in self._stat_cards.items():
            card.set_compact(True)
            card.set_selected(key == card_key)
        for key, card in self._stat_cards_list.items():
            card.set_selected(key == card_key)
        self._apply_list_headers(card_key)
        self._rebuild_batch_actions(card_key)
        self._stack.setCurrentIndex(1)
        self.mark_detail_closed()

    def set_list_data(
        self, card_key: str, rows: list,
        item_keys: list | None = None,
    ):
        if self._list_table is None or card_key not in self._LIST_CARD_KEYS:
            return
        self._list_item_keys = list(item_keys) if item_keys else []
        self._list_table.load_data(rows or [])
        if self._detail_open:
            self.detail_close_requested.emit()
        self.mark_detail_closed()

    def hide_list(self):
        self._current_list_key = None
        if self._detail_open:
            self.detail_close_requested.emit()
        self.mark_detail_closed()
        for card in self._stat_cards.values():
            card.set_compact(False)
            card.set_selected(False)
        for card in self._stat_cards_list.values():
            card.set_selected(False)
        self._stack.setCurrentIndex(0)

    def _apply_list_headers(self, card_key: str):
        if self._list_table is None:
            return
        headers = self._LIST_TABLE_HEADERS.get(card_key, [])
        if not headers:
            return
        self._list_table.set_headers(
            headers=headers,
            mono_columns=self._LIST_MONO_COLUMNS.get(card_key, set()),
            highlight_columns=set(),
            column_widths=self._LIST_COLUMN_WIDTHS.get(card_key, {}),
        )

    def _rebuild_batch_actions(self, card_key: str):
        while self._batch_row.count():
            item = self._batch_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        back_btn = QPushButton("← 返回总览")
        back_btn.setCursor(Qt.PointingHandCursor)
        back_btn.setToolTip("返回 4 卡总览视图")
        back_btn.clicked.connect(
            lambda checked=False: self.hide_list()
        )
        self._batch_row.addWidget(back_btn)

        self._batch_row.addStretch()

        for text, action_key in self._LIST_BATCH_ACTIONS.get(card_key, []):
            btn = QPushButton(text)
            btn.setCursor(Qt.PointingHandCursor)
            if action_key != "select_all":
                btn.setObjectName("PrimaryCTA")
            btn.clicked.connect(
                lambda checked=False, ak=action_key: self._on_batch_action(ak)
            )
            self._batch_row.addWidget(btn)

    def _on_batch_action(self, action_key: str):
        if self._list_table is None:
            return
        if action_key == "select_all":
            self._list_table.selectAll()
            return
        selected = self._list_table.selected_rows()
        if not selected:
            return
        if action_key == "sync":
            cids = [
                self._list_item_keys[r] for r in selected
                if r < len(self._list_item_keys)
            ]
            if cids:
                self.sync_selected_requested.emit(cids)
        elif action_key == "prune":
            cids = [
                self._list_item_keys[r] for r in selected
                if r < len(self._list_item_keys)
            ]
            if cids:
                self.prune_selected_requested.emit(cids)
        elif action_key == "import":
            keys = [
                self._list_item_keys[r] for r in selected
                if r < len(self._list_item_keys)
            ]
            if keys:
                self.import_selected_requested.emit(keys)
        elif action_key == "open_library":
            self.open_zotero_library_requested.emit()

    def selected_rows(self) -> list[int]:
        if self._list_table is None:
            return []
        return list(self._list_table.selected_rows())