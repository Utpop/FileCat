# -*- coding: utf-8 -*-
"""
ui.pages.extract —— FileCat 文件批量提取页。

沿用工作台的骨架与令牌：PageHead + Card 工具卡 + 选项行 + 清单卡 + 底部状态条。
所有数据来自 adapters.extract_adapter（记为 EA），后台任务走 workers.task_workers。
界面不实现任何提取算法，也不直接 import namecat_py.extract。
"""

from __future__ import annotations

import os
import subprocess
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QFrame,
                                QHeaderView, QLineEdit, QMessageBox, QProgressBar,
                                QTreeWidget, QTreeWidgetItem, QWidget)

from adapters import extract_adapter as EA
from ui.base import (C, F, Button, Card, Check, Chip, EmptyState, Label,
                     PageHead, StatusSlot, hbox, hline, vbox)
from ui.pages.workspace import Combo, FieldEdit, PathInput
from workers import task_workers as W

COLS = ["文件名", "来源目录", "原位置", "目标位置", "大小", "状态", "说明"]
COL_W = [200, 200, 260, 260, 84, 92, 300]


class ExtractPage(QWidget):
    """文件提取工作台：选目录 → 配选项 → 扫描 → 模拟/执行/停止 → 撤销/CSV。"""

    status_message = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Root")
        self.state = EA.ExtractFormState()
        self.bundle: Optional[EA.ExtractBundle] = None
        self._busy = False
        self._task_id = ""
        self._collapsed = False
        self.runner = W.TaskRunner(self, max_threads=1)

        outer = vbox(self, (20, 18, 20, 12), 12)
        outer.addWidget(self._head())
        outer.addWidget(self._toolbar())
        outer.addWidget(self._options())
        # 不用独立横幅：反馈统一进工具栏右侧的 StatusSlot，与文件重命名页同款
        outer.addWidget(self._list_card(), 1)
        outer.addWidget(self._footer())
        self._refresh_ui()

    # ---------------- 页头 ----------------
    def _head(self):
        self.btn_open = Button("打开目录", "open")
        self.btn_open.setToolTip("在资源管理器里打开目标文件夹")
        self.btn_open.clicked.connect(self._open_root)
        self.btn_logs = Button("撤销日志", "history")
        self.btn_logs.setToolTip("打开固定撤销日志目录 Extract/Rollback_Log")
        self.btn_logs.clicked.connect(self._open_logs)
        self.btn_undo = Button("撤销上次", "refresh")
        self.btn_undo.setToolTip("从 Extract/Rollback_Log 读取最新有效记录并还原")
        self.btn_undo.clicked.connect(self.undo)
        return PageHead(
            "文件提取",
            "递归扫描子目录，把文件集中到目标文件夹；先预览、再模拟或执行，全程可撤销",
            [self.btn_open, self.btn_logs, self.btn_undo])

    # ---------------- 工具栏（目标文件夹） ----------------
    def _toolbar(self):
        card = Card()
        lay = vbox(card, 14, 12)
        row = hbox(None, 0, 8)
        self.path = PathInput(self.state.root)
        self.path.edit.setPlaceholderText("选择或手动输入目标文件夹路径…")
        self.path.edited.connect(self._on_root_changed)
        row.addWidget(self.path, 1)
        pick = Button("浏览…", "folder")
        pick.setToolTip("选择目标文件夹（也是扫描根目录）")
        pick.clicked.connect(self._pick)
        row.addWidget(pick)
        self.btn_scan = Button("扫描预览", "scan", "cta", icon_px=16)
        self.btn_scan.setToolTip("按当前选项生成提取计划（只读，不改动文件）")
        self.btn_scan.clicked.connect(self.scan)
        row.addWidget(self.btn_scan)
        lay.addLayout(row)

        # 状态槽：与文件重命名页共用 ui/base.py 的 StatusSlot（同一套四态 +
        # 5 秒淡出 + 语气消息）。count_provider 注入当前清单条数。
        row2 = hbox(None, 0, 8)
        row2.addWidget(QWidget(), 1)     # 唯一 stretch 项，胶囊贴右
        self.status = StatusSlot(
            count_provider=lambda: len(self.bundle.items) if self.bundle else 0)
        row2.addWidget(self.status, 0, Qt.AlignVCenter)
        lay.addLayout(row2)
        self.status.idle()
        return card

    # ---------------- 选项行 ----------------
    def _options(self):
        card = Card()
        lay = vbox(card, 12, 10)

        r1 = hbox(None, 0, 14)
        self.cmb_mode = Combo(EA.MODES, self.state.mode, width=128)
        self.cmb_mode.changed.connect(self._on_mode)
        r1.addWidget(self._labeled("搬运方式", self.cmb_mode))
        self.cmb_depth = Combo(EA.DEPTHS, str(self.state.max_depth), width=112)
        self.cmb_depth.changed.connect(self._on_depth)
        r1.addWidget(self._labeled("扫描深度", self.cmb_depth))
        self.cmb_conflict = Combo(EA.POLICIES, self.state.conflict, width=140)
        self.cmb_conflict.changed.connect(self._on_conflict)
        r1.addWidget(self._labeled("同名处理", self.cmb_conflict))
        self.cb_clean = self._check("清理空文件夹", self.state.clean_empty_dirs,
                                    self._on_clean)
        r1.addWidget(self.cb_clean)
        r1.addWidget(QWidget(), 1)
        lay.addLayout(r1)

        r2 = hbox(None, 0, 14)
        self.ed_dirs = FieldEdit("", width=250)
        self.ed_dirs.edit.setPlaceholderText("排除目录名，逗号分隔，如 node_modules, .git")
        self.ed_dirs.edited.connect(self._on_excludes)
        r2.addWidget(self._labeled("排除目录", self.ed_dirs))
        self.ed_exts = FieldEdit("", width=250)
        self.ed_exts.edit.setPlaceholderText("排除扩展名，逗号分隔，如 .tmp, .log")
        self.ed_exts.edited.connect(self._on_excludes)
        r2.addWidget(self._labeled("排除扩展名", self.ed_exts))
        r2.addWidget(QWidget(), 1)
        lay.addLayout(r2)
        return card

    @staticmethod
    def _labeled(text, control):
        w = QWidget()
        l = hbox(w, 0, 6)
        l.addWidget(Label(text, 11, 400, C["t2"]), 0, Qt.AlignVCenter)
        l.addWidget(control, 0, Qt.AlignVCenter)
        return w

    @staticmethod
    def _check(text, checked, slot):
        w = QWidget()
        l = hbox(w, 0, 6)
        cb = Check(checked)
        cb.toggled.connect(slot)
        l.addWidget(cb, 0, Qt.AlignVCenter)
        l.addWidget(Label(text, 12, 400, C["t1"]), 0, Qt.AlignVCenter)
        w._cb = cb
        return w

    # ---------------- 清单卡 ----------------
    def _list_card(self):
        card = Card()
        lay = vbox(card, 0, 0)

        head = hbox(None, (14, 0, 14, 0), 8)
        head.addWidget(Label("提取清单", 14, 600, C["t1"]), 0, Qt.AlignVCenter)
        head.addSpacing(8)
        self.ed_filter = QLineEdit()
        self.ed_filter.setFont(F(12))
        self.ed_filter.setFixedHeight(28)
        self.ed_filter.setPlaceholderText("筛选文件名 / 路径 / 状态…")
        self.ed_filter.setStyleSheet(
            "QLineEdit{background:%s;border:1px solid %s;border-radius:4px;padding:0 8px;color:%s;}"
            % (C["white"], C["line2"], C["t1"]))
        self.ed_filter.textChanged.connect(self._on_filter)
        head.addWidget(self.ed_filter, 1)
        head.addSpacing(8)
        self.btn_collapse = Button("全部折叠", "shrink", "sm_soft", icon_px=12)
        self.btn_collapse.clicked.connect(self._toggle_collapse)
        head.addWidget(self.btn_collapse)
        self.btn_expand = Button("全部展开", "expand", "sm_soft", icon_px=12)
        self.btn_expand.clicked.connect(self._expand_all)
        head.addWidget(self.btn_expand)
        head.addSpacing(8)
        self.btn_csv = Button("导出 CSV", "save", "sm")
        self.btn_csv.clicked.connect(self.export_csv)
        head.addWidget(self.btn_csv)
        lay.addLayout(head)
        lay.addWidget(hline())

        # 表头直接用 QTreeWidget 原生的 QHeaderView：
        # 它与内容列共用同一套列几何，列边界永远对齐。曾用手绘 Strip + 固定
        # Label 宽度，结果表头有 14px 内边距、内容列还有树缩进，两套 x 起点
        # 对不上（见 AGENT.MD §8「表头与自绘列几何同源」）。这里不再另算一套。
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLS))
        self.tree.setHeaderLabels(COLS)
        header = self.tree.header()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        header.setFixedHeight(30)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for i, w in enumerate(COL_W):
            self.tree.setColumnWidth(i, w)
        self.tree.setRootIsDecorated(True)
        self.tree.setIndentation(14)
        self.tree.setAlternatingRowColors(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setFrameShape(QFrame.NoFrame)
        self.tree.setStyleSheet(
            "QTreeWidget{background:%s;border:0;color:%s;}"
            "QTreeWidget::item{height:30px;border-bottom:1px solid #F0F0F0;}"
            "QTreeWidget::item:hover{background:%s;}"
            "QTreeWidget::item:selected{background:%s;color:%s;}"
            "QHeaderView{background:%s;border:0;}"
            "QHeaderView::section{background:%s;border:0;border-right:1px solid %s;"
            "border-bottom:1px solid %s;color:%s;padding:0 10px;"
            "font-size:11px;height:30px;}"
            % (C["white"], C["t1"], C["s1"], C["brand_soft"], C["t1"],
               C["s1"], C["s1"], C["line"], C["line"], C["t2"]))
        lay.addWidget(self.tree, 1)

        self.empty = EmptyState(
            "inbox", "还没有扫描结果",
            "选好目标文件夹，点「扫描预览」生成任务清单；清单会按目标文件夹分组，"
            "可筛选、折叠或展开。")
        lay.addWidget(self.empty, 1)
        return card

    # ---------------- 底部状态条 ----------------
    def _footer(self):
        card = Card()
        lay = vbox(card, 10, 8)

        prog_row = hbox(None, 0, 8)
        self.progress = QProgressBar()
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        self.progress.setStyleSheet(
            "QProgressBar{background:%s;border:0;border-radius:3px;}"
            "QProgressBar::chunk{background:%s;border-radius:3px;}"
            % (C["s2"], C["brand"]))
        prog_row.addWidget(self.progress, 1)
        self.prog_label = Label("就绪", 11, 400, C["t2"])
        prog_row.addWidget(self.prog_label, 0)
        lay.addLayout(prog_row)

        row = hbox(None, 0, 8)
        self.chips: Dict[str, Chip] = {}
        for key, label, tone in (("total", "共", "info"), ("pending", "待处理", "info"),
                                  ("done", "完成", "ok"), ("skipped", "跳过", "muted"),
                                  ("conflict", "冲突", "warn"), ("error", "失败", "err")):
            ch = Chip(label, 0, tone)
            self.chips[key] = ch
            row.addWidget(ch)
        row.addWidget(QWidget(), 1)
        self.btn_dry = Button("模拟运行", "play", "mid")
        self.btn_dry.setToolTip("按当前计划演练一遍，绝不修改任何文件或目录")
        self.btn_dry.clicked.connect(self.dry_run)
        row.addWidget(self.btn_dry)
        self.btn_stop = Button("停止", "x", "mid")
        self.btn_stop.setToolTip("停止后续任务，已完成的保留真实记录")
        self.btn_stop.clicked.connect(self.stop)
        row.addWidget(self.btn_stop)
        self.btn_apply = Button("开始提取", "check", "cta", icon_px=15)
        self.btn_apply.setToolTip("按清单真正移动文件；覆盖策略会先备份原目标")
        self.btn_apply.clicked.connect(self.apply)
        row.addWidget(self.btn_apply)
        lay.addLayout(row)
        return card

    # ---------------- 表单事件 ----------------
    def _on_root_changed(self):
        self.state.root = self.path.text()
        self.bundle = None
        self._mark_dirty("目录已改动，请重新扫描预览")
        self._refresh_ui()

    def _on_mode(self, value):
        self.state.mode = value
        self._mark_dirty()

    def _on_depth(self, value):
        try:
            self.state.max_depth = max(0, int(value))
        except ValueError:
            self.state.max_depth = 0
        self._mark_dirty()

    def _on_conflict(self, value):
        self.state.conflict = value
        self.state.overwrite_confirmed = False
        note = EA.danger_note(value)
        if note:
            self.status.message(note, "warn")
        self._mark_dirty()

    def _on_clean(self, on):
        self.state.clean_empty_dirs = bool(on)
        self._mark_dirty()

    def _on_excludes(self):
        self.state.exclude_dirs = self.ed_dirs.text()
        self.state.exclude_exts = self.ed_exts.text()
        self._mark_dirty()

    def _on_filter(self, _text):
        self._refresh_table()

    def _mark_dirty(self, message: str = "选项已改动，请重新扫描预览"):
        self.bundle = None
        self.status.message(message, "info")
        self._refresh_ui()

    # ---------------- 浏览 / 打开 ----------------
    def _pick(self):
        start = self.state.root or os.path.expanduser("~")
        path = QFileDialog.getExistingDirectory(self, "选择目标文件夹", start)
        if path:
            self.path.setText(path)
            self.state.root = path
            self._mark_dirty("已选择目录，点「扫描预览」生成清单")

    def _open_root(self):
        self._open(self.state.root, "目标文件夹还没选择")

    def _open_logs(self):
        self._open(EA.rollback_dir(), "撤销日志目录还不存在")

    @staticmethod
    def _open(path: str, missing: str):
        path = (path or "").strip()
        if not path or not os.path.isdir(path):
            QMessageBox.information(None, "提示", missing)
            return
        try:
            if os.name == "nt":
                os.startfile(path)  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", path])
        except OSError as exc:
            QMessageBox.warning(None, "打不开", str(exc))

    # ---------------- 扫描 ----------------
    def scan(self):
        if self.runner.busy():
            return
        if not self.state.root or not os.path.isdir(self.state.root):
            self.status.message("请先选择一个存在的目标文件夹", "warn")
            return
        self._busy_set(True, "正在扫描目录…", 0, 0)
        task = W.extract_plan_task(self.state)
        task.signals.done.connect(self._scan_done)
        task.signals.failed.connect(self._task_failed)
        task.signals.progress.connect(self._on_progress)
        self.runner.submit(task)

    def _scan_done(self, bundle):
        self.bundle = bundle
        stats = bundle.stats()
        err = int(bundle.plan.get("error_count") or 0)
        # 与工作台同款：正常扫完走 done()（绿勾 + 5 秒淡回灰色），
        # 有目录读取异常才改成消息态停留，便于用户看见问题。
        if err:
            self.status.message("扫描完成 %d 项 · 目录读取异常 %d 处"
                                % (stats["total"], err), "warn")
        else:
            self.status.done()
        self.status_message.emit("扫描完成：%d 项" % stats["total"])
        self._busy_set(False, "扫描完成 · 共 %d 项" % stats["total"], 0, 0)
        self._refresh_ui()

    # ---------------- 执行 / 模拟 / 停止 ----------------
    def dry_run(self):
        self._run(True)

    def apply(self):
        if self.state.conflict == "overwrite" and not self.state.overwrite_confirmed:
            note = EA.danger_note("overwrite")
            if QMessageBox.question(
                    self, "确认危险操作",
                    note + "\n\n覆盖前会把原目标文件备份到 Extract/Rollback_Log/backups，"
                    "可通过「撤销上次」恢复。\n\n确定继续吗？",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            self.state.overwrite_confirmed = True
            # 确认状态要同步进 options，否则引擎会拒绝
            if self.bundle is not None:
                self.bundle.options["overwrite_confirmed"] = True
        self._run(False)

    def _run(self, dry_run: bool):
        if self.runner.busy() or self.bundle is None or not self.bundle.has_plan:
            return
        self._task_id = "extract_%s" % ("dry" if dry_run else "real")
        self._busy_set(True, "模拟运行中…" if dry_run else "正在提取文件…", 0,
                       len(self.bundle.items))
        task = W.extract_execute_task(self.bundle, dry_run, self._task_id)
        task.signals.done.connect(self._exec_done)
        task.signals.failed.connect(self._task_failed)
        task.signals.progress.connect(self._on_progress)
        self.runner.submit(task)

    def _exec_done(self, bundle):
        self.bundle = bundle
        r = bundle.result or {}
        st = bundle.stats()
        if r.get("dry_run"):
            tone, text = "success", "模拟运行完成：未修改任何文件、目录或日志"
        elif r.get("stopped"):
            tone, text = "warn", "任务已停止：已完成 %d 项，未处理项保持原状" % r.get("succeeded", 0)
        elif r.get("failed"):
            tone, text = "error", "执行结束：成功 %d · 失败 %d（详见清单说明列）" % (
                r.get("succeeded", 0), r.get("failed", 0))
        else:
            tone, text = "success", "提取完成：成功 %d 项 · 清理空目录 %d 个" % (
                r.get("succeeded", 0), r.get("cleaned_dirs", 0))
        if r.get("log_path"):
            text += " · 日志 %s" % os.path.basename(str(r["log_path"]))
        for err in (r.get("errors") or [])[:2]:
            text += " · %s" % err[:60]
        self.status.message(text, tone)
        self.status_message.emit(text)
        self._busy_set(False, "完成" if not r.get("stopped") else "已停止",
                       int(r.get("completed") or 0), st["total"])
        self._refresh_ui()
        # 模拟运行不写盘，无需刷预览；真实执行后清单已是执行结果，不再自动重扫
        # （用户想看磁盘现状可点「扫描预览」）。

    def stop(self):
        if not self._busy or not self._task_id:
            return
        if EA.stop_task(self._task_id):
            self.prog_label.setText("正在停止…当前文件操作完成后结束")
            self.btn_stop.setEnabled(False)

    # ---------------- 撤销 ----------------
    def undo(self):
        if self.runner.busy():
            return
        note = ("从固定目录 %s 读取最新一份有效提取记录并还原。\n\n"
                "只撤销实际执行成功的项；目标被后续修改或原位置已有文件时跳过并报告，"
                "不会强制覆盖。撤销失败会保留日志与备份。" % EA.rollback_dir())
        if QMessageBox.question(self, "撤销上次提取", note,
                                QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) != QMessageBox.Yes:
            return
        self._busy_set(True, "正在撤销…", 0, 0)
        task = W.extract_undo_task()
        task.signals.done.connect(self._undo_done)
        task.signals.failed.connect(self._task_failed)
        task.signals.progress.connect(self._on_progress)
        self.runner.submit(task)

    def _undo_done(self, result):
        r = result or {}
        complete = bool(r.get("complete"))
        text = "撤销完成：成功 %d · 跳过 %d · 失败 %d · 恢复目录 %d" % (
            r.get("succeeded", 0), r.get("skipped", 0),
            r.get("failed", 0), r.get("restored_dirs", 0))
        if not complete:
            text += "（日志与备份已保留，可再次撤销）"
        for err in (r.get("errors") or [])[:2]:
            text += " · %s" % str(err)[:60]
        self.status.message(text, "success" if complete else "warn")
        self.status_message.emit(text)
        self._busy_set(False, "撤销结束", 0, 0)
        # 撤销改了磁盘：旧清单失效，提示重新扫描
        self.bundle = None
        self._refresh_ui()

    # ---------------- CSV ----------------
    def export_csv(self):
        if self.bundle is None or not self.bundle.items:
            return
        start = os.path.join(self.state.root or os.path.expanduser("~"),
                             "FileCat_extract.csv")
        path, _ = QFileDialog.getSaveFileName(self, "导出清单为 CSV", start,
                                              "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            EA.export_csv(path, self.bundle.items)
        except OSError as exc:
            self.status.message("导出失败：%s" % exc, "error")
            return
        self.status.message("已导出：%s" % path, "success")
        self.status_message.emit("已导出 CSV")

    # ---------------- 后台回调 ----------------
    def _on_progress(self, cur, total, text):
        if total:
            self.progress.setMaximum(int(total))
            self.progress.setValue(min(int(cur), int(total)))
        self.prog_label.setText(str(text or "")[:48])

    def _task_failed(self, message):
        self.status.message("操作失败：%s" % message, "error")
        self.status_message.emit("操作失败")
        self._busy_set(False, "失败", 0, 0)

    def _busy_set(self, busy: bool, label: str, cur: int, total: int):
        self._busy = busy
        self.prog_label.setText(label)
        self.progress.setMaximum(max(1, int(total or 1)))
        self.progress.setValue(int(cur or 0))
        # 与工作台一致：任务开始把状态槽切到 busy（蓝字 + 旋转图标），
        # 结束时**不**覆盖回调刚写入的结果消息（错误 / 结果需停留）。
        if busy:
            self.status.busy(str(label or "处理中…"))
        self._refresh_ui()

    # ---------------- 表格渲染 ----------------
    def _refresh_table(self):
        items = self.bundle.items if self.bundle else []
        items = EA.filter_items(items, self.ed_filter.text())
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        if not items:
            self.tree.hide()
            self.empty.show()
            self.tree.setUpdatesEnabled(True)
            return
        self.empty.hide()
        self.tree.show()

        # 按目标目录分组
        groups: Dict[str, List[dict]] = {}
        for it in items:
            groups.setdefault(EA.group_key(it), []).append(it)
        root = self.state.root
        for gkey in sorted(groups.keys()):
            group = QTreeWidgetItem(self.tree)
            group.setText(0, "%s  (%d)" % (EA.relative_dir(gkey, root), len(groups[gkey])))
            group.setFont(0, F(12, 600))
            group.setForeground(0, QColor(C["t1"]))
            group.setBackground(0, QColor(C["s1"]))
            group.setFirstColumnSpanned(True)
            group.setExpanded(not self._collapsed)
            for it in groups[gkey]:
                row = QTreeWidgetItem(group)
                row.setText(0, str(it.get("name") or ""))
                row.setText(1, EA.relative_dir(str(it.get("source_dir") or ""), root))
                row.setText(2, str(it.get("source") or ""))
                row.setText(3, str(it.get("target") or ""))
                row.setText(4, EA.human_size(it.get("size") or 0))
                st = str(it.get("status") or "pending")
                row.setText(5, EA.status_text(st))
                row.setText(6, str(it.get("message") or ""))
                row.setToolTip(2, str(it.get("source") or ""))
                row.setToolTip(3, str(it.get("target") or ""))
                row.setToolTip(6, str(it.get("message") or ""))
                row.setForeground(5, QColor(C[EA.status_color(st)]))
        self.tree.setUpdatesEnabled(True)

    def _toggle_collapse(self):
        self._collapsed = not self._collapsed
        self.btn_collapse.setText("全部展开" if self._collapsed else "全部折叠")
        if self._collapsed:
            self.tree.collapseAll()
        else:
            self.tree.expandAll()

    def _expand_all(self):
        self._collapsed = False
        self.btn_collapse.setText("全部折叠")
        self.tree.expandAll()

    # ---------------- 控件启用状态（单一收口） ----------------
    def _refresh_ui(self):
        has_plan = self.bundle is not None and self.bundle.has_plan
        stats = self.bundle.stats() if self.bundle else {}
        for key, chip in self.chips.items():
            chip.set_value(int(stats.get(key, 0)))
        self.btn_scan.setEnabled(not self._busy)
        self.btn_dry.setEnabled(has_plan and not self._busy)
        self.btn_apply.setEnabled(has_plan and not self._busy)
        self.btn_stop.setEnabled(self._busy)
        self.btn_csv.setEnabled(bool(self.bundle and self.bundle.items) and not self._busy)
        self.btn_undo.setEnabled(not self._busy)
        self.btn_open.setEnabled(bool(self.state.root))
        self.btn_collapse.setEnabled(has_plan)
        self.btn_expand.setEnabled(has_plan)
        self._refresh_table()

    def shutdown(self):
        self.runner.wait(2000)
