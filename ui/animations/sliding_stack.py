"""滑动堆叠页面容器（QStackedWidget 兼容 + pos 动画切换）。

不用 QStackedWidget：QStackedLayout 会重置 page geometry，
覆盖 pos 动画。本类手动管理位置/尺寸，可自由动画。
"""

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import (
    QPropertyAnimation,
    QParallelAnimationGroup,
    QEasingCurve,
    QPoint,
    Signal
)

from ui.config import ANIM_DURATION_SLIDE

class SlidingStackedWidget(QWidget):
    """支持滑动切换的堆叠页面容器。"""
    currentChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._pages = []
        self._current_index = -1
        self._anim_group = None
        self._duration = ANIM_DURATION_SLIDE

    def addWidget(self, page):
        page.setParent(self)
        page.move(0, 0)
        page.resize(self.size())
        page.hide()

        self._pages.append(page)

        if self._current_index == -1:
            self.setCurrentIndex(0)

    def insertWidget(self, index, page):
        page.setParent(self)
        page.move(0, 0)
        page.resize(self.size())
        page.hide()

        self._pages.insert(index, page)

        if self._current_index == -1:
            self.setCurrentIndex(0)
        elif index <= self._current_index:
            self._current_index += 1

    def count(self):
        return len(self._pages)

    def widget(self, index):
        if 0 <= index < len(self._pages):
            return self._pages[index]
        return None

    def indexOf(self, page):
        try:
            return self._pages.index(page)
        except ValueError:
            return -1

    def currentIndex(self):
        return self._current_index

    def currentWidget(self):
        return self.widget(self._current_index)
    
    def setCurrentIndex(self, index):
        if not (0 <= index < len(self._pages)):
            return
        if index == self._current_index:
            return

        self._stop_anim()

        for p in self._pages:
            try:
                p.move(0, 0)
                p.resize(self.size())
            except RuntimeError:
                continue
            p.hide()

        page = self._pages[index]
        page.move(0, 0)
        page.resize(self.size())
        page.show()
        page.raise_()

        self._current_index = index
        self.currentChanged.emit(index)

    def setCurrentWidget(self, page):
        idx = self.indexOf(page)
        if idx >= 0:
            self.setCurrentIndex(idx)

    def slide_to_index(self, index, direction=1, duration=None):
        """滑动切换。direction=1 右滑入，-1 左滑入。"""
        if self._anim_group is not None:
            self._stop_anim()
            self._settle_pages()
        if index == self._current_index:
            return
        if not (0 <= index < len(self._pages)):
            return

        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            # 尺寸未确定，退化为无动画切换
            self.setCurrentIndex(index)
            return

        if duration is None:
            duration = self._duration

        current = self.currentWidget()
        target = self._pages[index]

        # ---- 计算起止位置 ----
        if direction >= 0:
            target_start = QPoint(w, 0)
            current_end = QPoint(-w, 0)
        else:
            target_start = QPoint(-w, 0)
            current_end = QPoint(w, 0)

        # ---- 准备目标页 ----
        target.setParent(self)
        target.move(target_start)
        target.resize(w, h)
        target.show()
        target.raise_()

        # ---- 准备当前页 ----
        if current is not None and current is not target:
            current.move(0, 0)
            current.resize(w, h)
            current.show()

        # ---- 当前页动画 ----
        anim_cur = None
        if current is not None and current is not target:
            anim_cur = QPropertyAnimation(current, b"pos", self)
            anim_cur.setDuration(duration)
            anim_cur.setStartValue(QPoint(0, 0))
            anim_cur.setEndValue(current_end)
            anim_cur.setEasingCurve(QEasingCurve.OutCubic)

        # ---- 目标页动画 ----
        anim_tgt = QPropertyAnimation(target, b"pos", self)
        anim_tgt.setDuration(duration)
        anim_tgt.setStartValue(target_start)
        anim_tgt.setEndValue(QPoint(0, 0))
        anim_tgt.setEasingCurve(QEasingCurve.OutCubic)

        # ---- 并行执行 ----
        group = QParallelAnimationGroup(self)
        if anim_cur is not None:
            group.addAnimation(anim_cur)
        group.addAnimation(anim_tgt)

        # 用默认参数捕获局部变量，避免潜在闭包陷阱
        group.finished.connect(
            lambda old=current, new=target, idx=index:
                self._on_slide_finished(old, new, idx)
        )

        self._anim_group = group
        group.start()

    def _on_slide_finished(self, old_page, new_page, index):
        """动画自然结束：归位、隐藏旧页、更新索引、发信号。"""
        if old_page is not None and old_page is not new_page:
            try:
                old_page.hide()
                old_page.move(0, 0)
            except RuntimeError:
                pass

        try:
            new_page.move(0, 0)
            new_page.resize(self.size())
        except RuntimeError:
            pass

        self._current_index = index
        self.currentChanged.emit(index)

        self._anim_group = None

    def _stop_anim(self):
        """停止动画组。stop() 不触发 finished，不会误调 _on_slide_finished。"""
        if self._anim_group is not None:
            try:
                self._anim_group.stop()
            except RuntimeError:
                pass
            self._anim_group = None

    def _settle_pages(self):
        """中断动画后归位所有页面（当前页显示，其余隐藏）。"""
        size = self.size()
        for i, p in enumerate(self._pages):
            try:
                p.move(0, 0)
                p.resize(size)
            except RuntimeError:
                continue
            if i == self._current_index:
                p.show()
                p.raise_()
            else:
                p.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._anim_group is not None:
            self._stop_anim()
            self._settle_pages()
        size = self.size()
        for p in self._pages:
            try:
                p.resize(size)
            except RuntimeError:
                continue