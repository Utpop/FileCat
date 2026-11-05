# -*- coding: utf-8 -*-
"""
采集 Windows 路径语义样本，供 Rust 侧 `tests/path_semantics.rs` 对比。

    python tools/gen_path_cases.py            # 重新生成
    python tools/gen_path_cases.py --check    # 只校验现有文件是否与 Python 一致

产出 `crates/namecat-core/tests/data/path_cases.json`（入库）。
数据源是**真 Python 的 ntpath/genericpath**，不是手写的期望值——手写的一旦
跟着实现猜，就等于自己给自己判卷。

约束：语料里只用**绝对路径**做 relpath / abspath，否则结果取决于当前工作目录，
换台机器就飘。
"""

from __future__ import annotations

import argparse
import genericpath
import json
import ntpath
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "crates", "namecat-core", "tests", "data", "path_cases.json")

# 单参数语料：覆盖盘符相对、UNC、设备前缀、结尾点/空格、中文、多字节
SINGLE = [
    "", ".", "..", "a", "a.txt", "a.tar.gz", ".gitignore", "..a", "...",
    "a.", "a..", "a b.txt", "中文 名字.JPG", "a" * 3,
    "C:", "C:\\", "C:/", "C:foo", "C:/foo", "C:\\foo", "C:\\foo\\bar",
    "C:\\\\foo", "C:////x", "C:..\\a", "C:\\a\\..\\b",
    "\\", "/", "\\\\", "\\foo", "/foo",
    "\\\\host\\share", "\\\\host\\share\\", "\\\\host\\share\\x",
    "//host/share/x", "\\\\host\\share\\dir\\file.txt",
    "\\\\?\\C:\\x", "//?/C:/x", "\\\\?\\UNC\\srv\\sh\\x", "\\\\.\\dev",
    "..\\a", "a\\..\\b", "a\\b\\..", "a\\.\\b", "a\\\\b",
    "C:/a\\b/c", "C:\\a b\\c.d.txt", "C:\\中文\\文件.txt",
    "C:\\trailing ", "C:\\trailing.", "C:\\a.b\\c", "C:\\a\\b.c.d",
    "D:\\照片\\2019 年\\img1.JPG", "C:\\x\\..\\..\\y",
    "İSTANBUL\\ǅ.txt", "ΣΑΣ\\ΑΣ.txt", "C:\\ÀÉÎ\\ß.TXT",
]

# 双参数语料（relpath 的 (path, start)）
REL_PAIRS = [
    ("C:\\a\\b", "C:\\a"),
    ("C:\\a\\b", "C:\\a\\c"),
    ("C:\\a", "C:\\a"),
    ("C:\\a\\b\\c", "C:\\"),
    ("C:\\", "C:\\a"),
    ("D:\\a", "C:\\a"),
    ("C:\\A\\B", "C:\\a\\b"),
    ("C:\\a b\\中文", "C:\\a b"),
]

# 多参数语料（commonpath）
COMMON = [
    ("C:\\a\\b", "C:\\a\\c"),
    ("C:\\a", "C:\\a"),
    ("C:\\a", "D:\\b"),
    ("C:\\A\\B", "C:\\a\\b"),
    ("C:\\a\\b", "C:\\a"),
    ("a\\b", "a\\c"),
    ("\\a", "\\b"),
    ("C:\\", "C:\\a"),
    ("C:\\a", "\\a"),
    ("C:\\a\\b", "C:\\a\\b\\c"),
]

# abspath：只放绝对路径（相对路径的结果取决于 cwd，不入库）
ABS = [
    "C:\\a\\..\\b",
    "C:\\a\\b\\",
    "C:/a/b",
    "C:\\",
    "\\\\host\\share\\a\\..\\b",
    "C:\\a b\\中文\\..\\x.txt",
]


def build() -> dict:
    data: dict = {
        "splitroot": [],
        "split": [],
        "join2": [],
        "join3": [],
        "normpath": [],
        "normcase": [],
        "splitext": [],
        "abspath": [],
        "relpath": [],
        "commonpath": [],
    }
    for p in SINGLE:
        data["splitroot"].append([p, *ntpath.splitroot(p)])
        data["split"].append([p, *ntpath.split(p)])
        data["normpath"].append([p, ntpath.normpath(p)])
        data["normcase"].append([p, ntpath.normcase(p)])
        data["splitext"].append([p, *genericpath._splitext(p, "\\", "/", ".")])
    for p in ABS:
        data["abspath"].append([p, ntpath.abspath(p)])
    for i, a in enumerate(SINGLE):
        b = SINGLE[(i * 7 + 3) % len(SINGLE)]
        c = SINGLE[(i * 11 + 5) % len(SINGLE)]
        data["join2"].append([[a, b], ntpath.join(a, b)])
        data["join3"].append([[a, b, c], ntpath.join(a, b, c)])
    for path, start in REL_PAIRS:
        try:
            got = ntpath.relpath(path, start)
        except ValueError:
            got = None
        data["relpath"].append([[path, start], got])
    for group in COMMON:
        try:
            got = ntpath.commonpath(list(group))
        except ValueError:
            got = None
        data["commonpath"].append([list(group), got])
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成路径语义样本")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if os.environ.get("NAMECAT_PATH_CASES_BASE"):
        raise SystemExit("路径语料必须是绝对路径才能跨机器稳定，别指定 NAMECAT_PATH_CASES_BASE")

    expected = json.dumps(build(), ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if args.check:
        with open(OUT, "r", encoding="utf-8") as fh:
            current = fh.read()
        if current != expected:
            print("路径样本已过期：重新运行 python tools/gen_path_cases.py")
            return 1
        print("路径样本与当前 Python 一致（%d 组）" % sum(len(v) for v in build().values()))
        return 0

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(expected)
    counts = {k: len(v) for k, v in build().items()}
    print("已写入 %s" % OUT)
    print("用例：" + " ".join("%s=%d" % (k, v) for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
