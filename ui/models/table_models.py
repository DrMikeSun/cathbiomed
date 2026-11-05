"""QAbstractTableModel 数据模型（样式经 FontRole/ForegroundRole/BackgroundRole
下发，不遍历单元格；行高亮经 _highlight_row + BackgroundRole；dataChanged
局部刷新）。

FontRole 只设 family + weight，**不设 pointSize/pixelSize**：Qt 6 两者互斥，
设任一会把另一重置 -1，合并 QSS font 时触发 setPointSize(-1) 警告；字号
统一交 QSS `* { font-size: 14px; }`。"""

from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex
from PySide6.QtGui import QFont, QBrush

from ui.theme.palette import Palette, qcolor
from ui.config import FONT_UI_FAMILY, FONT_MONO_FAMILY

class TableModel(QAbstractTableModel):
    """
    通用表格模型。

    参数：
        headers            列标题列表
        mono_columns       等宽字体的列索引集合，如 {1, 2}
        highlight_columns  高亮色 + 加粗的列索引集合，如 {4}
        parent             父对象

    用法：
        model = TableModel(
            headers=["标题", "作者", "年份", "状态"],
            mono_columns={2},
            highlight_columns={3},
        )
        model.set_rows([["A", "B", "2024", "成功"]])
    """

    def __init__(
        self,
        headers=None,
        mono_columns=None,
        highlight_columns=None,
        parent=None
    ):
        super().__init__(parent)

        self._headers = list(headers) if headers else []
        self._rows = []
        self._highlight_row = -1

        self._mono_columns = set(mono_columns or ())
        self._highlight_columns = set(highlight_columns or ())

        self._font_cache = {
            c: self._build_font(c)
            for c in range(len(self._headers))
        }
        self._fg_cache = {
            c: self._build_fg(c)
            for c in range(len(self._headers))
        }
        self._highlight_brush = QBrush(qcolor(Palette.CYAN, 40))

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._headers)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None

        r = index.row()
        c = index.column()

        if r < 0 or r >= len(self._rows):
            return None
        if c < 0 or c >= len(self._headers):
            return None

        # ---- 显示文本 ----
        if role == Qt.DisplayRole:
            value = self._rows[r][c]
            return "" if value is None else str(value)

        # ---- 居中 ----
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignCenter)

        # ---- 字体（查缓存） ----
        if role == Qt.FontRole:
            return self._font_cache.get(c)

        # ---- 前景色（查缓存） ----
        if role == Qt.ForegroundRole:
            return self._fg_cache.get(c)

        # ---- 背景色（查缓存的高亮刷） ----
        if role == Qt.BackgroundRole:
            if r == self._highlight_row:
                return self._highlight_brush
            return None

        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            if 0 <= section < len(self._headers):
                return self._headers[section]
        return None

    def _build_font(self, col):
        """构造列 QFont。只设 family + bold，**不设 pointSize/pixelSize**。

        QSS 全局 font-size 已定像素；返回 pointSize>0 的 QFont 会触发 Qt 合并 base pointSize(-1) 时的 setPointSize(-1) 警告。"""
        family = (
            FONT_MONO_FAMILY
            if col in self._mono_columns
            else FONT_UI_FAMILY
        )
        font = QFont()
        families = [f.strip() for f in str(family).split(",") if f.strip()]
        if families:
            font.setFamilies(families)
        else:
            font.setFamily(str(family))
        if col in self._highlight_columns:
            font.setBold(True)
        return font

    def _build_fg(self, col):
        if col in self._highlight_columns:
            return qcolor(Palette.HIGHLIGHT)
        if col in self._mono_columns:
            return qcolor(Palette.TEXT_SECONDARY)
        return qcolor(Palette.TEXT_PRIMARY)

    def _font_for(self, col):
        return self._font_cache.get(col)

    def _foreground_for(self, col):
        return self._fg_cache.get(col)

    def set_rows(self, rows, batch_size=500):
        if self._rows:
            self.beginResetModel()
            self._rows = []
            self._highlight_row = -1
            self.endResetModel()

        if not rows:
            return

        total = len(rows)
        for i in range(0, total, batch_size):
            end = min(i + batch_size, total)
            batch = [self._normalize_row(r) for r in rows[i:end]]
            start = len(self._rows)
            self.beginInsertRows(
                QModelIndex(), start, start + len(batch) - 1
            )
            self._rows.extend(batch)
            self.endInsertRows()

    def update_row(self, row, values):
        if row < 0 or row >= len(self._rows):
            return
        self._rows[row] = self._normalize_row(values)
        self._emit_row_changed(row)

    def set_highlight_row(self, row):
        if self._highlight_row == row:
            return

        old = self._highlight_row
        self._highlight_row = row

        if 0 <= old < len(self._rows):
            self._emit_row_changed(old)
        if 0 <= row < len(self._rows):
            self._emit_row_changed(row)

    def clear(self):
        self.beginResetModel()
        self._rows = []
        self._highlight_row = -1
        self.endResetModel()

    def set_headers(self, headers, mono_columns=None, highlight_columns=None):
        new_headers = list(headers or [])
        if not new_headers:
            return
        if mono_columns is not None:
            self._mono_columns = set(mono_columns)
        if highlight_columns is not None:
            self._highlight_columns = set(highlight_columns)

        self.beginResetModel()
        self._headers = new_headers
        self._rows = [self._normalize_row(r) for r in self._rows]
        self._font_cache = {
            c: self._build_font(c) for c in range(len(new_headers))
        }
        self._fg_cache = {
            c: self._build_fg(c) for c in range(len(new_headers))
        }

        if self._highlight_row >= len(self._rows):
            self._highlight_row = -1
        self.endResetModel()

    def _normalize_row(self, row):
        n = len(self._headers)
        r = list(row)
        if len(r) < n:
            r.extend([""] * (n - len(r)))
        elif len(r) > n:
            r = r[:n]
        return r

    def _emit_row_changed(self, row):
        if 0 <= row < len(self._rows):
            top_left = self.index(row, 0)
            bottom_right = self.index(row, self.columnCount() - 1)
            self.dataChanged.emit(top_left, bottom_right)

    def row_count(self):
        return len(self._rows)

    def column_count(self):
        return len(self._headers)

    def highlight_row(self):
        return self._highlight_row