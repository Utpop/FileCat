# -*- coding: utf-8 -*-
"""
ui.app —— GUI 启动入口。

只做启动：vendor 路径、DPI 策略、QApplication、字体、偏好、主窗口。
不在这里做任何业务初始化。
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from typing import List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)


def ensure_vendor_path() -> None:
    vendor = os.path.join(PROJECT, "_vendor")
    for path in (PROJECT, vendor):
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)


def _fatal(message: str) -> None:
    """启动失败必须“看得见”：写一份日志，再弹一个系统消息框。

    pythonw / VBS 启动时没有控制台，异常默认无处可去，表现就是“双击没反应”。
    """
    log_path = ""
    try:
        log_path = os.path.join(tempfile.gettempdir(), "NameCat_startup_error.log")
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(message)
    except OSError:
        pass
    head = "NameCat 启动失败。"
    tail = ("\n\n详细信息已写入：\n%s\n\n"
            "常见原因：依赖目录 _vendor 缺失、Python 版本不受支持（需要 3.10+），"
            "或解释器里没有装 PySide6。" % log_path) if log_path else ""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, head + tail + "\n\n" + message[-800:],
                                         "NameCat", 0x10)
    except Exception:
        print(head + tail, file=sys.stderr)


def run(argv: Optional[List[str]] = None) -> int:
    try:
        return _run(argv)
    except Exception:
        _fatal(traceback.format_exc())
        return 2


def _run(argv: Optional[List[str]] = None) -> int:
    ensure_vendor_path()

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication, QIcon
    from PySide6.QtWidgets import QApplication

    # 分数缩放不取整：125% 就按 1.25 渲染
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    from adapters import engine_adapter as A
    from ui import base, prefs
    from ui.window import MainWindow

    app = QApplication.instance() or QApplication(argv or sys.argv)
    app.setApplicationName(A.APP_NAME)
    app.setApplicationVersion(A.APP_VER)
    app.setOrganizationName(A.APP_NAME)

    icon_path = os.path.join(PROJECT, "assets", "app_icon.png")
    if os.path.isfile(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # 字体：系统自带 UI 字体（不再随包分发字体文件，见 ui/base.load_fonts）
    base.FAMILY = base.load_fonts()
    app.setFont(base.F(12, 400))

    win = MainWindow(prefs.load())
    # 显式 showNormal 并抬到前台：应对"快捷方式被设成最小化"这类情况。
    # 注意：这救不了以 SW_HIDE 启动的进程（见 NameCat.vbs 里 sh.Run 的注释）。
    win.showNormal()
    win.raise_()
    win.activateWindow()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(run())
