"""UIBridgeInteractor：pipeline.Interactor Protocol 的 Qt 实现。

4 协议方法签名必须与 pipeline.Interactor 逐字一致；不导入 medlitflow.pipeline。
Worker 线程调 4 方法 → Signal.emit（线程安全）→ 主线程 Slot 更新 UI。"""

from __future__ import annotations

from typing import TYPE_CHECKING
from PySide6.QtCore import QObject, Signal

if TYPE_CHECKING:
    from PySide6.QtCore import QMutex, QWaitCondition
    
class UIBridgeInteractor(QObject):
    """pipeline.Interactor 的 Qt 实现（4 协议方法 + 4 生命周期 Signal）。"""
    notified = Signal(str, str)             # (level, message)
    progressed = Signal(str, int, int)      # (stage, current, total)
    logged = Signal(str)                    # (message)
    prompt_requested = Signal(str, list)    # (text, choices)  — v1.6 完整实现用
    started = Signal()                      # 无参；作为"运行已开始"事件通知
    finished = Signal(str)                  # (run_root)
    failed = Signal(str)                    # (error)
    interrupted = Signal()                  # 中断已响应

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._prompt_result: str = ""
        self._prompt_condition: QWaitCondition | None = None
        self._prompt_mutex: QMutex | None = None

    def notify(self, level: str, message: str) -> None:
        self.notified.emit(str(level), str(message))

    def progress(self, stage: str, current: int, total: int) -> None:
        self.progressed.emit(str(stage), int(current), int(total))

    def log(self, message: str) -> None:
        self.logged.emit(str(message))

    def prompt(self, text: str, choices: list[str]) -> str:
        if not choices:
            return ""
        from PySide6.QtCore import QThread
        from PySide6.QtWidgets import QApplication, QInputDialog
        app = QApplication.instance()
        if app is None:
            return ""
        if QThread.currentThread() == app.thread():
            items = [str(c) for c in choices]
            try:
                choice, ok = QInputDialog.getItem(
                    None, "请选择", str(text or ""), items, 0, False,
                )
            except RuntimeError:
                return ""
            if not ok or not choice:
                return ""
            return str(choice)
        from PySide6.QtCore import QMutex, QWaitCondition
        mutex = QMutex()
        cond = QWaitCondition()
        self._prompt_mutex = mutex
        self._prompt_condition = cond
        self._prompt_result = ""
        mutex.lock()
        try:
            self.prompt_requested.emit(
                str(text or ""), [str(c) for c in choices],
            )
            woke = cond.wait(mutex, 60000)
            if not woke:
                return ""
            return self._prompt_result
        finally:
            mutex.unlock()
            self._prompt_mutex = None
            self._prompt_condition = None

    def respond_prompt(self, choice: str) -> None:
        if self._prompt_mutex is None or self._prompt_condition is None:
            return
        self._prompt_mutex.lock()
        try:
            self._prompt_result = str(choice or "")
            self._prompt_condition.wakeAll()
        finally:
            self._prompt_mutex.unlock()

    def emit_started(self) -> None:
        self.started.emit()

    def emit_finished(self, run_root: str) -> None:
        self.finished.emit(str(run_root))

    def emit_failed(self, error: str) -> None:
        self.failed.emit(str(error))

    def emit_interrupted(self) -> None:
        self.interrupted.emit()