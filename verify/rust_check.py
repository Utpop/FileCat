# -*- coding: utf-8 -*-
"""
Rust 引擎的**符号对齐检查**。

    python verify/rust_check.py                 # 全量报告
    python verify/rust_check.py -v              # 连"多余符号"也列出来
    python verify/rust_check.py -m rules        # 只看某几个模块
    python verify/rust_check.py --fail-on-any   # 任何缺失都算失败（CI 用）

为什么需要它：
    引擎的公开接口是 `namecat_py.<模块>`，界面与命令行直接 import 它。
    漏掉一个符号，`adapters/` 那边就是 AttributeError —— 而这通常要等用户
    点开某个页面才暴露。所以判定标准是：**清单里的公开符号在 Rust 侧一个不缺**。

`verify/engine_symbols.json` 是什么：
    Rust 化**之前**从 Python 实现里固化的期望符号表（12 个模块 / 193 个符号），
    入库、只读。它是这次的"验收基准"：迁完之后 Python 实现已经全部删掉
    （`engine/*.py` 不再有转发壳），所以不能、也不该再从 engine 侧取期望 ——
    否则就是拿实现证明自己。

判定口径：
    · 期望集：`engine_symbols.json` 里每个模块的符号列表；
    · Rust 侧：`__all__`（我们自己用 `shim::set_all` 声明的），没有则取 `dir()`
      里非下划线开头的名字；
    · 根模块常量（APP_VER / LOG_SCHEMA / STATUS_TEXT…）逐条比值：
      宿主 `engine` 包与扩展必须完全一致。

退出码：
    0  12 个模块符号齐全、根常量一致、子模块都能独立 import
    1  有缺失或不一致
    2  环境问题（扩展没构建 / 模块名写错）
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (os.path.join(ROOT, "_vendor"), ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

# 与 crates/namecat-py/src/lib.rs 的 SUBMODULES 顺序保持一致
MODULES = ["rules", "template", "zhconv", "scan", "plan", "context",
           "common", "ops", "llm", "provider", "secrets", "presets"]

# 根模块里两边都必须存在且取值相同的常量
ROOT_CONSTS = ["APP_NAME", "APP_VER", "LOG_DIR_NAME", "BACKUP_DIR_NAME", "LOG_DIR_ENV",
               "MAX_LOGS", "MAX_LOGS_TOTAL", "TEMP_PREFIX", "DATA_DIR_NAME",
               "ST_OK", "ST_SAME", "ST_CONFLICT", "ST_ERROR", "ST_SKIP",
               "STATUS_TEXT", "LOG_SCHEMA", "UNDO_SCHEMA"]

# Rust 化之前固化的期望符号清单（入库，只读）
MANIFEST = os.path.join(HERE, "engine_symbols.json")

# FileCat 文件提取引擎：独立 crate、独立子模块，不进上面 12 模块的期望集，
# 但也要有自己的符号闸门，避免漏挂函数直到点开页面才发现。
EXTRACT_SYMBOLS = [
    "SCHEMA", "INTERNAL_DIR_NAME", "ROLLBACK_DIR_NAME", "BACKUP_SUBDIR",
    "MODES", "POLICIES", "STATUSES",
    "program_root_hint", "rollback_dir", "parse_list",
    "build_plan", "execute", "stop_task", "undo_latest", "undo_path",
    "list_logs", "export_csv", "plan_stats", "read_log",
]


def rust_symbols(module: types.ModuleType) -> list[str]:
    declared = getattr(module, "__all__", None)
    if declared is not None:
        return sorted(n for n in declared if not n.startswith("_"))
    return sorted(n for n in dir(module) if not n.startswith("_"))


def load_manifest() -> dict:
    if not os.path.isfile(MANIFEST):
        raise SystemExit("找不到期望集：%s（这个文件必须入库）" % MANIFEST)
    with open(MANIFEST, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="检查 namecat_py 是否补齐了引擎的公开符号")
    parser.add_argument("-m", "--modules", nargs="*", default=None, help="只检查这些模块")
    parser.add_argument("-v", "--verbose", action="store_true", help="同时列出多余符号")
    parser.add_argument("--fail-on-any", action="store_true",
                        help="保留参数（历史 CI 写法）；现在任何缺失本来就返回 1")
    args = parser.parse_args(argv)

    try:
        namecat_py = importlib.import_module("namecat_py")
    except ImportError as exc:
        print("✗ 找不到 namecat_py：%s" % exc)
        print("  先跑 `python rust_build.py` 把扩展装进 _vendor/。")
        return 2

    targets = args.modules or MODULES
    unknown = [m for m in targets if m not in MODULES]
    if unknown:
        print("✗ 未知模块：%s（可用：%s）" % (", ".join(unknown), ", ".join(MODULES)))
        return 2

    print("FileCat Rust 引擎 · 符号对齐检查")
    manifest = load_manifest()
    print("期望集来源：%s（Rust 化之前固化，只读）" % os.path.relpath(MANIFEST, ROOT))
    print("=" * 78)
    print("%-10s %7s %5s  %s" % ("模块", "期望", "Rust", "缺失"))

    failed = []
    for name in targets:
        expected = manifest.get(name)
        if expected is None:
            print("✗ 清单里没有 %s" % name)
            failed.append(name)
            continue
        try:
            actual = rust_symbols(importlib.import_module("namecat_py." + name))
        except ImportError:
            actual = []
        missing = [s for s in expected if s not in actual]
        print("%-10s %7d %5d  %s" % (
            name, len(expected), len(actual),
            "—" if not missing else ("%d 个" % len(missing))))

        if missing:
            for sym in missing:
                print("     · 缺 %s" % sym)
        if args.verbose:
            for sym in [s for s in actual if s not in expected]:
                print("     + 多 %s" % sym)
        if missing:
            failed.append(name)

    # 根模块常量：值必须逐条相同（Rust 的 APP_VER 取自 Cargo.toml）
    print("-" * 78)
    engine = importlib.import_module("engine")
    mismatch = []
    for key in ROOT_CONSTS:
        want = getattr(engine, key, "<缺失>")
        got = getattr(namecat_py, key, "<缺失>")
        if want != got:
            mismatch.append((key, want, got))
    if mismatch:
        print("✗ 根模块常量不一致：")
        for key, want, got in mismatch:
            print("     · %s：engine=%r  namecat_py=%r" % (key, want, got))
        failed.append("<root>")
    else:
        print("✓ 根模块常量一致（%d 项）" % len(ROOT_CONSTS))

    # 子模块是否真的注册进了 sys.modules（只有 add_submodule 是不够的）
    unimportable = []
    for name in MODULES:
        try:
            importlib.import_module("namecat_py." + name)
        except ImportError:
            unimportable.append(name)
    if unimportable:
        print("✗ 这些子模块 import 不了：%s" % ", ".join(unimportable))
        failed.append("<submodules>")
    else:
        print("✓ 全部 %d 个子模块可独立 import" % len(MODULES))

    # FileCat 提取引擎：独立子模块，符号单独核对（不混入 193 个重命名符号的基准）
    try:
        extract = importlib.import_module("namecat_py.extract")
        missing_extract = [s for s in EXTRACT_SYMBOLS if not hasattr(extract, s)]
        if missing_extract:
            print("✗ namecat_py.extract 缺符号：%s" % ", ".join(missing_extract))
            failed.append("extract")
        else:
            print("✓ namecat_py.extract 独立引擎符号齐全（%d 项）" % len(EXTRACT_SYMBOLS))
    except ImportError as exc:
        print("✗ namecat_py.extract 无法 import：%s" % exc)
        failed.append("extract")

    print("=" * 78)
    if failed:
        print("✗ 未通过：%s" % ", ".join(failed))
        return 1
    total = sum(len(manifest.get(m, [])) for m in MODULES)
    print("✓ 通过（%d 个模块 / %d 个符号，直接查 namecat_py）" % (len(MODULES), total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
