# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

# 1. 基础静态资源（必须打包）
datas = [
    ('ui/theme/theme.qss', 'ui/theme'), 
    ('config', 'config')
]
binaries = []

# 2. PySide6 只收集核心 DLL（不收集无用组件）
binaries += collect_dynamic_libs('PySide6')

# 3. pyarrow 稳健收集（防底层依赖缺失，这是医学数据不丢的底线）
binaries += collect_dynamic_libs('pyarrow')
hiddenimports = collect_submodules('pyarrow')

# 4. openpyxl 显式收集（pandas.read_excel 对 .xlsx 走延迟 import，静态分析抓不到）
hiddenimports += collect_submodules('openpyxl')

# 补充核心模块
hiddenimports += [
    'medlitflow',
    'ui',
]

# 4. 显式剔除无用模块（体积大头都在这里）
excludes = [
    'tkinter', 
    'matplotlib', 
    'scipy', 
    'IPython', 
    'pytest', 
    'ruff', 
    'mypy', 
    'sphinx',
    # --- 剔除 PySide6 未使用的巨型模块 ---
    'PySide6.QtWebEngineCore',
    'PySide6.QtWebEngineWidgets',
    'PySide6.QtWebEngine',
    'PySide6.QtQml',
    'PySide6.QtQuick',
    'PySide6.QtQuick3D',
    'PySide6.Qt3DCore',
    'PySide6.Qt3DRender',
    'PySide6.Qt3DAnimation',
    'PySide6.Qt3DInput',
    'PySide6.Qt3DLogic',
    'PySide6.QtMultimedia',
    'PySide6.QtMultimediaWidgets',
    'PySide6.QtSql',
    'PySide6.QtTest',
    'PySide6.QtBluetooth',
    'PySide6.QtPositioning',
    'PySide6.QtSensors',
    'PySide6.QtSerialPort',
    'PySide6.QtWebSockets',
    'PySide6.QtCharts',
    'PySide6.QtDataVisualization',
    'PySide6.QtPdf',
    'PySide6.QtPdfWidgets',
    'PySide6.QtDesigner',
    'PySide6.QtUiTools',
    'PySide6.QtDBus',
    'PySide6.QtX11Extras',
]

a = Analysis(
    ['main.py'],
    pathex=['src', '.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CathBiomed',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=['*.dll', '*.pyd', 'vcruntime*.dll', 'python3*.dll', 'qt6*.dll'],
    name='CathBiomed',
)