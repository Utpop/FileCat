# -*- coding: utf-8 -*-
"""
Rust 引擎的开发构建脚本：`python rust_build.py`

做三件事，任何一步失败都明确报错、不静默继续：

1. **校验版本一致**：`engine/__init__.py` 的 `APP_VER` 必须等于 workspace
   `Cargo.toml` 的版本 —— Rust 侧 `namecat_py.APP_VER` 取自 Cargo，两处不一致
   就会出现"界面显示 1.6.3、引擎自称 1.6.2"这种诡异问题。
2. **maturin build**：产出 wheel（`target/wheels`）。
3. **装进 `_vendor/`**：把 wheel 里的 `namecat_py*.pyd` 解到 `_vendor/`，
   与 PySide6 同级。程序用 `PYTHONPATH=_vendor` 就能 import，和 PyInstaller
   打包时的布局完全一致（不给发布链路留"开发能跑、打包就崩"的惊喜）。

用法：
    python rust_build.py            # release 构建（默认，装进 _vendor）
    python rust_build.py --debug    # debug 构建，编译快，跑自检够用
    python rust_build.py --check    # 只做版本与符号校验，不编译
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
CARGO_TOML = os.path.join(ROOT, "Cargo.toml")
PY_CRATE = os.path.join(ROOT, "crates", "namecat-py")
WHEEL_DIR = os.path.join(ROOT, "target", "wheels")
VENDOR = os.path.join(ROOT, "_vendor")


def _cargo_home_bin() -> str:
    """cargo 装在用户目录，新开的 shell 未必在 PATH 里。"""
    candidate = os.path.join(os.path.expanduser("~"), ".cargo", "bin")
    return candidate if os.path.isdir(candidate) else ""


def _run(cmd, **kw) -> None:
    print("»", " ".join(cmd), flush=True)
    env = dict(os.environ)
    home_bin = _cargo_home_bin()
    if home_bin:
        env["PATH"] = home_bin + os.pathsep + env.get("PATH", "")
    proc = subprocess.run(cmd, cwd=ROOT, env=env, **kw)
    if proc.returncode != 0:
        raise SystemExit("命令失败（退出码 %d）：%s" % (proc.returncode, " ".join(cmd)))


def engine_app_ver() -> str:
    """从 engine/__init__.py 读 APP_VER（唯一来源，别在别处硬编码）。"""
    path = os.path.join(ROOT, "engine", "__init__.py")
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    match = re.search(r'^APP_VER\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not match:
        raise SystemExit("engine/__init__.py 里找不到 APP_VER")
    return match.group(1)


def workspace_ver() -> str:
    """从 Cargo.toml 的 [workspace.package] 读版本。"""
    with open(CARGO_TOML, "r", encoding="utf-8") as fh:
        text = fh.read()
    block = re.search(r"\[workspace\.package\](.*?)(?=\n\[|\Z)", text, re.S)
    if not block:
        raise SystemExit("Cargo.toml 里找不到 [workspace.package]")
    match = re.search(r'^version\s*=\s*"([^"]+)"', block.group(1), re.M)
    if not match:
        raise SystemExit("Cargo.toml 的 [workspace.package] 里找不到 version")
    return match.group(1)


def check_versions() -> str:
    app, cargo = engine_app_ver(), workspace_ver()
    if app != cargo:
        raise SystemExit(
            "版本不一致：engine/__init__.py APP_VER=%s，Cargo.toml=%s\n"
            "发版时两处要一起改（Rust 侧 APP_VER 取自 Cargo.toml）。" % (app, cargo)
        )
    print("✓ 版本一致：%s" % app)
    return app


def build(release: bool) -> str:
    os.makedirs(WHEEL_DIR, exist_ok=True)
    cmd = [
        sys.executable, "-m", "maturin", "build",
        "--manifest-path", os.path.join(PY_CRATE, "Cargo.toml"),
        "--out", WHEEL_DIR,
        # 必须钉死解释器：maturin 会自动在 PATH 里找 python，本机 PATH 上挂着
        # 另一份 3.13 → 编出 cp313 的 .pyd，主程序（3.15）import 直接失败。
        "--interpreter", sys.executable,
    ]
    if release:
        cmd.append("--release")
    _run(cmd)
    wheels = sorted(
        (os.path.join(WHEEL_DIR, n) for n in os.listdir(WHEEL_DIR) if n.endswith(".whl")),
        key=os.path.getmtime,
    )
    if not wheels:
        raise SystemExit("maturin 没有产出 wheel：%s" % WHEEL_DIR)
    return wheels[-1]


def install_to_vendor(wheel: str) -> str:
    """把 wheel 里的扩展模块解到 _vendor（不走 pip：避免 dist-info 污染与网络）。"""
    os.makedirs(VENDOR, exist_ok=True)
    installed = []
    with zipfile.ZipFile(wheel) as zf:
        for name in zf.namelist():
            base = os.path.basename(name)
            if name.endswith("/") or not base.endswith(".pyd"):
                continue
            target = os.path.join(VENDOR, base)
            with zf.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            installed.append(target)
    if not installed:
        raise SystemExit("wheel 里没有 .pyd：%s" % wheel)
    for path in installed:
        print("✓ 已安装 %s（%.1f MB）" % (path, os.path.getsize(path) / 1048576))
    # 顺手清掉同一模块的旧 ABI 后缀文件，免得两个 .pyd 抢同一个模块名
    stem = os.path.splitext(os.path.basename(installed[0]))[0]
    package = stem.split(".")[0]
    keep = os.path.basename(installed[0])
    for name in os.listdir(VENDOR):
        if name.endswith(".pyd") and name.split(".")[0] == package and name != keep:
            os.remove(os.path.join(VENDOR, name))
            print("· 清掉旧产物 %s" % name)
    return installed[0]


def smoke() -> None:
    """装完立刻验证：能 import、子模块能单独 import、常量与 Python 侧一致。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = VENDOR + os.pathsep + ROOT
    code = (
        "import namecat_py, namecat_py.rules, engine\n"
        "assert namecat_py.APP_VER == engine.APP_VER, (namecat_py.APP_VER, engine.APP_VER)\n"
        "assert namecat_py.STATUS_TEXT == engine.STATUS_TEXT\n"
        "assert namecat_py.LOG_SCHEMA == engine.LOG_SCHEMA\n"
        "print('✓ import 冒烟通过：%s，子模块 %d 个' % (namecat_py._hello()['version'],\n"
        "      len([n for n in dir(namecat_py) if not n.startswith('_')])))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env)
    if proc.returncode != 0:
        raise SystemExit("冒烟失败：扩展模块装好了但用不了")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="构建 NameCat 的 Rust 引擎扩展")
    parser.add_argument("--debug", action="store_true", help="debug 构建（编译更快）")
    parser.add_argument("--check", action="store_true", help="只校验，不编译")
    args = parser.parse_args(argv)

    check_versions()
    if args.check:
        return 0
    wheel = build(release=not args.debug)
    print("✓ wheel：%s" % wheel)
    install_to_vendor(wheel)
    smoke()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
