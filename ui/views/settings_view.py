"""设置视图（PAGE_SETTINGS：SECTIONS 驱动 + 保存/恢复）。

字段类型：lineedit / password / browse / combo / checkbox / spinbox /
doublespinbox。browse 由父组件打开对话框后经 set_field_value 回填。"""

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QComboBox,
    QCheckBox,
    QSpinBox,
    QDoubleSpinBox,
    QPushButton,
    QScrollArea,
    QFrame,
    QSizePolicy,
)

from PySide6.QtCore import Qt, Signal

from ui.theme.palette import Palette
from ui.components.section_header import SectionHeader
from ui.components.glass_panel import GlassPanel

class _WheelSafeSpinBox(QSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class _WheelSafeDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class _WheelSafeComboBox(QComboBox):
    def wheelEvent(self, event):
        event.ignore()

class SettingsView(QWidget):
    save_requested = Signal(dict)
    reset_requested = Signal()
    browse_requested = Signal(str)

    CONTENT_MARGINS = (40, 28, 40, 28)
    CONTENT_SPACING = 20
    SECTIONS_SPACING = 16

    TITLE = "系统设置"
    SUBTITLE = "配置数据源、存储路径与采集管线参数"

    OBJECT_NAME_SECTION = "SettingsSection"

    SECTIONS = [
        {
            "title": "API 密钥",
            "fields": [
                {"key": "ncbi_api_key", "label": "NCBI API Key",
                 "type": "password", "default": ""},
                {"key": "s2_api_key", "label": "Semantic Scholar API Key",
                 "type": "password", "default": ""},
                # OpenAlex 2026-02-13 起 API key 必需（免费：openalex.org/settings/api-key）
                {"key": "openalex_api_key", "label": "OpenAlex API Key",
                 "type": "password", "default": ""},
                {"key": "api_email", "label": "联系邮箱（NCBI/OpenAlex）",
                 "type": "lineedit", "default": ""},
                {"key": "reviewer_id", "label": "筛选人（PRISMA 署名）",
                 "type": "lineedit", "default": ""},
            ],
        },
        {
            "title": "数据源",
            "fields": [
                {"key": "pubmed_enabled", "label": "PubMed",
                 "type": "checkbox", "default": True},
                {"key": "pmc_enabled", "label": "PubMed Central (PMC)",
                 "type": "checkbox", "default": True},
                {"key": "europepmc_enabled", "label": "Europe PMC",
                 "type": "checkbox", "default": True},
                {"key": "crossref_enabled", "label": "Crossref",
                 "type": "checkbox", "default": True},
                {"key": "openalex_enabled", "label": "OpenAlex",
                 "type": "checkbox", "default": True},
                {"key": "default_limit", "label": "每源每查询上限",
                 "type": "spinbox", "default": 1000,
                 "min": 1, "max": 10000},
                {"key": "max_hits_threshold", "label": "总检索上限",
                 "type": "spinbox", "default": 5000,
                 "min": 100, "max": 100000},
            ],
        },
        {
            "title": "存储路径",
            "fields": [
                {"key": "data_dir", "label": "数据根目录（谨慎修改）",
                 "type": "browse", "default": ""},
            ],
        },
        {
            "title": "去重策略",
            "fields": [
                {"key": "dedup_threshold",
                 "label": "摘要变体相似度阈值",
                 "type": "doublespinbox", "default": 0.05,
                 "min": 0.0, "max": 1.0, "step": 0.01, "decimals": 2},
            ],
        },
        {
            "title": "PDF 下载",
            "fields": [
                {"key": "pdf_concurrency",
                 "label": "并发数（parallel=true 时生效）",
                 "type": "spinbox", "default": 4, "min": 1, "max": 32},
                {"key": "pdf_timeout",
                 "label": "下载超时（秒）",
                 "type": "spinbox", "default": 30, "min": 5, "max": 120},
                {"key": "pdf_retries",
                 "label": "每篇最多尝试几个来源",
                 "type": "spinbox", "default": 3, "min": 1, "max": 5},
                {"key": "retrieval_max_retries",
                 "label": "每个来源最多重试几次",
                 "type": "spinbox", "default": 1, "min": 1, "max": 5},
            ],
        },
        {
            "title": "导出配置",
            "fields": [
                {"key": "export_user_dir", "label": "导出目录",
                 "type": "browse", "default": ""},
                {"key": "export_mode", "label": "导出模式",
                 "type": "combo", "default": "symlink",
                 "options": ["symlink", "copy", "none"]},
                {"key": "export_auto", "label": "运行后自动导出",
                 "type": "checkbox", "default": False},
                {"key": "export_subdir_template", "label": "子目录模板",
                 "type": "lineedit", "default": "{strategy}_{date}"},
            ],
        },
        {
            "title": "Zotero 连接",
            "fields": [
                {"key": "zotero_library_type", "label": "库类型",
                 "type": "combo", "default": "user",
                 "options": ["user", "group"]},
                {"key": "zotero_library_id", "label": "库 ID（个人填 0）",
                 "type": "lineedit", "default": "0"},
                {"key": "zotero_link_mode", "label": "附件模式",
                 "type": "combo", "default": "imported_file",
                 "options": ["imported_file", "linked_file"]},
                {"key": "zotero_stale_warning_days",
                 "label": "失效提醒（天）",
                 "type": "spinbox", "default": 90,
                 "min": 1, "max": 3650},
            ],
        },
    ]

    def __init__(self, parent=None):
        super().__init__(parent)

        self._widgets = {}

        self._build_ui()
        self._connect_signals()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*self.CONTENT_MARGINS)
        layout.setSpacing(self.CONTENT_SPACING)

        self.header = SectionHeader(self.TITLE, self.SUBTITLE)
        layout.addWidget(self.header)
        layout.addWidget(self._build_scroll_area(), stretch=1)
        layout.addLayout(self._build_bottom_bar())

    def _build_scroll_area(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )

        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(self.SECTIONS_SPACING)

        self._section_panels: dict = {}
        for section in self.SECTIONS:
            panel = self._build_section(section)
            title = str(section.get("title", "") or "")
            if title:
                self._section_panels[title] = panel
            v.addWidget(panel)

        v.addStretch()
        scroll.setWidget(container)
        self._scroll = scroll
        return scroll

    def _build_section(self, section):
        panel = GlassPanel(self.OBJECT_NAME_SECTION)
        panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

        v = QVBoxLayout(panel)
        v.setContentsMargins(24, 20, 24, 20)
        v.setSpacing(14)

        title = QLabel(section["title"])
        title.setStyleSheet(self._section_title_style())
        v.addWidget(title)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        grid.setColumnMinimumWidth(0, 200)
        grid.setColumnStretch(1, 1)

        for row, field in enumerate(section.get("fields", [])):
            label = QLabel(field.get("label", ""))
            label.setStyleSheet(self._field_label_style())
            label.setAlignment(Qt.AlignVCenter | Qt.AlignRight)
            grid.addWidget(label, row, 0)

            widget = self._build_field_widget(field)
            grid.addWidget(widget, row, 1)

        v.addLayout(grid)
        return panel

    def _build_field_widget(self, field):
        key = field.get("key", "")
        ftype = field.get("type", "lineedit")
        default = field.get("default")

        if ftype == "checkbox":
            w = QCheckBox()
            w.setChecked(bool(default))
            w.setCursor(Qt.PointingHandCursor)
            self._widgets[key] = w
            return w

        if ftype == "combo":
            w = _WheelSafeComboBox()
            w.setCursor(Qt.PointingHandCursor)
            for opt in field.get("options", []):
                w.addItem(str(opt))
            if default is not None:
                idx = w.findText(str(default))
                if idx >= 0:
                    w.setCurrentIndex(idx)
            self._widgets[key] = w
            return w

        if ftype == "spinbox":
            w = _WheelSafeSpinBox()
            w.setRange(
                int(field.get("min", 0)),
                int(field.get("max", 999999)),
            )
            w.setValue(int(default if default is not None else 0))
            self._widgets[key] = w
            return w

        if ftype == "doublespinbox":
            w = _WheelSafeDoubleSpinBox()
            w.setRange(
                float(field.get("min", 0.0)),
                float(field.get("max", 999999.0)),
            )
            w.setDecimals(int(field.get("decimals", 2)))
            w.setSingleStep(float(field.get("step", 0.1)))
            w.setValue(float(default if default is not None else 0.0))
            self._widgets[key] = w
            return w

        if ftype == "password":
            w = QLineEdit()
            w.setEchoMode(QLineEdit.EchoMode.Password)
            w.setText(str(default) if default is not None else "")
            w.setPlaceholderText("（未设置；输入后显示为圆点）")
            self._widgets[key] = w
            return w

        if ftype == "browse":
            container = QWidget()
            h = QHBoxLayout(container)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(8)

            edit = QLineEdit()
            edit.setText(str(default) if default is not None else "")
            edit.setReadOnly(True)
            h.addWidget(edit, stretch=1)

            btn = QPushButton("浏览...")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(
                lambda checked=False, k=key: self.browse_requested.emit(k)
            )
            h.addWidget(btn)

            self._widgets[key] = edit
            return container

        if ftype == "lineedit":
            w = QLineEdit()
            w.setText(str(default) if default is not None else "")
            self._widgets[key] = w
            return w

        placeholder = QLabel(f"[未知字段类型: {ftype}]")
        placeholder.setStyleSheet(
            f"color: {Palette.DANGER_RED}; font-size: 12px;"
        )
        return placeholder

    def _build_bottom_bar(self):
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch()

        self.reset_btn = QPushButton("恢复默认")
        self.reset_btn.setCursor(Qt.PointingHandCursor)
        row.addWidget(self.reset_btn)

        self.save_btn = QPushButton("保存")
        self.save_btn.setObjectName("PrimaryCTA")
        self.save_btn.setCursor(Qt.PointingHandCursor)
        row.addWidget(self.save_btn)

        return row

    def _connect_signals(self):
        self.save_btn.clicked.connect(self._on_save)
        self.reset_btn.clicked.connect(self._on_reset)

    def _on_save(self):
        self.save_requested.emit(self.values())

    def _on_reset(self):
        self.reset()
        self.reset_requested.emit()

    def _section_title_style(self):
        return (
            f"color: {Palette.TEXT_PRIMARY};"
            f"font-size: 16px;"
            f"font-weight: bold;"
            f"letter-spacing: 1px;"
        )

    def _field_label_style(self):
        return (
            f"color: {Palette.TEXT_SECONDARY};"
            f"font-size: 13px;"
        )

    def values(self):
        result = {}
        for key, w in self._widgets.items():
            if isinstance(w, QCheckBox):
                result[key] = w.isChecked()
            elif isinstance(w, QComboBox):
                result[key] = w.currentText()
            elif isinstance(w, QDoubleSpinBox):
                result[key] = w.value()
            elif isinstance(w, QSpinBox):
                result[key] = w.value()
            elif isinstance(w, QLineEdit):
                result[key] = w.text()
        return result

    def set_values(self, data):
        if not isinstance(data, dict):
            return

        for key, value in data.items():
            w = self._widgets.get(key)
            if w is None:
                continue

            if isinstance(w, QCheckBox):
                w.setChecked(bool(value))
            elif isinstance(w, QComboBox):
                target = "" if value is None else str(value)
                idx = w.findText(target, Qt.MatchFixedString)
                if idx < 0 and target:
                    target_lower = target.strip().lower()
                    for i in range(w.count()):
                        if w.itemText(i).strip().lower() == target_lower:
                            idx = i
                            break
                if idx >= 0:
                    w.setCurrentIndex(idx)
            elif isinstance(w, QDoubleSpinBox):
                try:
                    w.setValue(float(value))
                except (TypeError, ValueError):
                    pass
            elif isinstance(w, QSpinBox):
                try:
                    w.setValue(int(value))
                except (TypeError, ValueError):
                    pass
            elif isinstance(w, QLineEdit):
                w.setText(str(value) if value is not None else "")

    def set_field_value(self, key, value):
        self.set_values({key: value})

    def reset(self):
        defaults = {}
        for section in self.SECTIONS:
            for field in section.get("fields", []):
                defaults[field["key"]] = field.get("default")
        self.set_values(defaults)

    def scroll_to_section(self, title: str) -> bool:
        name = str(title or "")
        if not name:
            return False
        panels = getattr(self, "_section_panels", None)
        if not panels:
            return False
        panel = panels.get(name)
        if panel is None:
            return False
        scroll = getattr(self, "_scroll", None)
        if scroll is None:
            return False
        try:
            scroll.ensureWidgetVisible(panel, 0, 0)
            return True
        except Exception:
            return False

    def field_keys(self):
        keys = []
        for section in self.SECTIONS:
            for field in section.get("fields", []):
                keys.append(field["key"])
        return keys

    def field_count(self):
        return len(self._widgets)

    def widget(self, key):
        return self._widgets.get(key)