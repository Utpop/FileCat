# -*- coding: utf-8 -*-
"""
Rust 绑定层的**符号对齐检查**（strangler 迁移的防漏网）。

    python verify/rust_check.py                 # 全量报告
    python verify/rust_check.py -v              # 连"多余符号"也列出来
    python verify/rust_check.py -m rules        # 只看某几个模块
    python verify/rust_check.py --fail-on-any   # 任何缺失都算失败（P1 起用）
    python verify/rust_check.py --snapshot      # 重新固化符号清单（换壳前跑）

为什么需要它：
    `engine/rules.py` 迁移后会变成一行 `from namecat_py.rules import *`。
    漏掉一个符号，`adapters/` 那边就是 AttributeError —— 而这通常要等
    用户点开某个页面才暴露。所以迁移完成的唯一标准是：
    **本模块自己的公开符号在 Rust 侧一个不缺**。

为什么要有 `verify/engine_symbols.json`：
    期望集必须在**换壳之前**固化。换壳后再去 `dir(engine.rules)` 取期望，
    得到的是空壳的符号表（0 个），检查会永远通过、形同废纸。
    所以清单由 `--snapshot` 在纯 Python 状态下生成并入库，此后只读；
    Python 侧真加了新符号，重跑 `--snapshot`（须先 git 回退该模块的壳）。

判定口径（两边对齐，避免拿 import 进来的杂项互相灌水）：
    · Python 侧只算"自己的"符号：`__all__` 优先；否则取模块字典里非下划线开头、
      且不是 `import` 进来的模块/函数/类（它们的 `__module__` 不是本模块）。
    · Rust 侧取 `__all__`（我们自己用 shim::set_all 声明的），没有则取 `dir()`。

退出码：
    0  已迁移模块全部对齐（未迁移模块只统计，不算失败）
    1  有已迁移模块缺符号；或者加了 `--fail-on-any` 时任何模块缺符号
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

# 换壳前固化的期望符号清单（入库，只读）
MANIFEST = os.path.join(HERE, "engine_symbols.json")


def own_symbols(module: types.ModuleType) -> list[str]:
    """模块自己的公开符号（口径见文件头）。

    判据是 `__module__`：从别处（re / typing / 另一个 engine 模块）拿来的东西
    都带着原产地，一律不算本模块的面。纯常量（字符串、数字、dict…）没有
    `__module__`，留给本模块。
    """
    declared = getattr(module, "__all__", None)
    if declared is not None:
        return sorted(declared)
    out = []
    for name, value in vars(module).items():
        if name.startswith("_"):
            continue
        if isinstance(value, types.ModuleType):
            continue
        origin = getattr(value, "__module__", None)
        if isinstance(origin, str) and origin != module.__name__:
            continue        # 外来函数/类/typing 别名/from __future__ 的 annotations
        out.append(name)
    return sorted(out)


def rust_symbols(module: types.ModuleType) -> list[str]:
    declared = getattr(module, "__all__", None)
    if declared is not None:
        return sorted(n for n in declared if not n.startswith("_"))
    return sorted(n for n in dir(module) if not n.startswith("_"))


def is_migrated(name: str) -> bool:
    """engine/<mod>.py 里出现 namecat_py 就是"已换壳"。"""
    path = os.path.join(ROOT, "engine", name + ".py")
    if not os.path.isfile(path):
        return False
    with open(path, "r", encoding="utf-8") as fh:
        return "namecat_py" in fh.read()


def load_manifest() -> dict:
    """读固化清单；没有就返回空字典（退化为按当前 Python 模块取值）。"""
    if not os.path.isfile(MANIFEST):
        return {}
    with open(MANIFEST, "r", encoding="utf-8") as fh:
        return json.load(fh)


def snapshot() -> int:
    """在**纯 Python 状态**下固化各模块的公开符号。

    换壳之后再跑会记下空壳的符号表 —— 所以这里直接拒绝。
    """
    migrated = [m for m in MODULES if is_migrated(m)]
    if migrated:
        print("✗ 以下模块已换壳，不能用来生成清单：%s" % ", ".join(migrated))
        print("  先把 engine/<模块>.py 用 git 还原成 Python 实现，再跑 --snapshot。")
        return 1
    data = {}
    for name in MODULES:
        module = importlib.import_module("engine." + name)
        data[name] = own_symbols(module)
    with open(MANIFEST, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    total = sum(len(v) for v in data.values())
    print("✓ 已固化 %s（%d 个模块 / %d 个符号）" % (MANIFEST, len(data), total))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="检查 namecat_py 是否补齐了 engine 的公开符号")
    parser.add_argument("-m", "--modules", nargs="*", default=None, help="只检查这些模块")
    parser.add_argument("-v", "--verbose", action="store_true", help="同时列出多余符号")
    parser.add_argument("--fail-on-any", action="store_true",
                        help="任何模块缺符号都返回 1（P1 起 CI 用这个）")
    parser.add_argument("--snapshot", action="store_true",
                        help="把当前 Python 模块的公开符号固化成 engine_symbols.json")
    args = parser.parse_args(argv)

    if args.snapshot:
        return snapshot()

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

    print("NameCat Rust 引擎 · 符号对齐检查")
    manifest = load_manifest()
    source = "固化清单" if manifest else "现行 Python 模块（清单缺失，建议先 --snapshot）"
    print("期望集来源：%s" % source)
    print("=" * 78)
    print("%-10s %-6s %7s %5s  %s" % ("模块", "状态", "期望", "Rust", "缺失"))

    failed = []
    for name in targets:
        expected = manifest.get(name)
        if expected is None:
            expected = own_symbols(importlib.import_module("engine." + name))
        try:
            rust_mod = importlib.import_module("namecat_py." + name)
            actual = rust_symbols(rust_mod)
        except ImportError:
            actual = []
        missing = [s for s in expected if s not in actual]
        migrated = is_migrated(name)
        state = "已换壳" if migrated else "未迁移"
        print("%-10s %-6s %7d %5d  %s" % (
            name, state, len(expected), len(actual),
            "—" if not missing else ("%d 个" % len(missing))))

        if missing and (migrated or args.fail_on_any or args.verbose):
            for sym in missing:
                print("     · 缺 %s" % sym)
        if args.verbose:
            extra = [s for s in actual if s not in expected]
            for sym in extra:
                print("     + 多 %s" % sym)
        if missing and (migrated or args.fail_on_any):
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

    print("=" * 78)
    if failed:
        print("✗ 未通过：%s" % ", ".join(failed))
        return 1
    migrated_count = sum(1 for m in MODULES if is_migrated(m))
    print("✓ 通过（已换壳 %d/%d 个模块）" % (migrated_count, len(MODULES)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
