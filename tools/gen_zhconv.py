# -*- coding: utf-8 -*-
"""
把 `engine/zhconv.py` 里的简繁映射表生成成 Rust 源码。

为什么用生成而不是手抄：两张表各 4012 个字，逐字对齐（FROM 第 N 个字对应
TO 第 N 个字）。手抄一处错位就会让整条映射全歪，而且错了也看不出来。
生成脚本每次从 Python 侧读真值，重跑即同步。

用法：
    python tools/gen_zhconv.py --python-file <旧版 zhconv.py>          # 生成
    python tools/gen_zhconv.py --python-file <旧版 zhconv.py> --check  # 只校验

注意：P1 之后 `engine/zhconv.py` 已经是 Rust 壳，**不能**再当数据源。需要改表时：
    git show <换壳前的提交>:engine/zhconv.py > _verify_tmp/zhconv_old.py
    python tools/gen_zhconv.py --python-file _verify_tmp/zhconv_old.py

生成物入库（crates/namecat-core/src/zhconv_data.rs），编译不依赖 Python。
"""

from __future__ import annotations

import argparse
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OUT = os.path.join(ROOT, "crates", "namecat-core", "src", "zhconv_data.rs")

PAIRS = [("S2T_FROM", "_S2T_FROM"), ("S2T_TO", "_S2T_TO"),
         ("T2S_FROM", "_T2S_FROM"), ("T2S_TO", "_T2S_TO")]

HEADER = """\
// 本文件由 tools/gen_zhconv.py 生成，**不要手改**。
// 数据源：engine/zhconv.py（取自 OpenCC 的 STCharacters / TSCharacters，
// Apache-2.0，https://github.com/BYVoid/OpenCC）。
// 重新生成：python tools/gen_zhconv.py
//
// FROM 与 TO 是逐字对齐的两行：第 N 个字符映射到 TO 的第 N 个字符。
"""


def rust_literal(text: str) -> str:
    """把 Python 字符串写成 Rust 字符串字面量（只转义真正需要的字符）。"""
    out = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def wrap(text: str, width: int = 100) -> list[str]:
    """按字符数折行（中文在源码里占 3 字节，但不影响切片）。"""
    return [text[i:i + width] for i in range(0, len(text), width)]


def load_tables(path: str) -> dict:
    """从一份「还带数据」的 zhconv.py 里取出四张表。

    做法是直接 exec 该文件的模块体（它只有字符串常量，没有副作用），
    比用正则抠引号可靠得多。
    """
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    namespace = {"__name__": "zhconv_reference", "__file__": path}
    try:
        exec(compile(source, path, "exec"), namespace)  # noqa: S102 - 只读自家文件
    except Exception as exc:                            # noqa: BLE001
        raise SystemExit("读数据源失败：%s（%s）" % (path, exc))
    tables = {}
    for _rust, py in PAIRS:
        value = namespace.get(py)
        if not isinstance(value, str):
            raise SystemExit("数据源里没有 %s（确认这份文件还是 Python 实现）" % py)
        tables[py] = value
    return tables


def build(tables: dict) -> str:
    parts = [HEADER]
    for rust_name, py_name in PAIRS:
        value = tables[py_name]
        parts.append("\npub const %s: &str = concat!(\n" % rust_name)
        for row in wrap(value):
            parts.append("    %s,\n" % rust_literal(row))
        parts.append(");\n")
    # 逐字对齐的断言放进 Rust 单元测试太啰嗦，这里直接把长度也写进去
    parts.append("\n// 表长（与数据源一致；测试里用它校验表没被截断）\n")
    for rust_name, py_name in PAIRS:
        parts.append("#[allow(dead_code)]\npub const %s_LEN: usize = %d;\n"
                     % (rust_name, len(tables[py_name])))
    return "".join(parts)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成简繁映射表的 Rust 源码")
    parser.add_argument("--python-file", required=True,
                        help="换壳前的 engine/zhconv.py（数据源）")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = parser.parse_args(argv)

    tables = load_tables(args.python_file)
    expected = build(tables)
    if args.check:
        if not os.path.isfile(OUT):
            print("✗ 还没生成：%s" % OUT)
            return 1
        with open(OUT, "r", encoding="utf-8", newline="\n") as fh:
            actual = fh.read()
        if actual != expected:
            print("✗ 已过期：%s（Python 侧改过表，重跑 python tools/gen_zhconv.py）" % OUT)
            return 1
        print("✓ 映射表与 engine/zhconv.py 一致")
        return 0

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(expected)
    s2t = len(tables["_S2T_FROM"])
    t2s = len(tables["_T2S_FROM"])
    print("✓ 已生成 %s" % OUT)
    print("  简→繁 %d 字，繁→简 %d 字，文件 %.1f KB" % (
        s2t, t2s, os.path.getsize(OUT) / 1024))
    if (len(tables["_S2T_FROM"]) != len(tables["_S2T_TO"])
            or len(tables["_T2S_FROM"]) != len(tables["_T2S_TO"])):
        print("✗ 表长不配对！Python 侧数据本身就坏了")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
