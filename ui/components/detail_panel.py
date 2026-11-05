"""文献详情面板（右侧滑入，三 Tab：核心 / 学术 / Zotero）。"""

from PySide6.QtWidgets import (
    QWidget,
    QLabel,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QPushButton,
    QScrollArea,
    QFrame,
    QSizePolicy,
    QStackedWidget,
)

from PySide6.QtCore import (
    Qt,
    Signal,
    QPropertyAnimation,
    QEasingCurve,
)

from ui.theme.palette import Palette, rgba_str
from ui.config import (
    DETAIL_PANEL_WIDTH,
    ANIM_DURATION_NORMAL,
)
from ui.components.glass_panel import GlassPanel
from ui.components.status_badge import StatusBadge
from ui.components.zotero_section import ZoteroSection

class DetailPanel(GlassPanel):
    """ 文献详情面板。 """

    CLOSE_BTN_SIZE = 28

    META_FIELDS = [
        ("authors", "作者"),
        ("journal", "期刊"),
        ("year",    "年份"),
        ("doi",     "DOI"),
        ("pmid",    "PMID"),
    ]

    METRIC_FIELDS = [
        ("citation_count",             "引用数"),
        ("journal_impact_factor",      "影响因子"),
        ("journal_jcr_quartile",       "JCR 分区"),
        ("reference_count",            "参考文献数"),
        ("influential_citation_count", "高影响力引用"),
        ("author_count",               "作者数"),
        ("institution_count",          "机构数"),
        ("country_codes",              "国家代码"),
        ("publication_type_category",  "文献类型"),
        ("open_access_flag",           "开放获取"),
    ]

    ACTIONS = [
        ("open",     "打开链接",  False),
        ("copy_doi", "复制 DOI",  False),
        ("open_pdf", "打开 PDF",  False),
        ("mark",     "标记",      True),
    ]

    close_requested = Signal()
    action_requested = Signal(str, dict)
    zotero_sync_requested = Signal(str)
    zotero_resync_requested = Signal(str)
    zotero_withdraw_requested = Signal(str)
    zotero_clear_links_requested = Signal(str)
    zotero_open_requested = Signal(str)
    zotero_reverify_requested = Signal(str)

    def __init__(self, parent=None, actions=None):
        super().__init__("DetailPanel")

        self.setMaximumWidth(0)
        self.setVisible(False)
        self.set_glow(Palette.CYAN, alpha=90, blur=50)
        self._data = {}
        self._anim = None
        self._hide_slot = None
        self._actions_list = (
            list(actions) if actions is not None else list(self.ACTIONS)
        )
        self.zotero_section = ZoteroSection()
        self._zotero_has_data: bool = False

        self._build_ui()
        self._connect_zotero_section()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        content = QWidget()
        content.setFixedWidth(DETAIL_PANEL_WIDTH)
        outer.addWidget(content)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 4, 24, 12)
        layout.setSpacing(6)

        layout.addLayout(self._build_header())
        title_wrapper = QWidget()
        tw = QVBoxLayout(title_wrapper)
        tw.setContentsMargins(0, 2, 0, 2)
        tw.setSpacing(0)
        tw.addWidget(self._build_title_block())
        layout.addWidget(title_wrapper)
        layout.addLayout(self._build_status_row())
        layout.addWidget(self._build_file_location_row())
        layout.addWidget(self._build_tab_toggle())
        layout.addWidget(self._build_tab_stack(), stretch=1)
        layout.addLayout(self._build_actions())

    def _connect_zotero_section(self):
        self.zotero_section.sync_requested.connect(
            self.zotero_sync_requested.emit
        )
        self.zotero_section.resync_requested.connect(
            self.zotero_resync_requested.emit
        )
        self.zotero_section.withdraw_requested.connect(
            self.zotero_withdraw_requested.emit
        )
        self.zotero_section.clear_links_requested.connect(
            self.zotero_clear_links_requested.emit
        )
        self.zotero_section.open_requested.connect(
            self.zotero_open_requested.emit
        )
        self.zotero_section.reverify_requested.connect(
            self.zotero_reverify_requested.emit
        )

    def _build_header(self):
        row = QHBoxLayout()
        row.setSpacing(12)

        self.source_label = QLabel("—")
        self.source_label.setObjectName("CardTag")
        self.source_label.setStyleSheet("font-weight: bold;")
        row.addWidget(self.source_label)
        row.addStretch()

        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedSize(self.CLOSE_BTN_SIZE, self.CLOSE_BTN_SIZE)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setStyleSheet(self._close_btn_style())
        self.close_btn.clicked.connect(self.close_requested.emit)
        row.addWidget(self.close_btn)

        return row

    def _build_title_block(self):
        self._title_scroll = QScrollArea()
        self._title_scroll.setWidgetResizable(True)
        self._title_scroll.setFrameShape(QFrame.NoFrame)
        self._title_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._title_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._title_scroll.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Fixed,
        )
        self._title_scroll.setFixedHeight(24)
        self._title_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { width: 6px; background: transparent; }"
            "QScrollBar::handle:vertical {"
            " background: rgba(255, 255, 255, 60);"
            " border-radius: 3px; min-height: 12px; }"
            "QScrollBar::handle:vertical:hover {"
            " background: rgba(255, 255, 255, 110); }"
            "QScrollBar::add-line:vertical,"
            " QScrollBar::sub-line:vertical { height: 0; }"
        )

        self.title_label = QLabel("")
        self.title_label.setObjectName("DetailTitle")
        self.title_label.setWordWrap(True)
        self.title_label.setStyleSheet(self._title_style())
        self.title_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.title_label.setMinimumWidth(0)
        self.title_label.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Preferred,
        )
        self._title_scroll.setWidget(self.title_label)
        return self._title_scroll

    def _build_meta_grid(self):
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)

        self._meta_value_labels = {}

        for row, (key, label_text) in enumerate(self.META_FIELDS):
            key_label = QLabel(label_text)
            key_label.setStyleSheet(self._meta_key_style())

            value_label = QLabel("—")
            value_label.setWordWrap(True)
            value_label.setStyleSheet(self._meta_value_style())
            value_label.setTextInteractionFlags(
                Qt.TextSelectableByMouse
            )

            if key == "authors":
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                scroll.setFrameShape(QFrame.NoFrame)
                scroll.setHorizontalScrollBarPolicy(
                    Qt.ScrollBarAlwaysOff
                )
                scroll.setVerticalScrollBarPolicy(
                    Qt.ScrollBarAsNeeded
                )
                scroll.setStyleSheet(
                    "QScrollArea { background: transparent;"
                    " border: none; }"
                    "QScrollBar:vertical {"
                    " width: 6px; background: transparent; }"
                    "QScrollBar::handle:vertical {"
                    " background: rgba(255, 255, 255, 60);"
                    " border-radius: 3px; min-height: 12px; }"
                    "QScrollBar::handle:vertical:hover {"
                    " background: rgba(255, 255, 255, 110); }"
                    "QScrollBar::add-line:vertical,"
                    " QScrollBar::sub-line:vertical { height: 0; }"
                )
                scroll.setWidget(value_label)
                scroll.setFixedHeight(20)
                self._authors_scroll = scroll
                grid.addWidget(
                    key_label, row, 0,
                    Qt.AlignTop | Qt.AlignLeft,
                )
                grid.addWidget(scroll, row, 1)
            else:
                grid.addWidget(
                    key_label, row, 0,
                    Qt.AlignTop | Qt.AlignLeft,
                )
                grid.addWidget(value_label, row, 1)

            self._meta_value_labels[key] = value_label

        return grid

    def _build_status_row(self):
        row = QHBoxLayout()
        row.setSpacing(6)

        status_key = QLabel("状态")
        status_key.setStyleSheet(
            f"color: {Palette.TEXT_MUTED};"
            f"font-size: 11px;"
            f"letter-spacing: 1px;"
        )
        row.addWidget(status_key)

        self.status_badge = StatusBadge("neutral")
        row.addWidget(self.status_badge)
        row.addStretch()

        return row

    def _build_file_location_row(self):
        self.file_location_label = QLabel("")
        self.file_location_label.setWordWrap(True)
        self.file_location_label.setVisible(False)
        self.file_location_label.setStyleSheet(
            self._file_location_style(Palette.TEXT_MUTED)
        )
        return self.file_location_label

    def _build_tab_toggle(self):
        from ui.components.segment_toggle import SegmentToggle

        self._tab_toggle = SegmentToggle(
            [
                ("核心信息", "core"),
                ("学术指标", "metrics"),
                ("Zotero", "zotero"),
            ],
            button_height=24,
            button_padding_h=8,
        )
        self._tab_toggle.set_current_key("core")
        self._tab_toggle.changed.connect(self._on_tab_changed)
        return self._tab_toggle

    def _build_tab_stack(self):
        self._tab_stack = QStackedWidget()
        self._tab_stack.addWidget(self._wrap_meta_page())
        self._tab_stack.addWidget(self._wrap_metrics_page())
        self._tab_stack.addWidget(self._wrap_zotero_page())
        self._tab_stack.setCurrentIndex(0)
        self._tab_stack.setSizePolicy(
            QSizePolicy.Preferred, QSizePolicy.Expanding,
        )
        return self._tab_stack

    def _wrap_meta_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addLayout(self._build_meta_grid())

        abstract_wrapper = QWidget()
        aw = QVBoxLayout(abstract_wrapper)
        aw.setContentsMargins(0, 12, 0, 0)
        aw.setSpacing(4)
        aw.addWidget(self._make_divider())
        aw.addWidget(self._build_abstract_block(), stretch=1)
        v.addWidget(abstract_wrapper, stretch=1)

        return w

    def _wrap_metrics_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._build_metrics_block())
        v.addStretch()
        return w

    def _wrap_zotero_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self.zotero_section)

        self._zotero_empty_hint = QLabel("该文献暂无 Zotero 关联")
        self._zotero_empty_hint.setAlignment(Qt.AlignCenter)
        self._zotero_empty_hint.setWordWrap(True)
        self._zotero_empty_hint.setStyleSheet(
            f"color: {Palette.TEXT_MUTED};"
            f"font-size: 12px;"
            f"padding: 20px 0;"
        )
        self._zotero_empty_hint.setVisible(False)
        v.addWidget(self._zotero_empty_hint)

        v.addStretch()
        return w

    def _on_tab_changed(self, key):
        if not hasattr(self, "_tab_stack") or self._tab_stack is None:
            return
        key = str(key)
        if key == "core":
            self._tab_stack.setCurrentIndex(0)

            from PySide6.QtCore import QTimer
            QTimer.singleShot(20, self._recalc_authors_height)
            QTimer.singleShot(20, self._recalc_title_height)
        elif key == "metrics":
            self._tab_stack.setCurrentIndex(1)
        elif key == "zotero":
            self._tab_stack.setCurrentIndex(2)

            if getattr(self, "_zotero_empty_hint", None) is not None:
                self._zotero_empty_hint.setVisible(
                    not getattr(self, "_zotero_has_data", False)
                )

    def _build_metrics_block(self):
        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        title = QLabel("学术指标")
        title.setStyleSheet(self._meta_key_style())
        v.addWidget(title)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        grid.setColumnMinimumWidth(0, 78)
        grid.setColumnMinimumWidth(2, 78)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        self._metric_value_labels = {}
        for idx, (key, label_text) in enumerate(self.METRIC_FIELDS):
            row = idx // 2
            col_pair = (idx % 2) * 2

            key_label = QLabel(label_text)
            key_label.setStyleSheet(self._meta_key_style())

            value_label = QLabel("—")
            value_label.setWordWrap(True)
            value_label.setStyleSheet(self._meta_value_style())
            value_label.setTextInteractionFlags(
                Qt.TextSelectableByMouse
            )

            grid.addWidget(
                key_label, row, col_pair,
                Qt.AlignTop | Qt.AlignLeft,
            )
            grid.addWidget(value_label, row, col_pair + 1)
            self._metric_value_labels[key] = value_label

        v.addLayout(grid)
        return container

    def _build_abstract_block(self):
        title = QLabel("摘要")
        title.setStyleSheet(self._meta_key_style())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(self._scroll_style())

        self.abstract_label = QLabel("—")
        self.abstract_label.setWordWrap(True)
        self.abstract_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.abstract_label.setTextInteractionFlags(
            Qt.TextSelectableByMouse
        )
        self.abstract_label.setStyleSheet(self._abstract_style())

        scroll.setWidget(self.abstract_label)

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(3)
        container_layout.addWidget(title)
        container_layout.addWidget(scroll, stretch=1)

        container.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self._abstract_container = container

        return container

    def _build_actions(self):
        row = QHBoxLayout()
        row.setSpacing(8)

        for action_key, text, primary in self._actions_list:
            btn = QPushButton(text)
            btn.setCursor(Qt.PointingHandCursor)
            if primary:
                btn.setObjectName("PrimaryCTA")
            btn.clicked.connect(
                lambda checked=False, k=action_key: self._emit_action(k)
            )
            row.addWidget(btn)

        row.addStretch()
        return row

    def _title_style(self):
        return (
            f"color: {Palette.TEXT_PRIMARY};"
            f"font-family: 'Times New Roman', 'Georgia', "
            f"'SimSun', '宋体', serif;"
            f"font-size: 16px;"
            f"font-weight: 600;"
            f"letter-spacing: 0.5px;"
            f"line-height: 150%;"
        )

    def _meta_key_style(self):
        return (
            f"color: {Palette.TEXT_MUTED};"
            f"font-size: 12px;"
            f"letter-spacing: 1px;"
        )

    def _meta_value_style(self):
        return (
            f"color: {Palette.TEXT_SECONDARY};"
            f"font-size: 13px;"
        )

    def _file_location_style(self, color):
        return (
            f"color: {color};"
            f"font-size: 12px;"
            f"letter-spacing: 0.5px;"
            f"padding-left: 2px;"
        )

    def _abstract_style(self):
        return (
            f"color: {Palette.TEXT_SECONDARY};"
            f"font-size: 13px;"
            f"line-height: 160%;"
        )

    def _format_metric_value(self, key, value):
        if value is None:
            return "—"

        try:
            if str(value).strip().lower() in (
                "nan", "none", "<na>", "null",
            ):
                return "—"
        except Exception:
            pass
        if isinstance(value, str) and not value.strip():
            return "—"

        if key in ("citation_count", "reference_count",
                   "influential_citation_count", "author_count",
                   "institution_count"):
            try:
                n = int(value)
                return str(n) if n > 0 else "—"
            except (ValueError, TypeError):
                return "—"

        if key == "journal_impact_factor":
            try:
                f = float(value)
                if f != f:  # NaN
                    return "—"
                return f"{f:.3f}"
            except (ValueError, TypeError):
                return "—"

        if key == "journal_jcr_quartile":
            s = str(value).strip().upper()
            return s if s else "—"

        if key == "country_codes":
            s = str(value).strip()
            return s if s else "—"

        if key == "publication_type_category":
            mapping = {
                "research": "研究", "review": "综述",
                "editorial": "社论", "other": "其他",
            }
            s = str(value).strip().lower()
            return mapping.get(s, s.upper() if s else "—")

        if key == "open_access_flag":
            s = str(value).strip().lower()
            if s in ("true", "1", "yes"):
                return "是"
            if s in ("false", "0", "no"):
                return "否"
            return "—"

        return str(value) if value else "—"

    def _jcr_style(self, quartile):
        q = (quartile or "").strip().upper()
        color = {
            "Q1": Palette.STATUS_SUCCESS,
            "Q2": Palette.STATUS_INFO,
            "Q3": Palette.STATUS_WARNING,
            "Q4": Palette.STATUS_ERROR,
        }.get(q, Palette.TEXT_SECONDARY)
        return (
            f"color: {color};"
            f"font-size: 13px;"
            f"font-weight: bold;"
        )

    def _scroll_style(self):
        return (
            f"QScrollArea {{"
            f"  background: rgba(255, 255, 255, 10);"
            f"  border: 1px solid rgba(255, 255, 255, 20);"
            f"  border-radius: 10px;"
            f"}}"
        )

    def _close_btn_style(self):
        return (
            f"QPushButton {{"
            f"  background: {rgba_str(Palette.HIGHLIGHT, 60)};"
            f"  border: 1px solid {rgba_str(Palette.HIGHLIGHT, 160)};"
            f"  border-radius: {self.CLOSE_BTN_SIZE // 2}px;"
            f"  color: {Palette.TEXT_PRIMARY};"
            f"  font-size: 15px;"
            f"  font-weight: bold;"
            f"  padding: 0;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: {rgba_str(Palette.HIGHLIGHT, 140)};"
            f"  border: 1px solid {Palette.HIGHLIGHT};"
            f"  color: {Palette.TEXT_PRIMARY};"
            f"}}"
            f"QPushButton:pressed {{"
            f"  background: {rgba_str(Palette.HIGHLIGHT, 200)};"
            f"}}"
        )

    def _make_divider(self):
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(
            f"background: rgba(255, 255, 255, 25);"
            f"border: none;"
        )
        return divider

    def _emit_action(self, action_key):
        self.action_requested.emit(action_key, dict(self._data))

    def set_literature(self, data):
        self._data = dict(data or {})
        self.source_label.setText(self._data.get("source", "—"))

        title = self._data.get("title", "")
        self.title_label.setText(title or "（无标题）")

        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._recalc_title_height)

        for key, _ in self.META_FIELDS:
            value = self._data.get(key, "")
            label = self._meta_value_labels.get(key)
            if label is not None:
                label.setText(str(value) if value else "—")

        authors_text = str(self._data.get("authors", "") or "")
        self._update_authors_display(authors_text)

        status = self._data.get("status", "neutral")
        status_text = self._data.get("status_text")
        if status_text:
            self.status_badge.set_status_and_text(status, status_text)
        else:
            self.status_badge.set_status(status)

        self._update_file_location(self._data)

        abstract = self._data.get("abstract", "")
        self.abstract_label.setText(abstract or "（无摘要）")

        for key, _ in self.METRIC_FIELDS:
            raw = self._data.get(key)
            label = self._metric_value_labels.get(key)
            if label is None:
                continue
            display = self._format_metric_value(key, raw)
            label.setText(display)
            if key == "journal_jcr_quartile" and display != "—":
                label.setStyleSheet(self._jcr_style(display))
            else:
                label.setStyleSheet(self._meta_value_style())

    def _update_authors_display(self, text):
        if getattr(self, "_authors_scroll", None) is None:
            return
        label = self._meta_value_labels.get("authors")
        if label is None:
            return

        text = text or "—"
        label.setText(text)

        fm = label.fontMetrics()
        line_h = fm.height()
        max_2line = 2 * line_h

        scroll = self._authors_scroll
        viewport_w = scroll.viewport().width()
        if viewport_w <= 0:
            viewport_w = max(
                200,
                DETAIL_PANEL_WIDTH - 24 - 24 - 40 - 12,
            )

        bounding = fm.boundingRect(
            0, 0, viewport_w, 99999,
            Qt.TextWordWrap,
            text,
        )
        needed_h = bounding.height()

        if needed_h <= max_2line:
            scroll.setFixedHeight(max(needed_h, line_h))
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        else:
            scroll.setFixedHeight(max_2line + 4)
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    _FILE_LOCKED_SUBDIR = {
        "include":    "included",
        "exclude":    "excluded",
        "maybe":      "maybe",
        "unscreened": "unscreened",
    }

    def _update_file_location(self, data):
        label = getattr(self, "file_location_label", None)
        if label is None:
            return
        if not isinstance(data, dict):
            label.setText("")
            label.setVisible(False)
            return

        has_context = (
            "file_locked_by_strategy" in data
            or "file_locked_decision" in data
            or "file_missing" in data
        )
        if not has_context:
            label.setText("")
            label.setVisible(False)
            return

        if data.get("file_missing"):
            text = "⚠️ 文件缺失，归档时将自动补足"
            color = Palette.RISK_ORANGE
        elif data.get("file_locked_by_strategy"):
            decision = str(data.get("file_locked_decision", "") or "").lower()
            strategy = str(data.get("file_locked_by_strategy"))
            if decision:
                subdir = self._FILE_LOCKED_SUBDIR.get(decision, "inbox")
                text = (
                    f"📁 文件位置：{subdir}/"
                    f"（由策略 {strategy} 的 {decision} 锁定）"
                )
            else:
                text = f"📁 文件位置：inbox/（策略 {strategy} 无决策）"
            color = Palette.TEXT_SECONDARY
        else:
            text = "📁 文件位置：inbox/（未归档）"
            color = Palette.TEXT_MUTED

        label.setText(text)
        label.setStyleSheet(self._file_location_style(color))
        label.setVisible(True)

    def clear(self):
        self._data = {}
        self.source_label.setText("—")
        self.title_label.setText("")
        for key, _ in self.META_FIELDS:
            label = self._meta_value_labels.get(key)
            if label is not None:
                label.setText("—")
        self.status_badge.set_status("neutral")
        self.abstract_label.setText("—")

        for key, _ in self.METRIC_FIELDS:
            label = self._metric_value_labels.get(key)
            if label is not None:
                label.setText("—")
                label.setStyleSheet(self._meta_value_style())

        if getattr(self, "_authors_scroll", None) is not None:
            self._authors_scroll.setFixedHeight(20)
            self._authors_scroll.setVerticalScrollBarPolicy(
                Qt.ScrollBarAsNeeded
            )

        if getattr(self, "file_location_label", None) is not None:
            self.file_location_label.setText("")
            self.file_location_label.setVisible(False)

        self.clear_zotero_status()

    def data(self):
        """返回当前文献数据副本"""
        return dict(self._data)

    def set_zotero_status(self, data):
        if not data:
            self._zotero_has_data = False
            self.zotero_section.clear()
            if getattr(self, "_zotero_empty_hint", None) is not None:
                self._zotero_empty_hint.setVisible(True)
            return
        self._zotero_has_data = True
        self.zotero_section.set_status(data)
        self.zotero_section.setVisible(True)

        if getattr(self, "_zotero_empty_hint", None) is not None:
            self._zotero_empty_hint.setVisible(False)
        state = str(data.get("state", "") or "")
        origin = str(data.get("data_origin", "retrieval") or "retrieval")
        if state == "not_synced" and origin == "retrieval":
            self.zotero_section.set_collapsed(True)
        else:
            self.zotero_section.set_collapsed(False)

    def clear_zotero_status(self):
        self._zotero_has_data = False
        self.zotero_section.clear()

    def slide_in(self, duration=None):
        if duration is None:
            duration = ANIM_DURATION_NORMAL
        self.setVisible(True)
        self._animate_width(0, DETAIL_PANEL_WIDTH, duration)
        from PySide6.QtCore import QTimer
        QTimer.singleShot(duration + 20, self._recalc_authors_height)
        QTimer.singleShot(duration + 20, self._recalc_title_height)

    def _recalc_authors_height(self):
        if not self.isVisible():
            return
        if getattr(self, "_authors_scroll", None) is None:
            return
        authors_text = str(self._data.get("authors", "") or "")
        self._update_authors_display(authors_text)

    def _recalc_title_height(self):
        if getattr(self, "_title_scroll", None) is None:
            return
        label = getattr(self, "title_label", None)
        if label is None:
            return

        max_h = 80
        fm = label.fontMetrics()
        line_h = fm.height()

        text = label.text() or "—"
        viewport_w = self._title_scroll.viewport().width()
        if viewport_w <= 0:
            viewport_w = max(200, DETAIL_PANEL_WIDTH - 48)

        bounding = fm.boundingRect(
            0, 0, viewport_w, 99999, Qt.TextWordWrap, text,
        )
        needed_h = bounding.height()

        if needed_h <= max_h:
            self._title_scroll.setFixedHeight(max(needed_h + 4, line_h))
            self._title_scroll.setVerticalScrollBarPolicy(
                Qt.ScrollBarAlwaysOff
            )
        else:
            self._title_scroll.setFixedHeight(max_h)
            self._title_scroll.setVerticalScrollBarPolicy(
                Qt.ScrollBarAsNeeded
            )

    def slide_out(self, duration=None):
        if duration is None:
            duration = ANIM_DURATION_NORMAL
        self._animate_width(
            DETAIL_PANEL_WIDTH,
            0,
            duration,
            hide_at_end=True,
        )

    def _animate_width(self, start, end, duration, hide_at_end=False):
        if self._anim is not None:
            try:
                self._anim.stop()
            except RuntimeError:
                pass
            if self._hide_slot is not None:
                try:
                    self._anim.finished.disconnect(self._hide_slot)
                except (RuntimeError, TypeError):
                    pass
                self._hide_slot = None

        self._anim = QPropertyAnimation(self, b"maximumWidth", self)
        self._anim.setDuration(duration)
        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

        if hide_at_end:
            slot = lambda: self.setVisible(False)
            self._anim.finished.connect(slot)
            self._hide_slot = slot

        self._anim.start()