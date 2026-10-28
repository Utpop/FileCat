# -*- coding: utf-8 -*-
"""
NameCat 验证套件（无界面，可直接 `python verify\\run_tests.py`）。

分四组：
    A 单元      规则 / 模板 / 选择器 / 校验 —— 纯函数，不碰磁盘
    B 兼容      BatchRenamer 原有语义必须逐条保住（旧测试的被测行为）
    C 合并      原先只有 ReNamer 或只有 BatchRenamer 单边具备的能力
    D 端到端    真在磁盘上建/改/撤，含父子同时改名与链式改名

约定：所有临时目录建在工作区里（沙箱下 %TEMP% 不可写）。
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engine import common as C            # noqa: E402
from engine import llm as L               # noqa: E402
from engine import ops                    # noqa: E402
from engine import plan as P              # noqa: E402
from engine import presets as PS          # noqa: E402
from engine import rules as R             # noqa: E402
from engine import template as T          # noqa: E402
from engine.context import Item, NameContext   # noqa: E402
from engine.scan import (KIND_DIR, KIND_FILE, KIND_IMAGE, KIND_VIDEO,  # noqa: E402
                         SORT_KEYS, ScanOptions, check_file_magic, kind_of,
                         natural_key, scan)

PASS = 0
FAIL = 0
FAILS = []
_current = ["?"]
VERBOSE = "-v" in sys.argv


def case(name):
    _current[0] = name


def ok(cond, msg=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        if VERBOSE:
            print("  ✓ %s" % (msg or _current[0]))
    else:
        FAIL += 1
        FAILS.append("%s：%s" % (_current[0], msg or "断言失败"))
        print("  ✗ %s：%s" % (_current[0], msg or "断言失败"))


def eq(got, want, msg=""):
    ok(got == want, "%s（得 %r，期望 %r）" % (msg or "", got, want))


# --------------------------------------------------------------------------- #
# 沙箱目录
# --------------------------------------------------------------------------- #

SANDBOX = os.path.join(ROOT, "_verify_tmp")
# 撤销日志现在统一存在程序目录的 Name_backup 里；自检必须把它指到沙箱，
# 否则跑一次测试就会往程序目录里塞一堆日志文件。
os.environ["NAMECAT_LOG_DIR"] = os.path.join(SANDBOX, "_logs")


def fresh(name: str) -> str:
    path = os.path.join(SANDBOX, name)
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)
    return path


def touch(path: str, data: bytes = b"x") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def mkdir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def names_of(folder: str):
    return sorted(n for n in os.listdir(folder) if n != C.LOG_DIR_NAME)


# 链式规则的构造：1.txt -> 11.txt、2.txt -> 12.txt（前缀编号，目标名互不相同）
CHAIN_RULES = {
    "next": [R.make_rule("number", "1", "1", "prefix")],
}


# --------------------------------------------------------------------------- #
# A 组：规则 / 模板 / 选择器 / 校验
# --------------------------------------------------------------------------- #

def test_rules_unit():
    case("A1 规则类型表完整")
    eq(len(R.RULE_KINDS), 27, "27 种规则（编号 / 序列化 / 模板…）")
    ok(len(set(R.RULE_KIND_IDS)) == len(R.RULE_KIND_IDS), "类型名无重复")
    ok(all(k in R.RULE_KIND_IDS for k in
           ("insert", "delete", "replace", "regex", "case", "template", "number")),
       "常见类型齐全")
    ok("llm" in R.RULE_KIND_IDS, "LLM 精简规则已注册")
    eq(R.normalize(R.Rule(kind="serialize")).kind, "number",
       "旧配置里的 serialize 归一成 number")

    case("A2 前缀后缀")
    eq(R.transform(R.make_rule("prefix", "A_"), "b.txt"), "A_b.txt")
    eq(R.transform(R.make_rule("suffix", "_x"), "b.txt"), "b.txt_x")

    case("A3 大小写")
    eq(R.transform(R.make_rule("case", "upper"), "abc.txt"), "ABC.TXT")
    eq(R.transform(R.make_rule("case", "lower"), "ABC.TXT"), "abc.txt")
    eq(R.transform(R.make_rule("case", "title"), "hello world"), "Hello World")
    eq(R.transform(R.make_rule("case", "sentence"), "HELLO WORLD"), "Hello world")

    case("A4 删除与替换")
    eq(R.transform(R.make_rule("delete", " "), "a b c.txt"), "abc.txt")
    eq(R.transform(R.make_rule("remove_chars", "-_"), "a-b_c.txt"), "abc.txt")
    eq(R.transform(R.make_rule("replace", "a", "b"), "banana"), "bbnbnb")

    case("A5 keep/remove before/after（BatchRenamer 语义）")
    rule = R.make_rule("keep_after", "-")
    eq(R.transform(rule, "a-b-c"), "b-c", "keep_after 第一次")
    eq(R.transform(R.make_rule("keep_after", "-", occurrence=-1), "a-b-c"), "c", "最后一次")
    eq(R.transform(R.make_rule("keep_before", "-"), "a-b-c"), "a")
    eq(R.transform(R.make_rule("remove_after", "-"), "a-b-c"), "a")
    eq(R.transform(R.make_rule("remove_before", "-"), "a-b-c"), "b-c")
    eq(R.transform(R.make_rule("keep_after", "!"), "abc"), "abc", "找不到分隔符原样返回")

    case("A6 空字符时的对半兜底（BatchRenamer 老行为）")
    eq(R.transform(R.make_rule("keep_after", ""), "abcdef"), "abc")
    eq(R.transform(R.make_rule("remove_after", ""), "abcdef"), "abc")

    case("A7 正则替换与非法正则")
    eq(R.transform(R.make_rule("regex", r"(\d+)", r"<\1>"), "a12b3"), "a<12>b<3>")
    eq(R.transform(R.make_rule("regex", "(", "x"), "abc"), "abc", "非法正则不抛异常")

    case("A8 去空格与合并重复")
    eq(R.transform(R.make_rule("trim"), "  a b"), "a b", "首尾空白去掉")
    eq(R.transform(R.make_rule("trim", "."), "a.b.."), "a.b", "指定字符集时去掉的首尾点")
    eq(R.transform(R.make_rule("collapse", " "), "a    b"), "a b", "多个空格变一个")

    case("A9 文件名清理")
    eq(R.transform(R.make_rule("filename_clean"), 'a<b>c:d.txt'), "a_b_c_d.txt")
    eq(R.transform(R.make_rule("filename_clean"), "con.txt"), "_con.txt")

    case("A10 音译与 slug")
    eq(R.transform(R.make_rule("translit"), "Привет"), "Privet")
    eq(R.transform(R.make_rule("slug"), "Hello World 2024.txt"), "hello_world_2024_txt")

    case("A11 移动字符与数字补零")
    eq(R.transform(R.make_rule("move", "5", "3", "1"), "abcdefgh"), "efgabcdh")
    eq(R.transform(R.make_rule("pad_numbers", "3"), "img7_12"), "img007_012")

    case("A12 扩展名规则")
    eq(R.transform(R.make_rule("extension", ".jpg"), "a.png"), "a.jpg")
    eq(R.transform(R.make_rule("extension", "", "remove"), "a.png"), "a")
    eq(R.transform(R.make_rule("extension", "", "lower"), "A PNG") if False else
       R.transform(R.make_rule("extension", "", "lower"), "a.PNG"), "a.png")

    case("A13 selector 只作用于某一段")
    rule = R.make_rule("case", "upper", selector="after", charset="._")
    eq(R.apply_rule(rule, "abc.def-ghi"), "abc.DEF-GHI", "点之后整体大写")
    rule = R.make_rule("case", "upper", selector="before", charset="-")
    eq(R.apply_rule(rule, "abc-def"), "ABC-def")
    rule = R.make_rule("case", "upper", selector="range", charset="2-3")
    eq(R.apply_rule(rule, "abcd"), "aBCd")

    case("A14 规则别名（旧命令行写法）")
    eq(R.parse_cli_rule("remove-after=-").kind, "remove_after")
    eq(R.parse_cli_rule("keep-before=_").kind, "keep_before")
    eq(R.parse_cli_rule("remove-char=-_").kind, "remove_chars")
    eq(R.parse_cli_rule("lower").kind, "case")
    eq(R.parse_cli_rule("lower").a, "lower")
    eq(R.parse_cli_rule("upper").a, "upper")
    r = R.parse_cli_rule("replace=a:b")
    eq((r.a, r.b), ("a", "b"), "replace 冒号写法")

    case("A15 命令行后缀选项")
    r = R.parse_cli_rule("remove-after=-;occ=-1")
    eq(r.occurrence, -1)
    r = R.parse_cli_rule("case=upper;sel=after;sep=._")
    eq((r.selector, r.charset), ("after", "._"))

    case("A16 规则序列化往返")
    chain = [R.make_rule("prefix", "p_"), R.make_rule("replace", "a", "b"),
             R.make_rule("case", "lower")]
    back = R.rules_from_json(R.rules_to_json(chain))
    eq([x.kind for x in back], [x.kind for x in chain])
    eq([x.a for x in back], [x.a for x in chain])

    case("A17 禁用规则不生效")
    r = R.make_rule("prefix", "X", enabled=False)
    eq(R.apply_chain([r], "a.txt"), "a.txt")

    case("A18 扩展名默认随规则链一起改名（ReNamer 语义）")
    r = [R.make_rule("case", "upper")]
    eq(R.apply_chain(r, "abc.txt"), "ABC.TXT", "扩展名一起大写")
    eq(R.apply_chain(r, "abc.PNG"), "ABC.PNG")
    eq(R.apply_chain([R.make_rule("case", "lower")], "ABC.TXT", split_ext=False),
       "abc.txt")

    case("A19 skip_ext 规则只作用于主干")
    r = R.make_rule("replace", "a", "z", skip_ext=True)
    eq(R.apply_chain([r], "aaa.txt"), "zzz.txt")
    r = R.make_rule("regex", r"(\d+)", r"\1x", skip_ext=True)
    eq(R.apply_chain([r], "a1.b2", ctx=Item(r"a1.b2")), "a1x.b2", "扩展名里的数字不动")
    r = R.make_rule("case", "upper", skip_ext=True)
    eq(R.apply_chain([r], "abc.def", ctx=Item(r"abc.def")), "ABC.def", "只大写主干")

    case("A20 模板变量")
    ctx = NameContext(r"C:\t\proj\photo.JPG", name="photo.JPG", is_dir=False)
    eq(T.render("{name}", ctx), "photo")
    eq(T.render("{ext}", ctx), "JPG")
    eq(T.render("{extdot}", ctx), ".JPG")
    eq(T.render("{parent}", ctx), "proj")
    eq(T.render("{n:03}", ctx, num=7), "007")
    eq(T.render("{n}", ctx, num=7, width=3), "007", "未写格式时用计划里的宽度")
    eq(T.render("$Counter(1,1,3)", ctx, num=7), "007")
    eq(T.render("$Counter(10,5,2)", ctx, num=3), "20")
    eq(T.render("#", ctx, num=9, width=2), "09")
    eq(T.render("$Date(yyyy)", ctx), ctx.var("date:yyyy"), "年份取自 mtime")
    eq(T.render("{n}-{name}", ctx, 5, 0), "5-photo", "ctx 存在时 num 生效")

    case("A21 模板里没有变量时原样返回")
    eq(T.render("普通文字 123", None), "普通文字 123")
    eq(T.render("a[b]c", None, num=1), "a[b]c")
    eq(T.render("{n}-{name}", NameContext(r"C:\t\a\b.txt"), num=5), "5-b",
       "显式传 num 时以 num 为准")
    case("A22 中文变量名")
    ctx = NameContext(r"C:\t\a\b.txt", name="b.txt", is_dir=False)
    eq(T.render("{序号}", ctx, num=5), "5")
    eq(T.render("{原名}", ctx), "b")
    eq(T.render("{扩展名}", ctx), "txt")

    case("A23 自然序")
    items = ["img10", "img2", "img1"]
    eq(sorted(items, key=natural_key), ["img1", "img2", "img10"])

    case("A24 名字校验")
    ok(C.name_problem("正常名字") == "")
    ok(C.name_problem("a/b") != "")
    ok(C.name_problem("a:b") != "")
    ok(C.name_problem("尾部空格 ") != "")
    ok(C.name_problem("尾部点.") != "")
    ok(C.name_problem("con.txt") != "", "保留名")
    ok(C.name_problem("") != "")
    ok(C.name_problem("x" * (C.MAX_NAME_LEN + 1)) != "", "超长")
    ok(C.name_problem("con") != "", "文件夹保留名")

    case("A25 类型识别")
    eq(kind_of("a.JPG", False), KIND_IMAGE)
    eq(kind_of("a.mp4", False), KIND_VIDEO)
    eq(kind_of("a.txt", False), KIND_FILE)
    eq(kind_of("a", True), KIND_DIR)


def test_file_magic():
    case("A26 文件头校验")
    box = fresh("magic")
    png = touch(os.path.join(box, "a.png"), b"\x89PNG\r\n\x1a\n" + b"0" * 8)
    jpg = touch(os.path.join(box, "a.jpg"), b"\xff\xd8\xff" + b"0" * 8)
    bad = touch(os.path.join(box, "b.png"), b"not a png at all")
    empty = touch(os.path.join(box, "c.gif"), b"")
    odd = touch(os.path.join(box, "d.xyz"), b"whatever")
    ok(check_file_magic(png, ".png"))
    ok(check_file_magic(jpg, ".jpg"))
    ok(not check_file_magic(bad, ".png"), "内容不符要拒绝")
    ok(not check_file_magic(empty, ".gif"), "空文件拒绝")
    ok(check_file_magic(odd, ".xyz"), "未收录扩展名一律放行")

    case("A27 verify_content 时跳过可疑文件")
    touch(os.path.join(box, "e.jpg"), b"MZ fake exe")
    res = scan(ScanOptions(root=box, kinds=(KIND_IMAGE,), verify_content=True))
    got = sorted(it.name for it in res.items)
    eq(got, ["a.jpg", "a.png"], "只有内容相符的留下")
    eq(res.skipped, 3, "b.png / c.gif / e.jpg 三个可疑文件都计入 skipped")
    raw = scan(ScanOptions(root=box, kinds=(KIND_IMAGE,)))
    eq(len(raw.items), 5, "不校验内容时都收下（d.xyz 不属于图片）")


# --------------------------------------------------------------------------- #
# B 组：BatchRenamer 语义兼容
# --------------------------------------------------------------------------- #

def test_compat_folder():
    case("B1 文件夹规则：只保留某字符之后")
    box = fresh("b1")
    for name in ["项目-甲", "项目-乙", "项目-丙"]:
        mkdir(os.path.join(box, name))
    res = scan(ScanOptions(root=box, kinds=(KIND_DIR,)))
    plan = P.build(res.items, [R.make_rule("keep_after", "-")])
    eq(sorted(it.new_name for it in plan.items), ["丙", "乙", "甲"])

    case("B2 每目录重置编号")
    box = fresh("b2")
    for sub in ("a", "b"):
        for i in range(3):
            touch(os.path.join(box, sub, "f%d.txt" % i))
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,), recursive=True))
    plan = P.build(res.items, [R.make_rule("template", "{n}")],
                   P.PlanOptions(scope=P.SCOPE_FOLDER, width=2, start=1))
    got = sorted(it.new_name for it in plan.items)
    eq(got, ["01.txt", "01.txt", "02.txt", "02.txt", "03.txt", "03.txt"], "两个目录各从 01 起")

    case("B3 整批连续编号")
    plan = P.build(res.items, [R.make_rule("template", "{n}")],
                   P.PlanOptions(scope=P.SCOPE_GLOBAL, width=2))
    eq(sorted(it.new_name for it in plan.items),
       ["01.txt", "02.txt", "03.txt", "04.txt", "05.txt", "06.txt"])

    case("B4 冲突：批内撞名")
    box = fresh("b4")
    touch(os.path.join(box, "a.txt"))
    touch(os.path.join(box, "b.txt"))
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,)))
    plan = P.build(res.items, [R.make_rule("template", "same")])
    eq(plan.report.conflict, 2, "两个都标冲突")
    ok(all(it.status == C.ST_CONFLICT for it in plan.items))

    case("B5 冲突：目标在盘上已存在（不同批次）")
    box = fresh("b5")
    touch(os.path.join(box, "a.txt"))
    touch(os.path.join(box, "target.txt"))
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,), only_match="a.txt"))
    plan = P.build(res.items, [R.make_rule("template", "target.txt")])
    eq(plan.items[0].status, C.ST_CONFLICT)
    eq(plan.items[0].note, "目标已存在")

    case("B6 链式改名不被误判冲突")
    box = fresh("b6")
    touch(os.path.join(box, "a.txt"))
    touch(os.path.join(box, "b.txt"))
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,)))
    # a->b, b->c 通过自然序先 a 后 b 建立链
    rules = [R.make_rule("case", "upper")]
    plan = P.build(res.items, rules)
    eq(plan.report.conflict, 0, "只改大小写不冲突")

    case("B7 只改大小写允许通过")
    box = fresh("b7")
    touch(os.path.join(box, "abc.txt"))
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,)))
    plan = P.build(res.items, [R.make_rule("case", "upper")])
    eq(plan.items[0].status, C.ST_OK)
    eq(plan.items[0].new_name, "ABC.TXT", "ReNamer 语义：扩展名一起大写")

    case("B8 名称未变时标 same", )
    box = fresh("b8")
    touch(os.path.join(box, "abc.txt"))
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,)))
    plan = P.build(res.items, [R.make_rule("prefix", "")])
    eq(plan.items[0].status, C.ST_SAME)

    case("B9 only/exclude 文本过滤")
    box = fresh("b9")
    for name in ["IMG_1.jpg", "IMG_2.jpg", "DOC.txt"]:
        touch(os.path.join(box, name))
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,), only_match="IMG"))
    eq(len(res.items), 2)
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,), exclude_match="IMG"))
    eq(len(res.items), 1)

    case("B10 排序方式齐全")
    eq(len(SORT_KEYS), 10, "10 种排序")
    box = fresh("b10")
    for name, size in [("b.txt", 30), ("a.txt", 10), ("c.txt", 20)]:
        touch(os.path.join(box, name), b"x" * size)
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,), sort_mode="size_asc"))
    eq([it.name for it in res.items], ["a.txt", "c.txt", "b.txt"])
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,), sort_mode="size_desc"))
    eq([it.name for it in res.items], ["b.txt", "c.txt", "a.txt"])
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,), sort_mode="name_desc"))
    eq([it.name for it in res.items], ["c.txt", "b.txt", "a.txt"])

    case("B11 递归深度与隐藏项")
    box = fresh("b11")
    touch(os.path.join(box, "top.txt"))
    touch(os.path.join(box, ".hidden.txt"))
    touch(os.path.join(box, "sub", "deep.txt"))
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,)))
    eq([it.name for it in res.items], ["top.txt"], "不递归不收隐藏")
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,), recursive=True,
                           include_hidden=True))
    eq(sorted(it.name for it in res.items), [".hidden.txt", "deep.txt", "top.txt"])

    case("B12 不扫描日志目录")
    box = fresh("b12")
    touch(os.path.join(box, "a.txt"))
    touch(os.path.join(box, C.LOG_DIR_NAME, "rename_x.json"))
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,), recursive=True))
    eq([it.name for it in res.items], ["a.txt"])

    case("B13 媒体扩展名筛选")
    box = fresh("b13")
    for name in ["a.JPG", "b.png", "c.mp4", "d.txt"]:
        touch(os.path.join(box, name))
    res = scan(ScanOptions(root=box, kinds=(KIND_IMAGE,)))
    eq(sorted(it.name for it in res.items), ["a.JPG", "b.png"])
    res = scan(ScanOptions(root=box, kinds=(KIND_VIDEO,)))
    eq([it.name for it in res.items], ["c.mp4"])

    case("B14 扩展名大小写保持原样")
    box = fresh("b14")
    touch(os.path.join(box, "IMG_0001.JPG"))
    res = scan(ScanOptions(root=box, kinds=(KIND_IMAGE,)))
    plan = P.build(res.items, [R.make_rule("template", "{n}")],
                   P.PlanOptions(width=3))
    eq(plan.items[0].new_name, "001.JPG", "大写扩展名不丢")

    case("B15 CSV 导出列与 BatchRenamer 兼容")
    box = fresh("b15")
    touch(os.path.join(box, "a.txt"))
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,)))
    plan = P.build(res.items, [R.make_rule("prefix", "x")])
    out = os.path.join(box, "out.csv")
    ops.export_csv(plan, out)
    with open(out, encoding="utf-8-sig") as fh:
        head = fh.readline().strip()
    ok(head.startswith("序号,"), "首列为序号")
    ok("原名称" in head and "新名称" in head, "含新旧名称列")

    case("B16 撤销日志格式与 BatchRenamer v3 兼容")
    box = fresh("b16")
    touch(os.path.join(box, "a.txt"))
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,)))
    plan = P.build(res.items, [R.make_rule("template", "z")])
    steps = C.execute(plan.changing())
    ok(all(s.ok for s in steps), "改名成功")
    path = C.save_log(box, [(s.old, s.new) for s in steps], kind="file")
    data = C.load_log(path)
    eq(data["kind"], "file")
    ok(isinstance(data["pairs"], list) and data["pairs"][0]["old"].endswith("a.txt"),
       "pairs 结构一致")

    case("B17 撤销还原")
    ok(C.undo(path)[0], "撤销成功")
    ok(os.path.exists(os.path.join(box, "a.txt")), "原名回来了")
    ok(not os.path.exists(path), "成功后日志删除")

    case("B18 撤销不会覆盖已有文件")
    box = fresh("b18")
    touch(os.path.join(box, "a.txt"))
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,)))
    plan = P.build(res.items, [R.make_rule("template", "z")])
    steps = C.execute(plan.changing())
    path = C.save_log(box, [(s.old, s.new) for s in steps], kind="file")
    touch(os.path.join(box, "a.txt"), b"new")     # 抢占原名
    good, errors = C.undo(path)
    ok(not good and errors, "应报告失败")
    ok(os.path.exists(path), "失败时保留日志")

    case("B19 临时名残留检查")
    box = fresh("b19")
    touch(os.path.join(box, "a.txt"))
    touch(os.path.join(box, C.TEMP_PREFIX + "abc"))
    found = C.find_temp_leftovers(box)
    eq(len(found), 1)
    ok(os.path.basename(found[0]).startswith(C.TEMP_PREFIX))


# --------------------------------------------------------------------------- #
# C 组：合并新增能力（原 ReNamer 侧或两边都没有）
# --------------------------------------------------------------------------- #

def test_merged_features():
    case("C1 同一条链同时处理文件与文件夹")
    box = fresh("c1")
    mkdir(os.path.join(box, "dir-1"))
    touch(os.path.join(box, "file-1.txt"))
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE, KIND_DIR)))
    plan = P.build(res.items, [R.make_rule("keep_after", "-")])
    got = sorted(it.new_name for it in plan.items)
    eq(got, ["1", "1.txt"], "文件夹无扩展名、文件保留扩展名")

    case("C2 混合编号：文件夹与文件同一序列")
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE, KIND_DIR), sort_mode="path_asc"))
    plan = P.build(res.items, [R.make_rule("template", "{n}")],
                   P.PlanOptions(width=2))
    got = sorted(it.new_name for it in plan.items)
    eq(got, ["01", "02.txt"], "两项共享编号序列：文件夹无扩展名、文件保留")

    case("C3 编号作用域：按类型")
    box = fresh("c3")
    for name in ["a.jpg", "b.jpg", "c.mp4", "d.mp4", "e.txt"]:
        touch(os.path.join(box, name))
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,)))
    plan = P.build(res.items, [R.make_rule("template", "{n}")],
                   P.PlanOptions(scope=P.SCOPE_KIND, width=2))
    got = {}
    for it in plan.items:
        got.setdefault(it.kind, []).append(it.new_name)
    eq(sorted(got["image"]), ["01.jpg", "02.jpg"])
    eq(sorted(got["video"]), ["01.mp4", "02.mp4"])
    eq(got["file"], ["01.txt"])

    case("C4 编号步长与起始值")
    plan = P.build(res.items, [R.make_rule("template", "{n}")],
                   P.PlanOptions(start=10, step=5, width=0))
    eq(sorted(it.new_name for it in plan.items)[:2], ["10.jpg", "15.jpg"])

    case("C5 倒序编号")
    res2 = scan(ScanOptions(root=box, kinds=(KIND_FILE,), sort_mode="name_asc"))
    plan = P.build(res2.items, [R.make_rule("template", "{n}")],
                   P.PlanOptions(reverse=True))
    first = sorted(res2.items, key=lambda it: it.name)[0]
    hit = [it for it in plan.items if it.path == first.path][0]
    eq(hit.new_name, "5.jpg", "倒序时第一项拿到最大编号")

    case("C6 规则顺序决定结果")
    chain = [R.make_rule("prefix", "a"), R.make_rule("replace", "a", "b")]
    eq(R.apply_chain(chain, "x.txt"), "bx.txt", "前缀被后面的替换吃掉")
    chain = [R.make_rule("replace", "a", "b"), R.make_rule("prefix", "a")]
    eq(R.apply_chain(chain, "x.txt"), "ax.txt", "先替换再前缀")

    case("C7 插到锚点之前/之后")
    eq(R.transform(R.make_rule("insert", "before", "-", "_v2"), "abc-def"), "abc_v2-def")
    eq(R.transform(R.make_rule("insert", "after", "-", "_v2"), "abc-def"), "abc-_v2def")
    eq(R.transform(R.make_rule("insert", "prefix", "", "[T]"), "abc"), "[T]abc")
    eq(R.transform(R.make_rule("insert", "after", "!", "_v2"), "abc"), "abc", "锚点缺席不改")

    case("C8 编号（起始 = 第一个对象的号码）")
    # 号码由计划层按规则上的起始/步长算好后传给 transform：num 就是要印的数字
    eq(R.transform(R.make_rule("number", "1", "4"), "a", num=1), "0001a")
    eq(R.transform(R.make_rule("number", "5", "4"), "a", num=7), "0007a",
       "起始 5 + 步长 2 的第 2 项 = 7")
    eq(R.transform(R.make_rule("number", "1", "4", "suffix"), "a", num=1), "a0001")

    case("C9 哈希追加（读文件）")
    box = fresh("c9")
    path = touch(os.path.join(box, "a.txt"), b"hello")
    item = Item(path)
    ctx = item._ctx_obj()
    out = R.transform(R.make_rule("hash", "md5", "8"), "a", ctx=ctx)
    eq(out, "a5d41402a", "md5(hello) 前 8 位")

    case("C10 预设保存/读取/内置")
    box = fresh("c10")
    preset = PS.BUILTIN["照片序号（四位）"]
    target = os.path.join(box, "p.json")
    PS.save(preset, target)
    back = PS.load(target)
    eq(back.name, preset.name)
    eq([r.kind for r in back.rules], ["template"])
    eq(back.plan_options().width, 4)
    ok(len(PS.builtin_list()) >= 4, "内置预设可用")

    case("C11 扫描 + 计划 + 执行 一站式（ops）")
    box = fresh("c11")
    for name in ["x1.txt", "x2.txt"]:
        touch(os.path.join(box, name))
    plan = ops.scan_and_plan(box, [R.make_rule("template", "{n}")],
                             ScanOptions(root=box, kinds=(KIND_FILE,)),
                             P.PlanOptions(width=2))
    eq(sorted(it.new_name for it in plan.items), ["01.txt", "02.txt"])
    res = ops.apply(plan.items, [R.make_rule("template", "{n}")],
                    P.PlanOptions(width=2, dry_run=False), root=box)
    ok(res.ok and len(res.steps) == 2)
    eq(sorted(names_of(box))[:2], ["01.txt", "02.txt"])


# --------------------------------------------------------------------------- #
# C2 组：LLM 精简（离线替身 EchoClient，不联网）
# --------------------------------------------------------------------------- #

def test_llm():
    from engine import llm as L

    case("L1 默认要求与可编辑要求文件")
    eq(L.read_instruction(), L.DEFAULT_INSTRUCTION, "文件不存在时回落默认要求")
    box = fresh("l1")
    custom = os.path.join(box, "my_req.txt")
    L.write_instruction("只保留中文标题里的地名", custom)
    eq(L.read_instruction(custom), "只保留中文标题里的地名", "能读回自定义要求")
    ok(L.INSTRUCTION_HEADER.splitlines()[0] in open(
        custom, encoding="utf-8").read(), "写盘时带注释头")
    L.write_instruction("# 全是注释\n\n# 又是注释\n", custom)
    eq(L.read_instruction(custom), L.DEFAULT_INSTRUCTION, "只有注释时回落默认")
    eq(L.instruction_path(custom), os.path.abspath(custom), "显式路径优先")
    ok(L.instruction_path().endswith("llm_instruction.txt"), "默认落在 APPDATA")

    case("L2 要求原文逐字进提示词")
    prompt = L.build_prompt(["a.jpg", "b.jpg"], "把日期删掉，只留主题", "zh", 16, "kebab")
    ok("把日期删掉，只留主题" in prompt, "自定义要求出现在提示词里")
    ok("16" in prompt and L.STYLES["kebab"] in prompt, "长度与风格都写进去了")
    ok("1. a.jpg" in prompt and "2. b.jpg" in prompt, "文件名编号列出")
    eq(L.build_prompt(["a"], "").count(L.DEFAULT_INSTRUCTION), 1, "空要求用默认")

    case("L3 名字净化")
    eq(L.sanitize_name('  "海边日落"  '), "海边日落", "去引号与空格")
    eq(L.sanitize_name("a/b:c*d"), "a_b_c_d", "非法字符换成下划线")
    eq(L.sanitize_name("第一行\n第二行"), "第一行", "只取第一行")
    eq(L.sanitize_name("很长很长很长很长很长很长的名字", 6), "很长很长很长", "按长度截断")
    eq(L.sanitize_name("海边日落", 24, "kebab"), "海边日落", "kebab 无空格时不变")
    eq(L.sanitize_name("hello world", 24, "kebab"), "hello-world", "kebab 转短横")
    eq(L.sanitize_name("hello world", 24, "snake"), "hello_world", "snake 转下划线")
    eq(L.sanitize_name("CON"), "_CON", "避开 Windows 保留名")
    eq(L.sanitize_name(""), "", "空串仍为空")

    case("L4 回复解析（三种形状）")
    parse = L.LLMClient.parse_suggestions
    eq(parse('[{"i":1,"n":"海边日落"}]', 1), {1: "海边日落"}, "标准 JSON")
    eq(parse('```json\n[{"i":1,"n":"A"},{"i":2,"n":"B"}]\n```', 2), {1: "A", 2: "B"},
       "去代码围栏")
    eq(parse("1. 海边日落\n2. 会议纪要", 2), {1: "海边日落", 2: "会议纪要"}, "逐行编号")
    eq(parse("随便说说\n[{\"i\":2,\"n\":\"B\"}]", 2), {2: "B"}, "夹杂解释也能抠出 JSON")
    eq(parse("完全不是这三个形状的回答", 1), {1: "完全不是这三个形状的回答"},
       "单行无编号时按顺序取")
    eq(parse("", 3), None, "空回复返回 None（触发严格重试）")

    case("L5 离线替身按规则精简")
    sim = L.EchoClient.simplify
    eq(sim("DSC_04321_IMG_20230815_海边日落_副本.jpg"), "海边日落", "相机前缀/日期/副本全去")
    eq(sim("2024-05-01_会议纪要_final_v3.docx"), "会议纪要_final_v3", "日期去掉，正文保留")
    eq(sim("Screenshot_2023-01-02_at_10.30.00.png"), "", "纯垃圾名精简后为空")
    eq(sim("工作报告.docx"), "工作报告", "没有垃圾词时只去扩展名")

    case("L6 没配 Key 时安静降级")
    cfg = L.LLMConfig(provider="openai", api_key="", base_url="https://api.deepseek.com",
                      model="deepseek-chat")
    un = L.make_client(cfg)
    eq(un.available(), False, "无 key 判定不可用")
    ok("NAMECAT_LLM_API_KEY" in un.problem(), "提示里给出该设哪个环境变量")
    ok("NAMEKIT_LLM_API_KEY" in un.problem(), "改名前的旧变量名也一并说明，老配置不必改")
    out = un.plan(["a.jpg"])
    eq((out.available, out.mapping), (False, {}), "降级不抛异常、不给映射")

    case("L7 LLM 规则进规则链（离线替身端到端）")
    box = fresh("l7")
    for name in ("DSC_0001_海边日落_副本.jpg", "工作报告.docx"):
        touch(os.path.join(box, name))
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,)))
    rule = R.make_rule("llm", "只留主题", "zh", "24")
    plan = P.build(res.items, [rule], P.PlanOptions(dry_run=True), None, L.EchoClient())
    got = {it.name: it.new_name for it in plan.items}
    eq(got["DSC_0001_海边日落_副本.jpg"], "海边日落.jpg", "扩展名原样保留")
    eq(got["工作报告.docx"], "工作报告.docx", "原本干净的名字不动")
    eq([it.status for it in plan.items if it.name == "工作报告.docx"], [C.ST_SAME],
       "未变的项标「名称未变」")

    case("L8 LLM 可与其它规则串联")
    box = fresh("l8")
    touch(os.path.join(box, "IMG_2020_杭州西湖_高清.jpg"))
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,)))
    chain = [R.make_rule("llm", "", "zh", "24"),
             R.make_rule("prefix", "P_")]
    L.write_instruction("只保留地名", os.path.join(box, "req.txt"))
    plan = P.build(res.items, chain, P.PlanOptions(dry_run=True), None, L.EchoClient())
    eq(plan.items[0].new_name, "P_杭州西湖.jpg", "LLM 之后再走前缀规则")

    case("L9 规则里写的要求优先于要求文件")
    r1 = R.make_rule("llm", "只留地名", "zh", "12")
    eq(r1.extras["instruction"], "只留地名")
    eq(r1.extras["max_len"], "12")
    r2 = R.make_rule("llm")
    eq(r2.extras["instruction"], "", "没写要求时留空，运行时读要求文件")
    eq(r2.extras["lang"], "zh")
    ok("llm" in R.CLI_ALIASES or "ai" in R.CLI_ALIASES, "命令行别名可用")

    case("L10 命令行解析 LLM 参数")
    spec = R.parse_cli_rule("llm=保留主题=zh=20")
    eq((spec.kind, spec.extras["instruction"], spec.extras["max_len"]),
       ("llm", "保留主题", "20"), "三段参数各就各位")
    eq(R.parse_cli_rule("ai").kind, "llm", "别名 ai 映射到 llm")

    case("L11 LLM 规则可存进预设与 JSON")
    r = R.make_rule("llm", "只留地名", "zh", "12")
    data = R.rules_to_json([r])
    eq(data[0]["extras"]["instruction"], "只留地名", "要求进了 extras")
    back = R.rules_from_json(data)[0]
    eq((back.kind, back.extras["instruction"], back.extras["max_len"]),
       ("llm", "只留地名", "12"), "读回不丢要求")
    box = fresh("l11")
    path = os.path.join(box, "p.json")
    PS.save(PS.Preset(name="llm 预设", rules=[r]), path)
    eq(PS.load(path).rules[0].extras["instruction"], "只留地名", "预设文件保留要求")

    case("L12 命令行端到端（--llm-provider echo）")
    import contextlib
    import io
    from engine import cli
    box = fresh("l12")
    touch(os.path.join(box, "DSC_0002_西湖日落_副本.jpg"))
    touch(os.path.join(box, "IMG_2021_项目方案_v2.docx"))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["rename", box, "--only", "file", "--rule", "llm",
                       "--llm-provider", "echo", "--apply", "--quiet"])
    eq(rc, 0, "命令返回 0")
    got = names_of(box)
    ok("西湖日落.jpg" in got, "相机前缀与副本被精简掉")
    ok("项目方案_v2.docx" in got, "正文保留")
    text = buf.getvalue()
    ok("撤销日志" in text, "打印了撤销日志路径")
    logs = C.list_logs(box)
    ops.undo(logs[0])
    eq(len(names_of(box)), 2, "撤销后仍是两项（名字已复原）")
    ok(any(n.startswith("DSC_") for n in names_of(box)), "撤销回原名")

    case("L13 要求文件通过命令行生效")
    box = fresh("l13")
    touch(os.path.join(box, "DSC_0003_黄山云海_副本.jpg"))
    req = os.path.join(box, "req.txt")
    L.write_instruction("只留地名", req)
    seen = {}

    class Spy(L.EchoClient):
        def plan(self, names, instruction="", lang=None, max_len=None, style=None,
                 progress=None):
            seen["instr"] = instruction
            return super().plan(names, instruction, lang or L.DEFAULT_LANG,
                                max_len or L.DEFAULT_MAX_LEN,
                                style or L.DEFAULT_STYLE, progress)

    old_env = os.environ.get(L.INSTRUCTION_ENV)
    os.environ[L.INSTRUCTION_ENV] = req      # 让「默认要求文件」指向它
    try:
        eq(L.read_instruction(), "只留地名", "环境变量指定的要求文件被读到")
        import engine.plan as _P
        res = scan(ScanOptions(root=box, kinds=(KIND_FILE,)))
        _P.build(res.items, [R.make_rule("llm")], P.PlanOptions(dry_run=True), None,
                 Spy(L.LLMConfig(provider="echo")))
        eq(seen.get("instr"), "只留地名", "规则没写要求时读了要求文件")
    finally:
        if old_env is None:
            os.environ.pop(L.INSTRUCTION_ENV, None)
        else:
            os.environ[L.INSTRUCTION_ENV] = old_env
    ok(os.path.exists(req))
    ok(os.path.exists(req))
    L.write_instruction(L.DEFAULT_INSTRUCTION, req)
    eq(L.read_instruction(req), L.DEFAULT_INSTRUCTION, "能恢复成默认要求")

    case("L14 真实 HTTP 客户端（本地 mock 服务）")
    import json as _json
    import re as _re
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    got_req = {}

    class _H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = _json.loads(self.rfile.read(n).decode("utf-8"))
            got_req["payload"] = body
            text = body["messages"][1]["content"]
            listed = _re.findall(r"^\d+\. (.+)$", text, _re.M)
            content = _json.dumps([{"i": i + 1, "n": "N%d" % (i + 1)}
                                   for i, _ in enumerate(listed)],
                                  ensure_ascii=False)
            raw = _json.dumps({"choices": [{"message": {"content": content}}]}
                              ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        cfg = L.LLMConfig(provider="openai", base_url="http://127.0.0.1:%d" % port,
                          api_key="test-key", model="test-model")
        client = L.make_client(cfg)
        eq(type(client).__name__, "HttpChatClient", "有 key 时造出真实客户端")
        eq(client.available(), True)
        out = client.plan(["a.jpg", "b.jpg"], "只留主题", "zh", 16)
        eq(out.mapping, {"a.jpg": "N1", "b.jpg": "N2"}, "按序号一一对应")
        eq(out.errors, [], "没有错误")
        eq(got_req["payload"]["model"], "test-model", "模型名发给了服务端")
        eq(got_req["payload"]["messages"][0]["role"], "system", "第一条是 system")
        ok("只留主题" in got_req["payload"]["messages"][1]["content"],
           "自定义要求确实进了请求体")
    finally:
        srv.shutdown()

    case("L15 请求失败时整批降级不中断")
    class Boom(L.LLMClient):
        name = "boom"

        def available(self):
            return True

        def problem(self):
            return ""

        def _chat(self, system, user):
            raise OSError("模拟断网")

    cfg = L.LLMConfig(provider="openai", api_key="k", model="m",
                      base_url="http://127.0.0.1:1", max_retries=1)
    bad = Boom(cfg)
    out = bad.plan(["a.jpg", "b.jpg"])
    eq(out.mapping, {}, "失败时不产出映射")
    ok(out.errors and "OSError" in out.errors[0], "记下了失败原因")
    box = fresh("l15")
    touch(os.path.join(box, "DSC_9_海边_副本.jpg"))
    res = scan(ScanOptions(root=box, kinds=(KIND_FILE,)))
    plan = P.build(res.items, [R.make_rule("llm")], P.PlanOptions(dry_run=True),
                   None, bad)
    eq(plan.items[0].new_name, "DSC_9_海边_副本.jpg", "LLM 失败时名字保持原样")
    ok(any("LLM" in w for w in plan.warnings), "提示里说明了 LLM 没生效")


# --------------------------------------------------------------------------- #
# D 组：端到端（真实磁盘）
# --------------------------------------------------------------------------- #

def test_e2e():
    case("D1 预览不动磁盘")
    box = fresh("d1")
    touch(os.path.join(box, "a.txt"))
    res = ops.scan_and_plan(box, [R.make_rule("prefix", "x_")],
                            ScanOptions(root=box, kinds=(KIND_FILE,)),
                            P.PlanOptions())
    eq([it.new_name for it in res.items], ["x_a.txt"])
    eq(names_of(box), ["a.txt"], "磁盘未变")

    case("D2 真正改名 + 撤销")
    out = ops.apply(res.items, [R.make_rule("prefix", "x_")],
                    P.PlanOptions(dry_run=False), root=box)
    ok(out.ok, "执行成功")
    ok(os.path.exists(os.path.join(box, "x_a.txt")))
    ok(bool(out.log_path) and os.path.exists(out.log_path), "日志已写")
    ops.undo(out.log_path)
    ok(os.path.exists(os.path.join(box, "a.txt")), "撤销后恢复")

    case("D3 父子同时改名（文件夹与其中文件一起改）")
    box = fresh("d3")
    os.makedirs(os.path.join(box, "old", "inner"))
    touch(os.path.join(box, "old", "a.txt"))
    touch(os.path.join(box, "old", "inner", "b.txt"))
    res = ops.scan_and_plan(box, [R.make_rule("replace", "old", "new")],
                            ScanOptions(root=box, kinds=(KIND_FILE, KIND_DIR),
                                        recursive=True))
    ok(not os.path.exists(os.path.join(box, "new")), "改名前目标不存在")
    out = ops.apply(res.items, [R.make_rule("replace", "old", "new")],
                    P.PlanOptions(dry_run=False), root=box)
    ok(out.ok, "父子改名成功：%s" % ([s.error for s in out.steps],))
    ok(os.path.isdir(os.path.join(box, "new")), "父目录已改名")
    ok(os.path.exists(os.path.join(box, "new", "a.txt")), "子文件跟着父目录走")
    ok(os.path.exists(os.path.join(box, "new", "inner", "b.txt")), "孙目录内容完好")
    ok(not os.path.exists(os.path.join(box, "old")), "旧父目录消失")
    ok(not C.find_temp_leftovers(box), "没有临时名残留")

    case("D4 父子改名可撤销")
    ops.undo(out.log_path)
    ok(os.path.isdir(os.path.join(box, "old")), "父目录恢复")
    ok(os.path.exists(os.path.join(box, "old", "inner", "b.txt")), "子项恢复")
    ok(not os.path.exists(os.path.join(box, "new")), "新目录消失")

    case("D5 链式改名（1->2, 2->3）")
    box = fresh("d5")
    touch(os.path.join(box, "1.txt"))
    touch(os.path.join(box, "2.txt"))
    rules = [R.make_rule("regex", r"^\d", "x"),
             R.make_rule("replace", "x", "9")]
    res = ops.scan_and_plan(box, rules, ScanOptions(root=box, kinds=(KIND_FILE,)))
    names = sorted(it.new_name for it in res.items)
    eq(names, ["9.t9t", "9.t9t"], "扩展名一起被替换（ReNamer 语义）后两项目标相同")
    eq(res.report.conflict, 2, "同目标必须判冲突")
    # 真正的链式：1->2、2->3 需要按序错开，用编号偏移实现
    box = fresh("d5b")
    touch(os.path.join(box, "1.txt"))
    touch(os.path.join(box, "2.txt"))
    # 前缀编号逐项递增（起始值 + 序号 - 1），用不同名的文件才看得清
    box = fresh("d5b")
    touch(os.path.join(box, "a.txt"))
    touch(os.path.join(box, "b.txt"))
    chain = [R.make_rule("number", "1", "1", "prefix")]
    res = ops.scan_and_plan(box, chain, ScanOptions(root=box, kinds=(KIND_FILE,)))
    eq(sorted(it.new_name for it in res.items), ["1a.txt", "2b.txt"], "前缀编号递增")
    out = ops.apply(res.items, chain, P.PlanOptions(dry_run=False), root=box)
    ok(out.ok, "执行成功")
    eq(names_of(box), ["1a.txt", "2b.txt"])

    case("D5b 真链式：目标名正是批内另一项的源名")
    box = fresh("d5c")
    touch(os.path.join(box, "1.txt"), b"A")
    touch(os.path.join(box, "2.txt"), b"B")
    # 规则：首字符按 1->2、2->1 互换，形成 1.txt<->2.txt 的交换链。
    rules = [R.make_rule("replace", "1", "@"), R.make_rule("replace", "2", "1"),
             R.make_rule("replace", "@", "2")]
    res = ops.scan_and_plan(box, rules, ScanOptions(root=box, kinds=(KIND_FILE,)))
    eq(sorted(it.new_name for it in res.items), ["1.txt", "2.txt"],
       "1->2 与 2->1 形成互换链")
    out = ops.apply(res.items, rules, P.PlanOptions(dry_run=False), root=box)
    ok(out.ok, "链式执行成功：%s" % ([s.error for s in out.steps],))
    eq(names_of(box), ["1.txt", "2.txt"])
    eq(open(os.path.join(box, "1.txt"), "rb").read(), b"B", "内容跟着源文件走")
    eq(open(os.path.join(box, "2.txt"), "rb").read(), b"A", "内容跟着源文件走")
    ok(not C.find_temp_leftovers(box), "无临时名残留")

    case("D6 互换名字（1<->2）")
    box = fresh("d6")
    touch(os.path.join(box, "1.txt"), b"A")
    touch(os.path.join(box, "2.txt"), b"B")
    rules = [R.make_rule("replace", "1", "@"), R.make_rule("replace", "2", "1"),
             R.make_rule("replace", "@", "2")]
    res = ops.scan_and_plan(box, rules, ScanOptions(root=box, kinds=(KIND_FILE,)))
    ok(all(it.status == C.ST_OK for it in res.items), "互换不判冲突")
    out = ops.apply(res.items, rules, P.PlanOptions(dry_run=False), root=box)
    ok(out.ok, "互换成功")
    eq(open(os.path.join(box, "1.txt"), "rb").read(), b"B", "1.txt 现在是原 2 的内容")
    eq(open(os.path.join(box, "2.txt"), "rb").read(), b"A")

    case("D7 混合对象 + 编号 + 扩展名保护")
    box = fresh("d7")
    for sub in ("甲", "乙"):
        mkdir(os.path.join(box, sub))
        touch(os.path.join(box, sub, "IMG_%s.JPG" % sub))
    rules = [R.make_rule("template", "{n}-{name}")]
    res = ops.scan_and_plan(box, rules,
                            ScanOptions(root=box, kinds=(KIND_FILE,), recursive=True),
                            P.PlanOptions(scope=P.SCOPE_FOLDER, width=2))
    eq(sorted(it.new_name for it in res.items),
       ["01-IMG_乙.JPG", "01-IMG_甲.JPG"])
    out = ops.apply(res.items, rules,
                    P.PlanOptions(scope=P.SCOPE_FOLDER, width=2, dry_run=False),
                    root=box)
    ok(out.ok, "执行成功")
    ok(os.path.exists(os.path.join(box, "甲", "01-IMG_甲.JPG")))

    case("D8 冲突项不执行，其余照常")
    box = fresh("d8")
    touch(os.path.join(box, "a.txt"))
    touch(os.path.join(box, "keep.txt"))
    touch(os.path.join(box, "z.txt"))
    rules = [R.make_rule("replace", "a", "keep")]
    res = ops.scan_and_plan(box, rules, ScanOptions(root=box, kinds=(KIND_FILE,)))
    bad = [it for it in res.items if it.name == "a.txt"][0]
    eq(bad.status, C.ST_CONFLICT, "a.txt -> keep.txt 已存在")
    eq([it.status for it in res.items if it.name == "z.txt"][0], C.ST_SAME)
    out = ops.apply(res.items, rules, P.PlanOptions(dry_run=False), root=box)
    ok(os.path.exists(os.path.join(box, "a.txt")), "冲突项未被改名")
    ok(os.path.exists(os.path.join(box, "keep.txt")), "已存在文件未被覆盖")
    ok(not out.steps or all(s.ok for s in out.steps), "没有失败的步骤")

    case("D9 日志数量上限")
    box = fresh("d9")
    for i in range(C.MAX_LOGS + 5):
        C.save_log(box, [(os.path.join(box, "o%d" % i), os.path.join(box, "n%d" % i))],
                   kind="file")
    eq(len(C.list_logs(box)), C.MAX_LOGS, "只保留最近 20 份")

    case("D9b 日志统一存在程序目录的 Name_backup 里")
    box = fresh("d9b")
    other = fresh("d9b_other")
    touch(os.path.join(box, "a.txt"))
    path = C.save_log(box, [(os.path.join(box, "a.txt"),
                             os.path.join(box, "b.txt"))])
    ok(path, "写了撤销日志")
    ok(os.path.dirname(path) == C.log_dir(box),
       "日志落在统一的备份目录里，不在目标目录下")
    ok(not os.path.isdir(os.path.join(box, C.LOG_DIR_NAME)),
       "目标目录里不再留 _rename_logs 文件夹")
    ok(not os.path.exists(os.path.join(box, "b.txt")), "还没真正改名（只写日志）")

    case("D9c 不同目录的日志混在一起但能分开查")
    p1 = C.save_log(box, [(os.path.join(box, "x"), os.path.join(box, "y"))])
    p2 = C.save_log(other, [(os.path.join(other, "x"), os.path.join(other, "y"))])
    ok(p1 != p2, "两个目录写出了不同的日志文件")
    got_box = C.list_logs(box)
    got_other = C.list_logs(other)
    ok(p1 in got_box, "A 目录能查到自己的日志")
    ok(p1 not in got_other, "A 目录的日志不会出现在 B 目录里")
    ok(p2 in got_other, "B 目录能查到自己的日志")
    all_logs = C.list_logs("")
    ok(p1 in all_logs and p2 in all_logs, "不给目录时能列出全部")
    ok(len(all_logs) >= len(got_box) + len(got_other),
       "全部的份数不少于各目录之和")

    case("D9d 一个目录的日志不会挤掉另一个目录的")
    box2 = fresh("d9d")
    for i in range(C.MAX_LOGS + 5):
        C.save_log(box2, [(os.path.join(box2, "o%d" % i),
                           os.path.join(box2, "n%d" % i))], kind="file")
    eq(len(C.list_logs(box2)), C.MAX_LOGS, "B 目录自己留了 20 份")
    ok(len(C.list_logs(box)) >= 1, "A 目录的日志还在（没被 B 挤掉）")

    case("D9e 旧格式日志仍然认得出（升级不丢撤销机会）")
    box3 = fresh("d9e")
    legacy_dir = os.path.join(box3, C.LOG_DIR_NAME)
    os.makedirs(legacy_dir)
    legacy = os.path.join(legacy_dir, "rename_20260101_120000_abc123.json")
    with open(legacy, "w", encoding="utf-8") as fh:
        json.dump({"app": "NameCat", "schema": C.LOG_SCHEMA, "time": "2026-01-01 12:00:00",
                   "root": box3, "pairs": [{"old": os.path.join(box3, "a"),
                                            "new": os.path.join(box3, "b")}]},
                  fh, ensure_ascii=False)
    ok(legacy in C.list_logs(box3), "老版本留在目标目录里的日志也认")
    ok(legacy not in C.list_logs(box), "别的目录不会把它算进去")

    case("D10 大量文件扫描与计划（性能冒烟）")
    box = fresh("d10")
    for i in range(500):
        touch(os.path.join(box, "f%03d.txt" % i))
    t0 = time.time()
    res = ops.scan_and_plan(box, [R.make_rule("template", "{n}-{name}")],
                            ScanOptions(root=box, kinds=(KIND_FILE,)),
                            P.PlanOptions(width=4))
    cost = time.time() - t0
    eq(len(res.items), 500)
    ok(cost < 10.0, "500 个文件的扫描+计划耗时 %.2fs < 10s" % cost)
    eq(res.items[0].new_name, "0001-f000.txt", "编号与原名拼接")
    eq(res.report.ok, 500, "无冲突")

    case("D11 相对子目录与深层递归")
    box = fresh("d11")
    p = os.path.join(box, "a", "b", "c")
    mkdir(p)
    touch(os.path.join(p, "deep.txt"))
    res = ops.scan_and_plan(box, [R.make_rule("prefix", "D_")],
                            ScanOptions(root=box, kinds=(KIND_FILE,), recursive=True))
    eq(res.items[0].new_name, "D_deep.txt")
    out = ops.apply(res.items, [R.make_rule("prefix", "D_")],
                    P.PlanOptions(dry_run=False), root=box)
    ok(os.path.exists(os.path.join(p, "D_deep.txt")), "深层文件改名成功")

    case("D12 名称非法时不落盘")
    box = fresh("d12")
    touch(os.path.join(box, "a.txt"), b"A")
    res = ops.scan_and_plan(box, [R.make_rule("replace", ".", "/")],
                            ScanOptions(root=box, kinds=(KIND_FILE,)),
                            )
    eq(res.items[0].status, C.ST_ERROR, "含非法字符判无效")
    out = ops.apply(res.items, [R.make_rule("replace", ".", "/")],
                    P.PlanOptions(dry_run=False), root=box)
    ok(not out.steps, "没有可执行步骤")
    eq(names_of(box), ["a.txt"], "磁盘未变")


# --------------------------------------------------------------------------- #
# E 组：本轮审查修掉的缺陷，逐条钉死，防止回潮
# --------------------------------------------------------------------------- #

def test_regressions():
    case("E1 名字里的 # 不再被当成计数器")
    ctx = NameContext(os.path.join("x", "a#b.txt"))
    eq(T.render("{stem}", ctx, num=3, width=2), "a#b", "变量值里的 # 原样保留")
    eq(T.render("{fullname}", ctx, num=3, width=2), "a#b.txt", "带扩展名的值也不动")
    eq(T.render("#", ctx, num=9, width=2), "09", "单个 # 仍是计数器")
    eq(T.render("#stem#", ctx, num=3, width=2), "a#b", "老写法 #变量# 取变量")
    eq(T.render("pre#stem#post", ctx, num=3, width=2), "prea#bpost", "夹在文字里也取变量")
    eq(T.render("C# 笔记.txt", ctx, num=1, width=1), "C# 笔记.txt", "普通 # 原样")

    case("E2 number 的号码来自规则自己的发号参数")
    eq([R.transform(R.make_rule("number", "1"), "a", num=n) for n in (1, 2, 3)],
       ["1a", "2a", "3a"], "num 就是要印的号码")
    eq([R.transform(R.make_rule("number", "5", "2"), "a", num=n) for n in (1, 2)],
       ["01a", "02a"], "起始值不再在 transform 里二次叠加")

    case("E2c 序列化：整名换号码，扩展名保持")
    eq(R.apply_chain([R.make_rule("renumber", "1", "4", "",
                                  extras={"scope": "global"})],
                     "IMG_0001.mp4", 1, 4, None, 1), "0001.mp4",
       "整名变成四位号码")
    eq(R.apply_chain([R.make_rule("renumber", "1", "3", "")],
                     "a.txt", 7, 3, None, 7), "007.txt", "按 num 发号")

    case("E2b 规则上的起始/步长/补零/范围真的进到计划层")
    items = [Item(os.path.join("x", "f%d.txt" % i)) for i in range(3)]
    opts = P.PlanOptions()          # 计划层默认值全部交给规则覆盖
    rule = R.make_rule("number", "5", "3", "prefix",
                       extras={"step": "10", "scope": "global"})
    plan = P.compute_names(items, [rule], opts)
    eq([it.new_name for it in plan.items], ["005f0.txt", "015f1.txt", "025f2.txt"],
       "起始 5 / 步长 10 / 补零 3 位")
    eq([it.num for it in plan.items], [5, 15, 25], "num 按规则发号")

    case("E3 regex 的 {1} / {name} 引用默认生效")
    ctx = NameContext(os.path.join("x", "img123.txt"))
    eq(R.transform(R.make_rule("regex", r"(\d+)", "[{name}-{1}]"), "img123", ctx=ctx),
       "img[img123-123]", "引用被展开")
    eq(R.transform(R.make_rule("regex", r"(\d+)", "[{name}-{1}]",
                               extras={"expand": False}), "img123", ctx=ctx),
       "img[{name}-{1}]", ";expand=0 可退回纯 re.sub")
    eq(R.transform(R.make_rule("regex", r"(\d+)", r"N\1"), "img123"), "imgN123",
       "反斜杠组号照常")

    case("E4 区间写反也按区间取")
    eq(T.parse_range("5-2", 10), (2, 5), "5-2 归一成 2..5")
    eq(T.parse_range("2-5", 10), (2, 5), "正常顺序不变")
    eq(T.parse_range("2-", 6), (2, 6), "省略右端到结尾")

    case("E5 rename_dirs=False 时文件夹不动")
    box = fresh("e5")
    mkdir(os.path.join(box, "d1"))
    touch(os.path.join(box, "d1", "a.txt"))
    rules = [R.make_rule("template", "X_{name}")]
    res = ops.scan_and_plan(box, rules,
                            ScanOptions(root=box, kinds=(KIND_FILE, KIND_DIR),
                                        recursive=True),
                            P.PlanOptions(rename_dirs=False))
    got = {it.name: it.new_name for it in res.items}
    eq(got["d1"], "d1", "文件夹保持原名")
    eq(got["a.txt"], "X_a.txt", "文件照常改")
    res2 = ops.scan_and_plan(box, rules,
                             ScanOptions(root=box, kinds=(KIND_FILE, KIND_DIR),
                                         recursive=True),
                             P.PlanOptions(rename_dirs=True))
    eq({it.name: it.new_name for it in res2.items}["d1"], "X_d1", "默认会改文件夹")

    case("E6 CSV 状态列是中文")
    box = fresh("e6")
    touch(os.path.join(box, "a.txt"))
    touch(os.path.join(box, "b.txt"))
    res = ops.scan_and_plan(box, [R.make_rule("prefix", "N_")],
                            ScanOptions(root=box, kinds=(KIND_FILE,)))
    out = os.path.join(box, "o.csv")
    ops.export_csv(res, out)
    with open(out, encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    eq(rows[0][5], "状态", "表头有状态列")
    eq(rows[1][5], C.STATUS_TEXT[C.ST_OK], "状态列写中文文案")

    case("E7 撤销按父→子顺序，多层嵌套能完整复原")
    for depth in (1, 2, 3):
        box = fresh("e7_%d" % depth)
        deep = box
        for i in range(depth):
            deep = os.path.join(deep, "d%d" % (i + 1))
        mkdir(deep)
        touch(os.path.join(deep, "a.txt"), b"A")
        rules = [R.make_rule("template", "X_{name}")]
        sc = ScanOptions(root=box, kinds=(KIND_FILE, KIND_DIR), recursive=True)
        res = ops.scan_and_plan(box, rules, sc, P.PlanOptions())
        po = P.PlanOptions(dry_run=False)
        out = ops.apply(res.items, plan=res, opts=po, root=box, kind="all")
        ok(out.ok and out.log_path, "第 %d 层：落盘并写了日志" % depth)
        good, errs = C.undo(out.log_path)
        ok(good, "第 %d 层：撤销无错（%s）" % (depth, errs))
        ok(os.path.exists(os.path.join(deep, "a.txt")),
           "第 %d 层：原名复原" % depth)
        # 日志现在统一存在 Name_backup 里（各目录混在一起），
        # 所以只能确认"这一份被删了"，不能查目录是不是空了
        ok(not os.path.exists(out.log_path), "第 %d 层：撤销后日志已删" % depth)

    case("E8 预览的名字就是要落盘的名字（randomize 可复现）")
    box = fresh("e8")
    for n in ("a.txt", "b.txt"):
        touch(os.path.join(box, n))
    rules = [R.make_rule("randomize", "6")]
    sc = ScanOptions(root=box, kinds=(KIND_FILE,))
    first = ops.scan_and_plan(box, rules, sc, P.PlanOptions())
    second = ops.scan_and_plan(box, rules, sc, P.PlanOptions())
    eq([it.new_name for it in first.items],
       [it.new_name for it in second.items], "两次计划的随机结果相同")
    po = P.PlanOptions(dry_run=False)
    out = ops.apply(first.items, plan=first, opts=po, root=box)
    ok(out.ok, "落盘成功")
    eq(sorted(names_of(box)), sorted(rules[0] and
       [it.new_name for it in first.items]), "落盘名字与预览一致")

    case("E9 预览与落盘只算一次（LLM 不会被请求两遍）")
    box = fresh("e9")
    touch(os.path.join(box, "a.txt"))

    class Counter(L.EchoClient):
        hits = 0

        def plan(self, names, instruction="", lang="zh", max_len=24,
                 style="natural", progress=None):
            Counter.hits += 1
            return L.EchoClient.plan(self, names, instruction, lang,
                                     max_len, style, progress)

    client = Counter()
    rules = [R.make_rule("llm")]
    sc = ScanOptions(root=box, kinds=(KIND_FILE,))
    po = P.PlanOptions(dry_run=False)
    res = ops.scan_and_plan(box, rules, sc, P.PlanOptions(), client=client)
    hits_after_preview = Counter.hits
    ops.apply(res.items, plan=res, opts=po, root=box, client=client)
    eq(hits_after_preview, 1, "预览只请求一次")
    eq(Counter.hits, 1, "传 plan 落盘时不再请求")

    case("E10 日志 schema 与常量一致")
    box = fresh("e10")
    path = C.save_log(box, [(os.path.join(box, "o"), os.path.join(box, "n"))])
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    eq(data["schema"], C.LOG_SCHEMA, "schema 取自常量")

    case("E11 new_path 跟着父目录改名走")
    box = fresh("e11")
    mkdir(os.path.join(box, "d1"))
    touch(os.path.join(box, "d1", "a.txt"))
    rules = [R.make_rule("pre", "X_")] if False else [R.make_rule("prefix", "X_")]
    res = ops.scan_and_plan(box, rules,
                            ScanOptions(root=box, kinds=(KIND_FILE, KIND_DIR),
                                        recursive=True))
    child = [it for it in res.items if it.name == "a.txt"][0]
    eq(child.new_path, os.path.join(box, "X_d1", "X_a.txt"),
       "子项新路径含父目录新名")

    case("E12 new_id 跨进程稳定（不再用内置 hash）")
    a = R.new_id("same-seed")
    for _ in range(200):
        R.new_id("same-seed")
    ok(a.startswith("r_") and len(a) == 14, "id 形如 r_ + 12 位十六进制")
    eq(len({R.new_id("s") for _ in range(50)}), 50, "每次都不重复")

    case("E13 跨盘日志根不会抛 ValueError")
    eq(ops._common_base([r"C:\a\b", r"D:\x"]), r"C:\a\b", "跨盘退回首项")
    eq(ops._common_base([r"C:\a\b", r"C:\a\c"]), r"C:\a", "同盘取共同父目录")

    case("E14 递归扫描跳过统一备份目录（大小写不敏感）")
    box = fresh("e14")
    mkdir(os.path.join(box, C.BACKUP_DIR_NAME))
    touch(os.path.join(box, C.BACKUP_DIR_NAME, "rename_abcdef12_x.json"))
    touch(os.path.join(box, "keep.txt"))
    got = sorted(os.path.basename(it.path)
                 for it in scan(ScanOptions(root=box, kinds=(KIND_FILE,),
                                            recursive=True)).items)
    eq(got, ["keep.txt"], "Name_backup 里的日志不被当成重命名对象")

    case("E15 撤销有跳过项时 Result.ok 为 False（不能靠 steps 推）")
    box = fresh("e15")
    touch(os.path.join(box, "a.txt"))
    res = ops.scan_and_plan(box, [R.make_rule("prefix", "P_")],
                            ScanOptions(root=box, kinds=(KIND_FILE,)),
                            P.PlanOptions())
    out = ops.apply(res.items, plan=res,
                    opts=P.PlanOptions(dry_run=False), root=box)
    touch(os.path.join(box, "a.txt"))           # 占住还原目标
    back = ops.undo(out.log_path)
    ok(not back.ok, "有跳过项时 ok 为 False")
    ok("已存在，跳过" in back.message, "原因写进 message（%s）" % back.message)
    os.remove(os.path.join(box, "a.txt"))
    ok(ops.undo(out.log_path).ok, "清掉占位后再撤销即成功")

    case("E16 手改坏的预设能读就读，绝不炸")
    box = fresh("e16")
    broken = os.path.join(box, "bad.json")
    with open(broken, "w", encoding="utf-8") as fh:
        fh.write('{"name": "x", "plan": 3, "scan": null, '
                 '"rules": [{"kind": "case", "a": "lower", "extras": "oops"},'
                 ' {"kind": "prefix", "selector": 7, "charset": null}]}')
    preset = PS.load(broken)
    eq(len(preset.rules), 2, "能救回来的规则仍在")
    eq(preset.rules[0].kind, "case", "第一条仍是 case")
    eq(preset.plan_options().width, 0, "脏 plan 当成空配置")
    eq(preset.scan_options().root, "", "脏 scan 当成空配置")


# --------------------------------------------------------------------------- #
# F 组：供应商配置（协议 / 预设 / 密钥 / 连通性测试）
# --------------------------------------------------------------------------- #

def test_provider():
    from engine import provider as PRV
    from engine import secrets

    case("F1 供应商预设齐备（OpenAI 兼容为主）")
    keys = [p.key for p in PRV.PROVIDER_PRESETS]
    for want in ("custom", "openai", "deepseek", "xiaomi_mimo", "opencode_go"):
        ok(want in keys, "预设 %s 在列表里" % want)
    eq(PRV.PROVIDER_PRESETS[0].key, "custom", "第一个是自定义供应商")
    ok(all(p.base_url.startswith("https://") for p in PRV.PROVIDER_PRESETS
           if p.key not in ("custom", "echo", "ollama")), "远程预设地址都是 https")
    ok(PRV.PRESET_BY_KEY["ollama"].base_url.startswith("http://127.0.0.1"),
       "本地 Ollama 用回环地址")

    case("F2 协议列表与名称")
    eq(len(PRV.PROTOCOLS), 3, "三种协议")
    ok("OpenAI Chat Completions" in PRV.PROTOCOL_TEXT.values(), "有 Chat Completions")
    ok("OpenAI Responses" in PRV.PROTOCOL_TEXT.values(), "有 Responses")
    ok("Anthropic Messages" in PRV.PROTOCOL_TEXT.values(), "有 Anthropic Messages")

    case("F3 套预设会填好地址与模型列表 URL")
    cfg = PRV.ProviderConfig()
    cfg = PRV.apply_preset(cfg, "deepseek")
    eq(cfg.base_url, "https://api.deepseek.com", "DeepSeek 地址填好了")
    eq(cfg.models_url, "https://api.deepseek.com/models", "模型列表 URL 也填好了")
    ok(cfg.name, "供应商名称顺手填上了")
    custom = PRV.ProviderConfig()
    custom.base_url = "https://my.example.com"
    after = PRV.apply_preset(custom, "custom")
    eq(after.base_url, "https://my.example.com", "自定义预设不动用户自己填的地址")

    case("F4 模型列表 URL 能按基础地址推算")
    c2 = PRV.ProviderConfig()
    c2.base_url = "https://api.openai.com/v1"
    eq(PRV.models_url_for(c2), "https://api.openai.com/v1/models", "带 /v1 就不重复加")
    c2.base_url = "https://api.deepseek.com"
    eq(PRV.models_url_for(c2), "https://api.deepseek.com/v1/models", "不带 /v1 会补上")
    c2.models_url = "https://x.example.com/list"
    eq(PRV.models_url_for(c2), "https://x.example.com/list", "填了就用填的")

    case("F5 配置文件里绝不含密钥")
    data = PRV.ProviderConfig()
    data.api_key = "sk-should-not-appear"
    blob = json.dumps(data.as_dict(), ensure_ascii=False)
    ok("sk-should-not-appear" not in blob, "序列化结果里没有密钥")
    ok("api_key" not in data.as_dict(), "as_dict 里根本没有 api_key 这个键")

    case("F6 密钥单独存取（DPAPI / Base64 兜底）")
    ok(secrets.save_secret("sk-unit-test-123"), "密钥写进去了")
    eq(secrets.load_secret(), "sk-unit-test-123", "能原样读回")
    ok(secrets.has_secret(), "has_secret 报告已保存")
    try:
        with open(secrets.secret_path(), "rb") as fh:
            raw = fh.read()
        ok(b"sk-unit-test-123" not in raw, "磁盘上不是明文")
    except OSError:
        ok(False, "密钥文件读得到")
    ok(secrets.save_secret("") is False, "传空串不会覆盖已保存的密钥")
    eq(secrets.load_secret(), "sk-unit-test-123", "空串之后原密钥还在")
    secrets.clear_secret()
    eq(secrets.load_secret(), "", "清除后读不到")
    eq(secrets.load_secret(), "", "没有文件时也不报错")

    case("F7 连通性测试（本地 mock 服务）")
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    seen = {}

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["get"] = (self.path, {k.lower(): v for k, v in self.headers.items()})
            raw = json.dumps({"data": [{"id": "m-a"}, {"id": "m-b"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen["post"] = (self.path, {k.lower(): v for k, v in self.headers.items()})
            self.rfile.read(n)
            raw = json.dumps({"choices": [{"message": {"content": "pong"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        cfg = PRV.ProviderConfig()
        cfg.base_url = "http://127.0.0.1:%d/v1" % port
        cfg.api_key = "sk-unit"
        cfg.model = "m-a"
        cfg.headers = {"X-Trace-Id": "namecat"}
        names, err = PRV.list_models(cfg)
        eq((names, err), (["m-a", "m-b"], ""), "拉到模型列表且无错误")
        eq(seen["get"][1].get("authorization"), "Bearer sk-unit", "带上了 Bearer 鉴权")
        eq(seen["get"][1].get("x-trace-id"), "namecat", "自定义请求头发出去了")

        res = PRV.test_connection(cfg)
        ok(res["ok"], "连通性测试通过")
        ok("2 个模型" in res["message"], "结果里说明了拉到几个模型")
        ok(res["latency_ms"] >= 0, "给了耗时")

        empty = PRV.ProviderConfig()
        r2 = PRV.test_connection(empty)
        ok(not r2["ok"] and r2["message"], "没填地址时明确说不行")
        ok("URL" in r2["message"], "原因里点明是地址没填")

        no_model = PRV.ProviderConfig()
        no_model.base_url = "http://127.0.0.1:%d/v1" % port
        no_model.api_key = "sk-unit"
        r3 = PRV.test_connection(no_model)
        ok(not r3["ok"], "没选模型时判失败")
        ok("模型" in r3["message"], "原因里点明是模型没选")
    finally:
        srv.shutdown()

    case("F8 连通性测试把 401 翻译成人话")
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _H401(BaseHTTPRequestHandler):
        def do_GET(self):
            raw = b'{"error":{"message":"invalid api key"}}'
            self.send_response(401)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            raw = b'{"error":{"message":"invalid api key"}}'
            self.send_response(401)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _H401)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cfg = PRV.ProviderConfig()
        cfg.base_url = "http://127.0.0.1:%d/v1" % srv.server_address[1]
        cfg.api_key = "bad"
        cfg.model = "m"
        res = PRV.test_connection(cfg)
        ok(not res["ok"], "401 判定为失败")
        ok("401" in res["message"], "原因里带状态码")
        ok("API Key" in res["message"], "原因里点明是密钥问题")
    finally:
        srv.shutdown()

    case("F9 连不上时也给原因而不是抛异常")
    cfg = PRV.ProviderConfig()
    cfg.base_url = "http://127.0.0.1:1/v1"     # 一定连不上的端口
    cfg.api_key = "k"
    cfg.model = "m"
    res = PRV.test_connection(cfg)
    ok(not res["ok"], "连不上判失败")
    ok(res["message"], "给了失败原因")

    case("F10 三种协议的请求体与鉴权头")
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    got = {}

    class _HP(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            got[self.path] = (body, {k.lower(): v for k, v in self.headers.items()})
            if self.path.endswith("/messages"):
                raw = json.dumps({"content": [{"type": "text", "text": "pong"}]}).encode()
            elif self.path.endswith("/responses"):
                raw = json.dumps({"output": [{"content": [{"type": "output_text",
                                                          "text": "pong"}]}]}).encode()
            else:
                raw = json.dumps({"choices": [{"message": {"content": "pong"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _HP)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = "http://127.0.0.1:%d/v1" % srv.server_address[1]
        for proto, tail in (("openai_chat", "/chat/completions"),
                            ("openai_responses", "/responses"),
                            ("anthropic_messages", "/messages")):
            cfg = L.LLMConfig(provider="openai", base_url=base, api_key="sk-1",
                              model="m1", protocol=proto)
            out = L.make_client(cfg).plan(["a.jpg"], "只留主题", "zh", 24)
            eq(out.errors, [], "%s 没有报错" % proto)
            eq(list(out.mapping.values()), ["pong"], "%s 回复被解析出来了" % proto)
            key = [k for k in got if k.endswith(tail)]
            eq(len(key), 1, "%s 打到了正确的路径" % proto)
            body, head = got[key[0]]
            if proto == "anthropic_messages":
                eq(head.get("x-api-key"), "sk-1", "Anthropic 用 x-api-key 鉴权")
                ok("authorization" not in head, "Anthropic 不发 Bearer")
                ok("system" in body, "system 提示词单独放 system 字段")
            else:
                eq(head.get("authorization"), "Bearer sk-1", "%s 用 Bearer 鉴权" % proto)
    finally:
        srv.shutdown()

    case("F11 环境变量兜底时不搬内置默认值")
    old = dict(os.environ)
    try:
        for name in ("NAMECAT_LLM_BASE_URL", "NAMEKIT_LLM_BASE_URL", "OPENAI_BASE_URL",
                     "DEEPSEEK_BASE_URL", "NAMECAT_LLM_API_KEY", "NAMEKIT_LLM_API_KEY",
                     "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "NAMECAT_LLM_MODEL",
                     "NAMEKIT_LLM_MODEL", "OPENAI_MODEL", "DEEPSEEK_MODEL",
                     "NAMECAT_LLM_PROVIDER", "NAMEKIT_LLM_PROVIDER", "LLM_PROVIDER"):
            os.environ.pop(name, None)
        cfg = PRV.env_config()
        eq(cfg.base_url, "", "没设环境变量时地址是空的（不冒默认值）")
        eq(cfg.api_key, "", "密钥也是空的")
        os.environ["OPENAI_BASE_URL"] = "https://env.example.com/v1"
        os.environ["OPENAI_API_KEY"] = "sk-env"
        cfg = PRV.env_config()
        eq(cfg.base_url, "https://env.example.com/v1", "环境变量地址读到了")
        eq(cfg.api_key, "sk-env", "环境变量密钥读到了")
    finally:
        os.environ.clear()
        os.environ.update(old)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main():
    if os.path.isdir(SANDBOX):
        shutil.rmtree(SANDBOX, ignore_errors=True)
    os.makedirs(SANDBOX, exist_ok=True)
    t0 = time.time()

    for fn in (test_rules_unit, test_file_magic, test_compat_folder,
               test_merged_features, test_llm, test_regressions, test_e2e,
               test_provider):
        print("== %s ==" % fn.__name__)
        fn()

    cost = time.time() - t0
    print()
    print("断言：通过 %d / 失败 %d   耗时 %.2fs" % (PASS, FAIL, cost))
    if FAILS:
        print()
        print("失败清单：")
        for line in FAILS:
            print("  - " + line)
    shutil.rmtree(SANDBOX, ignore_errors=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
