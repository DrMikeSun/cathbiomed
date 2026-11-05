"""玻璃化数据表格（QTableView + TableModel，只渲染可见单元格）。"""

from PySide6.QtWidgets import (
    QTableView,
    QHeaderView,
    QAbstractItemView,
)

from PySide6.QtCore import Signal, Qt

from ui.models.table_models import TableModel
from ui.config import (
    TABLE_ROW_HEIGHT,
    TABLE_HEADER_HEIGHT,
)

class DataTable(QTableView):
    row_clicked = Signal(int)

    OBJECT_NAME = "QuantumTable"

    def __init__(
        self,
        headers=None,
        mono_columns=None,
        highlight_columns=None,
        column_widths=None,
        selection_mode=QAbstractItemView.SingleSelection,
        parent=None
    ):
        super().__init__(parent)

        self.setObjectName(self.OBJECT_NAME)
        self._selection_mode = selection_mode

        if headers is None:
            headers = []
        self._headers = list(headers)
        self._column_widths = dict(column_widths) if column_widths else {}

        self._model = TableModel(
            headers=self._headers,
            mono_columns=mono_columns,
            highlight_columns=highlight_columns,
            parent=self,
        )
        self.setModel(self._model)
        self._setup_view()
        self._setup_header()
        self._connect_signals()
        self.setSortingEnabled(False)

    def _setup_view(self):
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(TABLE_ROW_HEIGHT)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(self._selection_mode)
        self.setShowGrid(False)
        self.setWordWrap(False)

    def _setup_header(self):
        header = self.horizontalHeader()
        header.setStretchLastSection(False)
        header.setHighlightSections(False)
        header.setFixedHeight(TABLE_HEADER_HEIGHT)

        for col in range(self._model.columnCount()):
            width = self._column_widths.get(col)
            if width is None:
                header.setSectionResizeMode(col, QHeaderView.Stretch)
            else:
                header.setSectionResizeMode(col, QHeaderView.Fixed)
                self.setColumnWidth(col, width)

    def _connect_signals(self):
        self.clicked.connect(self._on_clicked)

    def _on_clicked(self, index):
        if not index.isValid():
            return
        self.row_clicked.emit(index.row())

    def load_data(self, data):
        self._model.set_rows(data)

    def update_row(self, row, row_data):
        self._model.update_row(row, row_data)

    def clear_data(self):
        self._model.clear()

    def set_headers(
        self, headers,
        mono_columns=None,
        highlight_columns=None,
        column_widths=None,
    ):
        new_headers = list(headers or [])
        if not new_headers:
            return
        if column_widths is not None:
            self._column_widths = dict(column_widths)
        self._model.set_headers(
            headers=new_headers,
            mono_columns=mono_columns,
            highlight_columns=highlight_columns,
        )
        self._headers = new_headers
        self._setup_header()

    def set_row_highlight(self, row):
        self._model.set_highlight_row(row)

    def row_count(self):
        return self._model.rowCount()

    def column_count(self):
        return self._model.columnCount()

    def selected_row(self):
        indexes = self.selectionModel().selectedRows()
        if not indexes:
            return -1
        return indexes[0].row()

    def selected_rows(self):
        indexes = self.selectionModel().selectedRows()
        if not indexes:
            return []
        return [idx.row() for idx in indexes]