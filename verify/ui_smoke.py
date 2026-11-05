# -*- coding: utf-8 -*-
"""
verify/ui_smoke.py —— 界面交互级冒烟（离屏，不需要显示器）。

把界面上每个可点的地方都走一遍：每种规则逐条添加、规则选择弹窗、高级筛选、
编号选项、扫描→算名→勾选→CSV→执行→撤销、预设应用/保存/删除、LLM 页。
任何一步抛异常都会打印堆栈并以退出码 1 结束。

    py -3 verify/ui_smoke.py
"""
import os
import shutil
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 撤销日志统一存在程序目录的 Name_backup 里；自检把它指到临时目录，
# 否则跑一次冒烟就会往程序目录里塞日志文件
_TMP_LOGS = os.path.join(tempfile.gettempdir(), "namecat_ui_smoke_logs")
os.environ["NAMECAT_LOG_DIR"] = _TMP_LOGS

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import (QApplication, QDialog, QMessageBox,  # noqa: E402
                               QPushButton, QWidget)

app = QApplication([])
from ui import base  # noqa: E402

base.FAMILY = base.load_fonts()
app.setFont(base.F(12, 400))

from adapters import engine_adapter as A  # noqa: E402
from ui.pages.workspace import AdvancedDialog, RulePicker  # noqa: E402
from ui.window import MainWindow  # noqa: E402

FAILS = []


def step(name, fn):
    try:
        fn()
        print("  ok   %s" % name)
    except Exception as exc:
        FAILS.append((name, traceback.format_exc()))
        print("  FAIL %s -> %s: %s" % (name, type(exc).__name__, exc))


def wait_until(pred, timeout_ms=6000):
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


print("== 1. 启动 ==")
w = MainWindow({})
w.resize(1440, 900)
w.show()
app.processEvents()
ws = w.page_ws

print("== 2. 添加规则（全部种类全走一遍）==")
tmpdir = tempfile.mkdtemp(prefix="nc_bug_")
from namecat_py.rules import RULE_KINDS  # 只为拿全量 kind 列表做遍历
for kind, text, _params, _note in RULE_KINDS:
    step("add_rule(%s)" % kind, lambda k=kind: ws.add_rule(k))
print("   规则数：", len(ws.rules))

print("== 3. 规则选择弹窗 ==")


def picker():
    dlg = RulePicker(ws)
    dlg._filter("正则")
    dlg._take("regex")
    assert dlg.chosen() == "regex"


step("RulePicker 构造/过滤/选择", picker)

print("== 4. 高级筛选弹窗 ==")


def advanced():
    dlg = AdvancedDialog(ws.state, ws)
    dlg.only_match.setText("IMG")
    dlg.exclude_exts.setText("tmp")
    dlg._apply()
    assert ws.state.only_match == "IMG", ws.state.only_match


step("AdvancedDialog 构造/应用", advanced)

print("== 5. 编号规则：发号 + 插号同一条 ==")


def number_rule():
    rule = A.new_rule("number")
    ws.rules = [rule]
    ws.selected_id = rule.id
    ws._refresh_rules()                      # 属性面板要能装下这一整条（不挤不溢）
    A.set_field(rule, "extras.step", "2")
    A.set_field(rule, "extras.reverse", "1")
    A.set_field(rule, "b", "3")
    ws._refresh_rules()
    assert A.get_field(rule, "extras.step") == "2"
    assert A.get_field(rule, "extras.reverse") == "1"
    A.set_field(rule, "extras.reverse", "")  # 关掉倒序
    ws._refresh_rules()
    assert A.get_field(rule, "extras.reverse") == ""


step("编号规则参数（起始/步长/补零/范围/倒序）", number_rule)

print("== 5b. 可点区域点击信号（防止事件覆盖失效回归）==")
from PySide6.QtTest import QTest  # noqa: E402


def click_sigs():
    from PySide6.QtCore import Qt as Qta
    # 直接点：处理器会弹模态框，所以用记录桩验证「点击 → 处理器」这条链
    called = []
    ws._advanced = lambda: called.append("adv")
    QTest.mouseClick(ws.adv_holder, Qta.LeftButton)
    assert called == ["adv"], called
    assert isinstance(w.findChild(QWidget, "HelpBtn"), QPushButton), "帮助按钮应存在"


step("高级筛选 点击", click_sigs)

print("== 6. 扫描 → 算名 ==")
for i in range(1, 4):
    with open(os.path.join(tmpdir, "IMG_%04d.jpg" % i), "w") as fh:
        fh.write("x")
ws.path.setText(tmpdir)
ws.state.root = tmpdir
ws.rules = [A.new_rule("replace")]
ws.rules[0].a, ws.rules[0].b = "IMG_", "P_"
ws.selected_id = ws.rules[0].id
ws._refresh_rules()
ws.scan(plan_after=True)
wait_until(lambda: ws.bundle is not None and ws.bundle.has_plan)
print("   items:", len(ws.bundle.items()), "stats:", ws.bundle.stats())

print("== 7. 勾选排除 ==")


def toggle():
    rows = ws._row_widgets()
    rows[0]._cb.setChecked(False)
    app.processEvents()
    assert ws.bundle.stats()["skip"] == 1, ws.bundle.stats()
    rows[0]._cb.setChecked(True)
    app.processEvents()
    assert ws.bundle.stats()["skip"] == 0


step("单行取消勾选", toggle)
step("全不选", lambda: (ws._check_all(False), ws._check_all(True)))

print("== 8. CSV 导出 ==")
csv_path = os.path.join(tmpdir, "plan.csv")
step("A.export_csv", lambda: A.export_csv(ws.bundle.plan, csv_path))
print("   csv:", os.path.isfile(csv_path))

print("== 9. 执行落盘 ==")
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
ws.confirm_apply()
wait_until(lambda: not ws._busy and not ws.runner.busy(), 8000)
print("   目录：", sorted(os.listdir(tmpdir))[:6])

print("== 10. 执行记录 + 撤销 ==")
w._goto(2)
w.page_history.set_root(tmpdir)
wait_until(lambda: bool(w.page_history._rows), 6000)
print("   日志条数：", len(w.page_history._rows))


def history_all():
    # 日志统一存在 Name_backup 里，不给目录 = 看全部目录的日志
    w.page_history.set_root("")
    wait_until(lambda: bool(w.page_history._rows), 6000)
    assert w.page_history._rows, "全部目录里应有日志"
    assert not os.path.isdir(os.path.join(tmpdir, "_rename_logs")), \
        "目标目录里不该再留 _rename_logs"
    # 自检把日志目录指到了临时目录（NAMECAT_LOG_DIR），所以这里查的是那个
    assert os.path.isdir(A.backup_dir()), "统一的备份目录应存在"
    assert A.backup_dir() == _TMP_LOGS, "备份目录应遵循 NAMECAT_LOG_DIR"


step("执行记录（全部目录）", history_all)

step("撤销", lambda: (w.page_history._undo(w.page_history._rows[0]),
                    wait_until(lambda: not w.page_history.runner.busy(), 8000)))
print("   撤销后目录：", sorted(os.listdir(tmpdir))[:6])

print("== 11. 预设：应用 / 保存 / 删除 ==")
name, preset = A.builtin_presets()[0]
step("应用内置预设", lambda: (w.apply_preset(preset), None))
print("   应用后规则数：", len(ws.rules), "only:", ws.state.only)


def save_preset():
    p = ws.collect_preset("自检预设")
    path = A.save_preset(p, os.path.join(tmpdir, "自检预设.json"))
    assert os.path.isfile(path)


step("保存预设", save_preset)
w.page_presets.reload()
print("   预设行数：", len(w.page_presets._rows))
step("删除预设", lambda: A.delete_preset(os.path.join(tmpdir, "自检预设.json")))

print("== 12. LLM 页 ==")
step("读取要求文件", lambda: A.llm_read_instruction())
step("写要求文件（临时路径）",
     lambda: A.llm_write_instruction("自检要求", custom=os.path.join(tmpdir, "req.txt")))
step("环境变量来源", lambda: (len(A.llm_env_sources()) > 0) or (_ for _ in ()).throw(AssertionError))
step("客户端", lambda: A.llm_client())


def llm_page():
    w._goto(3)
    app.processEvents()
    w.page_llm.reload(True)
    w.page_llm.overrides()


step("LLM 页刷新", llm_page)

print("== 13. 收尾 ==")
w.close()
shutil.rmtree(_TMP_LOGS, ignore_errors=True)
print()
if FAILS:
    print("失败 %d 项：" % len(FAILS))
    for name, tb in FAILS:
        print("-" * 60)
        print(name)
        print(tb)
    sys.exit(1)
print("全部通过（全部规则 + 全流程 + 撤销 + 预设 + LLM）")
