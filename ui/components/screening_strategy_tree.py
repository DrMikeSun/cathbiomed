"""筛选中心左栏策略树（5 组：已筛选/部分/采集中/未筛选/已归档）。"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QTreeWidget, QTreeWidgetItem,
    QMenu, QPushButton, QStyle, QStyledItemDelegate, QHeaderView,
)
from PySide6.QtCore import Qt, Signal, QSize, QRectF, QTimer
from PySide6.QtGui import (
    QColor, QPainter, QPainterPath, QBrush, QFont, QFontMetrics,
)

from ui.theme.palette import Palette, qcolor
from ui.components.glass_panel import GlassPanel

class _WrapDelegate(QStyledItemDelegate):
    ROW_HEIGHT_NORMAL = 28
    ROW_HEIGHT_SELECTED_MIN = 28

    def paint(self, painter, option, index):
        if not index.parent().isValid():
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        bg_color = None
        if option.state & QStyle.State_Selected:
            bg_color = qcolor(Palette.CYAN, 0x60)
        elif option.state & QStyle.State_MouseOver:
            bg_color = qcolor(Palette.CYAN, 0x30)
        if bg_color is not None:
            path = QPainterPath()
            path.addRoundedRect(QRectF(option.rect), 6, 6)
            painter.fillPath(path, bg_color)

        text = index.data(Qt.DisplayRole) or ""

        fg = index.data(Qt.ForegroundRole)
        if isinstance(fg, QBrush):
            color = fg.color()
        elif isinstance(fg, QColor):
            color = fg
        else:
            color = QColor(Palette.TEXT_SECONDARY)

        font = index.data(Qt.FontRole)
        if not isinstance(font, QFont):
            font = option.font
        painter.setFont(font)
        painter.setPen(color)

        text_rect = option.rect.adjusted(4, 2, -4, -2)

        if option.state & QStyle.State_Selected:
            painter.drawText(
                text_rect,
                Qt.AlignLeft | Qt.AlignVCenter | Qt.TextWordWrap,
                text,
            )
        else:
            fm = QFontMetrics(font)
            elided = fm.elidedText(
                text,
                Qt.TextElideMode.ElideRight,
                text_rect.width(),
            )
            painter.drawText(
                text_rect,
                Qt.AlignLeft | Qt.AlignVCenter,
                elided,
            )

        painter.restore()

class ScreeningStrategyTree(GlassPanel):
    OBJECT_NAME = "ScreeningStrategyTree"

    CONTENT_MARGINS = (12, 14, 12, 12)
    CONTENT_SPACING = 8
    MIN_WIDTH = 220
    MAX_WIDTH = 220

    GROUP_ORDER = ["已筛选", "部分筛选", "采集中", "未筛选", "已归档"]

    STATUS_COLORS = {
        "已筛选":   "AI_GREEN",
        "部分筛选": "RISK_ORANGE",
        "采集中":   "TACTICAL_PURPLE",
        "未筛选":   "TEXT_MUTED",
        "已归档":   "TEXT_MUTED",
    }

    strategy_changed = Signal(str)
    refresh_requested = Signal(str)
    reopen_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(self.OBJECT_NAME)
        self.setMinimumWidth(self.MIN_WIDTH)
        self.setMaximumWidth(self.MAX_WIDTH)

        self._item_by_name: dict[str, QTreeWidgetItem] = {}
        self._status_by_name: dict[str, str] = {}
        self._selected_item: QTreeWidgetItem | None = None

        self._build_ui()
        self._connect_signals()

        self._wrap_delegate = _WrapDelegate(self)
        self.tree.setItemDelegate(self._wrap_delegate)

    def _build_ui(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(*self.CONTENT_MARGINS)
        v.setSpacing(self.CONTENT_SPACING)

        # ---- 顶部：标题 + 刷新按钮 ----
        top = QHBoxLayout()
        top.setSpacing(6)
        title = QLabel("策略")
        title.setStyleSheet(self._section_label_style())
        top.addWidget(title)
        top.addStretch()
        self.refresh_btn = QPushButton("⟳")
        self.refresh_btn.setFixedSize(26, 26)
        self.refresh_btn.setCursor(Qt.PointingHandCursor)
        self.refresh_btn.setToolTip("刷新策略列表")
        top.addWidget(self.refresh_btn)
        v.addLayout(top)

        # ---- 搜索框 ----
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索策略...")
        self.search_edit.setClearButtonEnabled(True)
        v.addWidget(self.search_edit)

        # ---- 策略树 ----
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(12)
        self.tree.setRootIsDecorated(True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.setStyleSheet(self._tree_style())
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tree.header().setStretchLastSection(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        v.addWidget(self.tree, stretch=1)

    def _connect_signals(self):
        self.tree.itemClicked.connect(self._on_item_clicked)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        self.search_edit.textChanged.connect(self._on_search_changed)
        self.refresh_btn.clicked.connect(self._on_refresh_clicked)

    def _on_item_clicked(self, item, _column):
        name = item.data(0, Qt.UserRole)
        if name:
            if self._selected_item is not None and self._selected_item is not item:
                self._recalc_item_height(self._selected_item, False)
            self._recalc_item_height(item, True)
            self._selected_item = item

            try:
                self.tree.doItemsLayout()
            except Exception:
                pass
            name_str = str(name)
            QTimer.singleShot(
                0, lambda n=name_str: self.strategy_changed.emit(n)
            )

    def _on_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        name = item.data(0, Qt.UserRole)
        if not name:
            return
        status = self._status_by_name.get(str(name), "")
        menu = QMenu(self)
        menu.addAction("刷新", lambda: self.refresh_requested.emit(str(name)))
        if status in ("confirmed", "archived_unprocessed"):
            menu.addAction(
                "重开策略",
                lambda: self.reopen_requested.emit(str(name)),
            )
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _on_search_changed(self, text):
        keyword = (text or "").strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(i)
            any_visible = False
            for j in range(group.childCount()):
                child = group.child(j)
                name = str(child.data(0, Qt.UserRole) or "")
                visible = not keyword or keyword in name.lower()
                child.setHidden(not visible)
                if visible:
                    any_visible = True
            group.setHidden(not any_visible)

    def _on_refresh_clicked(self):
        current = self.current_strategy()
        if current:
            self.refresh_requested.emit(current)

    def _schedule_layout_refresh(self):
        QTimer.singleShot(0, self._refresh_layout)

    def _refresh_layout(self):
        try:
            self.tree.doItemsLayout()
        except Exception:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        if self._selected_item is not None:
            self._recalc_item_height(self._selected_item, True)
            try:
                self.tree.doItemsLayout()
            except Exception:
                pass

    def _recalc_item_height(self, item, selected):
        if item is None:
            return
        if not selected:
            item.setSizeHint(
                0, QSize(1, _WrapDelegate.ROW_HEIGHT_NORMAL)
            )
            return
        text = str(item.data(0, Qt.UserRole) or "")
        if not text:
            item.setSizeHint(
                0, QSize(1, _WrapDelegate.ROW_HEIGHT_NORMAL)
            )
            return

        viewport_w = self.tree.viewport().width()
        if viewport_w <= 0:
            viewport_w = (
                self.MIN_WIDTH
                - self.CONTENT_MARGINS[0]
                - self.CONTENT_MARGINS[2]
            )
        indent = self.tree.indentation()
        text_w = max(60, viewport_w - indent - 16)
        fm = QFontMetrics(item.font(0))
        bounding = fm.boundingRect(
            0, 0, text_w, 99999, Qt.TextWordWrap, text,
        )
        height = max(
            _WrapDelegate.ROW_HEIGHT_SELECTED_MIN,
            bounding.height() + 8,
        )
        item.setSizeHint(0, QSize(1, height))

    def set_strategy_groups(self, groups):
        self.tree.clear()
        self._item_by_name.clear()
        self._status_by_name.clear()

        if not isinstance(groups, dict):
            return

        for group_name in self.GROUP_ORDER:
            items = groups.get(group_name, []) or []
            if not items:
                continue
            group_item = QTreeWidgetItem(self.tree, [f"{group_name}（{len(items)}）"])
            group_item.setExpanded(True)
            group_item.setFlags(
                Qt.ItemIsEnabled
            )
            group_item.setForeground(
                0, QColor(Palette.TEXT_MUTED)
            )
            group_item.setSizeHint(
                0, QSize(1, _WrapDelegate.ROW_HEIGHT_NORMAL)
            )

            for st in items:
                name = str(st.get("name", "") or "")
                if not name:
                    continue
                status = str(st.get("status", "") or "")
                count = int(st.get("count", 0) or 0)
                has_draft = bool(st.get("has_draft", False))

                display = self._format_item_text(name, count, has_draft)
                child = QTreeWidgetItem(group_item, [display])
                child.setData(0, Qt.UserRole, name)
                child.setData(0, Qt.UserRole + 1, count)

                color_key = self.STATUS_COLORS.get(group_name, "TEXT_MUTED")
                child.setData(0, Qt.UserRole + 2, color_key)
                child.setToolTip(0, name)

                if has_draft:
                    child.setForeground(0, QColor(Palette.RISK_ORANGE))
                    f = child.font(0)
                    f.setBold(True)
                    child.setFont(0, f)
                else:
                    child.setForeground(0, QColor(getattr(Palette, color_key)))

                self._item_by_name[name] = child
                self._status_by_name[name] = status

        if self.tree.currentItem() is None and self._item_by_name:
            first_name = next(iter(self._item_by_name))
            first_item = self._item_by_name[first_name]
            self.tree.setCurrentItem(first_item)
            self._recalc_item_height(first_item, True)
            self._selected_item = first_item
        self._schedule_layout_refresh()

    def current_strategy(self):
        item = self.tree.currentItem()
        if item is None:
            return ""
        return str(item.data(0, Qt.UserRole) or "")

    def set_current(self, strategy):
        if not strategy:
            self.tree.setCurrentItem(None)
            self.tree.clearSelection()
            if self._selected_item is not None:
                self._recalc_item_height(self._selected_item, False)
                self._selected_item = None

            try:
                self.tree.doItemsLayout()
            except Exception:
                pass
            return
        item = self._item_by_name.get(str(strategy))
        if item is not None:
            self.tree.clearSelection()
            self.tree.setCurrentItem(item)
            item.setSelected(True)
            if self._selected_item is not None and self._selected_item is not item:
                self._recalc_item_height(self._selected_item, False)
            self._recalc_item_height(item, True)
            self._selected_item = item
            try:
                self.tree.doItemsLayout()
            except Exception:
                pass

    def update_draft_flag(self, strategy, has_draft):
        item = self._item_by_name.get(str(strategy))
        if item is None:
            return
        name = str(strategy)
        count = self._get_item_count(item)
        item.setText(0, self._format_item_text(name, count, bool(has_draft)))
        item.setToolTip(0, name)

        f = item.font(0)
        if has_draft:
            item.setForeground(0, QColor(Palette.RISK_ORANGE))
            f.setBold(True)
        else:
            original_color_key = item.data(0, Qt.UserRole + 2) or "TEXT_MUTED"
            item.setForeground(
                0,
                QColor(getattr(Palette, original_color_key, Palette.TEXT_MUTED)),
            )
            f.setBold(False)
        item.setFont(0, f)

    def _format_item_text(self, name, count, has_draft):
        if count > 0:
            return f"● {name} ({count})"
        return f"● {name}"

    @staticmethod
    def _get_item_count(item):
        try:
            c = item.data(0, Qt.UserRole + 1)
            return int(c) if c is not None else 0
        except (ValueError, TypeError):
            return 0

    def _section_label_style(self):
        return (
            f"color: {Palette.TEXT_MUTED};"
            f"font-size: 12px;"
            f"letter-spacing: 1px;"
        )

    def _tree_style(self):
        return (
            f"QTreeWidget {{"
            f"  background: rgba(255, 255, 255, 8);"
            f"  border: 1px solid rgba(255, 255, 255, 20);"
            f"  border-radius: 10px;"
            f"  padding: 4px;"
            f"  color: {Palette.TEXT_SECONDARY};"
            f"  font-size: 13px;"
            f"  outline: none;"
            f"}}"
            f"QTreeWidget::item {{"
            f"  padding-left: 4px;"
            f"  border-radius: 6px;"
            f"}}"
            f"QTreeWidget::branch {{"
            f"  background: transparent;"
            f"}}"
        )