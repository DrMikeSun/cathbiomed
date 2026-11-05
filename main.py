"""
MedLitFlow 顶层启动器。

职责
─────
    1. 把项目根加入 sys.path，支持从任意目录启动
    2. 调用 ui.app.main()

职责划分
─────────
- 本文件只做「路径处理 + 委派」，不重复任何 UI 逻辑。
- QSS 加载、应用元信息、基础字体、主窗口创建全部在 ui/app.py。
- 具体视图的组装在 ui/views/main_window.py。

启动方式
─────────
    python main.py                          # 从项目根启动
    python D:\\科研助手\\medlitflow\\main.py   # 从任意目录启动
    python -m ui.app                        # 等价方式（跳过本文件）
"""

import sys
from pathlib import Path


# ============================================================
# 路径处理 —— 必须在导入 ui.* 之前完成
# ============================================================
BASE_DIR = Path(__file__).resolve().parent

_src_dir = BASE_DIR / "src"
if _src_dir.is_dir() and str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


# ============================================================
# 委派给 ui.app
# ============================================================
from ui.app import main


if __name__ == "__main__":
    main()