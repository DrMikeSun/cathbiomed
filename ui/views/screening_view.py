"""筛选中心视图（三栏：左策略树 + 中进度条/文献表 + 右 DetailPanel）。

筛选中心不显示"标记"按钮（用户已在此页）；主按钮为"打开 PDF"。"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QFrame, QLabel,
)
from PySide6.QtCore import Qt, Signal

from ui.theme.palette import Palette

from ui.components.section_header import SectionHeader
from ui.components.detail_panel import DetailPanel
from ui.components.screening_strategy_tree import ScreeningStrategyTree
from ui.components.screening_document_table import ScreeningDocumentTable
from ui.components.screening_progress_bar import ScreeningProgressBar
from ui.components.screening_draft_manager import ScreeningDraftManager

class ScreeningView(QWidget):
    refresh_status_requested = Signal(str)
    strategy_changed = Signal(str)
    cleanup_trash_requested = Signal()
    decision_changed = Signal(str, str, str)
    save_decisions_requested = Signal(str)
    confirm_archive_requested = Signal(str)
    export_report_requested = Signal(str, str)
    open_pdf_requested = Signal(str)
    row_selected = Signal(str)
    reopen_strategy_requested = Signal(str)
    detail_action_requested = Signal(str, dict)
    log_message = Signal(str, str)
    rows_filtered = Signal()
    mark_noise_requested = Signal(list)
    restore_special_requested = Signal(list)
    hard_delete_requested = Signal(list)

    CONTENT_MARGINS = (24, 20, 24, 16)
    CONTENT_SPACING = 12
    TITLE = "筛选中心"
    SUBTITLE = "选择策略 → 勾选决策 → 保存 → 确认归档"

    def __init__(self, parent=None):
        super().__init__(parent)

        self._current_strategy = ""
        self._user_selected = False
        self._draft_manager: ScreeningDraftManager | None = None
        self._right_open = False
        self._current_row = -1
        self._armed_row = -1
        self._all_screening_rows: list[dict] = []
        self._cid_index: dict[str, int] = {}
        self._search_pending_text: str = ""
        from PySide6.QtCore import QTimer as _QTimer
        self._search_timer = _QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._apply_search_pending)

        self._build_ui()
        self._connect_signals()

    def _build_ui(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(*self.CONTENT_MARGINS)
        v.setSpacing(self.CONTENT_SPACING)

        # 标题
        self.header = SectionHeader(self.TITLE, self.SUBTITLE)
        v.addWidget(self.header)

        # 三栏容器（左 + 中）
        self._body = QWidget()
        body_h = QHBoxLayout(self._body)
        body_h.setContentsMargins(0, 0, 0, 0)
        body_h.setSpacing(12)

        # 左栏：策略树
        self.strategy_tree = ScreeningStrategyTree()
        body_h.addWidget(self.strategy_tree)

        # 中栏容器
        center = QWidget()
        center_v = QVBoxLayout(center)
        center_v.setContentsMargins(0, 0, 0, 0)
        center_v.setSpacing(10)

        self.progress_bar = ScreeningProgressBar()
        center_v.addWidget(self.progress_bar)

        self._hint_label = QLabel(
            "💡 选中行按 I/E/M/U 批量决策；再次点击已选中的行查看详情"
        )
        self._hint_label.setStyleSheet(
            f"color: {Palette.TEXT_MUTED};"
            f"font-size: 11px;"
            f"padding: 2px 4px;"
        )
        center_v.addWidget(self._hint_label)

        self.document_table = ScreeningDocumentTable()
        center_v.addWidget(self.document_table, stretch=1)

        body_h.addWidget(center, stretch=1)

        self.detail_panel = DetailPanel(actions=[
            ("open",     "打开链接", False),
            ("copy_doi", "复制 DOI", False),
            ("open_pdf", "打开 PDF", True),
        ])
        body_h.addWidget(self.detail_panel)

        v.addWidget(self._body, stretch=1)
        v.addWidget(self._build_action_bar())

    def _build_action_bar(self):
        bar = QFrame()
        bar.setFixedHeight(52)
        bar.setStyleSheet(
            f"QFrame {{"
            f"  background: rgba(255, 255, 255, 8);"
            f"  border-top: 1px solid rgba(255, 255, 255, 20);"
            f"}}"
        )
        h = QHBoxLayout(bar)
        h.setContentsMargins(12, 8, 12, 8)
        h.setSpacing(8)

        self.save_btn = QPushButton("保存决策")
        self.save_btn.setObjectName("PrimaryCTA")
        self.save_btn.setCursor(Qt.PointingHandCursor)
        h.addWidget(self.save_btn)

        self.export_btn = QPushButton("导出报告▾")
        self.export_btn.setCursor(Qt.PointingHandCursor)
        h.addWidget(self.export_btn)

        self.cleanup_btn = QPushButton("清理回收站")
        self.cleanup_btn.setCursor(Qt.PointingHandCursor)
        h.addWidget(self.cleanup_btn)

        h.addStretch()

        self.confirm_btn = QPushButton("确认结论⚠")
        self.confirm_btn.setObjectName("PrimaryCTA")
        self.confirm_btn.setCursor(Qt.PointingHandCursor)
        h.addWidget(self.confirm_btn)

        return bar

    def _connect_signals(self):
        # 左栏
        self.strategy_tree.strategy_changed.connect(self._on_strategy_clicked)
        self.strategy_tree.refresh_requested.connect(self._on_strategy_refresh)
        self.strategy_tree.reopen_requested.connect(self._on_strategy_reopen)

        # 中栏
        self.document_table.decision_clicked.connect(self._on_decision_clicked)
        self.document_table.decisions_batch_changed.connect(
            self._on_decisions_batch
        )
        self.document_table.row_clicked.connect(self._on_row_clicked)
        self.document_table.row_selected.connect(self._on_row_selected)
        self.document_table.open_pdf_requested.connect(self.open_pdf_requested.emit)
        self.document_table.escape_pressed.connect(self._close_detail_panel)
        self.progress_bar.search_text_changed.connect(self._on_search_changed)

        # 底部
        self.save_btn.clicked.connect(self._on_save_clicked)
        self.export_btn.clicked.connect(self._on_export_clicked)
        self.cleanup_btn.clicked.connect(self._on_cleanup_clicked)
        self.confirm_btn.clicked.connect(self._on_confirm_clicked)

        # 右栏关闭
        self.detail_panel.close_requested.connect(self._close_detail_panel)
        self.detail_panel.action_requested.connect(
            self.detail_action_requested.emit
        )
        self.document_table.mark_noise_requested.connect(
            self.mark_noise_requested.emit
        )
        self.document_table.restore_special_requested.connect(
            self.restore_special_requested.emit
        )
        self.document_table.hard_delete_requested.connect(
            self.hard_delete_requested.emit
        )

    def _on_strategy_clicked(self, strategy):
        self._current_strategy = str(strategy or "")
        self._user_selected = True
        self.strategy_changed.emit(self._current_strategy)

    def _on_strategy_refresh(self, strategy):
        self.refresh_status_requested.emit(str(strategy))

    def _on_strategy_reopen(self, strategy):
        s = str(strategy or "")
        if not s:
            return
        self.reopen_strategy_requested.emit(s)

    def _on_decision_clicked(self, cid, new_decision):
        if not cid:
            return

        strategy = (
            str(self._current_strategy or "")
            or str(self.current_strategy() or "")
        )
        if not strategy:
            self.append_log("warning", "未选择策略，无法记录决策")
            return
        if not self._current_strategy:
            self._current_strategy = strategy
        if self._draft_manager is not None:
            self._draft_manager.set_decision(
                strategy, cid, new_decision,
            )
        self._sync_decision_to_cache(cid, new_decision)
        self.decision_changed.emit(
            cid, strategy, new_decision,
        )

    def _on_decisions_batch(self, cids, new_decision):
        if not cids:
            return
        strategy = (
            str(self._current_strategy or "")
            or str(self.current_strategy() or "")
        )
        if not strategy:
            self.append_log("warning", "未选择策略，无法记录批量决策")
            return
        if not self._current_strategy:
            self._current_strategy = strategy
        if self._draft_manager is not None:
            self._draft_manager.set_decisions_batch(
                strategy, cids, new_decision,
            )
        for cid in cids:
            self._sync_decision_to_cache(cid, new_decision)
            self.decision_changed.emit(
                cid, strategy, new_decision,
            )

    def _sync_decision_to_cache(self, cid, decision):
        if not cid:
            return
        cid_str = str(cid)
        decision_str = str(decision or "unscreened")
        idx = self._cid_index.get(cid_str)
        if idx is not None and 0 <= idx < len(self._all_screening_rows):
            self._all_screening_rows[idx]["decision"] = decision_str

    def _on_row_selected(self, cid):
        if not cid:
            return
        if not self._right_open:
            return
        try:
            if len(self.document_table.selectionModel().selectedRows()) > 1:
                return
        except Exception:
            pass
        try:
            model = self.document_table._model
            for r in range(model.rowCount()):
                if model.row_cid(r) == str(cid):
                    self._current_row = r
                    break
        except Exception:
            pass
        self.row_selected.emit(str(cid))

    def _on_row_clicked(self, row, cid):
        if not cid:
            return

        if self._right_open and row == self._current_row:
            self._close_detail_panel()
            return

        if self._right_open:
            self._current_row = row
            self._armed_row = -1
            self.row_selected.emit(str(cid))
            return

        if self._armed_row == row:
            self._armed_row = -1
            self._current_row = row
            self.detail_panel.slide_in()
            self._right_open = True
            self.row_selected.emit(str(cid))
        else:
            self._armed_row = row

    def _on_search_changed(self, text):
        self._armed_row = -1
        self._search_pending_text = str(text or "")
        self._search_timer.start()

    def _apply_search_pending(self):
        self._apply_search_filter(self._search_pending_text)

    def _apply_search_filter(self, text):
        keyword = (text or "").strip().lower()
        if not keyword:
            self.document_table.set_rows(self._all_screening_rows)
        else:
            filtered = [
                r for r in self._all_screening_rows
                if keyword in str(r.get("title", "")).lower()
                or keyword in str(r.get("first_author", "")).lower()
                or keyword in str(r.get("journal", "")).lower()
                or keyword in str(r.get("doi", "")).lower()
                or keyword in str(r.get("year", "")).lower()
            ]
            self.document_table.set_rows(filtered)

        self.rows_filtered.emit()

    def _on_save_clicked(self):
        strategy = self.current_strategy()
        if not strategy:
            self.append_log("warning", "未选择策略")
            return
        self._current_strategy = strategy
        if self._draft_manager is not None:
            self._draft_manager.flush(strategy)
            if self._draft_manager.has_dirty_draft(strategy):
                self.append_log(
                    "error",
                    "草稿落盘失败（磁盘空间/权限问题），请检查后重试",
                )
                return
        self.save_decisions_requested.emit(strategy)

    def _on_export_clicked(self):
        strategy = self.current_strategy()
        if not strategy:
            self.append_log("warning", "未选择策略")
            return
        self._current_strategy = strategy
        if self._draft_manager is not None:
            self._draft_manager.flush(strategy)
        self.export_report_requested.emit(strategy, "")

    def _on_cleanup_clicked(self):
        self.cleanup_trash_requested.emit()

    def _on_confirm_clicked(self):
        strategy = self.current_strategy()
        if not strategy:
            self.append_log("warning", "未选择策略，请先点击左侧策略")
            return
        self._current_strategy = strategy
        if self._draft_manager is not None:
            self._draft_manager.flush(strategy)
        self.confirm_archive_requested.emit(strategy)

    def show_detail_panel(self, data):
        self.detail_panel.set_literature(data)
        if not self._right_open:
            self.detail_panel.slide_in()
            self._right_open = True

    def _close_detail_panel(self):
        if not self._right_open:
            return
        self.detail_panel.slide_out()
        self._right_open = False
        self._current_row = -1
        self._armed_row = -1

    def locate_cid(self, cid):
        s = str(cid or "")
        if not s:
            return False

        try:
            if self.progress_bar.search_text():
                self.progress_bar.set_search_text("")
                self._search_pending_text = ""
                if self._search_timer.isActive():
                    self._search_timer.stop()
                self._apply_search_filter("")
        except Exception:
            pass

        target_row = -1
        try:
            model = self.document_table._model
            for r in range(model.rowCount()):
                if model.row_cid(r) == s:
                    target_row = r
                    break
        except Exception:
            return False
        if target_row < 0:
            return False

        try:
            from PySide6.QtWidgets import QAbstractItemView
            from PySide6.QtCore import QItemSelectionModel
            table = self.document_table
            idx = table._model.index(target_row, 0)
            table.clearSelection()
            table.setCurrentIndex(idx)
            table.selectionModel().select(
                idx,
                QItemSelectionModel.Select | QItemSelectionModel.Rows,
            )
            table.setFocus()
            table.scrollTo(idx, QAbstractItemView.PositionAtCenter)
        except Exception:
            return False

        self._current_row = target_row
        self._armed_row = -1
        self.row_selected.emit(s)
        return True

    def set_strategy_groups(self, groups):
        self.strategy_tree.set_strategy_groups(groups)
        self._sync_current_from_tree()

    def _sync_current_from_tree(self):
        if not self._current_strategy:
            tree_current = self.strategy_tree.current_strategy()
            if tree_current:
                self._current_strategy = tree_current

    def set_current_strategy(self, name):
        s = str(name or "").strip()
        if not s:
            self._current_strategy = ""
            try:
                self.strategy_tree.tree.setCurrentItem(None)
            except Exception:
                pass
            return
        self._current_strategy = s
        self.strategy_tree.set_current(s)

    def current_strategy(self):
        return self._current_strategy or self.strategy_tree.current_strategy()

    def set_user_selected(self, flag=True):
        self._user_selected = bool(flag)

    def is_user_selected(self):
        return self._user_selected

    def set_stats(self, data):
        if not isinstance(data, dict):
            return
        included = int(data.get("include", 0) or 0)
        excluded = int(
            data.get("excluded", 0)
            or data.get("exclude", 0)
            or 0
        )
        maybe = int(data.get("maybe", 0) or 0)
        unscreened = int(data.get("unscreened", 0) or 0)
        mapped = {
            "include":    included,
            "exclude":    excluded,
            "maybe":      maybe,
            "unscreened": unscreened,
            "total":      int(
                data.get("total", 0)
                or (included + excluded + maybe + unscreened)
            ),
        }
        self.progress_bar.set_stats(mapped)

    def append_log(self, level, message):
        try:
            self.log_message.emit(str(level), str(message))
        except Exception:
            pass
        try:
            from PySide6.QtCore import qDebug
            qDebug(f"[screening/{level}] {message}")
        except Exception:
            pass

    def set_screening_rows(self, rows):
        self._all_screening_rows = list(rows or [])
        self._cid_index = {
            str(r.get("canonical_id", "")): i
            for i, r in enumerate(self._all_screening_rows)
            if r.get("canonical_id")
        }
        if self._right_open:
            self._close_detail_panel()
        else:
            self._armed_row = -1
            self._current_row = -1
        self._apply_search_filter(self.progress_bar.search_text())

    def set_draft_manager(self, manager):
        self._draft_manager = manager
        if manager is not None:
            manager.draft_changed.connect(self._on_draft_changed)

    def _on_draft_changed(self, strategy):
        if self._draft_manager is None:
            return
        self.strategy_tree.update_draft_flag(
            strategy, self._draft_manager.has_dirty(strategy)
            if hasattr(self._draft_manager, "has_dirty")
            else self._draft_manager.has_draft(strategy)
        )

    def showEvent(self, event):
        super().showEvent(event)
        if self._all_screening_rows:
            try:
                from PySide6.QtCore import QTimer
                QTimer.singleShot(
                    0,
                    lambda: self._apply_search_filter(
                        self.progress_bar.search_text()
                    ),
                )
            except Exception:
                pass

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self._right_open:
            self._close_detail_panel()
            event.accept()
            return
        super().keyPressEvent(event)