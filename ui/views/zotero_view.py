"""Zotero 页薄壳（承载 ZoteroSyncPanel + 右侧 DetailPanel）。

9 个信号逐个转发 Panel→View；数据接口全部委托 Panel。"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QWidget

from ui.components.detail_panel import DetailPanel
from ui.components.zotero_sync_panel import ZoteroSyncPanel

class ZoteroView(QWidget):
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
    detail_action_requested = Signal(str, dict)

    CONTENT_MARGINS = (40, 28, 40, 28)
    CONTENT_SPACING = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(*self.CONTENT_MARGINS)
        h.setSpacing(self.CONTENT_SPACING)

        self.panel = ZoteroSyncPanel()
        h.addWidget(self.panel, stretch=1)

        self.detail_panel = DetailPanel(actions=[
            ("open",     "打开链接", False),
            ("copy_doi", "复制 DOI", False),
            ("open_pdf", "打开 PDF", True),
        ])
        h.addWidget(self.detail_panel)

        self._detail_open = False

        self._connect_signals()

    def _connect_signals(self):
        self.panel.refresh_all_requested.connect(
            self.refresh_all_requested.emit
        )
        self.panel.card_clicked.connect(self.card_clicked.emit)
        self.panel.open_zotero_item_requested.connect(
            self.open_zotero_item_requested.emit
        )
        self.panel.sync_selected_requested.connect(
            self.sync_selected_requested.emit
        )
        self.panel.prune_selected_requested.connect(
            self.prune_selected_requested.emit
        )
        self.panel.import_selected_requested.connect(
            self.import_selected_requested.emit
        )
        self.panel.open_zotero_library_requested.connect(
            self.open_zotero_library_requested.emit
        )
        self.panel.collection_changed.connect(
            self.collection_changed.emit
        )
        self.panel.configure_collections_requested.connect(
            self.configure_collections_requested.emit
        )

        self.panel.row_clicked.connect(self.row_clicked.emit)
        self.panel.detail_close_requested.connect(self.close_detail)
        self.detail_panel.close_requested.connect(self.close_detail)
        self.detail_panel.action_requested.connect(
            self.detail_action_requested.emit
        )

    def set_detail(self, data: dict) -> None:
        if not isinstance(data, dict) or not data:
            return
        self.detail_panel.set_literature(data)
        if not self._detail_open:
            self.detail_panel.slide_in()
            self._detail_open = True

    def close_detail(self) -> None:
        self.detail_panel.slide_out()
        self.panel.mark_detail_closed()
        self._detail_open = False

    def set_connection_status(self, *args, **kwargs):
        self.panel.set_connection_status(*args, **kwargs)

    def set_collections(self, *args, **kwargs):
        self.panel.set_collections(*args, **kwargs)

    def set_stats(self, *args, **kwargs):
        self.panel.set_stats(*args, **kwargs)

    def set_refresh_status(self, *args, **kwargs):
        self.panel.set_refresh_status(*args, **kwargs)

    def append_activity(self, *args, **kwargs):
        self.panel.append_activity(*args, **kwargs)

    def clear_activity(self):
        self.panel.clear_activity()

    def show_list(self, card_key: str):
        self.panel.show_list(card_key)

    def set_list_data(
        self, card_key: str, rows: list,
        item_keys: list | None = None,
    ):
        self.panel.set_list_data(card_key, rows, item_keys)

    def hide_list(self):
        self.panel.hide_list()