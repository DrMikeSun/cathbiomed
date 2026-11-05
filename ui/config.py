"""medlitflow UI 配置中枢（界面层常量）。"""

# 应用信息
APP_NAME = "CathBiomed"
APP_DISPLAY_NAME = "CathBiomed"
APP_ORG = "CathBiomed"

from medlitflow import __version__ as APP_VERSION

# 页面索引（与 Sidebar 菜单顺序一致）
PAGE_DASHBOARD = 0
PAGE_SEARCH = 1
PAGE_LITERATURE = 2
PAGE_SCREENING = 3
PAGE_ZOTERO = 4
PAGE_TASKS = 5
PAGE_LOGS = 6
PAGE_SETTINGS = 7

# 侧栏菜单定义
SIDEBAR_MENUS = [
    ("⌂", "仪表盘", PAGE_DASHBOARD),
    ("⌕", "检索", PAGE_SEARCH),
    ("▣", "文献库", PAGE_LITERATURE),
    ("☑", "筛选中心", PAGE_SCREENING),
    ("⇄", "Zotero", PAGE_ZOTERO),
    ("⟳", "采集任务", PAGE_TASKS),
    ("▤", "日志", PAGE_LOGS),
    ("⚙", "设置", PAGE_SETTINGS),
]

# 顶栏状态项定义
HEADER_STATUS_ITEMS = [
    ("RunState", "IDLE"),
]


# UI 尺寸
SIDEBAR_WIDTH_EXPANDED = 220
HEADER_HEIGHT = 64
DETAIL_PANEL_WIDTH = 420
STAT_CARD_MIN_WIDTH = 180
STAT_CARD_MIN_HEIGHT = 200
TABLE_ROW_HEIGHT = 42
TABLE_HEADER_HEIGHT = 44
DEFAULT_WINDOW_SIZE = (1600, 1080)

# 动画时长（毫秒）
ANIM_DURATION_FAST = 150
ANIM_DURATION_NORMAL = 260
ANIM_DURATION_SLOW = 400
ANIM_DURATION_SLIDE = 400
ANIM_DURATION_FADE = 300
ANIM_DURATION_GLOW = 1500

# 字体
FONT_UI_FAMILY = "Microsoft YaHei UI"
FONT_MONO_FAMILY = "Consolas, Courier New, monospace"

# 其他
# 默认日志最大行数（防止界面卡顿）
LOG_MAX_LINES = 2000