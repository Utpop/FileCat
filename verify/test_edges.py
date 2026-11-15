# -*- coding: utf-8 -*-
"""
verify/test_edges.py —— 边界条件与异常路径的补充测试（原有 run_tests.py 的增量）。

分两类断言：
    check() / eq()      期望行为。**必须通过**，失败则退出码 1。
    defect()            已确认的缺陷复现条件。只记录"仍存在 / 已修复"，
                        不计入通过率 —— 修好之后这里会自动变绿，不会永远红。

覆盖面（run_tests.py 未覆盖的部分）：
    · 规则链把名字清空、超长补零宽度这类"参数合法但结果非法"的输入
    · 链式改名的失败回滚完整性（中转名残留）
    · 跨盘符对象（folder）参与计划时的路径换算
    · 撤销只成功一半时的日志清理与幂等性
    · 撤销日志目录不可写、日志文件本身损坏
    · 命令行的异常输入与退出码（子进程真跑）
    · 规则归一化对程序化脏输入的耐受度
    · 界面：应用预设是否保住当前目录、LLM 页切页是否丢输入

单独运行：
    PYTHONPATH=_vendor python verify/test_edges.py
    PYTHONPATH=_vendor python verify/test_edges.py -v      # 打印每条通过的断言
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 撤销日志必须写进沙箱，别污染程序目录的 Name_backup
SANDBOX = os.path.join(ROOT, "_verify_edges")
os.environ["NAMECAT_LOG_DIR"] = os.path.join(SANDBOX, "_logs")

from engine import APP_NAME                              # noqa: E402
from namecat_py import common as C                      # noqa: E402
from namecat_py import llm as L                         # noqa: E402
from namecat_py import ops                              # noqa: E402
from namecat_py import plan as P                        # noqa: E402
from namecat_py import presets as PS                    # noqa: E402
from namecat_py import rules as R                       # noqa: E402
from namecat_py import template as T                    # noqa: E402
from namecat_py.context import Item, NameContext        # noqa: E402
from namecat_py.scan import ScanOptions, nkey, scan     # noqa: E402

PASS = 0
FAIL = 0
FAILS = []
DEFECTS = []
VERBOSE = "-v" in sys.argv
_case = ["?"]


def case(name):
    _case[0] = name
    if VERBOSE:
        print("-- %s" % name)


def check(cond, msg=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        if VERBOSE:
            print("   ok  %s" % (msg or _case[0]))
    else:
        FAIL += 1
        FAILS.append("%s：%s" % (_case[0], msg or "断言失败"))
        print("   FAIL %s：%s" % (_case[0], msg or "断言失败"))


def eq(got, want, msg=""):
    check(got == want, "%s（得 %r，期望 %r）" % (msg or "值不符", got, want))


def defect(name, reproduces, detail=""):
    """记录一个已知缺陷：reproduces=True 表示缺陷仍在。"""
    DEFECTS.append((name, bool(reproduces), detail))
    if reproduces:
        print("   [缺陷] %s —— 仍存在%s" % (name, ("：" + detail) if detail else ""))
    else:
        print("   [已修复] %s%s" % (name, ("：" + detail) if detail else ""))


# --------------------------------------------------------------------------- #
# 沙箱
# --------------------------------------------------------------------------- #

def fresh(name: str) -> str:
    path = os.path.join(SANDBOX, name)
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def touch(path: str, data: bytes = b"x") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def items_of(box: str, names) -> list:
    return [Item(os.path.join(box, n), n, False) for n in names]


def build(box, names, rules, **opts):
    its = items_of(box, names)
    return P.build(its, rules, P.PlanOptions(**opts))


def cli(*args, **kw):
    """真跑一次命令行，返回 (退出码, 合并输出)。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "_vendor")
    env["NAMECAT_LOG_DIR"] = os.path.join(SANDBOX, "_logs")
    proc = subprocess.run([sys.executable, os.path.join(ROOT, "main.py"), *args],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# --------------------------------------------------------------------------- #
# A. 规则链产出"非法但合法"的名字
# --------------------------------------------------------------------------- #

def test_empty_and_oversize():
    case("A1 规则把名字删空")
    box = fresh("a1")
    p = touch(os.path.join(box, "abc.txt"), b"hello")
    its = [Item(p, "abc.txt", False)]
    plan = P.build(its, [R.make_rule("delete", "abc")], P.PlanOptions())
    it = plan.items[0]
    # 期望：判为"无效"，绝不落盘成一个没有主干的 .txt
    defect("名字主干被删空后仍判为可改、并落盘成隐藏的 .txt",
           it.status == "ok" and it.new_name == ".txt",
           "delete=abc 作用于 abc.txt → %r / %s" % (it.new_name, it.status))
    # 同一条规则作用于文件夹（没有扩展名）时，反而是"名称未变"，提示词也误导
    d = os.path.join(box, "abc")
    os.makedirs(d, exist_ok=True)
    dp = P.build([Item(d, "abc", True)], [R.make_rule("delete", "abc")], P.PlanOptions())
    defect("文件夹被删空时报「规则未改变名称」而不是「无效」",
           dp.items[0].status == "same", "状态 %s / 说明 %r"
           % (dp.items[0].status, dp.items[0].note))

    case("A2 名字长度上限")
    eq(C.name_problem("a" * 255), "", "255 个字符合法")
    check(C.name_problem("a" * 256) != "", "256 个字符被拒")
    # 超长名字在落盘前必须被拦下来（中文按字符算，Windows 按 UTF-16 单元算）
    box = fresh("a2")
    touch(os.path.join(box, "x.txt"))
    plan = build(box, ["x.txt"], [R.make_rule("prefix", "长" * 300)])
    eq(plan.items[0].status, "error", "超长目标名判无效")
    check("过长" in plan.items[0].note, "说明里点明过长")

    case("A3 补零宽度 / 随机串长度的上限")
    ctx = NameContext(os.path.join(box, "7.txt"), "7.txt", False, 1, 1, 7, 0)
    t0 = time.time()
    huge = R.transform(R.make_rule("number", "1", "20000000"), "7", 7, 0, ctx)
    cost = time.time() - t0
    defect("补零宽度 / 随机串长度没有上限，单个对象即可撑出几十 MB 的名字",
           len(huge) > 1000,
           "b=20000000 产出 %d 字符（%.2fs）" % (len(huge), cost))
    eq(len(huge), T.MAX_WIDTH + 1, "编号宽度被夹到 %d（号码 + 原字符）" % T.MAX_WIDTH)
    eq(len(R.pad_numbers("7", 10 ** 6)), T.MAX_WIDTH, "pad_numbers 宽度同样夹到上限")
    eq(len(R.transform(R.make_rule("randomize", "9999"), "a", 1, 0, ctx)),
       T.MAX_WIDTH + 1, "随机串长度夹到上限（1 个原字符 + 64）")
    eq(len(T.apply_num_format(7, "999999")), T.MAX_WIDTH, "{n:999999} 也夹到上限")
    # 上限之外的宽度不会再产出超长名字，落盘前也不会因为"名字过长"被误判
    its = [Item(os.path.join(box, "7.txt"), "7.txt", False)]
    pl = P.build(its, [R.make_rule("number", "1", "20000000")], P.PlanOptions())
    eq(pl.items[0].status, "ok", "夹紧之后的号码是合法名字")


# --------------------------------------------------------------------------- #
# B. 落盘与回滚
# --------------------------------------------------------------------------- #

def test_rollback():
    case("B1 链式改名在落位阶段失败后的回滚完整性")
    box = fresh("b1")
    for n in ("a.txt", "b.txt", "c.txt"):
        touch(os.path.join(box, n), n.encode())
    its = items_of(box, ["a.txt", "b.txt", "c.txt"])
    # a/b 互换 → 触发「临时名中转」；c 改成 d
    mapping = {"a.txt": "b.txt", "b.txt": "a.txt", "c.txt": "d.txt"}
    for it in its:
        it.new_name = mapping[it.name]
    plan = P.finalize(P.Plan(items=its))
    eq(plan.report.ok, 3, "三项都通过校验")
    # 校验之后、落盘之前，别的程序抢先占了 c 的目标名
    touch(os.path.join(box, "d.txt"), b"external")
    steps = C.execute([it for it in its if it.status == "ok"])
    left = sorted(os.listdir(box))
    temps = [n for n in left if n.startswith("__nk_tmp_")]
    rolled = [s for s in steps if not s.ok and "已回滚" in (s.error or "")]
    defect("链式改名落位失败后，已落位的项被留在 __nk_tmp_ 临时名下（且谎报「已回滚」）",
           bool(temps),
           "残留 %d 个临时名：%s；同时 %d 项自称已回滚"
           % (len(temps), temps[:3], len(rolled)))
    check(os.path.exists(os.path.join(box, "d.txt")), "外部抢建的文件没被覆盖")
    # 崩溃残留（不是本函数制造的）要能被找出来，且有命令行入口
    touch(os.path.join(box, "__nk_tmp_crash01"))
    eq(len(C.find_temp_leftovers(box)), 1, "遗留的临时名可以被扫出来")
    code, out = cli("leftovers", box)
    eq(code, 1, "leftovers 发现残留时返回 1")
    check("__nk_tmp_crash01" in out, "输出里列出了残留文件")

    case("B2 非链式路径失败时回滚干净")
    box = fresh("b2")
    for n in ("a.txt", "b.txt", "c.txt"):
        touch(os.path.join(box, n), n.encode())
    its = items_of(box, ["a.txt", "b.txt", "c.txt"])
    for it in its:
        it.new_name = "n_" + it.name
    plan = P.finalize(P.Plan(items=its))
    eq(plan.report.ok, 3, "三项通过校验")
    # 落盘前一刻，第三个目标被一个同名「文件夹」占住 → 该项失败，前两项必须回滚
    os.makedirs(os.path.join(box, "n_c.txt"), exist_ok=True)
    steps = C.execute(list(its))
    eq(len(steps), 3, "三项都尝试了")
    check(not steps[2].ok, "被占住的那项失败")
    eq(sorted(os.listdir(box)), ["a.txt", "b.txt", "c.txt", "n_c.txt"],
       "前两项已回滚成原名（没有留下半成品）")
    check(all("回滚" in (s.error or "") or s.ok for s in steps[:2]), "已回滚的项有说明")
    check(bool(steps[2].error), "失败项带错误文本：%r" % (steps[2].error or "")[:60])

    case("B3 目标已存在时跳过而不是覆盖")
    box = fresh("b3")
    touch(os.path.join(box, "a.txt"), b"AAA")
    touch(os.path.join(box, "b.txt"), b"BBB")
    plan = build(box, ["a.txt"], [R.make_rule("template", "b")])
    eq(plan.items[0].status, "conflict", "目标已存在的项标冲突")
    eq(open(os.path.join(box, "b.txt"), "rb").read(), b"BBB", "原文件内容未被覆盖")

    case("B4 批内重名")
    box = fresh("b4")
    for n in ("a.txt", "b.txt", "c.txt"):
        touch(os.path.join(box, n))
    plan = build(box, ["a.txt", "b.txt", "c.txt"], [R.make_rule("template", "same")])
    eq(plan.items[0].status, "conflict", "三项同名 → 全部冲突")
    eq(plan.report.conflict, 3, "冲突计数正确")

    case("B5 只改大小写")
    box = fresh("b5")
    touch(os.path.join(box, "abc.txt"))
    plan = build(box, ["abc.txt"], [R.make_rule("case", "upper")])
    eq(plan.items[0].status, "ok", "只改大小写判为可改（Windows 允许）")
    res = ops.apply(plan.items, [R.make_rule("case", "upper")],
                    P.PlanOptions(dry_run=False), root=box)
    check(res.ok, "大小写改名成功")
    eq(sorted(os.listdir(box)), ["ABC.TXT"], "文件名已变大写")


# --------------------------------------------------------------------------- #
# C. 跨盘符 / 公共根
# --------------------------------------------------------------------------- #

def test_cross_drive():
    case("C1 跨盘符文件夹参与计划")
    # 纯字符串构造：不需要真的存在 D 盘（relpath 只做盘符比较）
    a = Item("Q:\\probe\\A", "A", True)
    b = Item("Z:\\probe\\B", "B", True)
    f = Item("Q:\\probe\\A\\x.txt", "x.txt", False)
    for it in (a, b, f):
        it.new_name = it.name + "_n"
    try:
        P.finalize(P.Plan(items=[a, b, f]))
        raised = ""
    except ValueError as exc:
        raised = str(exc)
    defect("『跨盘符拖入』（文档声明支持）里有文件夹被改名时，整批预览直接抛 ValueError",
           "mount" in raised, "finalize 抛出：%s" % (raised or "无异常"))
    # 只有文件（无文件夹改名）时反而没事 —— 说明缺陷只在 dir_map 非空时触发
    only_files = [Item("Q:\\probe\\a.txt", "a.txt", False),
                  Item("Z:\\probe\\b.txt", "b.txt", False)]
    for it in only_files:
        it.new_name = "x_" + it.name
    try:
        P.finalize(P.Plan(items=only_files))
        crashed = ""
    except Exception as exc:                        # noqa: BLE001
        crashed = "%s: %s" % (type(exc).__name__, exc)
    eq(crashed, "", "纯文件的跨盘批次不抛异常（只是目标目录不存在，判无效而已）")

    case("C2 公共根计算对跨盘不抛异常")
    eq(P._common_root([]), "", "空列表返回空串")
    check(P._common_root([Item("Q:\\a\\x", "x", False)]) != "", "单项有根")


# --------------------------------------------------------------------------- #
# D. 撤销与日志
# --------------------------------------------------------------------------- #

def test_undo_and_logs():
    case("D1 完整往返：落盘 → 撤销")
    box = fresh("d1")
    for n in ("一 二.txt", "b.app.txt", "c.TXT"):
        touch(os.path.join(box, n))
    rules = [R.make_rule("template", "{n}-{name}")]
    res = ops.scan_and_plan(box, rules, ScanOptions(root=box),
                            P.PlanOptions(width=2, dry_run=False))
    out = ops.apply(res.items, rules, P.PlanOptions(width=2, dry_run=False), root=box)
    check(out.ok, "落盘成功")
    check(bool(out.log_path) and os.path.isfile(out.log_path), "写了撤销日志")
    after = sorted(os.listdir(box))
    check(all(n.startswith(("01", "02", "03")) for n in after), "新名带序号：%s" % after)
    back = ops.undo(out.log_path)
    check(back.ok, "撤销成功")
    eq(sorted(os.listdir(box)), ["b.app.txt", "c.TXT", "一 二.txt"], "原名全部还原")
    check(not os.path.exists(out.log_path), "成功后日志被删除")

    case("D2 撤销：缺文件不算失败，冲突才留日志")
    box = fresh("d2")
    a = touch(os.path.join(box, "a.txt"))
    a_new = os.path.join(box, "a_new.txt")
    os.rename(a, a_new)
    log = os.path.join(SANDBOX, "d2", "half.json")
    with open(log, "w", encoding="utf-8") as fh:
        json.dump({"pairs": [{"old": a, "new": a_new},
                             {"old": os.path.join(box, "b.txt"),
                              "new": os.path.join(box, "b_new.txt")}]}, fh)
    ok1, errs1 = C.undo(log)
    check(ok1 and not errs1, "原文件已不存在的那条直接跳过，不算失败")
    check(os.path.exists(a), "能还原的已经还原")
    check(not os.path.exists(log), "该做的都做完了 → 日志清理掉")

    # 真正会卡住的是"目标名被占住"：不覆盖，留日志只重试这一条
    box = fresh("d2b")
    a = touch(os.path.join(box, "a.txt"))
    a_new = os.path.join(box, "a_new.txt")
    os.rename(a, a_new)
    b = os.path.join(box, "b.txt")
    b_new = touch(os.path.join(box, "b_new.txt"))
    touch(b)                                    # b 这个名字已被别的文件占住
    log = os.path.join(SANDBOX, "d2b", "half.json")
    with open(log, "w", encoding="utf-8") as fh:
        json.dump({"pairs": [{"old": a, "new": a_new},
                             {"old": b, "new": b_new}]}, fh)
    ok1, errs1 = C.undo(log)
    check(not ok1 and len(errs1) == 1, "冲突的那条报错，另一条照常还原")
    check(os.path.exists(a), "没冲突的那条已经还原")
    left = C.log_pairs(C.load_log(log)) if os.path.exists(log) else []
    eq(len(left), 1, "日志里只剩没还原成功的那一条")
    ok2, errs2 = C.undo(log)
    defect("撤销部分成功后再撤销，错误数会越滚越多、日志永远删不掉",
           len(errs2) > len(errs1),
           "第二次撤销报 %d 条错误（第一次 %d 条）" % (len(errs2), len(errs1)))
    check(len(errs2) <= 1, "重复撤销只重试剩下的那条：%d 条错误" % len(errs2))

    case("D3 日志目录不可写")
    box = fresh("d3")
    touch(os.path.join(box, "a.txt"))
    blocker = os.path.join(SANDBOX, "d3_blocked")
    if os.path.isdir(blocker):
        shutil.rmtree(blocker, ignore_errors=True)
    with open(blocker, "w", encoding="utf-8") as fh:
        fh.write("我是文件不是目录")
    old = os.environ.get("NAMECAT_LOG_DIR")
    os.environ["NAMECAT_LOG_DIR"] = blocker
    try:
        rules = [R.make_rule("prefix", "x_")]
        plan = P.build(items_of(box, ["a.txt"]), rules, P.PlanOptions())
        res = ops.apply(plan.items, rules, P.PlanOptions(dry_run=False), root=box)
    finally:
        if old is None:
            os.environ.pop("NAMECAT_LOG_DIR", None)
        else:
            os.environ["NAMECAT_LOG_DIR"] = old
    defect("撤销日志写不进去时仍然报告「已重命名 N 项」成功，用户无从知道这次不可撤销",
           res.ok and not res.log_path and "无法撤销" not in res.message,
           "message=%r log_path=%r" % (res.message, res.log_path))
    check("无法撤销" in res.message, "文案里说明了本次不可撤销：%r" % res.message)

    case("D4 日志文件自身损坏")
    box = fresh("d4")
    logs = os.path.join(SANDBOX, "d4_logs")
    if os.path.isdir(logs):
        shutil.rmtree(logs, ignore_errors=True)
    os.makedirs(logs, exist_ok=True)
    good = {"app": APP_NAME, "time": "2026-11-01 10:00:00", "root": box,
            "pairs": [{"old": "a", "new": "b"}]}
    with open(os.path.join(logs, "rename_aaaaaaaa_20261101_100000_abcdef.json"),
              "w", encoding="utf-8") as fh:
        json.dump(good, fh)
    with open(os.path.join(logs, "rename_bbbbbbbb_20261101_110000_abcdef.json"),
              "w", encoding="utf-8") as fh:
        json.dump({"pairs": [1, 2]}, fh)
    old = os.environ.get("NAMECAT_LOG_DIR")
    os.environ["NAMECAT_LOG_DIR"] = logs
    try:
        from adapters import engine_adapter as A
        try:
            rows = A.logs("")
            crashed = ""
        except Exception as exc:                     # noqa: BLE001
            rows, crashed = [], "%s: %s" % (type(exc).__name__, exc)
    finally:
        if old is None:
            os.environ.pop("NAMECAT_LOG_DIR", None)
        else:
            os.environ["NAMECAT_LOG_DIR"] = old
    defect("一份日志的 pairs 结构异常就让整个「执行记录」列表读取失败（好日志也读不出来）",
           bool(crashed), crashed or "读取正常，返回 %d 行" % len(rows))

    case("D5 命令行撤销不存在的日志")
    code, out = cli("undo", "--log", os.path.join(SANDBOX, "no_such_log.json"))
    defect("main.py undo --log <不存在> 直接抛 traceback，而不是一句人话",
           "Traceback" in out, "退出码 %d" % code)
    check(code != 0, "失败时退出码非 0")

    case("D6 命令行导出到不可写路径")
    box = fresh("d6")
    touch(os.path.join(box, "a.txt"))
    code, out = cli("preview", box, "--rule", "template={n}",
                    "--csv", os.path.join(SANDBOX, "no_dir", "x.csv"))
    defect("--csv 指向不可写路径时抛 traceback",
           "Traceback" in out, "退出码 %d" % code)

    case("D7 日志轮转按目录分组")
    logs = os.path.join(SANDBOX, "d7_logs")
    shutil.rmtree(logs, ignore_errors=True)
    os.makedirs(logs)
    old = os.environ.get("NAMECAT_LOG_DIR")
    os.environ["NAMECAT_LOG_DIR"] = logs
    try:
        for root in ("C:\\probe\\one", "C:\\probe\\two"):
            for i in range(C.MAX_LOGS + 3):
                C.save_log(root, [("a", "b")])
        one = [n for n in os.listdir(logs)
               if C._split_log_name(n)[0] == C._root_hash("C:\\probe\\one")]
        two = [n for n in os.listdir(logs)
               if C._split_log_name(n)[0] == C._root_hash("C:\\probe\\two")]
        eq(len(one), C.MAX_LOGS, "目录 one 只留 %d 份" % C.MAX_LOGS)
        eq(len(two), C.MAX_LOGS, "目录 two 也只留 %d 份（互不挤占）" % C.MAX_LOGS)
        eq(len(C.list_logs("C:\\probe\\one")), C.MAX_LOGS, "按目录列出")
        check(len(C.list_logs("")) >= C.MAX_LOGS, "不带目录时列出全部")
    finally:
        if old is None:
            os.environ.pop("NAMECAT_LOG_DIR", None)
        else:
            os.environ["NAMECAT_LOG_DIR"] = old


# --------------------------------------------------------------------------- #
# E. 扫描
# --------------------------------------------------------------------------- #

def test_scan():
    case("E1 过滤组合")
    box = fresh("e1")
    for n in ("IMG_001.jpg", "IMG_002.png", "note.txt", ".hidden.txt", "skip_me.jpg"):
        touch(os.path.join(box, n))
    eq(len(scan(ScanOptions(root=box, kinds=("file",))).items), 4, "隐藏项默认排除")
    eq(len(scan(ScanOptions(root=box, kinds=("file",), include_hidden=True)).items), 5,
       "勾选后包含隐藏项")
    eq([i.name for i in scan(ScanOptions(root=box, kinds=("image",))).items],
       ["IMG_001.jpg", "IMG_002.png", "skip_me.jpg"], "只收图片")
    eq([i.name for i in scan(ScanOptions(root=box, kinds=("file",),
                                        include_exts={".txt"})).items],
       ["note.txt"], "按扩展名白名单")
    eq([i.name for i in scan(ScanOptions(root=box, kinds=("file",),
                                        exclude_exts={".txt"})).items],
       ["IMG_001.jpg", "IMG_002.png", "skip_me.jpg"], "按扩展名黑名单")
    eq([i.name for i in scan(ScanOptions(root=box, kinds=("file",),
                                        only_match="IMG")).items],
       ["IMG_001.jpg", "IMG_002.png"], "名字包含过滤")
    eq([i.name for i in scan(ScanOptions(root=box, kinds=("file",),
                                        exclude_match="skip")).items],
       ["IMG_001.jpg", "IMG_002.png", "note.txt"], "名字排除过滤")

    case("E2 排序稳定且自然序")
    box = fresh("e2")
    for n in ("img10.jpg", "img2.jpg", "img1.jpg"):
        touch(os.path.join(box, n))
    eq([i.name for i in scan(ScanOptions(root=box, kinds=("file",))).items],
       ["img1.jpg", "img2.jpg", "img10.jpg"], "自然序（2 在 10 前）")
    eq([i.name for i in scan(ScanOptions(root=box, kinds=("file",),
                                        sort_mode="name_desc")).items],
       ["img10.jpg", "img2.jpg", "img1.jpg"], "倒序")
    eq(len(scan(ScanOptions(root=box, kinds=("file",), sort_mode="size_asc")).items), 3,
       "按大小排序不崩")

    case("E3 递归深度")
    box = fresh("e3")
    deep = box
    for i in range(6):
        deep = os.path.join(deep, "d%d" % i)
        touch(os.path.join(deep, "leaf%d.txt" % i))
    eq(len(scan(ScanOptions(root=box, kinds=("file",), recursive=False)).items), 0,
       "不递归时根目录没有文件")
    eq(len(scan(ScanOptions(root=box, kinds=("file",), recursive=True,
                            max_depth=3)).items), 3, "深度 3 只到第 3 层")
    eq(len(scan(ScanOptions(root=box, kinds=("file",), recursive=True,
                            max_depth=12)).items), 6, "深度 12 全收")

    case("E4 选中项语义")
    box = fresh("e4")
    touch(os.path.join(box, "keep.txt"))
    touch(os.path.join(box, "other.txt"))
    sub = os.path.join(box, "sub")
    touch(os.path.join(sub, "inner.txt"))
    sel = {nkey(os.path.join(box, "keep.txt"))}
    eq([i.name for i in scan(ScanOptions(root=box, kinds=("file",), selected=sel)).items],
       ["keep.txt"], "只留被选中的文件")
    sel = {nkey(sub)}
    eq([i.name for i in scan(ScanOptions(root=box, kinds=("file",), selected=sel)).items],
       ["inner.txt"], "选中的文件夹代表它里面的内容")
    # 关掉递归时，位于子目录的选中项仍必须被下探到
    eq([i.name for i in scan(ScanOptions(root=box, kinds=("file",), recursive=False,
                                        selected={nkey(os.path.join(sub, "inner.txt"))})).items],
       ["inner.txt"], "不递归也要够到子目录里的选中项")
    # 传原始绝对路径（不做 nkey 归一化）也必须命中：字段文档写的就是"绝对路径"
    eq([i.name for i in scan(ScanOptions(root=box, kinds=("file",),
                                        selected={os.path.join(box, "KEEP.TXT")})).items],
       ["keep.txt"], "原始路径（大小写不同）也能命中选中项")
    # 有选中项落在 root 之外时默认放宽成「root 全部 + 这些」
    outside = fresh("e4_out")
    touch(os.path.join(outside, "x.txt"))
    widened = scan(ScanOptions(root=box, kinds=("file",),
                              selected={os.path.join(outside, "x.txt")}))
    eq(len(widened.items), 3, "默认：root 里全部 + 外面那一项")
    strict = scan(ScanOptions(root=box, kinds=("file",), strict_selected=True,
                              selected={os.path.join(outside, "x.txt")}))
    eq(len(strict.items), 1, "严格模式：只要选中的那些（跨盘多选用）")

    case("E5 文件头校验")
    box = fresh("e5")
    touch(os.path.join(box, "real.png"), b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    touch(os.path.join(box, "fake.png"), b"not a png at all")
    touch(os.path.join(box, "empty.jpg"), b"")
    touch(os.path.join(box, "unknown.xyz"), b"whatever")
    got = [i.name for i in scan(ScanOptions(root=box, kinds=("file",),
                                           verify_content=True)).items]
    eq(got, ["real.png", "unknown.xyz"], "只放行真图片与未收录扩展名")
    eq(scan(ScanOptions(root=box, kinds=("file",), verify_content=True)).skipped, 2,
       "跳过的数量可统计（假图片 + 空文件）")


# --------------------------------------------------------------------------- #
# F. 编号 / 选择器 / 模板
# --------------------------------------------------------------------------- #

def test_numbering_and_template():
    case("F1 四种编号范围")
    box = fresh("f1")
    for d in ("d1", "d2"):
        os.makedirs(os.path.join(box, d), exist_ok=True)
        for i in range(1, 4):
            touch(os.path.join(box, d, "f%d.txt" % i))
    res = scan(ScanOptions(root=box, kinds=("file",), recursive=True))
    rules = [R.make_rule("template", "{n}")]
    got = {}
    for scope in P.SCOPES:
        pl = P.build(list(res.items), rules, P.PlanOptions(scope=scope, width=2))
        got[scope] = [it.num for it in pl.items]
    eq(sorted(got["global"]), [1, 2, 3, 4, 5, 6], "整批连续")
    eq(sorted(got["folder"]), [1, 1, 2, 2, 3, 3], "每文件夹重新开始")
    eq(sorted(got["kind"]), [1, 2, 3, 4, 5, 6], "全是文件 → 与整批一致")
    eq(sorted(got["folder_kind"]), [1, 1, 2, 2, 3, 3], "文件夹+类型")

    case("F2 倒序编号")
    pl = P.build(list(res.items), rules, P.PlanOptions(reverse=True, width=2))
    eq([it.num for it in pl.items][:3], [6, 5, 4], "倒序从尾部开始发号")

    case("F3 步长为 0")
    pl = P.build(list(res.items), rules, P.PlanOptions(step=0, width=2))
    names = [it.new_name for it in pl.items]
    defect("--step 0 不回落成 1，而是一批同名（全被判冲突、什么都改不了）",
           len(set(names)) == 1, "六个对象全叫 %r" % names[0])
    eq(len(set(names)), len(names), "步长按 1 处理，号码互不相同")
    check(any("步长" in w for w in pl.warnings), "提示里说明步长被按 1 处理")
    eq(pl.report.ok, 6, "六项都可改")

    case("F4 选择器（作用范围）")
    ctx = NameContext(os.path.join(box, "show-2026-10-19.mp4"),
                      "show-2026-10-19.mp4", False, 1, 1, 1, 0)

    def sel_rule(kind, a, sel, sep, occ=1):
        rule = R.make_rule(kind, a)
        rule.selector, rule.charset, rule.occurrence = sel, sep, occ
        return rule

    text = "show-2026-10-19"
    eq(R.apply_rule(sel_rule("prefix", "★", "after", "-"), text, 1, 0, ctx),
       "show-★2026-10-19", "after：只改分隔符之后的那一段")
    eq(R.apply_rule(sel_rule("prefix", "★", "before", "-"), text, 1, 0, ctx),
       "★show-2026-10-19", "before：只改分隔符之前的那一段")
    eq(R.apply_rule(sel_rule("prefix", "★", "from", "-"), text, 1, 0, ctx),
       "show★-2026-10-19", "from：从分隔符起（含）都算作用段")
    eq(R.apply_rule(sel_rule("prefix", "★", "range", "2-4"), text, 1, 0, ctx),
       "s★how-2026-10-19", "range：只改字符区间")
    eq(R.apply_rule(sel_rule("prefix", "★", "after", "-", -1), text, 1, 0, ctx),
       "show-2026-10-★19", "occurrence=-1 用最后一次出现")
    check(T.select("show", "range", "2-4") == "how", "range 2-4 取第 2~4 个字符")

    # until（「到某字符止（含）」）：选中头部，拼回去时却把原头部又接了一遍
    got = R.apply_rule(sel_rule("case", "upper", "until", "-"), text, 1, 0, ctx)
    defect("作用范围选「到某字符止（含）」（until）时，改名结果是原头部 + 新头部，尾部被整段丢掉",
           got != "SHOW-2026-10-19",
           "case=upper;sel=until;sep=- 作用于 %r 得到 %r（应为 'SHOW-2026-10-19'；"
           "根因是 splice 用了 after 的公式 text[:i+1]+new，应为 new+text[i+1:]）"
           % (text, got))

    case("F5 分隔符找不到时原样返回")
    rule = R.make_rule("keep_after", "#")
    rule.selector, rule.charset = "after", "#"
    eq(R.apply_rule(rule, "no-separator", 1, 0, ctx), "no-separator", "找不到就原样返回")

    case("F6 模板")
    eq(T.render("{n}", ctx, num=7, width=4), "0007", "{n} 按传入宽度补零")
    eq(T.render("{n:03}", ctx, num=7), "007", "{n:03} 自己指定宽度")
    eq(T.render("{name}_{parent}", ctx),
       "show-2026-10-19_" + os.path.basename(os.path.dirname(ctx.path)),
       "name / parent 变量")
    eq(T.render("{fullname}", ctx), "show-2026-10-19.mp4", "fullname 带扩展名")
    eq(T.render("{extdot}|{ext}", ctx), ".mp4|mp4", "extdot 带点、ext 不带")
    eq(T.render("{index}/{total}", ctx), "1/1", "index / total")
    eq(T.render("{nope}", ctx), "", "未知变量渲染成空串")
    eq(T.render("a#b", ctx), "a#b", "正文里的 # 不被当计数器")
    eq(T.render("#", ctx, num=5), "5", "整个模板只有一个 # 时当计数器")
    eq(T.render("$Counter(5,2,3)", ctx, num=3), "009", "$Counter(起,步,宽)")
    eq(T.render("<file.name>", ctx), "show-2026-10-19", "ReNamer 的 <file.name>")
    eq(T.render("plain text", ctx), "plain text", "不含变量时原样返回")
    eq(T.render("{name", ctx), "{name", "未闭合的花括号原样保留")
    eq(T.parse_range("5-2", 6), (2, 5), "区间写反要交换")
    eq(T.parse_range("0", 6), (1, 6), "0 当整段")
    eq(T.cut_index("a-b-c", "-", -2), 1, "倒数第二次出现")
    eq(T.cut_index("a-b-c", "-", 9), -1, "超出次数返回 -1")

    case("F6b 转义展开")
    eq(T.render(r"\x41", ctx), "A", "\\xNN 展开成字符")
    eq(T.render(r"\x{1F600}", ctx), "\U0001F600", "\\x{...} 展开成字符")
    eq(T.render("无转义 ABC", ctx), "无转义 ABC", "普通文本原样返回")
    eq(T.render(r"\x41{n}", ctx, num=2), "A2", "转义之后再算变量")

    case("F7 模板产出的扩展名处理")
    box = fresh("f7")
    touch(os.path.join(box, "a.tar.gz"))
    pl = build(box, ["a.tar.gz"], [R.make_rule("template", "{n}")])
    eq(pl.items[0].new_name, "1.gz", "没写扩展名变量时找回原扩展名")
    pl = build(box, ["a.tar.gz"], [R.make_rule("template", "{n}{extdot}")])
    eq(pl.items[0].new_name, "1.gz", "写了扩展名变量时按模板走")

    case("F8 扩展名规则")
    box = fresh("f8")
    touch(os.path.join(box, "a.TXT"))
    eq(build(box, ["a.TXT"], [R.make_rule("extension", "", "lower")]).items[0].new_name,
       "a.txt", "扩展名转小写")
    eq(build(box, ["a.TXT"], [R.make_rule("extension", ".md")]).items[0].new_name,
       "a.md", "换扩展名")
    eq(build(box, ["a.TXT"], [R.make_rule("extension", "", "remove")]).items[0].new_name,
       "a", "去掉扩展名")

    case("F9 序列化（renumber）")
    box = fresh("f9")
    for n in ("x.mp4", "y.mkv", "z.mov"):
        touch(os.path.join(box, n))
    rules = [R.make_rule("renumber", "1", "4")]
    res = scan(ScanOptions(root=box, kinds=("file",)))
    pl = P.build(list(res.items), rules, P.PlanOptions())
    eq([it.new_name for it in pl.items], ["0001.mp4", "0002.mkv", "0003.mov"],
       "整名换号码、扩展名保留")


# --------------------------------------------------------------------------- #
# G. 规则归一化 / 脏输入
# --------------------------------------------------------------------------- #

def test_normalize():
    case("G1 别名与缺省")
    for alias, kind in (("serialize", "number"), ("upper", "case"),
                        ("clean", "filename_clean"), ("s2t", "zhconv"),
                        ("regexp", "regex"), ("ai", "llm")):
        rule = R.parse_cli_rule("%s=x" % alias)
        eq(rule.kind, kind, "别名 %s → %s" % (alias, kind))
    eq(R.parse_cli_rule("upper").a, "upper", "upper 别名带上 case 参数")
    eq(R.parse_cli_rule("t2s").a, "t2s", "t2s 别名带上方向")
    eq(R.parse_cli_rule("remove-after=-;occ=-1").occurrence, -1, "分号后缀解析 occurrence")
    eq(R.parse_cli_rule("case=upper;sel=after;sep=._").charset, "._", "分号后缀解析 sep")
    try:
        R.parse_cli_rule("不存在的规则=1")
        check(False, "未知规则应该抛 ValueError")
    except ValueError:
        check(True, "未知规则抛 ValueError")

    case("G2 脏 JSON")
    got = R.rules_from_json([{"kind": "llm", "extras": None}, {"kind": None},
                             {"extras": "x"}, 3, None,
                             {"kind": "number", "occurrence": "2"},
                             {"kind": "replace", "a": 5}])
    eq(len(got), 5, "非字典项被丢弃，其余照常读回")
    eq(got[0].kind, "llm", "llm 规则读回")
    eq(got[3].occurrence, 2, "occurrence 字符串转数字")

    case("G3 程序化构造的脏 Rule")
    try:
        r1 = R.normalize(R.Rule(kind="llm", extras=None))
        bad1 = ""
    except Exception as exc:                        # noqa: BLE001
        r1, bad1 = None, "%s: %s" % (type(exc).__name__, exc)
    defect("normalize(Rule(extras=None)) 崩 AttributeError",
           bool(bad1), bad1 or "已能容忍")
    eq(bad1, "", "extras=None 不再抛异常")
    check(isinstance(getattr(r1, "extras", None), dict), "extras 被补成字典")
    eq(getattr(r1, "extras", {}).get("lang"), "zh", "llm 缺省语言补上")
    try:
        r2 = R.normalize(R.Rule(selector=3))
        bad2 = ""
    except Exception as exc:                        # noqa: BLE001
        r2, bad2 = None, "%s: %s" % (type(exc).__name__, exc)
    defect("normalize(Rule(selector=3)) 崩 AttributeError",
           bool(bad2), bad2 or "已能容忍")
    eq(bad2, "", "selector 非字符串不再抛异常")
    eq(getattr(r2, "selector", ""), "text", "认不出来的 selector 回落 text")

    case("G3b 未知规则类型留痕")
    bad_rule = R.rules_from_json([{"kind": "只有新版本才有的规则", "a": "x"}])[0]
    eq(bad_rule.kind, "replace", "未知 kind 按文本替换兜底")
    eq(bad_rule.extras.get("unknown_kind"), "只有新版本才有的规则", "原名记在 extras 里")
    check("未知规则" in bad_rule.describe(), "描述里标出未知：%s" % bad_rule.describe())
    from adapters import engine_adapter as A
    check(any("未知规则" in p for p in A.rule_problems(bad_rule)),
          "界面参数校验里会提示：%s" % A.rule_problems(bad_rule))
    # 宽度上限也要在界面侧拦得住
    wide = A.new_rule("number")
    A.set_field(wide, "b", "500")
    check(any("最多" in p for p in A.rule_problems(wide)), "补零宽度超上限被拦下")

    case("G4 预设往返")
    box = fresh("g4")
    preset = PS.Preset(name="边界/预设:名字", rules=[
        R.make_rule("template", "{n}-{name}"),
        R.make_rule("number", "5", extras={"step": "3", "scope": "folder"}),
    ], scan={"recursive": True, "max_depth": 5}, plan={"width": 3, "scope": "folder"},
        note="往返测试")
    path = PS.save(preset, os.path.join(box, "p.json"))
    back = PS.load(path)
    eq([r.to_dict() for r in back.rules], [r.to_dict() for r in preset.rules],
       "规则原样往返")
    eq(back.scan_options().max_depth, 5, "扫描选项读回")
    eq(back.plan_options().width, 3, "计划选项读回")
    # 文件名里的非法字符被替换，不会写到一个不存在的路径去
    eq(os.path.basename(PS.save(preset)), "边界_预设_名字.json", "预设文件名被净化")
    os.remove(os.path.join(PS.preset_dir(), "边界_预设_名字.json"))

    case("G5 预设脏数据")
    with open(os.path.join(box, "dirty.json"), "w", encoding="utf-8") as fh:
        json.dump({"name": 3, "rules": "不是数组", "scan": 3, "plan": [1, 2],
                   "note": None}, fh)
    dirty = PS.load(os.path.join(box, "dirty.json"))
    eq(dirty.name, "3", "名字转成文本")
    eq(dirty.rules, [], "非数组规则当空")
    eq(dirty.plan_options().width, 0, "脏 plan 回落默认值")


# --------------------------------------------------------------------------- #
# H. LLM 层（离线，不联网）
# --------------------------------------------------------------------------- #

def test_llm_offline():
    case("H1 没有 Key 时规则原样放行")
    cfg = L.LLMConfig(provider="openai", api_key="", base_url="https://x", model="m")
    client = L.make_client(cfg)
    check(not client.available(), "无 Key 判不可用")
    box = fresh("h1")
    touch(os.path.join(box, "a.txt"))
    pl = build(box, ["a.txt"], [R.make_rule("llm")])
    eq(pl.items[0].status, "same", "LLM 不可用时名字不变")
    check(any("LLM" in w for w in pl.warnings), "提示里说明了原因：%s" % pl.warnings)

    case("H2 模型输出净化")
    eq(L.sanitize_name('  "海边 日落"  '), "海边 日落", "去引号与空格")
    eq(L.sanitize_name("a/b:c*d"), "a_b_c_d", "非法字符替换")
    eq(L.sanitize_name("第一行\n第二行"), "第一行", "只取首行")
    eq(L.sanitize_name("x" * 40, 6), "xxxxxx", "按长度截断")
    eq(L.sanitize_name("con"), "_con", "保留名加前缀")
    eq(L.sanitize_name("海边 日落", 24, "kebab"), "海边-日落", "kebab 风格")
    eq(L.sanitize_name("海边 日落", 24, "snake"), "海边_日落", "snake 风格")
    eq(L.sanitize_name(""), "", "空输入返回空")

    case("H3 解析模型回复的各种形状")
    parse = L.LLMClient.parse_suggestions
    eq(parse('[{"i":1,"n":"a"},{"i":2,"n":"b"}]', 2), {1: "a", 2: "b"}, "标准 JSON")
    eq(parse('```json\n[{"i":1,"n":"a"}]\n```', 1), {1: "a"}, "带 ``` 包裹")
    eq(parse('结果如下：\n[{"i":1,"n":"a"}]', 1), {1: "a"}, "夹杂解释文字")
    eq(parse("1. 甲\n2. 乙", 2), {1: "甲", 2: "乙"}, "逐行编号")
    eq(parse('{"1":"甲"}', 1), {1: "甲"}, "字典形状")
    eq(parse("", 1), None, "空回复返回 None")
    eq(parse("完全看不懂的回答", 1), {1: "完全看不懂的回答"}, "退化按行取")

    case("H4 Echo 替身")
    echo = L.EchoClient()
    check(echo.available(), "离线替身永远可用")
    eq(echo.simplify("DSC_04321_IMG_20230815_海滩日落_副本.jpg"),
       "海滩日落", "机械精简掉相机前缀/日期/副本")
    eq(echo.simplify("Screenshot_2023-08-15_at_10.30.45"), "", "全是垃圾词时返回空")

    case("H5 回环地址不走系统代理")
    from namecat_py import provider as PRV
    check(PRV.opener_for("http://127.0.0.1:11434/v1/models") is PRV._LOOPBACK_OPENER,
          "127.0.0.1 用「无代理」opener")
    check(PRV.opener_for("http://localhost:8080/") is PRV._LOOPBACK_OPENER,
          "localhost 同样绕过代理")
    check(PRV.opener_for("https://api.deepseek.com") is PRV._DEFAULT_OPENER,
          "外网仍然走系统代理")
    # 连不上时给出的是连接错误，而不是代理的 502
    cfg = PRV.ProviderConfig()
    cfg.base_url = "http://127.0.0.1:1/v1"
    cfg.api_key = "k"
    cfg.model = "m"
    res = PRV.test_connection(cfg)
    check(not res["ok"], "端口没人监听 → 判定失败")
    check("连不上服务器" in res["message"],
          "原因是连接失败（不是代理报的 502）：%r" % res["message"][:40])


# --------------------------------------------------------------------------- #
# I. 命令行
# --------------------------------------------------------------------------- #

def test_cli():
    case("I1 退出码")
    box = fresh("i1")
    touch(os.path.join(box, "a.txt"), b"x")
    eq(cli("rules")[0], 0, "rules 退出 0")
    eq(cli("presets")[0], 0, "presets 退出 0")
    eq(cli("preview", box, "--rule", "template={n}")[0], 0, "预览退出 0")
    eq(cli("preview", box)[0], 2, "没有规则退出 2")
    eq(cli("rename", os.path.join(box, "不存在"))[0], 2, "目录不存在退出 2")
    eq(cli("undo", box)[0], 1, "没有日志退出 1")

    case("I2 预览不改动文件")
    code, out = cli("preview", box, "--rule", "prefix=z_")
    eq(sorted(os.listdir(box)), ["a.txt"], "预览不落盘")
    check("→" in out or "->" in out, "预览里给出了新旧对照")

    case("I3 落盘 + 撤销往返")
    code, out = cli("rename", box, "--rule", "prefix=z_", "--apply", "-q")
    eq(code, 0, "落盘退出 0")
    eq(sorted(os.listdir(box)), ["z_a.txt"], "已改名")
    code, out = cli("undo", box, "-q")
    eq(code, 0, "撤销退出 0")
    eq(sorted(os.listdir(box)), ["a.txt"], "已还原")
    eq(cli("undo", box, "-q")[0], 1, "再撤销没有日志 → 退出 1")

    case("I4 非法规则")
    code, out = cli("preview", box, "--rule", "根本没有这个规则=1")
    eq(code, 1, "非法规则退出 1")
    check("规则解析失败" in out, "给出人话错误")


# --------------------------------------------------------------------------- #
# J. 界面（离屏；没有 Qt 就跳过）
# --------------------------------------------------------------------------- #

def test_ui():
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        from adapters import engine_adapter as A
        from ui import base
        base.FAMILY = base.load_fonts()
        from ui.pages.workspace import WorkspacePage
    except Exception as exc:                        # noqa: BLE001
        print("   （跳过界面用例：%s）" % exc)
        return

    case("J1 应用预设是否保住当前目录")
    box = fresh("j1")
    touch(os.path.join(box, "a.txt"))
    ws = WorkspacePage()
    ws.state.root = box
    ws.path.setText(box)
    ws.rules = [A.new_rule("prefix")]
    name, preset = A.builtin_presets()[0]
    ws.apply_preset(preset)
    defect("应用预设会顺手清空当前目录与已扫结果，用户得重新选一次目录",
           ws.state.root == "" and ws.path.text() == "",
           "应用「%s」后 root=%r" % (name, ws.state.root))
    app.processEvents()

    case("J2 规则参数校验")
    bad = A.new_rule("regex")
    A.set_field(bad, "a", "([")
    check(any("正则" in p for p in A.rule_problems(bad)), "非法正则被拦下")
    empty = A.new_rule("delete")
    check(any("不能为空" in p for p in A.rule_problems(empty)), "空参数被拦下")
    good = A.new_rule("template")
    eq(A.rule_problems(good), [], "默认模板没有告警")
    check(A.rule_warnings(good), "模板没写扩展名变量时给提醒")

    case("J3 LLM 页切页是否丢输入")
    from ui.pages.llm_settings import LlmPage
    page = LlmPage()
    page.reload(False, force=True)
    page.key_edit.setText("sk-typed-but-not-saved")
    page._on_key_edited()
    page.reload(False)                              # 相当于切走再切回
    defect("LLM 页切页/刷新会把刚输入还没保存的密钥静默清空",
           page.key_edit.text() == "" and page._key_input == "",
           "刷新后输入框=%r" % page.key_edit.text())
    eq(page.key_edit.text(), "sk-typed-but-not-saved", "未保存的输入被保住")
    page.reload(False, force=True)                  # 显式强制才覆盖
    eq(page.key_edit.text(), "", "force=True 时才清空")
    app.processEvents()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main():
    if os.path.isdir(SANDBOX):
        shutil.rmtree(SANDBOX, ignore_errors=True)
    os.makedirs(SANDBOX, exist_ok=True)
    os.environ["NAMECAT_LOG_DIR"] = os.path.join(SANDBOX, "_logs")
    t0 = time.time()
    for fn in (test_empty_and_oversize, test_rollback, test_cross_drive,
               test_undo_and_logs, test_scan, test_numbering_and_template,
               test_normalize, test_llm_offline, test_cli, test_ui):
        print("== %s ==" % fn.__name__)
        try:
            fn()
        except Exception:                            # noqa: BLE001
            import traceback
            FAILS.append("%s 整组抛异常" % fn.__name__)
            print("   FAIL 整组抛异常：\n%s" % traceback.format_exc())
    cost = time.time() - t0

    alive = [d for d in DEFECTS if d[1]]
    print()
    print("断言：通过 %d / 失败 %d   缺陷：仍存在 %d / 已修复 %d   耗时 %.2fs"
          % (PASS, FAIL, len(alive), len(DEFECTS) - len(alive), cost))
    if alive:
        print()
        print("仍存在的缺陷：")
        for name, _r, detail in alive:
            print("  · %s%s" % (name, ("（%s）" % detail) if detail else ""))
    if FAILS:
        print()
        print("失败清单：")
        for line in FAILS:
            print("  - " + line)
    shutil.rmtree(SANDBOX, ignore_errors=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
