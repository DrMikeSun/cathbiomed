"""空状态提示（局部空态；区别于整页占位的 PlaceholderView）。

全内联样式无 QSS 依赖；居中显示可嵌任意容器。
"""

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel
)

from PySide6.QtCore import Qt

from ui.theme.palette import Palette


class EmptyState(QWidget):
    # ---- 默认符号 ----
    DEFAULT_SYMBOL = "∅"

    # ---- 符号样式 ----
    SYMBOL_FONT_SIZE = 44
    SYMBOL_COLOR = Palette.TEXT_MUTED

    # ---- 主文本样式 ----
    TITLE_FONT_SIZE = 16
    TITLE_COLOR = Palette.TEXT_SECONDARY

    # ---- 副文本样式 ----
    SUBTITLE_FONT_SIZE = 13
    SUBTITLE_COLOR = Palette.TEXT_MUTED

    # ---- 布局 ----
    V_MARGIN = 32            # 上下内边距
    H_MARGIN = 24            # 左右内边距
    V_SPACING = 10           # 各行之间的垂直间距

    # ---- QSS 兼容 objectName（当前无对应规则，样式靠内联） ----
    OBJECT_NAME_SYMBOL = "EmptyStateSymbol"
    OBJECT_NAME_TITLE = "EmptyStateTitle"
    OBJECT_NAME_SUBTITLE = "EmptyStateSubtitle"

    def __init__(
        self,
        title="暂无数据",
        subtitle="",
        symbol=None,
        parent=None
    ):
        super().__init__(parent)

        self._symbol_text = (
            symbol if symbol is not None else self.DEFAULT_SYMBOL
        )
        self._title_text = str(title) if title is not None else ""
        self._subtitle_text = str(subtitle) if subtitle is not None else ""

        self._build_ui()
        self._refresh_visibility()

    def _build_ui(self):

        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            self.H_MARGIN,
            self.V_MARGIN,
            self.H_MARGIN,
            self.V_MARGIN
        )
        outer.setSpacing(self.V_SPACING)
        outer.setAlignment(Qt.AlignCenter)

        # ---- 符号 ----
        self.symbol_label = QLabel(self._symbol_text)
        self.symbol_label.setObjectName(self.OBJECT_NAME_SYMBOL)
        self.symbol_label.setAlignment(Qt.AlignCenter)
        self.symbol_label.setStyleSheet(self._symbol_style())
        outer.addWidget(self.symbol_label)

        # ---- 主文本 ----
        self.title_label = QLabel(self._title_text)
        self.title_label.setObjectName(self.OBJECT_NAME_TITLE)
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setStyleSheet(self._title_style())
        outer.addWidget(self.title_label)

        # ---- 副文本 ----
        self.subtitle_label = QLabel(self._subtitle_text)
        self.subtitle_label.setObjectName(self.OBJECT_NAME_SUBTITLE)
        self.subtitle_label.setAlignment(Qt.AlignCenter)
        self.subtitle_label.setStyleSheet(self._subtitle_style())
        outer.addWidget(self.subtitle_label)

        # ---- 操作容器 ----
        self._actions = QHBoxLayout()
        self._actions.setContentsMargins(0, 0, 0, 0)
        self._actions.setSpacing(8)
        self._actions.setAlignment(Qt.AlignCenter)
        outer.addLayout(self._actions)

    def _symbol_style(self):
        return (
            f"color: {self.SYMBOL_COLOR};"
            f"font-size: {self.SYMBOL_FONT_SIZE}px;"
            f"font-weight: 300;"
        )

    def _title_style(self):
        return (
            f"color: {self.TITLE_COLOR};"
            f"font-size: {self.TITLE_FONT_SIZE}px;"
            f"font-weight: 600;"
            f"letter-spacing: 1px;"
        )

    def _subtitle_style(self):
        return (
            f"color: {self.SUBTITLE_COLOR};"
            f"font-size: {self.SUBTITLE_FONT_SIZE}px;"
            f"letter-spacing: 1px;"
        )

    def _refresh_visibility(self):
        """
        按当前文本状态刷新符号与副文本的可见性。
        空的文本不占用布局空间。
        """
        self.symbol_label.setVisible(bool(self._symbol_text))
        self.subtitle_label.setVisible(bool(self._subtitle_text))

    def set_symbol(self, symbol):
        """
        设置符号。传入空字符串或 None 时隐藏符号行。
        """
        self._symbol_text = str(symbol) if symbol else ""
        self.symbol_label.setText(self._symbol_text)
        self.symbol_label.setVisible(bool(self._symbol_text))

    def set_title(self, title):
        """设置主文本"""
        self._title_text = str(title) if title is not None else ""
        self.title_label.setText(self._title_text)

    def set_subtitle(self, subtitle):
        """
        设置副文本。传入空字符串时隐藏副文本行。
        """
        self._subtitle_text = str(subtitle) if subtitle else ""
        self.subtitle_label.setText(self._subtitle_text)
        self.subtitle_label.setVisible(bool(self._subtitle_text))

    def add_action(self, widget):
        """
        向操作区添加控件（通常是按钮）。
        可多次调用，按添加顺序水平排列。
        """
        self._actions.addWidget(widget)

    def clear_actions(self):
        """
        清空操作区。
        会从布局中移除控件，但不删除控件本身，
        调用方仍可复用被移除的按钮对象。
        """
        while self._actions.count():
            item = self._actions.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

    def title(self):
        """返回当前主文本"""
        return self._title_text

    def subtitle(self):
        """返回当前副文本"""
        return self._subtitle_text

    def symbol(self):
        """返回当前符号文本"""
        return self._symbol_text