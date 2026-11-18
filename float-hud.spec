# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（文件夹版 / onedir）。

用法:
    python -m PyInstaller float-hud.spec --noconfirm --clean
产物:
    dist/float-hud/float-hud.exe

体积优化的关键有两处：
  1. excludes —— 本项目只用 Core / Gui / Network / Widgets 四个 Qt 模块，
     其余 Qml/Quick/Pdf/Sql/OpenGL… 都可安全剔除；
  2. 末尾的过滤 —— opengl32sw.dll（20MB 软件 OpenGL 后备库，纯 QPainter
     绘制用不到）和 translations/（94 个 Qt 界面翻译，文案全是自绘）。
90MB → 64MB。
"""

EXCLUDES = [
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtDBus",
    "PySide6.QtSvgWidgets",
    "PySide6.QtConcurrent",
    "tkinter",
    "unittest",
    "pydoc",
    "doctest",
]

a = Analysis(
    ["float_hud.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # 温度读取走 WMI，是运行时动态导入的，静态分析抓不到
        "win32com.client",
        "pythoncom",
        "pywintypes",
        "win32api",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

# 剔除 PySide6 里的非必需大件
_DROP_FILES = {"opengl32sw.dll"}
_DROP_PARTS = {"translations"}


def _keep(path: str) -> bool:
    p = path.replace("\\", "/")
    if p.split("/")[-1] in _DROP_FILES:
        return False
    return not any(part in p for part in _DROP_PARTS)


a.binaries = [b for b in a.binaries if _keep(b[0])]
a.datas = [d for d in a.datas if _keep(d[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="float-hud",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              # UPX 易被杀软误报，且对 Qt DLL 收益有限
    console=False,          # 无黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="app.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="float-hud",
)
