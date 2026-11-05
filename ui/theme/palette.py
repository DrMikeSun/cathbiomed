"""MedLitFlow UI 主题色值中枢（QSS + Python 统一引用）。"""

from PySide6.QtGui import QColor

_QSS_TOKEN_NAMES = frozenset({
    # 基底色（原始 + 医学别名）
    "PITCH_DEEP", "PITCH_DARK", "PITCH_RAISED",
    "BACKGROUND_DEEP", "BACKGROUND_DARK", "BACKGROUND_RAISED",
    # 场景光源色
    "FLOODLIGHT", "REFLECT_BLUE", "AMBIENT_BLUE",
    # 文本色
    "TEXT_PRIMARY", "TEXT_SECONDARY", "TEXT_MUTED",
    "TEXT_ON_ACCENT", "TEXT_ON_LIGHT",
    # 强调色
    "CYAN", "PRIMARY_BLUE", "HIGHLIGHT", "ACCENT_PALE",
    # 语义色（原始 + 医学别名）
    "AI_GREEN", "RISK_ORANGE", "DANGER_RED", "TACTICAL_PURPLE",
    "STATUS_SUCCESS", "STATUS_WARNING", "STATUS_ERROR",
    "STATUS_PROCESSING", "STATUS_INFO",
    # 阴影
    "SHADOW_BASE",
    # 组合令牌
    "HEADER_BG", "SIDEBAR_BG", "TOOLTIP_BG", "DROPDOWN_BG",
    # 字体
    "FONT_UI", "FONT_MONO",
})

class Palette:
    """色值中枢。白名单内的字符串类属性会被 tokens() 注入 QSS。"""    
    # 基底色 —— 深海蓝黑    
    PITCH_DEEP = "#04080F"          # 最深的背景基底
    PITCH_DARK = "#0A1520"          # 主背景底色
    PITCH_RAISED = "#0C1626"        # 抬升层（备用）

    # 医学语义别名（同值）
    BACKGROUND_DEEP = PITCH_DEEP
    BACKGROUND_DARK = PITCH_DARK
    BACKGROUND_RAISED = PITCH_RAISED

    # 场景光源色
    FLOODLIGHT = "#69b4ff"          # 泛光灯冷白偏蓝（主光源）
    REFLECT_BLUE = "#006fff"        # 深钢蓝反射光
    AMBIENT_BLUE = "#0085ff"        # 环境氛围光

    # 文本色
    TEXT_PRIMARY = "#FFFFFF"        # 主文本
    TEXT_SECONDARY = "#C8CCD4"      # 次级文本
    TEXT_MUTED = "#9E9E9E"          # 弱化文本
    TEXT_ON_ACCENT = "#FFFFFF"      # 强调背景上的文字
    TEXT_ON_LIGHT = "#0A1520"       # 浅色背景上的深色文字

    # 强调色 —— 蓝色系
    CYAN = "#0085FF"                # 主强调
    PRIMARY_BLUE = "#006FFF"        # 深一档
    HIGHLIGHT = "#69B4FF"           # 高亮色
    ACCENT_PALE = "#E0FFFF"         # 极浅青（预留）

    # 语义色
    AI_GREEN = "#00D68F"            # 正向指标（绿）
    RISK_ORANGE = "#FF9F43"         # 风险提示（橙）
    DANGER_RED = "#FF5C5C"          # 警戒 / 错误（红）
    TACTICAL_PURPLE = "#7B6CFF"     # 处理中 / 战术（紫）

    # 医学语义别名（同值）
    STATUS_SUCCESS = AI_GREEN       # 成功 / 完成
    STATUS_WARNING = RISK_ORANGE    # 警告 / 待确认
    STATUS_ERROR = DANGER_RED       # 错误 / 失败
    STATUS_PROCESSING = TACTICAL_PURPLE  # 处理中 / 运行中
    STATUS_INFO = CYAN              # 信息 / 提示

    # 阴影
    SHADOW_BASE = "#0A0A0A"         # 深色方向性阴影基底

    # 组合令牌
    HEADER_BG = "rgba(8, 20, 40, 140)"      # 顶栏玻璃背景
    SIDEBAR_BG = "rgba(10, 22, 40, 120)"    # 侧栏玻璃背景
    TOOLTIP_BG = "rgba(10, 22, 38, 230)"    # Tooltip 背景
    DROPDOWN_BG = "rgba(16, 34, 56, 240)"   # 下拉框背景

    # 字体
    FONT_UI = '"Microsoft YaHei UI"'
    FONT_MONO = '"Consolas", "Courier New", monospace'

    @classmethod
    def tokens(cls):
        """返回白名单内的字符串类属性（供 QSS Template.substitute）。"""
        result = {}
        for key, value in vars(cls).items():
            if key not in _QSS_TOKEN_NAMES:
                continue
            if not isinstance(value, str):
                continue
            result[key] = value
        return result

def qcolor(color, alpha=None):
    """色值字符串 → QColor；alpha 可选覆盖。"""
    c = QColor(color)
    if alpha is not None:
        c.setAlpha(alpha)
    return c

def rgba_str(color, alpha):
    """生成 rgba(r, g, b, alpha) 字符串（动态 QSS 值用）。"""
    c = QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"