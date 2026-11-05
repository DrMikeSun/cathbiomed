"""QWidget 淡入淡出（复用 effect；绝不替换已有非 opacity effect）。"""

from PySide6.QtCore import QPropertyAnimation, QEasingCurve
from PySide6.QtWidgets import QGraphicsOpacityEffect

from ui.config import ANIM_DURATION_FADE

# 内部：准备 effect 与 animation
def _prepare(widget, duration):
    """返回 (effect, anim) 或 (None, None)。绝不替换已有非 opacity effect。"""

    # ---- 尝试读取 widget 当前的 graphicsEffect ----
    try:
        existing = widget.graphicsEffect()
    except RuntimeError:
        return None, None

    # ---- 场景 1：widget 已有 opacity effect —— 复用 ----
    if isinstance(existing, QGraphicsOpacityEffect):
        anim = getattr(widget, "_fade_anim", None)
        if anim is not None:
            try:
                anim.stop()
            except RuntimeError:
                anim = None

        # 缓存失效或 target 改变：重建 animation
        if anim is None:
            anim = QPropertyAnimation(existing, b"opacity", widget)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            widget._fade_anim = anim
        else:
            try:
                if anim.targetObject() is not existing:
                    anim = QPropertyAnimation(existing, b"opacity", widget)
                    anim.setEasingCurve(QEasingCurve.OutCubic)
                    widget._fade_anim = anim
            except RuntimeError:
                anim = QPropertyAnimation(existing, b"opacity", widget)
                anim.setEasingCurve(QEasingCurve.OutCubic)
                widget._fade_anim = anim

        anim.setDuration(duration)
        return existing, anim
    # ---- 场景 2：widget 已有非 opacity effect（如阴影） ----
    if existing is not None:
        return None, None

    # ---- 场景 3：widget 无 effect —— 创建并绑定 ----
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)

    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    anim.setDuration(duration)
    widget._fade_anim = anim

    return effect, anim

# 内部：统一执行 fade
def _run_fade(widget, start_value, end_value, duration, on_finished):
    """执行 fade。多次调用只触发最后一次回调。"""
    effect, anim = _prepare(widget, duration)
    # ---- 无法 fade：直接回调 ----
    if anim is None:
        if on_finished is not None:
            try:
                on_finished()
            except Exception:
                pass
        return
    # ---- 更新最新回调（供 _cleanup 读取） ----
    widget._fade_on_finished = on_finished
    # ---- 绑定 finished 清理回调（仅一次） ----
    if not getattr(anim, "_fade_cleanup_bound", False):
        def _cleanup():
            cb = getattr(widget, "_fade_on_finished", None)
            if cb is not None:
                try:
                    cb()
                except Exception:
                    pass

        anim.finished.connect(_cleanup)
        anim._fade_cleanup_bound = True
    # ---- 启动 ----
    try:
        anim.stop()
    except RuntimeError:
        return

    try:
        current = effect.opacity()
    except RuntimeError:
        current = start_value
    anim.setStartValue(current)
    anim.setEndValue(end_value)
    anim.start()

# 公开接口
def fade_in(widget, duration=None, on_finished=None):
    """opacity 0 → 1。已有阴影等非 opacity effect 时跳过并立即回调。"""
    if duration is None:
        duration = ANIM_DURATION_FADE
    _run_fade(widget, 0.0, 1.0, duration, on_finished)

def fade_out(widget, duration=None, on_finished=None):
    """opacity 1 → 0。已有阴影等非 opacity effect 时跳过并立即回调。"""
    if duration is None:
        duration = ANIM_DURATION_FADE
    _run_fade(widget, 1.0, 0.0, duration, on_finished)