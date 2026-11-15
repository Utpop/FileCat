# -*- coding: utf-8 -*-
"""
FileCat 入口：图形界面 + 命令行共用同一个文件。

用法：
    python main.py                      # 打开图形界面
    python main.py --gui                # 同上（显式）
    python main.py gui                  # 同上（子命令写法）

    python main.py preview <目录> --rule "template={n}" --width 3
    python main.py rename  <目录> --rule "replace=_:- " --apply
    python main.py undo    <目录>
    python main.py rules                 # 列出全部规则
    python main.py presets               # 列出内置预设
    python main.py rename --llm-edit     # 生成/查看「给 LLM 的要求」文件

命令行行为与本文件改造前完全一致：不带参数时、以及所有子命令，都原样转交
`engine.cli.main()`，返回码语义不变（`python -m engine.cli` 依然等价）。
只有明确要求 GUI（无参数 / `--gui` / `gui`）时才启动窗口。
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

VENDOR = os.path.join(HERE, "_vendor")
if os.path.isdir(VENDOR) and VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)

GUI_FLAGS = ("--gui", "-g", "gui")


def _wants_gui(argv) -> bool:
    """不带参数 = 开界面；显式 --gui/-g/gui = 开界面；其它一律走命令行。"""
    if not argv:
        return True
    return len(argv) == 1 and argv[0].lower() in GUI_FLAGS


def _engine_hint() -> str:
    """引擎扩展是否就位；缺了返回一句人话，就位返回空串。

    `engine/*.py` 现在只是一行 `from namecat_py.xxx import *`，真正的实现是
    `_vendor/namecat_py.cpXXX-win_amd64.pyd`。直接 clone 下来不构建就运行，
    报的是一句 `No module named 'namecat_py'` —— 对着这句话没人猜得到该干什么，
    所以在入口拦一下。
    """
    try:
        import namecat_py  # noqa: F401
    except ImportError as exc:
        return (
            "引擎扩展没加载：%s\n"
            "\n"
            "FileCat 的引擎（engine/）已经是 Rust 实现，源码运行前要先构建一次：\n"
            "    py -3 rust_build.py\n"
            "\n"
            "需要 Rust 工具链与 maturin；构建产物装到 _vendor/namecat_py.*.pyd。\n"
            "不想装 Rust 就直接用 Releases 里的 FileCat.exe（已内含引擎）。"
            % exc
        )
    return ""


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    hint = _engine_hint()
    if hint:
        print(hint, file=sys.stderr)
        return 1
    if _wants_gui(args):
        from ui.app import run
        return run([])
    from engine.cli import main as cli_main
    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
