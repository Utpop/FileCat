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


def _ck(cond):
    if not cond:
        raise AssertionError("断言失败")


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

print("== 4a. 扫描对象下拉 ==")


def only_combo():
    # 原样式：无前置标签、宽 148、原文案；默认「图片 + 视频」(media)
    assert ws.state.only == "media", ws.state.only
    opts = [v for v, _ in ws.only._opts]
    assert "all" not in opts, "「文件 + 文件夹」不应出现在下拉里：%s" % opts
    assert dict(ws.only._opts)["file"] == "仅文件"
    assert dict(ws.only._opts)["media"] == "图片 + 视频"
    assert ws.only.width() == 148, ws.only.width()
    # 旧预设里的 kinds 命中 all 时回落到默认档 media
    st = A.FormState()
    st.apply_scan_dict({"kinds": ["file", "dir", "image", "video"]})
    assert st.only == "media", st.only


step("扫描对象下拉（原样式 / 无 all / 默认仅文件）", only_combo)

print("== 4b. 预览卡表头的排序方式下拉 ==")


def sort_combo():
    # 排序方式从高级筛选挪到预览卡表头：换档写回 state 并触发重扫
    assert hasattr(ws, "sort"), "预览卡表头应有排序下拉"
    before = ws.state.sort_mode
    ws._on_sort_changed("mtime_asc")
    assert ws.state.sort_mode == "mtime_asc", ws.state.sort_mode
    ws._on_sort_changed(before)     # 还原
    assert ws.state.sort_mode == before


step("排序方式下拉（预览卡表头）", sort_combo)

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

print("== 6b. 工具栏进度条与状态胶囊共存 ==")


def progress_bar():
    # 进度条与「已扫描 N 项」胶囊都在工具栏第二行；空闲时进度条隐藏，
    # 且 progress_wrap 是唯一 stretch 项，胶囊不会被挤压或与进度条折叠到一起。
    assert hasattr(ws, "progress_bar") and hasattr(ws, "progress_wrap")
    assert not ws.progress_bar.isVisible(), "空闲时进度条应隐藏"
    assert ws.scan_status.isVisible(), "状态胶囊应始终可见"
    # busy 态：进度条显示、胶囊仍在、且两者不重叠
    ws._busy_set(True, "自检中…")
    app.processEvents()
    assert ws.progress_bar.isVisible(), "忙时进度条应显示"
    assert ws.scan_status.isVisible(), "忙时状态胶囊不应被挤掉"
    bar_right = ws.progress_bar.mapToGlobal(ws.progress_bar.rect().topRight()).x()
    cap_left = ws.scan_status.mapToGlobal(ws.scan_status.rect().topLeft()).x()
    assert bar_right <= cap_left, "进度条与状态胶囊重叠：%d > %d" % (bar_right, cap_left)
    ws._on_progress(2, 4, "算名")
    assert ws.progress_bar.value() == 2 and ws.progress_bar.maximum() == 4
    ws._busy_set(False)
    app.processEvents()
    assert not ws.progress_bar.isVisible(), "任务结束后进度条应收起"


step("进度条 / 状态胶囊布局", progress_bar)

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
# 步骤 4 的高级筛选设了「名称包含 IMG」；执行后文件名变成 P_0001.jpg，
# 带着旧过滤条件重扫只会得到空表，验证不到刷新本身。这里先清掉过滤，
# 让「按磁盘现状重扫」拿到完整清单（plan.csv 也在目录里，两侧都会算上）。
ws.state.only_match = ""
ws.state.exclude_exts = ""
# 这段断言要求「预览 = 目录里全部文件」，用全文件档验证；默认档是
# 「图片 + 视频」，会合理地不显示 plan.csv / req.txt 这类非媒体文件。
ws.state.only = "file"
ws.only.set_value("file")
ws.confirm_apply()
# 执行完会自动按磁盘现状重扫：等重扫结束，再断言预览与目录一致。
wait_until(lambda: not ws._scan_outdated and not ws._rescan_after_apply
           and not ws._busy and not ws.runner.busy(), 12000)
on_disk = sorted(n for n in os.listdir(tmpdir) if os.path.isfile(os.path.join(tmpdir, n)))
shown = sorted(getattr(it, "name", "") for it in ws.bundle.items())
assert not ws._scan_outdated, "执行后清单仍标成失效，预览没有刷新"
assert shown == on_disk, "执行后预览未跟上磁盘：table=%s disk=%s" % (shown, on_disk)
assert not ws.btn_apply.isEnabled(), "刷新清单前不该允许再次执行旧计划"
step("执行后自动重扫预览", lambda: None)
print("   目录：", sorted(os.listdir(tmpdir))[:6])

print("== 10. 执行记录 + 撤销 ==")
w._goto(2)  # 执行记录页（文件提取页占 index 1）
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
# 撤销同样改了磁盘：工作台要按磁盘现状重建预览。
wait_until(lambda: not ws._scan_outdated and not ws._rescan_after_apply
           and not ws._busy and not ws.runner.busy(), 12000)
on_disk = sorted(n for n in os.listdir(tmpdir) if os.path.isfile(os.path.join(tmpdir, n)))
shown = sorted(getattr(it, "name", "") for it in ws.bundle.items())
assert shown == on_disk, "撤销后预览未跟上磁盘：table=%s disk=%s" % (shown, on_disk)
step("撤销后自动重扫预览", lambda: None)
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
from ui.pages.presets import PresetDialog
dlg = PresetDialog(w)
dlg.reload()
print("   预设对话框行数：", len(dlg._rows))
step("预设对话框含编辑/删除操作", lambda: (
    _ck(hasattr(dlg, "_edit") and hasattr(dlg, "_delete")),
    _ck(len(dlg._rows) >= 4)))
step("预设对话框 应用信号", lambda: dlg._apply(dlg._rows[0]))
dlg.close()
# 「加载预设」按钮走的是弹窗，不再是主窗口的一页
step("加载预设按钮弹出对话框", lambda: (
    _ck(hasattr(w, "open_preset_dialog")),
    _ck(w.stack.count() == 4),
    _ck(not hasattr(w, "page_presets"))))
called = []
w.open_preset_dialog = lambda: called.append(1)
ws.load_preset_requested.emit()
app.processEvents()
step("加载预设信号连到弹窗", lambda: _ck(called == [1]))
step("删除预设", lambda: A.delete_preset(os.path.join(tmpdir, "自检预设.json")))

print("== 12. LLM 页 ==")
step("读取要求文件", lambda: A.llm_read_instruction())
step("写要求文件（临时路径）",
     lambda: A.llm_write_instruction("自检要求", custom=os.path.join(tmpdir, "req.txt")))
step("环境变量来源", lambda: (len(A.llm_env_sources()) > 0) or (_ for _ in ()).throw(AssertionError))
step("客户端", lambda: A.llm_client())


def llm_page():
    w._goto(3)  # 供应商配置页
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
