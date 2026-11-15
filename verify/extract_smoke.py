# -*- coding: utf-8 -*-
"""verify/extract_smoke.py —— 文件提取引擎的 Python 端到端冒烟。

通过 PyO3 绑定真调 namecat_py.extract：扫描 → 计划 → 模拟 → 执行 → 撤销 → CSV。
验证固定日志目录、GIL 释放下的进度回调、以及新旧引擎互不干扰。
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "_vendor"), ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

import engine                                  # noqa: E402
from namecat_py import extract as X            # noqa: E402
from namecat_py import rules as R              # noqa: E402

FAILS = []


def check(cond, msg):
    if cond:
        print("  ok   %s" % msg)
    else:
        FAILS.append(msg)
        print("  FAIL %s" % msg)


def main():
    base = tempfile.mkdtemp(prefix="filecat_extract_smoke_")
    root = os.path.join(base, "data")
    program = os.path.join(base, "program")
    os.makedirs(os.path.join(root, "a", "b"))
    os.makedirs(program)
    with open(os.path.join(root, "a", "b", "deep.txt"), "w", encoding="utf-8") as f:
        f.write("DEEP")
    with open(os.path.join(root, "a", "mid.txt"), "w", encoding="utf-8") as f:
        f.write("MID")

    check(engine.APP_NAME == "FileCat", "程序名已改为 FileCat")
    pr = X.rollback_dir(program)
    check(pr.replace("\\", "/").endswith("Extract/Rollback_Log"), "固定日志目录布局：%s" % pr)
    check("Extract" in pr and "Rollback_Log" in pr, "目录含 Extract/Rollback_Log")
    check(X.parse_list(" .TMP, tmp ;BAK ", True) == [".tmp", ".bak"], "逗号/分号/大小写/空格解析")

    opts = {
        "root": root,
        "program_root": program,
        "mode": "flatten",
        "conflict": "rename",
        "max_depth": 0,
        "exclude_dirs": X.parse_list("", False),
        "exclude_exts": X.parse_list("", True),
        "clean_empty_dirs": True,
        "overwrite_confirmed": False,
    }

    progress = []
    plan = X.build_plan(opts, progress=lambda c, t, m: progress.append((c, t, m)))
    check(plan["scanned_count"] == 2, "扫描到 2 个文件")
    check(len(plan["items"]) == 2, "计划 2 项")
    check(all(it["target_dir"] == os.path.normpath(root) for it in plan["items"]),
          "一次到底：目标都在根目录")
    check(any(it["status"] == "ready" for it in plan["items"]), "状态为 ready")
    check(bool(progress), "进度回调在 GIL 释放下正常触发")
    stats = X.plan_stats(plan)
    check(stats["total"] == 2, "plan_stats 总数 2")

    # 模拟运行：文件系统零改动
    before = open(os.path.join(root, "a", "b", "deep.txt"), encoding="utf-8").read()
    sim = X.execute(opts, plan, True, "task_sim")
    check(sim["dry_run"] is True, "模拟运行标记")
    check(not sim["log_path"], "模拟运行不写日志")
    check(not os.path.exists(os.path.join(program, "Extract")), "模拟运行不建日志目录")
    check(open(os.path.join(root, "a", "b", "deep.txt"), encoding="utf-8").read() == before,
          "模拟运行不改文件")

    # 实际执行
    res = X.execute(opts, plan, False, "task_real")
    check(res["succeeded"] == 2, "实际执行成功 2 项：%s" % res["errors"])
    check(os.path.isfile(os.path.join(root, "deep.txt")), "深层文件已到根目录")
    check(os.path.isfile(os.path.join(root, "mid.txt")), "中层文件已到根目录")
    check(res["cleaned_dirs"] == 2, "清理了 2 个空目录")
    check(res["log_path"] and os.path.isfile(res["log_path"]), "生成撤销日志")
    check(res["log_path"].replace("\\", "/").find("Extract/Rollback_Log") >= 0,
          "日志落在固定目录")

    log = X.read_log(res["log_path"])
    check(log["schema"] == X.SCHEMA, "日志 Schema 版本")
    check(log["app"] == "FileCat", "日志 app 名")
    check(len(log["entries"]) == 2, "日志含 2 条成功记录")
    logs = X.list_logs(program)
    check(len(logs) == 1 and logs[0]["state"] == "active", "日志列表可读且状态 active")

    # CSV
    csv_path = os.path.join(base, "report.csv")
    X.export_csv(csv_path, res["items"])
    raw = open(csv_path, "rb").read()
    check(raw[:3] == b"\xef\xbb\xbf", "CSV 带 UTF-8 BOM")
    text = raw[3:].decode("utf-8")
    check("原始路径" in text and "目标路径" in text and "操作状态" in text,
          "CSV 含必需列")

    # 撤销
    undo = X.undo_latest(program)
    check(undo["succeeded"] == 2, "撤销成功 2 项：%s" % undo["errors"])
    check(os.path.isfile(os.path.join(root, "a", "b", "deep.txt")), "原路径已恢复")
    check(undo["restored_dirs"] == 2, "目录结构已恢复")
    logs2 = X.list_logs(program)
    check(logs2[0]["state"] == "undone", "日志标记已撤销")
    # 原引擎仍然可用（新旧隔离）
    check(len(R.RULE_KINDS) == 28, "原重命名引擎 28 条规则未受影响")

    shutil.rmtree(base, ignore_errors=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    rc = main()
    print()
    if FAILS:
        print("失败 %d 项：%s" % (len(FAILS), FAILS))
    else:
        print("全部通过（PyO3 接口 / 计划 / 模拟 / 执行 / 日志 / CSV / 撤销 / 新旧隔离）")
    raise SystemExit(rc)
