# -*- coding: utf-8 -*-
"""verify/extract_ui_smoke.py —— 文件提取页的离屏交互冒烟。

真点 GUI：选目录 → 扫描 → 筛选/折叠 → 模拟 → 执行 → CSV → 撤销 → 刷新。
同时验证新旧页面共存、导航索引、控件启用状态。
"""
import os
import shutil
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "_vendor"), ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

app = QApplication([])
from ui import base  # noqa: E402
base.FAMILY = base.load_fonts()
app.setFont(base.F(12, 400))

from ui.window import MainWindow  # noqa: E402
from adapters import extract_adapter as EA  # noqa: E402

FAILS = []


def step(name, fn):
    try:
        fn()
        print("  ok   %s" % name)
    except Exception as exc:
        FAILS.append((name, traceback.format_exc()))
        print("  FAIL %s -> %s: %s" % (name, type(exc).__name__, exc))


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

    print("== 1. 主窗口与新页面 ==")
    step("4 页导航", lambda: (w._goto(1), app.processEvents(),
                              _assert(w.stack.currentIndex() == 1),
                              _assert(w.stack.count() == 4)))
    ep = w.page_extract
    step("页面对象与统一令牌", lambda: (
        _assert(hasattr(ep, "tree") and hasattr(ep, "btn_scan")),
        _assert(ep.btn_apply.objectName() == "BtnCta"),
        _assert(ep.state.mode == "flatten"),
        # 两页共用 ui/base.py 的 StatusSlot，反馈逻辑同源（不再是横幅）
        _assert(type(ep.status) is type(w.page_ws.scan_status)),
        _assert(not hasattr(ep, "notice"))))

    base_dir = tempfile.mkdtemp(prefix="filecat_extract_ui_")
    root = os.path.join(base_dir, "data")
    os.makedirs(os.path.join(root, "a", "b"))
    for rel, body in (("a/b/深.txt", "D"), ("a/中.txt", "M"), ("根.txt", "R")):
        with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
            f.write(body)
    # 程序根目录指到沙箱，避免写进真实项目目录
    import namecat_py.extract as X
    # 通过环境变量做不到（规则禁止 cwd/_MEIPASS），这里直接给 options 注入：
    # 页面用 EA.program_root()，所以临时替换它为沙箱目录。
    program = os.path.join(base_dir, "program")
    os.makedirs(program)
    EA.program_root = lambda: program

    ep.path.setText(root)
    ep.state.root = root
    ep._on_root_changed()

    print("== 2. 扫描预览 ==")
    ep.scan()
    wait_until(lambda: ep.bundle is not None and not ep._busy)
    step("计划生成与清单", lambda: (
        _assert(ep.bundle is not None and ep.bundle.has_plan),
        _assert(len(ep.bundle.items) == 3),
        _assert(ep.chips["total"].val.text() == "3"),
        _assert(ep.btn_apply.isEnabled()),
        _assert(ep.tree.topLevelItemCount() >= 1)))

    # 几何回归：表头与内容列必须同源于 QHeaderView，不能存在两套列几何。
    # 旧实现用手绘 Strip 表头 + 固定 Label 宽度，与 QTreeWidget 内容的
    # 卡片内边距/树缩进错位，列标题与数据不对齐。
    step("表头与内容列对齐", lambda: (
        _assert(not ep.tree.header().isHidden()),
        _assert(ep.tree.header().count() == 7),
        _assert(ep.tree.header().sectionSize(0) == ep.tree.columnWidth(0)),
        _assert(ep.tree.header().sectionSize(2) == ep.tree.columnWidth(2)),
        _assert(ep.tree.header().sectionSize(5) == ep.tree.columnWidth(5)),
        _assert(ep.tree.header().height() == 30)))

    step("文本筛选", lambda: (
        ep.ed_filter.setText("深"),
        app.processEvents(),
        _assert(ep.tree.topLevelItemCount() >= 1),
        ep.ed_filter.setText(""),
        app.processEvents()))

    step("折叠 / 展开", lambda: (
        ep._toggle_collapse(),
        _assert(ep.btn_collapse.text() == "全部展开"),
        ep._expand_all(),
        _assert(ep.btn_collapse.text() == "全部折叠")))

    print("== 3. 模拟运行 ==")
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    ep.dry_run()
    wait_until(lambda: ep.bundle is not None and ep.bundle.result is not None
               and not ep._busy)
    step("模拟零写入", lambda: (
        _assert(ep.bundle.result.get("dry_run") is True),
        _assert(os.path.isfile(os.path.join(root, "a", "b", "深.txt"))),
        _assert(not os.path.exists(os.path.join(program, "Extract")))))

    print("== 4. 实际执行 ==")
    # 模拟后 bundle.plan 仍在，可重新执行
    ep._run(False)
    wait_until(lambda: ep.bundle is not None and ep.bundle.result is not None
               and not ep.bundle.result.get("dry_run") and not ep._busy)
    step("文件已提取", lambda: (
        _assert(os.path.isfile(os.path.join(root, "深.txt"))),
        _assert(os.path.isfile(os.path.join(root, "中.txt"))),
        _assert(ep.chips["done"].val.text() == "2")))
    step("日志在固定目录", lambda: (
        _assert(ep.bundle.result.get("log_path")),
        _assert("Extract" in str(ep.bundle.result["log_path"])),
        _assert("Rollback_Log" in str(ep.bundle.result["log_path"]))))

    print("== 5. CSV ==")
    csv_path = os.path.join(base_dir, "report.csv")
    EA.export_csv(csv_path, ep.bundle.items)
    step("CSV BOM", lambda: _assert(open(csv_path, "rb").read(3) == b"\xef\xbb\xbf"))

    print("== 6. 撤销 ==")
    ep.undo()
    wait_until(lambda: not ep._busy and ep.bundle is None)
    step("撤销还原", lambda: (
        _assert(os.path.isfile(os.path.join(root, "a", "b", "深.txt"))),
        _assert(ep.btn_apply.isEnabled() is False)))

    print("== 7. 原页面零回归 ==")
    step("工作台 / 记录 / LLM 索引", lambda: (
        w._goto(0), _assert(w.stack.currentIndex() == 0),
        w._goto(2), _assert(w.stack.currentIndex() == 2),
        w._goto(3), _assert(w.stack.currentIndex() == 3)))
    step("Ctrl+1…4 快捷键", lambda: (
        w._goto(1), _assert(w.nav[1].isChecked()),
        _assert(not w.nav[3].isChecked())))

    w.close()
    shutil.rmtree(base_dir, ignore_errors=True)
    return 1 if FAILS else 0


def _assert(cond):
    if not cond:
        raise AssertionError("断言失败")


if __name__ == "__main__":
    rc = main()
    print()
    if FAILS:
        print("失败 %d 项：" % len(FAILS))
        for name, tb in FAILS:
            print("-" * 60)
            print(name)
            print(tb)
    else:
        print("全部通过（4 页共存 / 扫描 / 筛选 / 折叠 / 模拟 / 执行 / CSV / 撤销 / 快捷键）")
    raise SystemExit(rc)
