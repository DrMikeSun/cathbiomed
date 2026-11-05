"""仪表盘统计卡片（标题 / 数值 / 标签）。

本卡片自带 DropShadowEffect，透明度动画须外层容器承载 opacity effect。"""
from PySide6.QtWidgets import (
    QLabel,
    QSizePolicy,
    QVBoxLayout
)

from PySide6.QtCore import Qt

from ui.config import (
    FONT_UI_FAMILY,
    STAT_CARD_MIN_WIDTH,
    STAT_CARD_MIN_HEIGHT,
)
from ui.components.glass_panel import GlassPanel
from ui.theme.palette import Palette

class StatCard(GlassPanel):
    """仪表盘统计卡片。"""

    # ---- 默认光晕参数（强调卡片） ----
    GLOW_COLOR = Palette.CYAN
    GLOW_ALPHA = 90
    GLOW_START_BLUR = 45
    GLOW_END_BLUR = 65
    GLOW_DURATION = 1500

    OBJECT_NAME = "QuantumCard"
    OBJECT_NAME_TITLE = "CardTitle"
    OBJECT_NAME_VALUE = "CardValue"
    OBJECT_NAME_TAG = "CardTag"

    # ---- 最小尺寸 ----
    MIN_WIDTH = STAT_CARD_MIN_WIDTH
    MIN_HEIGHT = STAT_CARD_MIN_HEIGHT

    # ---- 小窗口自适应 ----
    _VALUE_FONT_SIZES = (32, 28, 24, 20, 16)

    # ---- 降级提示 ----
    _GLOW_WARNED = False

    def __init__(
        self,
        title,
        value,
        tag="",
        unit="",
        glow=False,
        parent=None
    ):
        super().__init__(self.OBJECT_NAME)

        self.setMinimumSize(self.MIN_WIDTH, self.MIN_HEIGHT)

        self._raw_value = value      # 原始数值，供 set_unit 重新格式化
        self._unit = unit            # 单位后缀，如 "%" / " 篇" / ""
        self._glow_anim = None
        self._build_ui(title, tag)
        self._refresh_value_label()


        if glow:
            self._start_glow()

    def _build_ui(self, title, tag):

        layout = QVBoxLayout()
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(6)

        self.title_label = QLabel(title)
        self.title_label.setObjectName(self.OBJECT_NAME_TITLE)
        self.title_label.setMinimumWidth(0)
        self.title_label.setSizePolicy(
            QSizePolicy.Ignored, QSizePolicy.Preferred,
        )
        self.title_label.setToolTip(str(title) if title else "")
        self.value_label = QLabel("")
        self.value_label.setObjectName(self.OBJECT_NAME_VALUE)
        self.value_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.value_label.setMinimumWidth(0)
        self.value_label.setSizePolicy(
            QSizePolicy.Ignored, QSizePolicy.Preferred,
        )

        self.tag_label = QLabel(tag)
        self.tag_label.setObjectName(self.OBJECT_NAME_TAG)
        self.tag_label.setMinimumWidth(0)
        self.tag_label.setSizePolicy(
            QSizePolicy.Ignored, QSizePolicy.Preferred,
        )
        self.tag_label.setToolTip(str(tag) if tag else "")

        layout.addWidget(self.title_label)
        layout.addWidget(self.value_label)
        layout.addStretch()
        layout.addWidget(self.tag_label)

        self.setLayout(layout)

    def _format_value(self, value):
        if value is None:
            return "—"
        if self._unit:
            return f"{value} {self._unit}"
        return str(value)
    
    def _refresh_value_label(self):
        text = self._format_value(self._raw_value)
        self.value_label.setText(text)
        self.value_label.setToolTip(text)
        # 需求4：立即尝试适配（若 width 已确定）
        self._fit_value_font()

    def _fit_value_font(self):
        try:
            width = self.value_label.width()
        except RuntimeError:
            return
        if width <= 0:
            return

        text = self.value_label.text()
        if not text:
            return

        from PySide6.QtGui import QFont, QFontMetrics
        base_family = FONT_UI_FAMILY
        chosen_size = self._VALUE_FONT_SIZES[-1]
        for size in self._VALUE_FONT_SIZES:
            f = QFont(base_family)
            f.setPixelSize(size)
            fm = QFontMetrics(f)
            if fm.horizontalAdvance(text) <= width - 8:
                chosen_size = size
                break

        enable_wrap = chosen_size <= 20
        if self.value_label.wordWrap() != enable_wrap:
            self.value_label.setWordWrap(enable_wrap)

        self.value_label.setStyleSheet(
            f"color: {Palette.CYAN};"
            f"font-size: {chosen_size}px;"
            f"font-weight: 700;"
        )

    def _start_glow(self):
        try:
            from ui.animations.glow import GlowAnimation
        except ImportError:
            if not StatCard._GLOW_WARNED:
                try:
                    from ui.theme.loader import emit_warning
                    emit_warning(
                        "[StatCard] GlowAnimation 暂不可用，"
                        "已跳过光晕。请完成 ui/animations/glow.py 后重试。",
                        level="warning",
                    )
                except Exception:
                    pass
                StatCard._GLOW_WARNED = True
            return

        self._glow_anim = GlowAnimation(
            self,
            color=self.GLOW_COLOR,
            alpha=self.GLOW_ALPHA,
            start_blur=self.GLOW_START_BLUR,
            end_blur=self.GLOW_END_BLUR,
            duration=self.GLOW_DURATION
        )
        self._glow_anim.start()

    def stop_pulse(self):
        if self._glow_anim is not None:
            self._glow_anim.stop()

    def start_pulse(self):
        if self._glow_anim is not None and not self._glow_anim.is_active():
            self._glow_anim.start()

    def set_glow_color(self, color, alpha=None):
        if self._glow_anim is not None:
            self._glow_anim.set_color(color, alpha)
        else:
            self.set_glow(
                color,
                alpha=alpha if alpha is not None else self.GLOW_ALPHA,
                blur=self.GLOW_START_BLUR
            )

    def set_title(self, title):
        text = str(title)
        self.title_label.setText(text)
        self.title_label.setToolTip(text)

    def set_value(self, value):
        self._raw_value = value
        self._refresh_value_label()

    def set_tag(self, tag):
        text = str(tag)
        self.tag_label.setText(text)
        self.tag_label.setToolTip(text)

    def set_unit(self, unit):
        self._unit = unit or ""
        self._refresh_value_label()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_value_font()

    def value(self):
        return self._raw_value

    def unit(self):
        return self._unit