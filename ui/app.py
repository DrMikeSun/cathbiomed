"""
MedLitFlow UI 应用入口。

职责
─────
    1. 创建 QApplication
    2. 设置应用元信息与基础字体
    3. 加载 QSS（由 ui.theme.loader 提供）
    4. 创建并显示主窗口
    5. 启动事件循环

设计原则
─────────
- 本文件属于 ui 包，不承担路径处理职责。
  路径处理由项目根的 main.py 完成（把项目根加入 sys.path）。
- QSS 加载逻辑集中在 ui.theme.loader，本文件不重复实现。
- 主窗口延迟导入，避免在 app 创建前触发不必要的副作用。
- 基础字体作为 QSS 未覆盖控件的回退，与 theme.qss 的 font-family 一致。
- 无自绘、无 effect、无循环动画。

启动方式
─────────
    python main.py                # 从项目根启动（推荐）
    python -m ui.app              # 从项目根启动（等价）
    python path/to/main.py        # 从任意目录启动（由根 main.py 处理路径）
"""

import sys

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont

from ui.config import APP_NAME, APP_ORG, APP_VERSION
from ui.theme.loader import load_stylesheet


# ============================================================
# 应用入口
# ============================================================
def main():
    """
    启动 MedLitFlow UI。

    返回：
        退出码（通常由 sys.exit 使用）
    """

    app = QApplication(sys.argv)

    # ---- 应用元信息（影响 QSettings / 任务栏 / 窗口管理） ----
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORG)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)

    # ---- 基础字体（作为 QSS 未覆盖控件的回退） ----
    # 2026-10-27 修复（6A）：从 pointSize=10（≈13.3px）改为 pixelSize=14
    # 根因：theme.qss 的 `* { font-size: 14px; }` 使用像素单位，base_font
    #       用点单位（10pt）导致 QSS 未覆盖的少数控件（QMenu / QToolTip 等）
    #       字号偏小 0.7px，视觉上"轻微不齐"。
    base_font = QFont("Microsoft YaHei UI")
    base_font.setPixelSize(14)
    base_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(base_font)

    # ---- 加载样式（必须在创建窗口前） ----
    qss = load_stylesheet()
    if qss:
        app.setStyleSheet(qss)

    # ---- 创建主窗口（延迟导入，确保路径与样式已就绪） ----
    from ui.views.main_window import MedLitFlowMainWindow

    window = MedLitFlowMainWindow()
    window.show()
    window._connect_bridge(None)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()