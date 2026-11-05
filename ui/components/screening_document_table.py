"""筛选中心文献表（5 列 QTableView + 决策 delegate + 多选 + 快捷键）。

直接继承 QTableView（DataTable 硬编码 TableModel 无法换）；objectName=
"QuantumTable" 复用 QSS。

快捷键：I/E/M/U 批量决策；↑↓ 移动；空格下一条（Shift 上一条）；
Esc 发 escape_pressed。delegate 内部 emit decision_clicked（因
editorEvent 返回 True 后 view 不再 emit clicked）。

决策列 4 态单击循环：— → ✓ → ✗ → ? → —"""
from PySide6.QtWidgets import (
    QTableView, QAbstractItemView, QHeaderView, QStyledItemDelegate,
    QMenu,
)
from PySide6.QtCore import Qt, Signal, QModelIndex, QAbstractTableModel, QEvent
from PySide6.QtGui import QFont, QColor

from ui.config import TABLE_ROW_HEIGHT, TABLE_HEADER_HEIGHT, FONT_UI_FAMILY
from ui.theme.palette import Palette, qcolor

class _DecisionTableModel(QAbstractTableModel):
    HEADERS = ["决策", "标题", "作者", "IF", "类型"]
    COL_DECISION = 0

    DECISION_DISPLAY = {
        "unscreened": "—",
        "include":    "✓",
        "exclude":    "✗",
        "maybe":      "?",
    }

    DECISION_CYCLE = {
        "unscreened": "include",
        "include":    "exclude",
        "exclude":    "maybe",
        "maybe":      "unscreened",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole or orientation != Qt.Horizontal:
            return None
        if 0 <= section < len(self.HEADERS):
            return self.HEADERS[section]
        return None

    DECISION_LABELS = {
        "include":    "已纳入",
        "exclude":    "已排除",
        "maybe":      "待定",
        "unscreened": "未筛选",
    }

    DECISION_SUBDIRS = {
        "include":    "included",
        "exclude":    "excluded",
        "maybe":      "maybe",
        "unscreened": "unscreened",
    }

    DECISION_COLORS = {
        "unscreened": Palette.TEXT_MUTED,
        "include":    Palette.AI_GREEN,
        "exclude":    Palette.DANGER_RED,
        "maybe":      Palette.RISK_ORANGE,
    }

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r, c = index.row(), index.column()
        if r < 0 or r >= len(self._rows):
            return None
        row = self._rows[r]

        if role == Qt.DisplayRole:
            if c == self.COL_DECISION:
                return row.get("decision", "unscreened")
            if c == 1:
                title = str(row.get("title", "") or "")
                special = str(
                    row.get("special_status", "") or ""
                ).strip().lower()
                if special == "retrieval_noise":
                    return f"🗑 {title}"
                if special == "deleted_from_source":
                    return f"⚠ {title}"
                return title
            if c == 2:
                return str(row.get("first_author", "") or "")
            if c == 3:
                jif = row.get("journal_if")
                return "—" if jif is None else f"{float(jif):.2f}"
            if c == 4:
                return self._format_ptype(row.get("publication_type_category", ""))

        if role == Qt.ToolTipRole:
            if c == self.COL_DECISION:
                current = str(
                    row.get("decision", "unscreened") or "unscreened"
                ).lower()
                current_label = self.DECISION_LABELS.get(current, current)
                locked_strategy = str(
                    row.get("file_locked_by_strategy", "") or ""
                )
                locked_decision = str(
                    row.get("file_locked_decision", "") or ""
                ).lower()
                if locked_strategy and locked_decision:
                    subdir = self.DECISION_SUBDIRS.get(
                        locked_decision, "unscreened"
                    )
                    locked_label = self.DECISION_LABELS.get(
                        locked_decision, locked_decision,
                    )
                    return (
                        f"当前策略决策：{current_label}\n"
                        f"文件由策略 {locked_strategy} 的 "
                        f"{locked_label} 锁定于 library/{subdir}/"
                    )
                return f"当前策略决策：{current_label}（文件未锁定）"

            if c == 1:
                text = str(row.get("title", "") or "")
                return text if text else None
            if c == 2:
                text = str(row.get("first_author", "") or "")
                return text if text else None
            if c == 4:
                return self._format_ptype(
                    row.get("publication_type_category", "")
                )

        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == self.COL_DECISION:
            return base | Qt.ItemIsEditable
        return base

    def setData(self, index, value, role=Qt.EditRole):
        """delegate 用 EditRole 写决策。"""
        if not index.isValid() or index.column() != self.COL_DECISION:
            return False
        r = index.row()
        if r < 0 or r >= len(self._rows):
            return False
        decision = str(value or "unscreened").strip().lower()
        if decision not in self.DECISION_DISPLAY:
            return False
        self._rows[r]["decision"] = decision
        self.dataChanged.emit(index, index, [Qt.DisplayRole])
        return True

    def apply_decisions_batch(self, cids, decision):
        cids_set = set(str(c) for c in cids if c)
        if not cids_set:
            return 0
        decision_norm = str(decision or "unscreened").strip().lower()
        if decision_norm not in self.DECISION_DISPLAY:
            return 0
        changed_rows: list[int] = []
        for r, row in enumerate(self._rows):
            cid = str(row.get("canonical_id", "") or "")
            if cid not in cids_set:
                continue
            if row.get("decision", "unscreened") == decision_norm:
                continue
            row["decision"] = decision_norm
            changed_rows.append(r)
        if changed_rows:
            top = self.index(min(changed_rows), self.COL_DECISION)
            bottom = self.index(max(changed_rows), self.COL_DECISION)
            self.dataChanged.emit(top, bottom, [Qt.DisplayRole])
        return len(changed_rows)

    def set_rows(self, rows):
        self.beginResetModel()
        self._rows = [dict(r) for r in (rows or []) if isinstance(r, dict)]
        self.endResetModel()

    def row_cid(self, row):
        if 0 <= row < len(self._rows):
            return str(self._rows[row].get("canonical_id", "") or "")
        return ""

    def row_decision(self, row):
        if 0 <= row < len(self._rows):
            return str(self._rows[row].get("decision", "") or "unscreened")
        return "unscreened"

    def get_rows(self) -> list[dict]:
        return [dict(r) for r in self._rows]

    def get_row(self, index: int) -> dict | None:
        if 0 <= index < len(self._rows):
            return dict(self._rows[index])
        return None

    def get_row_by_cid(self, cid: str) -> dict | None:
        target = str(cid or "")
        if not target:
            return None
        for r in self._rows:
            if str(r.get("canonical_id", "") or "") == target:
                return dict(r)
        return None

    @staticmethod
    def _format_ptype(pt):
        return {
            "research":  "研究",
            "review":    "综述",
            "editorial": "社论",
            "other":     "其他",
        }.get(str(pt or "").strip().lower(), "—")

class _DecisionDelegate(QStyledItemDelegate):
    DECISION_DISPLAY = _DecisionTableModel.DECISION_DISPLAY
    DECISION_CYCLE = _DecisionTableModel.DECISION_CYCLE
    DECISION_COLORS = _DecisionTableModel.DECISION_COLORS
    # 双击判定阈值（秒）
    DOUBLE_CLICK_WINDOW = 0.4

    def __init__(self, table):
        super().__init__(table)
        self._table = table
        self._last_click_time = 0.0
        self._last_click_cid = ""

    def paint(self, painter, option, index):
        from PySide6.QtWidgets import QStyle

        decision = str(index.data(Qt.DisplayRole) or "unscreened")
        symbol = self.DECISION_DISPLAY.get(decision, "—")
        color = self.DECISION_COLORS.get(decision, Palette.TEXT_MUTED)

        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, qcolor(Palette.CYAN, 0xA0))
        elif option.state & QStyle.State_MouseOver:
            painter.fillRect(option.rect, qcolor(Palette.CYAN, 35))

        painter.save()
        f = QFont(FONT_UI_FAMILY)
        f.setBold(True)
        f.setPixelSize(16)
        painter.setFont(f)
        painter.setPen(QColor(color))
        painter.drawText(option.rect, Qt.AlignCenter, symbol)
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.MouseButtonDblClick:
            return True
        if event.type() != QEvent.MouseButtonRelease:
            return False
        if not option.rect.contains(event.pos()):
            return False

        import time
        now = time.monotonic()
        cid = self._table.row_cid(index.row())

        if (now - self._last_click_time < self.DOUBLE_CLICK_WINDOW
                and cid == self._last_click_cid):
            self._last_click_time = 0.0
            self._last_click_cid = ""
            return True
        self._last_click_time = now
        self._last_click_cid = cid

        current = str(index.data(Qt.DisplayRole) or "unscreened")
        next_decision = self.DECISION_CYCLE.get(current, "unscreened")
        ok = model.setData(index, next_decision, Qt.EditRole)
        if ok and cid:
            self._table.decision_clicked.emit(cid, next_decision)
        return True

class ScreeningDocumentTable(QTableView):
    decision_clicked = Signal(str, str)
    decisions_batch_changed = Signal(list, str)
    row_selected = Signal(str)
    row_clicked = Signal(int, str)
    open_pdf_requested = Signal(str)
    escape_pressed = Signal()
    mark_noise_requested = Signal(list)
    restore_special_requested = Signal(list)
    hard_delete_requested = Signal(list)

    COLUMN_WIDTHS = {0: 50, 1: None, 2: 100, 3: 60, 4: 80}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("QuantumTable")
        self._model = _DecisionTableModel(parent=self)
        self.setModel(self._model)

        self._setup_view()
        self._setup_header()

        self._decision_delegate = _DecisionDelegate(self)
        self.setItemDelegateForColumn(0, self._decision_delegate)

        self.clicked.connect(self._on_clicked)
        self.doubleClicked.connect(self._on_double_clicked)

        self._setup_context_menu()

    def _setup_view(self):
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(TABLE_ROW_HEIGHT)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setSortingEnabled(False)

    def _setup_header(self):
        header = self.horizontalHeader()
        header.setStretchLastSection(False)
        header.setHighlightSections(False)
        header.setFixedHeight(TABLE_HEADER_HEIGHT)
        for col in range(self._model.columnCount()):
            width = self.COLUMN_WIDTHS.get(col)
            if width is None:
                header.setSectionResizeMode(col, QHeaderView.Stretch)
            else:
                header.setSectionResizeMode(col, QHeaderView.Fixed)
                self.setColumnWidth(col, width)

    def _on_clicked(self, index):
        if not index.isValid():
            return
        if index.column() == _DecisionTableModel.COL_DECISION:
            return
        try:
            if len(self.selectionModel().selectedRows()) > 1:
                return
        except Exception:
            pass
        row = index.row()
        cid = self._model.row_cid(row)
        if cid:
            self.row_clicked.emit(row, str(cid))

    def _on_double_clicked(self, index):
        if index.column() == _DecisionTableModel.COL_DECISION:
            return
        cid = self._model.row_cid(index.row())
        if cid:
            self.open_pdf_requested.emit(cid)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_I:
            self._apply_batch("include")
            return
        if key == Qt.Key_E:
            self._apply_batch("exclude")
            return
        if key == Qt.Key_M:
            self._apply_batch("maybe")
            return
        if key == Qt.Key_U:
            self._apply_batch("unscreened")
            return
        if key == Qt.Key_Space:
            if event.modifiers() & Qt.ShiftModifier:
                self._move_selection(-1)
            else:
                self._move_selection(1)
            return
        if key == Qt.Key_Down:
            self._move_selection(1)
            return
        if key == Qt.Key_Up:
            self._move_selection(-1)
            return
        if key == Qt.Key_Escape:
            self.escape_pressed.emit()
            return
        super().keyPressEvent(event)

    def _move_selection(self, delta):
        total = self._model.rowCount()
        if total <= 0:
            return
        current = self.currentIndex()
        if current.isValid():
            target = max(0, min(total - 1, current.row() + delta))
        else:
            target = 0
        try:
            from PySide6.QtCore import QItemSelectionModel
            idx = self._model.index(target, 0)
            sel_rows = self.selectionModel().selectedRows()
            if len(sel_rows) > 1:
                self.selectionModel().setCurrentIndex(
                    idx, QItemSelectionModel.NoUpdate,
                )
            else:
                self.setCurrentIndex(idx)
                self.selectRow(target)
            self.scrollTo(idx)
            cid = self._model.row_cid(target)
            if cid:
                self.row_selected.emit(cid)
        except Exception:
            pass

    def _apply_batch(self, decision):
        cids_list = self.selected_cids()
        if not cids_list:
            return

        self._model.apply_decisions_batch(cids_list, decision)
        self.decisions_batch_changed.emit(cids_list, decision)

    def set_rows(self, rows):
        self._model.set_rows(rows)
        try:
            self.viewport().update()
        except Exception:
            pass

    def selected_cids(self):
        return [
            self._model.row_cid(idx.row())
            for idx in self.selectionModel().selectedRows()
            if self._model.row_cid(idx.row())
        ]

    def row_decision(self, row):
        return self._model.row_decision(row)

    def row_cid(self, row):
        return self._model.row_cid(row)

    def clear_rows(self):
        self._model.set_rows([])

    def _setup_context_menu(self):
        try:
            self.setContextMenuPolicy(Qt.CustomContextMenu)
            self.customContextMenuRequested.connect(self._on_context_menu)
        except Exception:
            pass

    def _on_context_menu(self, pos):
        index = self.indexAt(pos)
        if not index.isValid():
            return

        try:
            selected_rows = [
                idx.row()
                for idx in self.selectionModel().selectedRows()
            ]
        except Exception:
            selected_rows = []

        if index.row() in selected_rows:
            target_rows = selected_rows
        else:
            target_rows = [index.row()]

        cids = [
            self._model.row_cid(r) for r in target_rows
            if self._model.row_cid(r)
        ]
        if not cids:
            return

        specials = set()
        for r in target_rows:
            row_data = self._model.get_row(r)
            if row_data is not None:
                sp = str(
                    row_data.get("special_status", "") or ""
                ).strip().lower()
                specials.add(sp)

        if len(specials) > 1:
            return

        current_status = specials.pop() if specials else ""

        n = len(cids)
        menu = QMenu(self)

        if current_status == "":
            menu.addAction(
                f"🗑 标记为检索噪声（{n} 篇）",
                lambda checked=False, c=list(cids):
                    self.mark_noise_requested.emit(c),
            )
        elif current_status == "retrieval_noise":
            menu.addAction(
                f"恢复（{n} 篇）",
                lambda checked=False, c=list(cids):
                    self.restore_special_requested.emit(c),
            )
        elif current_status == "deleted_from_source":
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
                menu.exec(self.viewport().mapToGlobal(pos))
            except Exception:
                pass