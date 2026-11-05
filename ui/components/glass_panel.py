"""玻璃拟态面板基类（所有卡片/面板的视觉底座）。

子类覆写类变量（radius / panel_alpha / ...）自定义质感，无需重写 paintEvent。"""

from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect
)

from PySide6.QtGui import (
    QPainter,
    QPainterPath,
    QLinearGradient,
    QPen
)

from PySide6.QtCore import Qt

from ui.theme.palette import Palette, qcolor

class GlassPanel(QFrame):
    radius = 22
    panel_alpha = 20            # 表面
    top_glow_alpha = 35         # 顶部内发光
    border_alpha = 40           # 左右边框
    top_border_alpha = 90       # 顶边受光
    bottom_border_alpha = 100   # 底边背光

    shadow_blur = 40
    shadow_offset = (0, 16)
    shadow_alpha = 128

    def __init__(self, object_name="GlassPanel", parent=None):
        super().__init__(parent)

        self.setObjectName(object_name)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFrameShape(QFrame.NoFrame)
        self._cached_path: QPainterPath | None = None
        self._cached_rect = None
        self._cached_size: tuple[int, int] = (-1, -1)
        self._cached_radius: int = -1

        self.init_shadow()

    def init_shadow(self):

        self.shadow = QGraphicsDropShadowEffect()

        self.shadow.setBlurRadius(self.shadow_blur)
        self.shadow.setOffset(*self.shadow_offset)
        self.shadow.setColor(qcolor(Palette.SHADOW_BASE, self.shadow_alpha))

        if self.shadow_blur <= 0 and self.shadow_alpha <= 0:
            return
        self.setGraphicsEffect(self.shadow)

    def paintEvent(self, event):

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        size = (self.width(), self.height())
        radius = self.radius
        if (self._cached_path is None
                or self._cached_size != size
                or self._cached_radius != radius):
            rect = self.rect().adjusted(1, 1, -1, -1)
            path = QPainterPath()
            path.addRoundedRect(
                float(rect.x()),
                float(rect.y()),
                float(rect.width()),
                float(rect.height()),
                float(radius),
                float(radius),
            )
            self._cached_path = path
            self._cached_rect = rect
            self._cached_size = size
            self._cached_radius = radius

        path = self._cached_path
        rect = self._cached_rect
        r = radius

        painter.fillPath(
            path,
            qcolor(Palette.TEXT_PRIMARY, self.panel_alpha)
        )

        glow = QLinearGradient(
            float(rect.topLeft().x()),
            float(rect.topLeft().y()),
            float(rect.bottomLeft().x()),
            float(rect.bottomLeft().y())
        )
        glow.setColorAt(0.0, qcolor(Palette.TEXT_PRIMARY, self.top_glow_alpha))
        glow.setColorAt(0.5, qcolor(Palette.TEXT_PRIMARY, 0))

        painter.save()
        painter.setClipPath(path)
        painter.fillRect(rect, glow)
        painter.restore()

        pen = QPen(qcolor(Palette.TEXT_PRIMARY, self.top_border_alpha), 1)
        painter.setPen(pen)
        painter.drawLine(
            rect.left() + r,
            rect.top() + 1,
            rect.right() - r,
            rect.top() + 1
        )

        pen = QPen(qcolor(Palette.SHADOW_BASE, self.bottom_border_alpha), 1)
        painter.setPen(pen)
        painter.drawLine(
            rect.left() + r,
            rect.bottom() - 1,
            rect.right() - r,
            rect.bottom() - 1
        )

        pen = QPen(qcolor(Palette.TEXT_PRIMARY, self.border_alpha), 1)
        painter.setPen(pen)
        # 左
        painter.drawLine(
            rect.left() + 1,
            rect.top() + r,
            rect.left() + 1,
            rect.bottom() - r
        )
        # 右
        painter.drawLine(
            rect.right() - 1,
            rect.top() + r,
            rect.right() - 1,
            rect.bottom() - r
        )

        painter.end()

    def set_glow(self, color=None, alpha=80, blur=45):
        if color is None:
            color = Palette.CYAN

        c = qcolor(color, alpha)
        self.shadow.setColor(c)
        self.shadow.setBlurRadius(blur)

    def reset_shadow(self):
        self.shadow.setColor(qcolor(Palette.SHADOW_BASE, self.shadow_alpha))
        self.shadow.setBlurRadius(self.shadow_blur)
        self.shadow.setOffset(*self.shadow_offset)