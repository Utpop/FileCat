# -*- coding: utf-8 -*-
"""引擎基准：量当前（Rust）引擎的各阶段耗时。

跑法：
    set PYTHONPATH=_vendor
    python verify/bench.py              # 2000 个文件，每阶段 7 轮取中位数
    python verify/bench.py -n 500       # 小规模
    python verify/bench.py --keep       # 保留造出来的文件，便于手工复现

为什么放在 `verify/` 而不是临时目录：
    这份脚本要能长期跑。引擎换实现、加规则、改校验策略之后，重新量一轮就知道
    退化在哪一段 —— 它不该是某次迁移用完就丢的脚手架。

口径：
    · 每个阶段先预热 1 轮再量 7 轮，取中位数（首轮要读文件系统缓存）；
    · 落盘 / 撤销只取 500 个文件、3 轮 —— Windows Defender 实时扫描是主导项，
      2000 个要跑几分钟，而它对"比例"没有影响；
    · 落盘前每轮重新复制一份源目录，避免上一轮改名后的名字影响下一轮；
    · `RIGHT_PANEL` 里是 1.6.3（Python 引擎）在同一台机器、同一批文件上量到的
      参考值，只用来给出倍率 —— 不是硬性断言，机器不同就会漂。
"""

from __future__ import annotations

import argparse
import os
import shutil
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (os.path.join(ROOT, "_vendor"), ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

BASE = os.path.join(ROOT, "_verify_bench")
SRC = os.path.join(BASE, "src")

# 1.6.3（Python 引擎）实测中位数，单位 ms。见 docs/TEST_REPORT.md 第 8 节。
PY_ENGINE = {"scan": 50.2, "names": 27.1, "plan": 80.1, "scan+plan": 136.9,
             "apply": 611.0, "undo": 1132.9}

# 名字要有代表性：相机前缀 + 中文 + 括号编号 + 副本后缀 + 混合扩展名。
# 全用 a1/a2 会让"规则链 + 校验"跑在最快路径上，量不出东西。
SEEDS = [
    ("DSC_%04d_海边日落_副本.jpg", ".jpg"),
    ("IMG_2024%04d_工作报告_v2.docx", ".docx"),
    ("扫描件_2024_03_15 (%d).pdf", ".pdf"),
    ("ScreenShot 2024-11-05 at 10.3%d.png", ".png"),
    ("VID_20240101_%04d 最终版.mp4", ".mp4"),
    ("笔记 %d 未命名.txt", ".txt"),
]
FOLDERS = 20


def build_tree(src: str, total: int) -> int:
    shutil.rmtree(src, ignore_errors=True)
    per = max(1, total // FOLDERS)
    made = 0
    for folder_index in range(FOLDERS):
        folder = os.path.join(src, "第%02d组" % (folder_index + 1))
        os.makedirs(folder, exist_ok=True)
        for index in range(per):
            pattern, ext = SEEDS[(folder_index + index) % len(SEEDS)]
            name = (pattern % (index + 1)) if "%" in pattern else pattern
            if not name.endswith(ext):
                name += ext
            with open(os.path.join(folder, name), "wb") as fh:
                fh.write(b"x" * 1024)
            made += 1
    # 少量子目录：走递归分支，和只扫一层不是一条路径
    for index in range(5):
        nested = os.path.join(src, "第%02d组" % (index + 1), "素材")
        os.makedirs(nested, exist_ok=True)
        with open(os.path.join(nested, "素材 %d.jpg" % index), "wb") as fh:
            fh.write(b"x")
        made += 1
    return made


def timeit(fn, rounds: int = 7, warmup: int = 1) -> dict:
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(rounds):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return {"median": statistics.median(samples), "min": min(samples)}


def rules_of(R):
    """规则链：去副本后缀 → 删分隔符 → 三位编号。"""
    return [
        R.make_rule("replace", "_副本", ""),
        R.make_rule("remove_chars", "-_ "),
        R.make_rule("number", "1", "3"),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="FileCat 引擎基准")
    parser.add_argument("-n", "--files", type=int, default=2000)
    parser.add_argument("--keep", action="store_true", help="保留造出来的文件")
    parser.add_argument("--reuse", action="store_true",
                        help="复用上次造的文件（第二轮起用，避开冷缓存与 Defender 首扫）")
    args = parser.parse_args()

    import namecat_py.ops as ops
    import namecat_py.plan as plan
    import namecat_py.rules as rules_mod
    import namecat_py.scan as scan

    os.environ["APPDATA"] = os.path.join(BASE, "appdata")
    os.environ["NAMECAT_LOG_DIR"] = os.path.join(BASE, "logs")
    os.makedirs(BASE, exist_ok=True)

    if args.reuse and os.path.isdir(SRC):
        made = sum(len(files) for _, _, files in os.walk(SRC))
        print("复用已有目录：%d 个文件\n" % made)
    else:
        made = build_tree(SRC, args.files)
        print("造了 %d 个文件（%d 个目录）\n" % (made, FOLDERS))

    chain = rules_of(rules_mod)
    scan_opts = scan.ScanOptions(root=SRC, kinds=(scan.KIND_FILE,), recursive=True)
    plan_opts = plan.PlanOptions(dry_run=True, width=3)

    print("预热中…")
    items = list(scan.scan(scan_opts).items)

    result = {}
    result["scan"] = timeit(lambda: scan.scan(scan_opts))
    # 纯算名（不查盘）与「算名 + 两遍校验」分开量：后者每个目标名都要探一次盘，
    # 在开着 Defender 的机器上是 syscall 主导，混在一起会把规则链本身盖掉。
    result["names"] = timeit(lambda: plan.compute_names(items, chain, plan_opts))
    result["plan"] = timeit(lambda: plan.build(items, chain, plan_opts))
    result["scan+plan"] = timeit(
        lambda: ops.scan_and_plan(SRC, chain, scan_opts, plan_opts))

    # ---- 落盘 / 撤销：只取 500 个，每轮复制一份干净的源目录 ----
    small = os.path.join(BASE, "src_small")
    write_samples, undo_samples = [], []
    for _ in range(3):
        shutil.rmtree(small, ignore_errors=True)
        os.makedirs(small)
        copied = 0
        for folder in sorted(os.listdir(SRC))[:5]:
            src_folder = os.path.join(SRC, folder)
            if not os.path.isdir(src_folder):
                continue
            shutil.copytree(src_folder, os.path.join(small, folder))
            copied += sum(len(files) for _, _, files in os.walk(os.path.join(small, folder)))
            if copied >= 500:
                break
        mono = scan.ScanOptions(root=small, kinds=(scan.KIND_FILE,), recursive=True)
        planned = ops.scan_and_plan(small, chain, mono, plan.PlanOptions(dry_run=True, width=3))
        start = time.perf_counter()
        applied = ops.apply(planned.items, chain, plan.PlanOptions(dry_run=False, width=3),
                            root=small)
        write_samples.append((time.perf_counter() - start) * 1000)
        if not applied.ok:
            raise SystemExit("落盘失败：%s" % applied.message)
        start = time.perf_counter()
        undone = ops.undo(applied.log_path)
        undo_samples.append((time.perf_counter() - start) * 1000)
        if not undone.success:
            raise SystemExit("撤销失败：%s" % undone.message)
        written = len(applied.steps)
    result["apply"] = {"median": statistics.median(write_samples), "min": min(write_samples)}
    result["undo"] = {"median": statistics.median(undo_samples), "min": min(undo_samples)}

    print("%-10s %10s %10s %10s" % ("阶段", "本次(ms)", "1.6.3(ms)", "参考倍率"))
    print("-" * 46)
    labels = [("scan", "扫描"), ("names", "纯算名"), ("plan", "算名+校验"),
              ("scan+plan", "扫描+算名"), ("apply", "落盘 %d 项" % written),
              ("undo", "撤销 %d 项" % written)]
    for key, label in labels:
        now = result[key]["median"]
        ref = PY_ENGINE[key]
        print("%-10s %10.1f %10.1f %9.2fx" % (label, now, ref, ref / now))

    print("\n注：参考倍率 >1 表示比 1.6.3 快。它只能看量级 —— 本机负载不同会让"
          "绝对值漂 ±30%，\n    严谨对比要让两版在同一时段、同一批文件上背靠背跑"
          "（迁移期就是这么测的，见 docs/TEST_REPORT.md 第 8 节）。")
    if not args.keep:
        shutil.rmtree(BASE, ignore_errors=True)
    else:
        print("数据保留在 %s" % BASE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
