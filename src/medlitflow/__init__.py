"""CathBiomed — 医学文献采集工作台

本文件只暴露 __version__，禁止导入任何业务模块：
- pipeline / screening / archive 等业务模块会 import medlitflow，
  若本文件反向 import 它们，会触发循环。
- UI 层（ui/config.py）通过 from medlitflow import __version__ 读取版本号。
"""

__version__ = "1.5"

__all__ = ["__version__"]