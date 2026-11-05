"""QWidget 脉冲光晕（复用 DropShadow effect；不替换非 shadow effect）。"""

from PySide6.QtWidgets import QGraphicsDropShadowEffect
from PySide6.QtCore import QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor

from ui.config import ANIM_DURATION_GLOW
from ui.theme.palette import Palette

class GlowAnimation:
    """强调卡片的脉冲光晕。"""

    # ---- 默认参数（与 GlassPanel 的深色方向性阴影一致） ----
    DEFAULT_COLOR = Palette.SHADOW_BASE
    DEFAULT_ALPHA = 128
    DEFAULT_START_BLUR = 35
    DEFAULT_END_BLUR = 55
    DEFAULT_DURATION = ANIM_DURATION_GLOW

    # ---- 新建 effect 时的默认偏移（与 GlassPanel 一致） ----
    NEW_EFFECT_OFFSET = (0, 16)

    def __init__(
        self,
        widget,
        color=None,
        alpha=None,
        start_blur=None,
        end_blur=None,
        duration=None
    ):
        self.widget = widget

        self.color = color or self.DEFAULT_COLOR
        self.alpha = alpha if alpha is not None else self.DEFAULT_ALPHA
        self.start_blur = (
            start_blur if start_blur is not None
            else self.DEFAULT_START_BLUR
        )
        self.end_blur = (
            end_blur if end_blur is not None
            else self.DEFAULT_END_BLUR
        )
        self.duration = (
            duration if duration is not None
            else self.DEFAULT_DURATION
        )

        self.animation = None
        self.effect = None

        self._acquire_effect()
   
    # 内部：获取或创建 drop shadow effect   
    def _acquire_effect(self):
        """复用 DropShadow effect；无 effect 时创建；非 shadow effect 跳过。"""
        try:
            existing = self.widget.graphicsEffect()
        except RuntimeError:
            # widget 已被销毁
            self.effect = None
            return
        if isinstance(existing, QGraphicsDropShadowEffect):
            # 复用已有阴影，保留 offset，只改 color / alpha
            self.effect = existing
            self._apply_color()
            return
        if existing is None:
            self.effect = QGraphicsDropShadowEffect(self.widget)
            self.effect.setOffset(*self.NEW_EFFECT_OFFSET)
            self._apply_color()
            self.widget.setGraphicsEffect(self.effect)
            return
        try:
            from ui.theme.loader import emit_warning
            emit_warning(
                f"[Glow] widget 已有 {type(existing).__name__}，"
                f"跳过光晕以避免替换 effect。",
                level="warning",
            )
        except Exception:
            pass
        self.effect = None
          
    def _apply_color(self):
        """应用 self.color / self.alpha 到 effect。"""
        if self.effect is None:
            return
        try:
            c = QColor(self.color)
            c.setAlpha(self.alpha)
            self.effect.setColor(c)
        except RuntimeError:
            # effect 已被 Qt 销毁
            self.effect = None

    def start(self):
        """启动循环脉冲；effect 不可用时静默跳过。"""
        if self.effect is None:
            return
        self.stop()
        try:
            self.animation = QPropertyAnimation(
                self.effect,
                b"blurRadius",
                self.widget
            )
            self.animation.setDuration(self.duration)
            self.animation.setStartValue(self.start_blur)
            self.animation.setEndValue(self.end_blur)
            self.animation.setEasingCurve(QEasingCurve.InOutSine)
            self.animation.setLoopCount(-1)
            self.animation.start()
        except RuntimeError:
            self.animation = None

    def stop(self):
        """停止脉冲。"""
        if self.animation is not None:
            try:
                self.animation.stop()
            except RuntimeError:
                pass
            self.animation = None

    def set_color(self, color, alpha=None):
        """运行中动态改色。"""
        self.color = color
        if alpha is not None:
            self.alpha = alpha
        self._apply_color()

    def is_active(self):
        """返回动画当前是否在运行"""
        if self.animation is None:
            return False
        try:
            return self.animation.state() == QPropertyAnimation.Running
        except RuntimeError:
            return False

    def has_effect(self):
        """返回当前是否绑定了一个可用的 drop shadow effect"""
        return self.effect is not None