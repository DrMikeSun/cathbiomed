"""QSS 加载器：读 theme.qss，用 Palette.tokens() 替换 $VAR。"""

from pathlib import Path
from string import Template

from ui.theme.palette import Palette

_THEME_DIR = Path(__file__).resolve().parent
QSS_PATH = _THEME_DIR / "theme.qss"

# 回调未注册前，警告先缓存；register_log_callback 时 flush
_log_callback = None
_pending_warnings: list = []

_PENDING_WARNINGS_MAX = 100

def register_log_callback(callback) -> None:
    """注册日志回调并 flush 缓存。callback(level, message)。"""
    global _log_callback
    _log_callback = callback

    # flush 缓存
    pending = list(_pending_warnings)
    _pending_warnings.clear()
    for level, msg in pending:
        try:
            callback(level, msg)
        except Exception:
            pass

def _emit_warning(message: str, level: str = "warning") -> None:
    """print 兜底 + 回调/缓存；缓存超 100 条丢最旧。"""
    print(message)
    if _log_callback is not None:
        try:
            _log_callback(level, message)
        except Exception:
            pass
    else:
        if len(_pending_warnings) >= _PENDING_WARNINGS_MAX:
            _pending_warnings.pop(0)
        _pending_warnings.append((level, message))

def emit_warning(message: str, level: str = "warning") -> None:
    """公开警告出口（等价 _emit_warning）。"""
    _emit_warning(message, level)

def _scan_residual_placeholders(qss: str):
    """扫描残留 $VAR（忽略 $$ 转义），返回去重集合。"""
    residuals = set()
    i = 0
    n = len(qss)
    while i < n:
        ch = qss[i]
        if ch == "$":
            # $$ 为 string.Template 的转义，跳过
            if i + 1 < n and qss[i + 1] == "$":
                i += 2
                continue
            # 读取 $NAME
            j = i + 1
            name_chars = []
            while j < n and (qss[j].isalnum() or qss[j] == "_"):
                name_chars.append(qss[j])
                j += 1
            if name_chars:
                residuals.add("$" + "".join(name_chars))
                i = j
                continue
        i += 1
    return residuals

def load_stylesheet() -> str:
    """读 theme.qss 注入 Palette。文件缺失返空串；缺失 key 退化 safe_substitute。"""

    if not QSS_PATH.exists():
        _emit_warning(
            f"[QSS] 未找到样式文件：{QSS_PATH}\n"
            f"[QSS] 应用将以无样式状态启动。",
            level="error",
        )
        return ""

    template_text = QSS_PATH.read_text(encoding="utf-8")
    template = Template(template_text)

    tokens = Palette.tokens()

    try:
        qss = template.substitute(tokens)
    except KeyError as e:
        _emit_warning(
            f"[QSS] 缺少占位符：{e}\n"
            f"[QSS] Palette 提供 {len(tokens)} 个 token："
            f"{', '.join(sorted(tokens.keys()))}\n"
            f"[QSS] 退化为 safe_substitute —— 未替换的占位符将保留原文。",
            level="warning",
        )
        qss = template.safe_substitute(tokens)

    # ---- 残留占位符检查 ----
    residuals = _scan_residual_placeholders(qss)
    if residuals:
        _emit_warning(
            f"[QSS] 警告：仍有 {len(residuals)} 个占位符未替换："
            f"{', '.join(sorted(residuals))}\n"
            f"[QSS] 请检查 theme.qss 拼写，或在 palette.py 中补充对应 token。",
            level="warning",
        )

    return qss