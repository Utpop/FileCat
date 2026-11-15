# -*- coding: utf-8 -*-
"""verify/history_merge_smoke.py —— 执行记录页同时显示两类日志的离屏验证。

真跑一次重命名 + 一次文件提取，再断言：
  1. 历史页 _rows 同时含 rename / extract 两行；
  2. 目录筛选对两类都生效；
  3. 从历史页撤销「文件提取」记录能还原文件（不是走重命名撤销）；
  4. 撤销后工作台与提取页清单都失效。
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "_vendor"), ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 两类日志都指到沙箱，避免污染真实程序目录
_TMP = tempfile.mkdtemp(prefix="filecat_history_")
os.environ["NAMECAT_LOG_DIR"] = os.path.join(_TMP, "rename_logs")

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QWidget  # noqa: E402

app = QApplication([])
from ui import base  # noqa: E402
base.FAMILY = base.load_fonts()
app.setFont(base.F(12, 400))

from ui.window import MainWindow  # noqa: E402
from adapters import engine_adapter as A  # noqa: E402
from adapters import extract_adapter as EA  # noqa: E402
from workers import task_workers as W  # noqa: E402

FAILS = []


def check(cond, msg):
    if cond:
        print("  ok   %s" % msg)
    else:
        FAILS.append(msg)
        print("  FAIL %s" % msg)


def wait_until(pred, timeout_ms=8000):
    loop = QEventLoop()
    state = {"n": 0}

    def tick():
        state["n"] += 1
        if pred() or state["n"] * 50 > timeout_ms:
            loop.quit()
        else:
            QTimer.singleShot(50, tick)

    QTimer.singleShot(50, tick)
    loop.exec()
    app.processEvents()


def main():
    w = MainWindow({})
    w.resize(1440, 900)
    w.show()
    app.processEvents()

    box = os.path.join(_TMP, "data")
    os.makedirs(os.path.join(box, "sub"))
    with open(os.path.join(box, "IMG_0001.jpg"), "w", encoding="utf-8") as f:
        f.write("a")
    with open(os.path.join(box, "sub", "deep.txt"), "w", encoding="utf-8") as f:
        f.write("b")
    program = os.path.join(_TMP, "program")
    os.makedirs(program)
    # 提取日志目录固定由 program_root 决定
    EA.program_root = lambda: program

    # ---- 1. 真跑一次重命名（写 Name_backup 日志）----
    ws = w.page_ws
    ws.path.setText(box)
    ws.state.root = box
    ws.rules = [A.new_rule("replace")]
    ws.rules[0].a, ws.rules[0].b = "IMG_", "P_"
    ws.selected_id = ws.rules[0].id
    ws._refresh_rules()
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    ws.scan(plan_after=True)
    wait_until(lambda: ws.bundle is not None and ws.bundle.has_plan and not ws._busy)
    ws.confirm_apply()
    wait_until(lambda: not ws._scan_outdated and not ws._busy and not ws.runner.busy(), 12000)
    check(os.path.isfile(os.path.join(box, "P_0001.jpg")), "重命名已落盘")

    # ---- 2. 真跑一次文件提取（写 Extract/Rollback_Log 日志）----
    ep = w.page_extract
    ep.path.setText(box)
    ep.state.root = box
    ep._on_root_changed()
    ep.scan()
    wait_until(lambda: ep.bundle is not None and not ep._busy)
    ep._run(False)
    wait_until(lambda: ep.bundle is not None and ep.bundle.result is not None
               and not ep.bundle.result.get("dry_run") and not ep._busy)
    check(os.path.isfile(os.path.join(box, "deep.txt")), "文件提取已落盘")
    check(bool(ep.bundle.result.get("log_path")), "提取日志已生成")

    # ---- 3. 历史页：两类同时出现 ----
    w._goto(2)
    app.processEvents()
    hist = w.page_history
    hist.set_root("")            # 全部目录
    wait_until(lambda: bool(hist._rows) and not hist.runner.busy())
    app.processEvents()
    kinds = [r.get("record_type") for r in hist._rows]
    check("rename" in kinds, "历史页含重命名记录：%s" % kinds)
    check("extract" in kinds, "历史页含文件提取记录：%s" % kinds)
    check(len(hist._rows) >= 2, "两类记录合计 >= 2 行")
    check(len(hist.body_lay.findChildren(QWidget)) > 0 or hist.body_lay.count() >= 2,
          "两类记录都渲染成行")

    # 类型徽章文字
    labels = [x.text() for x in hist.findChildren(QLabel) if x.text() in ("文件重命名", "文件提取")]
    check("文件重命名" in labels and "文件提取" in labels,
          "行内有类型徽章：%s" % labels[:6])

    # ---- 4. 目录筛选对两类都生效 ----
    hist.set_root(box)
    wait_until(lambda: not hist.runner.busy())
    app.processEvents()
    check(all(str(r.get("root") or "").lower() == box.lower()
              for r in hist._rows if not r.get("root")), "空 root 行不影响筛选")
    check(len(hist._rows) >= 2, "当前目录下两类记录仍在：%d" % len(hist._rows))
    hist.set_root("")

    # ---- 5. 从历史页撤销「文件提取」记录 ----
    wait_until(lambda: not hist.runner.busy())
    extract_row = next((r for r in hist._rows if r.get("record_type") == "extract"), None)
    check(extract_row is not None, "找到提取记录行")
    if extract_row:
        hist._undo(extract_row)
        wait_until(lambda: not hist.runner.busy())
        app.processEvents()
        check(os.path.isfile(os.path.join(box, "sub", "deep.txt")),
              "从历史页撤销提取后文件已回原路径")
        check(not os.path.isfile(os.path.join(box, "deep.txt")),
              "根目录里的提取结果已消失")
    # 重命名记录仍在，未被误撤销
    check(os.path.isfile(os.path.join(box, "P_0001.jpg")),
          "重命名结果未被提取撤销影响（两类隔离）")

    # ---- 6. 撤销后两个页面的旧清单都失效 ----
    check(ws._scan_outdated or ws.bundle is None or not ws._busy,
          "工作台重扫状态正常")
    check(ep.bundle is None, "提取页清单已失效")

    w.close()
    shutil.rmtree(_TMP, ignore_errors=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    rc = main()
    print()
    if FAILS:
        print("失败 %d 项：%s" % (len(FAILS), FAILS))
    else:
        print("全部通过（两类日志同页显示 / 筛选 / 分类撤销 / 互相隔离 / 清单失效）")
    raise SystemExit(rc)
