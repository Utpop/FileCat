# -*- coding: utf-8 -*-
"""
build_exe.py —— 把 FileCat 打成 Windows 单文件 exe。

    py -3 build_exe.py              # 打包
    py -3 build_exe.py --zip        # 打包后额外压成 dist/FileCat-<版本>-win64.zip
    py -3 build_exe.py --no-verify  # 跳过打包后的自检（改脚本时省时间用）

打包结束会自动跑一次自检（沙箱里改名 → 撤销），不通过就报错退出：单文件窗口
程序出问题时没有任何可见输出，必须在这里拦住。

产物：
    dist/FileCat.exe                单文件，无控制台（窗口程序）
    dist/FileCat-<版本>-win64.zip   可选，Release 传这个

四条约定，改脚本前请先读：

1. **优先用 _vendor 里的 PySide6**。requirements.txt 钉的是 6.10.x，而打包机
   全局可能装了别的版本（比如 6.12）。脚本检测到 `_vendor/PySide6` 存在时，
   把它塞进子进程 PYTHONPATH 的第一位，让 PyInstaller 分析到的是 vendor 那份，
   和运行时的 PYTHONPATH 顺序保持一致。仓库不带 _vendor（`pip install -r
   requirements.txt -t _vendor` 可重建），没有就退回全局环境并打印提醒。
2. **assets 要跟着走**。界面图标 / 顶栏 logo 读的是 `<项目根>/assets/app_icon.png`，
   而这个"项目根"在打包后变成 `sys._MEIPASS`，所以按 `assets;assets` 原样挂进去，
   路径不用改代码。
3. **引擎是 Rust 扩展，打包前必须先有 `.pyd`**。`engine/*.py` 现在只是一行
   `from namecat_py.xxx import *`，真正的实现是 `_vendor/namecat_py.cpXXX-win_amd64.pyd`。
   漏了它打出来的 exe 会在启动瞬间 ImportError（窗口程序没控制台，表现成"双击没反应"）。
   所以 build() 开头有三道闸：扩展在不在 / ABI 与当前解释器对不对 / 是不是比
   `crates/` 源码旧，任何一条不过都直接退出（第三条可加 `--allow-stale-engine`
   压成告警）。

4. **瘦身靠生成 `.spec`，不是命令行参数**。PyInstaller 只能 `--exclude-module`
   （管 Python 模块），没有"排除某个二进制"的开关，而体积大头恰恰是二进制
   （`opengl32sw.dll` 一个就 7.5 MB）。所以每次构建都由 `write_spec()` 生成
   `FileCat.spec`，在里面按 `DROP_BINARIES` 过滤 `a.binaries` —— 清单与理由都在
   那个常量旁边，别在 spec 里手改（每次打包都会覆盖）。41.2 MB → 29.8 MB。

窗口程序（`--windowed`）不会带控制台，`Traceback` 无处可去 —— 真出错时看
ui/app.py 的 _fatal：它会写 `%TEMP%/FileCat_startup_error.log` 并弹一个系统框。
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "_vendor")
ICON = os.path.join(HERE, "assets", "app_icon.ico")
CRATES = os.path.join(HERE, "crates")

sys.path.insert(0, HERE)
from engine import APP_NAME, APP_VER  # noqa: E402  版本号唯一来源


def _vendor_pyside6() -> bool:
    return os.path.isdir(os.path.join(VENDOR, "PySide6"))


def _newest_source_mtime() -> float:
    """`crates/` 下最新的源码时间（.rs/.toml），用来判断 .pyd 是不是旧的。"""
    newest = 0.0
    for pattern in ("**/*.rs", "**/*.toml"):
        for path in glob.glob(os.path.join(CRATES, pattern), recursive=True):
            # 跳过构建产物目录（target 在根上，这里保险起见还是过滤一下）
            if os.sep + "target" + os.sep in path:
                continue
            newest = max(newest, os.path.getmtime(path))
    return newest


def check_engine(allow_stale: bool = False) -> str:
    """确认 Rust 引擎的 .pyd 可用，返回它的路径。

    三道闸都是有来由的：
    - 没有 .pyd → 打出来的 exe 双击"没反应"（窗口程序吞掉 ImportError）；
    - ABI 不对（cp313 的 pyd 配 3.15 的打包器）→ 编译期不报错，运行期 ImportError；
    - .pyd 比源码旧 → 打的是上一版引擎，界面上的行为跟本地跑的测试对不上。
    """
    found = sorted(glob.glob(os.path.join(VENDOR, "namecat_py*.pyd")))
    if not found:
        raise SystemExit(
            "找不到 _vendor/namecat_py*.pyd。\n"
            "引擎是 Rust 扩展，先构建再打包：  py -3 rust_build.py"
        )
    if len(found) > 1:
        raise SystemExit(
            "有多份引擎扩展，PyInstaller 不知道该带哪个：\n  %s\n"
            "删掉多余的（通常是换过 Python 版本留下的）。" % "\n  ".join(found)
        )
    pyd = found[0]
    tag = "cp%d%d" % sys.version_info[:2]
    if tag not in os.path.basename(pyd):
        raise SystemExit(
            "引擎扩展与打包用的解释器不匹配：\n"
            "  扩展：%s\n  解释器：%s (%s)\n"
            "用同一个解释器重建：  py -3 rust_build.py"
            % (os.path.basename(pyd), sys.executable, tag)
        )
    newest = _newest_source_mtime()
    if newest > os.path.getmtime(pyd):
        message = (
            "引擎扩展比 crates/ 源码旧（%s < 源码最新修改）。\n"
            "打出来的 exe 会是上一版引擎。重建：  py -3 rust_build.py"
            % os.path.basename(pyd)
        )
        if allow_stale:
            print("[build] 警告：" + message.replace("\n", " "))
        else:
            raise SystemExit(message + "\n（确知无害可加 --allow-stale-engine）")
    return pyd


# 瘦身清单：只放**确定用不到**的条目，每一项都写清理由 —— 这条链路上省 1 MB
# 不值得冒一次"某些机器上起不来"的风险。真要排除更多，先量一遍再动。
DROP_BINARIES = [
    # Qt 的软件 OpenGL 兜底（未压缩 20 MB / 压缩后 7.5 MB）。QWidget 走 raster
    # 绘制，不需要 OpenGL；只有显卡驱动缺失的老机器才可能用到它。真碰上那种
    # 机器，把这个条目删掉重新打包即可（`--keep-opengl`）。
    "opengl32sw.dll",
    # Qt 自己的 HTTP 栈。程序里所有联网（LLM / 供应商探测）都走 Python 的
    # urllib，没有任何一处用 QNetworkAccessManager。
    "qtnetwork.pyd",
    "qt6network.dll",
    "tls\\qopensslbackend.dll",
    "tls\\qschannelbackend.dll",
    "tls\\qcertonlybackend.dll",
    "networkinformation\\qnetworklistmanager.dll",
    # 用不到的 QPA 平台后端：真正需要的只有 platforms\\qwindows.dll。
    "platforms\\qdirect2d.dll",
    "platforms\\qminimal.dll",
    "platforms\\qoffscreen.dll",
    # 触摸板手势插件（桌面鼠标操作不用）。
    "generic\\qtuiotouchplugin.dll",
    # 用不到的图片格式：PNG/BMP 由 Qt 内建，JPG/SVG/ICO/GIF/WEBP 保留。
    "imageformats\\qtiff.dll",
    "imageformats\\qicns.dll",
    "imageformats\\qtga.dll",
    "imageformats\\qwbmp.dll",
]


SPEC_TEMPLATE = """# -*- mode: python ; coding: utf-8 -*-
# 由 build_exe.py 生成（每次打包都会覆盖），不要手改。
import os

VENDOR = @VENDOR@
PYD = @PYD@
HERE = @HERE@
ICON = @ICON@

a = Analysis(
    [os.path.join(HERE, "main.py")],
    pathex=[VENDOR, HERE],
    # 引擎扩展必须**显式**按二进制加进去。只靠 pathex 不够：PyInstaller 的
    # 静态分析看到的是 `from namecat_py.rules import *`，而 namecat_py 是扩展
    # 模块、不是包，12 个子导入全被判成 missing，父模块一并不收 —— 实测所有
    # TOC 里命中数为 0，exe 启动即崩。
    binaries=[(PYD, ".")],
    datas=[(os.path.join(HERE, "assets"), "assets")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PIL"],
    noarchive=False,
    optimize=0,
)

# ---- 瘦身：剔除确定用不到的二进制（清单与理由见 build_exe.py 的 DROP_BINARIES）----
DROP = @DROP@


def _keep(entry):
    # 统一成正斜杠再比：打包机上的分隔符是 \\，清单里写的是 \\，比之前先抹平，
    # 免得为了一个反斜杠字面量在生成器里数不清几层转义。
    name = entry[0].replace(os.sep, "/").replace("\\\\", "/").lower()
    return not any(pat.replace("\\\\", "/") in name for pat in DROP)


a.binaries = [b for b in a.binaries if _keep(b)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=@NAME@,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[ICON],
)
"""


def write_spec(pyd: str, keep_opengl: bool = False) -> str:
    """生成 `.spec` 并返回路径。

    为什么不把 spec 提交进仓库：里面的路径（_vendor、pyd 文件名）随机器和
    Python 版本变，硬编码只会让下一个人踩"路径写死"的坑。这里当成构建产物，
    每次由 `build_exe.py` 重新生成（`.gitignore` 里也已经忽略 `*.spec`）。
    """
    drop = [item for item in DROP_BINARIES
            if not (keep_opengl and item == "opengl32sw.dll")]
    text = SPEC_TEMPLATE
    for token, value in (
        ("@VENDOR@", VENDOR),
        ("@PYD@", pyd),
        ("@HERE@", HERE),
        ("@ICON@", ICON),
        ("@NAME@", APP_NAME),
        ("@DROP@", drop),
    ):
        text = text.replace(token, repr(value))
    path = os.path.join(HERE, APP_NAME + ".spec")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def build(allow_stale: bool = False, keep_opengl: bool = False) -> str:
    if not os.path.isfile(ICON):
        raise SystemExit("找不到图标：%s" % ICON)
    pyd = check_engine(allow_stale)
    print("[build] 引擎扩展：%s（%.1f MB）"
          % (os.path.basename(pyd), os.path.getsize(pyd) / 1024 / 1024))
    env = dict(os.environ)
    if _vendor_pyside6():
        # PYTHONPATH 排在 site-packages 之前：确保分析的是 vendor 那份依赖
        env["PYTHONPATH"] = VENDOR + os.pathsep + env.get("PYTHONPATH", "")
        print("[build] 使用 _vendor 里的依赖")
    else:
        print("[build] 未找到 _vendor/PySide6，改用全局环境（版本可能与 requirements 不一致）")

    spec = write_spec(pyd, keep_opengl)
    # 从 spec 构建（而不是直接喂 main.py）：只有 spec 里能过滤 `a.binaries`，
    # 而瘦身主要靠这一步 —— 命令行没有"排除某个二进制"的开关。
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(HERE, "build"),
        spec,
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


def verify_exe(exe: str) -> None:
    """打包后自检：在沙箱里真跑一次 CLI 改名 + 撤销。

    为什么不用 `FileCat.exe rules` 看输出：窗口程序默认没有控制台，`print`
    被 engine/cli.py 的流包装器悄悄丢掉，成功了也是"什么都没发生"，看不出对错。
    所以这里只**看文件效果** —— 改名成功说明 Rust 引擎在冻结环境里真的加载了。

    沙箱布局（`_verify_tmp/pack_smoke/`）：
        box/      被测目录（改名对象，断言只看这里的文件名）
        tmp/      TEMP/TMP —— **单文件 exe 启动时要往这里解包自身**，
                  系统 Temp 不可写时会弹 "Could not create temporary directory!"
                  然后卡住；指到沙箱里既避开环境问题，也不污染用户 Temp。
        logs/     NAMECAT_LOG_DIR
        appdata/  APPDATA（不碰真实配置与预设）
    """
    import shutil

    base = os.path.join(HERE, "_verify_tmp", "pack_smoke")
    box = os.path.join(base, "box")
    shutil.rmtree(base, ignore_errors=True)
    for name in ("box", "tmp", "logs", "appdata"):
        os.makedirs(os.path.join(base, name))

    originals = ["海边 日落 副本.jpg", "报告 final.docx", "note.txt"]
    for name in originals:
        with open(os.path.join(box, name), "wb") as fh:
            fh.write(b"x")

    env = dict(os.environ)
    env["TEMP"] = env["TMP"] = os.path.join(base, "tmp")   # 见 docstring
    env["NAMECAT_LOG_DIR"] = os.path.join(base, "logs")
    env["APPDATA"] = os.path.join(base, "appdata")

    def run(args) -> int:
        try:
            return subprocess.run(
                [exe] + args, env=env, timeout=60,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ).returncode
        except subprocess.TimeoutExpired:
            raise SystemExit(
                "自检失败：exe 60 秒没有结束。\n"
                "窗口程序没有控制台，启动失败时只会弹一个错误框然后卡住，\n"
                "在用户那里表现就是“双击没反应”。两种常见原因：\n"
                "  · 引擎 .pyd 没进包（看 --add-binary 那行）；\n"
                "  · TEMP 不可写，单文件模式解包失败。"
            )

    try:
        code = run(["rename", box, "--only", "file", "--rule", "template={n}", "--apply"])
        if code != 0:
            raise SystemExit("自检失败：改名退出码 %d" % code)
        numbered = [n for n in os.listdir(box)
                    if n.split(".")[0].isdigit() and n.lower().endswith((".jpg", ".docx", ".txt"))]
        if len(numbered) != len(originals):
            raise SystemExit("自检失败：期望 %d 个编号文件，实际 %s"
                             % (len(originals), sorted(os.listdir(box))))
        code = run(["undo", box])
        if code != 0:
            raise SystemExit("自检失败：撤销退出码 %d" % code)
        left = sorted(os.listdir(box))
        if left != sorted(originals):
            raise SystemExit("自检失败：撤销后目录内容不对：%s" % left)
        print("[build] 自检通过：改名 → 撤销往返正常（引擎在 exe 内可用）")
    finally:
        shutil.rmtree(base, ignore_errors=True)


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
            "%s %s（Windows 64 位，单文件绿色版）\r\n"
            "\r\n"
            "1. 解压到任意文件夹，双击 %s.exe 即可运行，无需安装 Python。\r\n"
            "2. 首次运行若被 Windows SmartScreen 拦截，选「更多信息」→「仍要运行」。\r\n"
            "   若弹出 Could not create temporary directory!：这是单文件模式的解包步骤失败，\r\n"
            "   说明当前账号的 %%TEMP%% 不可写。把 TEMP / TMP 指到可写目录再启动即可\r\n"
            "   （命令行：set TEMP=%%LOCALAPPDATA%%\\Temp 然后重新双击）。\r\n"
            "3. 命令行用法（在解压目录里打开终端）：\r\n"
            "     %s.exe rules                 列出全部 28 种规则\r\n"
            "     %s.exe presets               列出内置预设\r\n"
            "     %s.exe preview <目录> --rule \"template={n}\" --width 3\r\n"
            "     %s.exe rename  <目录> --rule \"replace=_:- \" --apply\r\n"
            "     %s.exe undo    <目录>\r\n"
            "     %s.exe leftovers <目录>      列出执行被中断后遗留的临时名文件\r\n"
            "   默认只预览，加 --apply 才真的落盘；改名后可用 undo 整批还原。\r\n"
            "4. 卸载：删掉这个文件夹即可（配置与预设在 %%APPDATA%%\\FileCat，重命名撤销日志在程序目录\\Name_backup，提取撤销日志在程序目录\\Extract\\Rollback_Log）。\r\n"
            % ((APP_NAME, APP_VER) + (APP_NAME,) * 7),
        )
    print("[build] 压缩包：%s（%.1f MB）" % (out, os.path.getsize(out) / 1024 / 1024))
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    exe = build(allow_stale="--allow-stale-engine" in args,
                keep_opengl="--keep-opengl" in args)
    if "--no-verify" not in args:
        verify_exe(exe)
    if "--zip" in args:
        pack_zip(exe)
