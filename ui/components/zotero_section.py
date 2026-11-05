"""DetailPanel 内嵌 Zotero 分区（6 态 × origin 组合分派按钮）。

6 态：not_synced / checking / synced / missing / retrieval_overridden /
unverified（另有 invalid 态：key 空/非法，仅显示"清理关联"）。"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from ui.components.glass_panel import GlassPanel
from ui.components.status_badge import StatusBadge
from ui.theme.palette import Palette

class ZoteroSection(GlassPanel):
    OBJECT_NAME = "ZoteroSection"

    STATE_NOT_SYNCED = "not_synced"
    STATE_SYNCED = "synced"
    STATE_MISSING = "missing"
    STATE_RETRIEVAL_OVERRIDDEN = "retrieval_overridden"
    STATE_CHECKING = "checking"
    STATE_UNVERIFIED = "unverified"

    shadow_blur = 0
    shadow_alpha = 0

    sync_requested = Signal(str)
    resync_requested = Signal(str)
    withdraw_requested = Signal(str)
    clear_links_requested = Signal(str)
    open_requested = Signal(str)
    reverify_requested = Signal(str)

    CONTENT_MARGINS = (16, 12, 16, 12)
    CONTENT_SPACING = 10
    BTN_SPACING = 8

    def __init__(self, parent=None):
        super().__init__(self.OBJECT_NAME)
        self._cid = ""
        self._item_key = ""
        self._state = self.STATE_NOT_SYNCED
        self._data_origin = "retrieval"
        self._collapsed = False
        self._build_ui()
        self.setVisible(False)

    def _build_ui(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(*self.CONTENT_MARGINS)
        v.setSpacing(self.CONTENT_SPACING)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._title_label = QLabel("Zotero")
        self._title_label.setStyleSheet(
            f"color: {Palette.TEXT_SECONDARY};"
            f" font-size: 13px;"
            f" font-weight: bold;"
            f" letter-spacing: 1px;"
        )
        top.addWidget(self._title_label)
        self._badge = StatusBadge("neutral")
        top.addWidget(self._badge)
        top.addStretch()

        self._expand_btn = QPushButton("展开")
        self._expand_btn.setCursor(Qt.PointingHandCursor)
        self._expand_btn.setVisible(False)
        self._expand_btn.clicked.connect(
            lambda checked=False: self.set_collapsed(False)
        )
        top.addWidget(self._expand_btn)
        v.addLayout(top)

        self._detail_label = QLabel("")
        self._detail_label.setWordWrap(True)
        self._detail_label.setStyleSheet(
            f"color: {Palette.TEXT_MUTED}; font-size: 12px;"
        )
        v.addWidget(self._detail_label)

        self._btn_row = QHBoxLayout()
        self._btn_row.setSpacing(self.BTN_SPACING)
        self._primary_btn = QPushButton("")
        self._primary_btn.setObjectName("PrimaryCTA")
        self._primary_btn.setCursor(Qt.PointingHandCursor)
        self._secondary_btn = QPushButton("")
        self._secondary_btn.setCursor(Qt.PointingHandCursor)
        self._btn_row.addWidget(self._primary_btn)
        self._btn_row.addWidget(self._secondary_btn)
        self._btn_row.addStretch()
        v.addLayout(self._btn_row)

    def set_status(self, data: dict):
        if not isinstance(data, dict) or not data:
            self.clear()
            return

        self._state = str(data.get("state", "not_synced"))
        self._data_origin = str(data.get("data_origin", "retrieval"))
        self._cid = str(data.get("canonical_id", "") or "")
        self._item_key = str(data.get("item_key", "") or "")

        state_to_badge = {
            "not_synced":           ("neutral", "未同步"),
            "checking":             ("info",    "检查中"),
            "synced":               ("success", "已同步"),
            "missing":              ("error",   "已失效"),
            "retrieval_overridden": ("info",    "双向存在"),
            "unverified":           ("warning", "暂未验证"),
            "invalid":              ("error",   "关联无效（需清理）"),
        }
        badge_status, badge_text = state_to_badge.get(
            self._state, ("neutral", "未知")
        )
        self._badge.set_status_and_text(badge_status, badge_text)

        # ---- 详情行 ----
        parts: list[str] = []
        if data.get("synced_at"):
            parts.append(f"同步时间：{data['synced_at']}")
        if data.get("link_mode"):
            parts.append(f"模式：{data['link_mode']}")
        if data.get("import_collection"):
            parts.append(f"分类：{data['import_collection']}")
        self._detail_label.setText("　".join(parts))

        # ---- 按钮分派 ----
        self._dispatch_buttons()

    def _dispatch_buttons(self):
        for btn in (self._primary_btn, self._secondary_btn):
            try:
                if btn.receivers(btn.clicked) > 0:
                    btn.clicked.disconnect()
            except (RuntimeError, TypeError):
                pass

        state = self._state
        origin = self._data_origin

        if state == "not_synced":
            self._primary_btn.setText("同步到 Zotero")
            self._primary_btn.setVisible(True)
            self._primary_btn.clicked.connect(
                lambda checked=False: self.sync_requested.emit(self._cid)
            )
            self._secondary_btn.setVisible(False)

        elif state == "checking":
            self._primary_btn.setVisible(False)
            self._secondary_btn.setVisible(False)

        elif state == "synced":
            self._primary_btn.setText("打开 Zotero 条目")
            self._primary_btn.setVisible(True)
            self._primary_btn.clicked.connect(
                lambda checked=False: self.open_requested.emit(
                    self._item_key
                )
            )
            self._secondary_btn.setText("撤回导入")
            self._secondary_btn.setVisible(True)
            self._secondary_btn.clicked.connect(
                lambda checked=False: self.withdraw_requested.emit(
                    self._cid
                )
            )

        elif state == "missing" and origin == "zotero_import":
            self._primary_btn.setText("重新同步")
            self._primary_btn.setVisible(True)
            self._primary_btn.clicked.connect(
                lambda checked=False: self.resync_requested.emit(
                    self._cid
                )
            )
            self._secondary_btn.setText("撤回导入")
            self._secondary_btn.setVisible(True)
            self._secondary_btn.clicked.connect(
                lambda checked=False: self.withdraw_requested.emit(
                    self._cid
                )
            )

        elif state == "missing" and origin == "retrieval":
            self._primary_btn.setText("重新同步")
            self._primary_btn.setVisible(True)
            self._primary_btn.clicked.connect(
                lambda checked=False: self.resync_requested.emit(
                    self._cid
                )
            )
            self._secondary_btn.setText("清理关联")
            self._secondary_btn.setVisible(True)
            self._secondary_btn.clicked.connect(
                lambda checked=False: self.clear_links_requested.emit(
                    self._cid
                )
            )

        elif state == "retrieval_overridden":
            self._primary_btn.setText("打开 Zotero 条目")
            self._primary_btn.setVisible(True)
            self._primary_btn.clicked.connect(
                lambda checked=False: self.open_requested.emit(
                    self._item_key
                )
            )
            self._secondary_btn.setText("清理关联")
            self._secondary_btn.setVisible(True)
            self._secondary_btn.clicked.connect(
                lambda checked=False: self.clear_links_requested.emit(
                    self._cid
                )
            )

        elif state == "unverified" and origin == "zotero_import":
            self._primary_btn.setText("🔄 重新验证")
            self._primary_btn.setVisible(True)
            self._primary_btn.clicked.connect(
                lambda checked=False: self.reverify_requested.emit(
                    self._cid
                )
            )
            self._secondary_btn.setText("撤回导入")
            self._secondary_btn.setVisible(True)
            self._secondary_btn.clicked.connect(
                lambda checked=False: self.withdraw_requested.emit(
                    self._cid
                )
            )

        elif state == "unverified" and origin == "retrieval":
            self._primary_btn.setText("🔄 重新验证")
            self._primary_btn.setVisible(True)
            self._primary_btn.clicked.connect(
                lambda checked=False: self.reverify_requested.emit(
                    self._cid
                )
            )
            self._secondary_btn.setText("清理关联")
            self._secondary_btn.setVisible(True)
            self._secondary_btn.clicked.connect(
                lambda checked=False: self.clear_links_requested.emit(
                    self._cid
                )
            )

        elif state == "invalid":
            self._primary_btn.setVisible(False)
            self._secondary_btn.setText("清理关联")
            self._secondary_btn.setVisible(True)
            self._secondary_btn.clicked.connect(
                lambda checked=False: self.clear_links_requested.emit(
                    self._cid
                )
            )

        else:
            self._primary_btn.setVisible(False)
            self._secondary_btn.setVisible(False)

        if self._collapsed:
            self._primary_btn.setVisible(False)
            self._secondary_btn.setVisible(False)

    def set_collapsed(self, collapsed: bool):
        collapsed = bool(collapsed)
        if self._collapsed == collapsed:
            return
        self._collapsed = collapsed
        self._detail_label.setVisible(not collapsed)
        self._expand_btn.setVisible(collapsed)
        if collapsed:
            self._primary_btn.setVisible(False)
            self._secondary_btn.setVisible(False)
        else:
            self._dispatch_buttons()


    def clear(self):
        self._cid = ""
        self._item_key = ""
        self._state = self.STATE_NOT_SYNCED
        self._data_origin = "retrieval"
        self._collapsed = False
        self._badge.set_status("neutral")
        self._detail_label.setText("")
        self._detail_label.setVisible(True)
        self._expand_btn.setVisible(False)

        for btn in (self._primary_btn, self._secondary_btn):
            try:
                if btn.receivers(btn.clicked) > 0:
                    btn.clicked.disconnect()
            except (RuntimeError, TypeError):
                pass
        self._primary_btn.setVisible(False)
        self._secondary_btn.setVisible(False)
        self.setVisible(False)