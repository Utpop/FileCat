# -*- coding: utf-8 -*-
"""
build_exe.py —— 把 NameCat 打成 Windows 单文件 exe。

    py -3 build_exe.py              # 只打包
    py -3 build_exe.py --zip        # 打包后额外压成 dist/NameCat-<版本>-win64.zip

产物：
    dist/NameCat.exe                单文件，无控制台（窗口程序）
    dist/NameCat-<版本>-win64.zip   可选，Release 传这个

两条约定，改脚本前请先读：

1. **优先用 _vendor 里的 PySide6**。requirements.txt 钉的是 6.10.x，而打包机
   全局可能装了别的版本（比如 6.12）。脚本检测到 `_vendor/PySide6` 存在时，
   把它塞进子进程 PYTHONPATH 的第一位，让 PyInstaller 分析到的是 vendor 那份，
   和运行时的 PHONEPATH 顺序保持一致。仓库不带 _vendor（`pip install -r
   requirements.txt -t _vendor` 可重建），没有就退回全局环境并打印提醒。
2. **assets 要跟着走**。界面图标 / 顶栏 logo 读的是 `<项目根>/assets/app_icon.png`，
   而这个"项目根"在打包后变成 `sys._MEIPASS`，所以按 `assets;assets` 原样挂进去，
   路径不用改代码。

窗口程序（`--windowed`）不会带控制台，`Traceback` 无处可去 —— 真出错时看
ui/app.py 的 _fatal：它会写 `%TEMP%/NameCat_startup_error.log` 并弹一个系统框。
"""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "_vendor")
ICON = os.path.join(HERE, "assets", "app_icon.ico")

sys.path.insert(0, HERE)
from engine import APP_NAME, APP_VER  # noqa: E402  版本号唯一来源


def _vendor_pyside6() -> bool:
    return os.path.isdir(os.path.join(VENDOR, "PySide6"))


def build() -> str:
    if not os.path.isfile(ICON):
        raise SystemExit("找不到图标：%s" % ICON)

    env = dict(os.environ)
    if _vendor_pyside6():
        # PYTHONPATH 排在 site-packages 之前：确保分析的是 vendor 那份依赖
        env["PYTHONPATH"] = VENDOR + os.pathsep + env.get("PYTHONPATH", "")
        print("[build] 使用 _vendor 里的依赖")
    else:
        print("[build] 未找到 _vendor/PySide6，改用全局环境（版本可能与 requirements 不一致）")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile", "--windowed",
        "--name", APP_NAME,
        "--icon", ICON,
        "--paths", VENDOR,
        "--add-data", "assets%sassets" % os.pathsep,
        "--exclude-module", "tkinter",
        "--exclude-module", "PIL",
        "main.py",
    ]
    print("[build] %s %s" % (APP_NAME, APP_VER))
    rc = subprocess.call(cmd, cwd=HERE, env=env)
    if rc != 0:
        raise SystemExit("打包失败，返回码 %d" % rc)

    exe = os.path.join(HERE, "dist", APP_NAME + ".exe")
    if not os.path.isfile(exe):
        raise SystemExit("没找到产物：%s" % exe)
    print("[build] 完成：%s（%.1f MB）" % (exe, os.path.getsize(exe) / 1024 / 1024))
    return exe


def pack_zip(exe: str) -> str:
    """把 exe + 说明压成 Release 上传用的 zip。"""
    name = "%s-%s-win64.zip" % (APP_NAME, APP_VER)
    out = os.path.join(HERE, "dist", name)
    if os.path.exists(out):
        os.remove(out)
    readme = os.path.join(HERE, "README.md")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.write(exe, APP_NAME + ".exe")
        if os.path.isfile(readme):
            zf.write(readme, "README.md")
        zf.writestr(
            "使用说明.txt",
            "NameCat %s（Windows 64 位，单文件绿色版）\r\n"
            "\r\n"
            "1. 解压到任意文件夹，双击 NameCat.exe 即可运行，无需安装 Python。\r\n"
            "2. 首次运行若被 Windows SmartScreen 拦截，选「更多信息」→「仍要运行」。\r\n"
            "3. 命令行用法（在解压目录里打开终端）：\r\n"
            "     NameCat.exe rules                 列出全部 28 种规则\r\n"
            "     NameCat.exe presets               列出内置预设\r\n"
            "     NameCat.exe preview <目录> --rule \"template={n}\" --width 3\r\n"
            "     NameCat.exe rename  <目录> --rule \"replace=_:- \" --apply\r\n"
            "     NameCat.exe undo    <目录>\r\n"
            "   默认只预览，加 --apply 才真的落盘；改名后可用 undo 整批还原。\r\n"
            "4. 卸载：删掉这个文件夹即可（配置与预设在 %%APPDATA%%\\NameCat，撤销日志在程序目录\\Name_backup）。\r\n"
            % APP_VER,
        )
    print("[build] 压缩包：%s（%.1f MB）" % (out, os.path.getsize(out) / 1024 / 1024))
    return out


if __name__ == "__main__":
    exe = build()
    if "--zip" in sys.argv[1:]:
        pack_zip(exe)
