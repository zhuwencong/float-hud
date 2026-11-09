#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在桌面创建「悬浮监控栏」快捷方式。

用法:
    python make_shortcut.py            # 创建/覆盖桌面快捷方式
    python make_shortcut.py --remove   # 删除桌面快捷方式

依赖 pywin32（仅在 Windows 上创建 .lnk 时需要）:
    pip install pywin32
"""

from __future__ import annotations

import os
import shutil
import sys

APP_NAME = "悬浮监控栏"
LNK_NAME = APP_NAME + ".lnk"


def find_pythonw() -> str | None:
    """定位当前解释器同目录的 pythonw.exe（无控制台窗口）。

    优先用正在运行本脚本的那个解释器所在的目录，保证快捷方式与当前
    虚拟环境/安装版本一致；找不到再退回 PATH 搜索。
    """
    exe = sys.executable
    base = os.path.dirname(exe)

    candidates = [
        os.path.join(base, "pythonw.exe"),
        os.path.join(base, "python.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c

    found = shutil.which("pythonw") or shutil.which("python")
    return found


def desktop_dir() -> str:
    """桌面路径。OneDrive 重定向过的机器上 Desktop 可能不在 ~/Desktop。"""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        # CSIDL_DESKTOPDIRECTORY = 0x0010
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0:
            if os.path.isdir(buf.value):
                return buf.value
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def main(argv: list[str]) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "float_hud.py")
    icon = os.path.join(here, "app.ico")
    lnk_path = os.path.join(desktop_dir(), LNK_NAME)

    if "--remove" in argv:
        if os.path.isfile(lnk_path):
            os.remove(lnk_path)
            print("已删除:", lnk_path)
        else:
            print("不存在，无需删除:", lnk_path)
        return 0

    if sys.platform != "win32":
        print("本脚本仅用于 Windows。其他系统请直接把 float_hud.py 放进自启动项。")
        return 1

    try:
        import win32com.client
    except ImportError:
        print("[错误] 需要 pywin32，请先执行：pip install pywin32")
        return 1

    if not os.path.isfile(script):
        print("[错误] 找不到 float_hud.py:", script)
        return 1

    target = find_pythonw()
    if not target:
        print("[错误] 找不到 Python 解释器")
        return 1

    shell = win32com.client.Dispatch("WScript.Shell")
    sc = shell.CreateShortcut(lnk_path)
    sc.TargetPath = target
    sc.Arguments = f'"{script}"'
    sc.WorkingDirectory = here
    if os.path.isfile(icon):
        sc.IconLocation = icon + ",0"
    sc.Description = "常驻桌面的系统监控悬浮条：CPU / 内存 / 磁盘 / 网络 / 温度 + 占用最高进程"
    sc.WindowStyle = 7  # 最小化；pythonw 本身不显示窗口
    sc.Save()

    if not os.path.isfile(lnk_path):
        print("[错误] 快捷方式创建失败")
        return 1

    print("已创建:", lnk_path)
    print("  目标    :", target)
    print("  参数    :", sc.Arguments)
    print("  工作目录:", here)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
