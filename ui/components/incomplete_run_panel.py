"""未完成 run 面板（采集任务页核心分区：DataTable + EmptyState）。"""

from datetime import datetime, timezone

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
)

from PySide6.QtCore import Qt, Signal

from ui.config import FONT_MONO_FAMILY
from ui.theme.palette import Palette
from ui.components.glass_panel import GlassPanel
from ui.components.data_table import DataTable
from ui.components.empty_state import EmptyState

class IncompleteRunPanel(GlassPanel):
    OBJECT_NAME = "IncompleteRunPanel"

    CONTENT_MARGINS = (24, 20, 24, 20)
    CONTENT_SPACING = 12
    ROW_SPACING = 8

    TABLE_HEADERS = ["Run", "策略", "断点", "中断时间", "中断原因"]
    TABLE_MONO_COLUMNS = {3}          # 中断时间列等宽
    TABLE_HIGHLIGHT_COLUMNS = {2}     # 断点列加粗
    TABLE_COLUMN_WIDTHS = {
        0: None,    # Run：Stretch
        1: 140,     # 策略
        2: 110,     # 断点
        3: 160,     # 中断时间
        4: 260,     # 中断原因
    }

    TABLE_MIN_HEIGHT = 140

    resume_requested = Signal(str)
    abandon_requested = Signal(str)
    refresh_requested = Signal()
    retry_all_requested = Signal() 

    def __init__(self, parent=None):
        super().__init__(self.OBJECT_NAME)

        self._runs = []

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

        self.count_label = QLabel("无未完成的 run")
        self.count_label.setStyleSheet(self._count_label_style())
        row.addWidget(self.count_label)

        row.addStretch()

        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setCursor(Qt.PointingHandCursor)
        row.addWidget(self.refresh_btn)

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
        v.addWidget(self.table, stretch=1)

        self.empty_state = EmptyState(
            title="无未完成的 run",
            subtitle="所有 run 均已提交或被放弃",
            symbol="✓",
        )
        self.empty_state.setVisible(False)
        v.addWidget(self.empty_state, stretch=1)

        return container

    def _build_button_row(self):
        row = QHBoxLayout()
        row.setSpacing(self.ROW_SPACING)

        row.addStretch()

        self.resume_btn = QPushButton("继续所选")
        self.resume_btn.setObjectName("PrimaryCTA")
        self.resume_btn.setCursor(Qt.PointingHandCursor)
        row.addWidget(self.resume_btn)

        self.abandon_btn = QPushButton("放弃所选")
        self.abandon_btn.setCursor(Qt.PointingHandCursor)
        row.addWidget(self.abandon_btn)

        # 问题 3：全部重试
        self.retry_all_btn = QPushButton("全部重试")
        self.retry_all_btn.setCursor(Qt.PointingHandCursor)
        row.addWidget(self.retry_all_btn)

        return row

    def _connect_signals(self):
        self.refresh_btn.clicked.connect(
            lambda checked=False: self.refresh_requested.emit()
        )
        self.resume_btn.clicked.connect(self._on_resume_clicked)
        self.abandon_btn.clicked.connect(self._on_abandon_clicked)
        self.retry_all_btn.clicked.connect(
            lambda checked=False: self.retry_all_requested.emit()
        )
        self.table.row_clicked.connect(self._on_row_clicked)

        try:
            sel_model = self.table.selectionModel()
            if sel_model is not None:
                sel_model.selectionChanged.connect(
                    lambda *_args: self._update_buttons()
                )
        except Exception:
            pass

    def _on_resume_clicked(self):
        run_name = self.selected_run()
        if run_name:
            self.resume_requested.emit(run_name)

    def _on_abandon_clicked(self):
        run_name = self.selected_run()
        if run_name:
            self.abandon_requested.emit(run_name)

    def _on_row_clicked(self, _row):
        self._update_buttons()

    def set_runs(self, runs):
        if runs is None:
            runs = []

        self._runs = [dict(r) for r in runs if isinstance(r, dict)]

        if not self._runs:
            self.count_label.setText("无未完成的 run")
            self.table.clear_data()
            self.table.setVisible(False)
            self.empty_state.setVisible(True)
            self._update_buttons()
            return

        self.count_label.setText(
            f"存在 {len(self._runs)} 个未完成的 run"
        )

        rows = [self._run_to_row(r) for r in self._runs]
        self.table.load_data(rows)
        self.table.setVisible(True)
        self.empty_state.setVisible(False)
        self._update_buttons()

    def selected_run(self):
        """返回当前选中的 run_name；无选中返回 None。"""
        row = self.table.selected_row()
        if row < 0 or row >= len(self._runs):
            return None
        return str(self._runs[row].get("run_name", "") or "") or None

    def run_count(self):
        return len(self._runs)

    def clear(self):
        self.set_runs([])

    def _run_to_row(self, run):
        return [
            str(run.get("run_name", "") or ""),
            str(run.get("strategy_name", "") or ""),
            str(run.get("stage_display", "") or "—"),
            self._format_time(run.get("interrupt_at", "")),
            str(run.get("interrupt_detail", "") or ""),
        ]

    def _format_time(self, iso_str):
        if not iso_str:
            return ""
        try:
            dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_local = dt.astimezone()
            return dt_local.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return str(iso_str)[:16]

    def _update_buttons(self):
        has_sel = self.selected_run() is not None
        self.resume_btn.setEnabled(has_sel)
        self.abandon_btn.setEnabled(has_sel)
        self.retry_all_btn.setEnabled(len(self._runs) > 0)

    def _count_label_style(self):
        return (
            f"color: {Palette.TEXT_SECONDARY};"
            f"font-size: 13px;"
            f"letter-spacing: 1px;"
            f"font-family: {FONT_MONO_FAMILY};"
        )