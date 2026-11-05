"""按钮按压反馈（opacity 短暂降低）。"""

from PySide6.QtCore import QPropertyAnimation, QEasingCurve
from PySide6.QtWidgets import QGraphicsOpacityEffect

from ui.config import ANIM_DURATION_FAST

# 公开接口
def button_press(button, duration=None, press_opacity=0.7):
    """按钮按下反馈。已有非 opacity effect 时静默跳过。"""
    if duration is None:
        duration = ANIM_DURATION_FAST

    # ---- 1. 检查 effect 冲突 ----
    try:
        existing = button.graphicsEffect()
    except RuntimeError:
        return

    if existing is not None and \
       not isinstance(existing, QGraphicsOpacityEffect):
        return

    # ---- 2. 停止旧动画 ----
    old_anim = getattr(button, "_press_anim", None)
    if old_anim is not None:
        try:
            old_anim.stop()
        except RuntimeError:
            pass

    # ---- 3. 复用或创建 opacity effect ----
    if isinstance(existing, QGraphicsOpacityEffect):
        effect = existing
    else:
        effect = QGraphicsOpacityEffect(button)
        button.setGraphicsEffect(effect)
    button._press_effect = effect

    # ---- 4. 复用或创建 animation ----
    anim = getattr(button, "_press_anim", None)
    target_ok = False
    if anim is not None:
        try:
            target_ok = (anim.targetObject() is effect)
        except RuntimeError:
            target_ok = False

    if not target_ok:
        anim = QPropertyAnimation(effect, b"opacity", button)
        anim.setDuration(duration)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        button._press_anim = anim
    else:
        anim.setDuration(duration)

    # ---- 5. 启动 ----
    try:
        anim.stop()
    except RuntimeError:
        return

    current = effect.opacity()
    anim.setStartValue(current)
    anim.setKeyValueAt(0.5, press_opacity)
    anim.setEndValue(1.0)
    anim.start()

def button_pulse(button, duration=None):
    """更缓慢的脉冲（duration 默认 ANIM_DURATION_FAST * 2）。"""
    if duration is None:
        duration = ANIM_DURATION_FAST * 2
    button_press(button, duration=duration, press_opacity=0.6)