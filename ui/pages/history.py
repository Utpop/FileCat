# -*- coding: utf-8 -*-
"""ui.pages.history —— 执行记录与撤销（同时显示重命名与文件提取两类日志）。

两类日志由各自的 Adapter 读取、各自的引擎撤销，界面只做归并展示：
    · 重命名   → engine_adapter.logs / undo，日志在 Name_backup
    · 文件提取 → extract_adapter.list_logs / undo_path，日志在 Extract/Rollback_Log
"""

from __future__ import annotations

import os
from typing import List

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QMessageBox, QPushButton, QWidget

from adapters import engine_adapter as A
from adapters import extract_adapter as EA
from ui.base import (C, F, Badge, Button, Card, Elided, Label, NoticeBar, PageHead, Strip,
                     fit_scroll_content, grid_row, hbox, hline, scroll_area,
                     svg_pixmap, vbox)
from ui.pages.workspace import Item
from workers import task_workers as W


class HistoryPage(QWidget):

    status_message = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Root")
        self._root = ""            # 空 = 看全部目录的日志
        self._rows: List[dict] = []
        self.runner = W.TaskRunner(self, max_threads=1)

        outer = vbox(self, (20, 18, 20, 12), 12)
        refresh = Button("刷新", "refresh")
        refresh.clicked.connect(self.reload)
        open_btn = Button("打开日志目录", "open")
        open_btn.clicked.connect(self.open_dir)
        outer.addWidget(PageHead(
            "执行记录", "同时列出重命名与文件提取两类记录；各自独立撤销，互不干扰",
            [refresh, open_btn]))

        self.notice = NoticeBar(
            "重命名日志在 Name_backup，文件提取日志在 Extract/Rollback_Log；"
            "两类记录合并展示，撤销各自走对应引擎")
        outer.addWidget(self.notice)

        bar = QFrame()
        bar.setObjectName("DirBar")
        bar.setAttribute(Qt.WA_StyledBackground, True)
        bar.setFixedHeight(48)
        bl = hbox(bar, (12, 0, 12, 0), 10)
        ic = Label("", 0)
        ic.setPixmap(svg_pixmap("folder", C["t2"], 15))
        ic.setFixedSize(15, 15)
        bl.addWidget(ic, 0, Qt.AlignVCenter)
        self.path_label = Elided("全部目录", 12, 500, C["t1"])
        self.path_label.setMinimumWidth(240)
        bl.addWidget(self.path_label, 0, Qt.AlignVCenter)
        self.meta = Elided("", 11, 400, C["t3"])
        self.meta.setMinimumWidth(0)
        bl.addWidget(self.meta, 1, Qt.AlignVCenter)
        bl.addWidget(QWidget(), 1)
        all_btn = Button("全部目录", kind="mid")
        all_btn.clicked.connect(self._show_all)
        bl.addWidget(all_btn, 0, Qt.AlignVCenter)
        browse = Button("选择目录", kind="mid")
        browse.clicked.connect(self._browse)
        bl.addWidget(browse, 0, Qt.AlignVCenter)
        use_ws = Button("用工作台目录", kind="mid")
        use_ws.clicked.connect(self._use_workspace)
        bl.addWidget(use_ws, 0, Qt.AlignVCenter)
        outer.addWidget(bar)

        card = Card()
        lay = vbox(card, 0, 0)
        lay.addWidget(grid_row([Item(("执行时间", 150)), Item(("类型", 76)),
                                Item("目标目录"),
                                Item(("数量", 80), align_right=True), Item("结果"),
                                Item(("日志状态", 200)),
                                Item(("操作", 110), align_right=True)],
                               36, pad=(0, 14), holder=Strip(C["s1"])))
        lay.addWidget(hline())
        self.body = QWidget()
        self.body_lay = vbox(self.body, 0, 0)
        self.body_scroll = scroll_area(self.body, min_height=62 * 3)
        lay.addWidget(self.body_scroll, 1)
        lay.addWidget(hline())

        foot = Strip(C["s1"])
        foot.setFixedHeight(44)
        fl = hbox(foot, (14, 0, 14, 0), 8)
        self.foot_left = Label("", 12, 400, C["t2"])
        fl.addWidget(self.foot_left, 1)
        self.foot_right = Label("", 11, 400, C["t3"])
        fl.addWidget(self.foot_right, 0)
        lay.addWidget(foot)
        outer.addWidget(card, 1)
        self._fill()

    # ---------------- 目录 ----------------
    def set_root(self, root: str, reload: bool = True):
        """指定只看某个目录的日志；传空串 = 看全部。"""
        root = (root or "").strip()
        if root == self._root and self._rows:
            return
        self._root = root
        self.path_label.setText(root or "全部目录")
        if reload:
            self.reload()

    def _browse(self):
        from PySide6.QtWidgets import QFileDialog
        start = self._root or os.path.expanduser("~")
        path = QFileDialog.getExistingDirectory(self, "只看这个目录的执行记录", start)
        if path:
            self.set_root(path)

    def _use_workspace(self):
        main = self.window()
        getter = getattr(main, "workspace_root", None)
        root = getter() if callable(getter) else ""
        if not root:
            self.notice.set_text("工作台还没有选择目录", "warn")
            self.notice.show()
            return
        self.set_root(root)

    def _show_all(self):
        self.set_root("")

    # ---------------- 读取 ----------------
    def reload(self):
        if self.runner.busy():
            return
        # 一次读两类日志（各自走自己的 Adapter），归并后按时间倒序
        task = W.history_all_task(self._root)
        task.signals.done.connect(self._done)
        task.signals.failed.connect(self._failed)
        self.runner.submit(task)

    def _done(self, rows):
        self._rows = list(rows or [])
        self._fill()
        n_ren = sum(1 for r in self._rows if r.get("record_type") == "rename")
        n_ext = len(self._rows) - n_ren
        text = "已读取 %d 份记录（重命名 %d · 文件提取 %d）" % (len(self._rows), n_ren, n_ext)
        self.notice.set_text(text, "info")
        if self._rows:
            self.notice.hide()
        self.status_message.emit(text)

    def _failed(self, message):
        self._rows = []
        self._fill()
        self.notice.set_text("读取日志失败：%s" % message, "error")
        self.notice.show()

    def _fill(self):
        while self.body_lay.count():
            item = self.body_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for row in self._rows:
            self.body_lay.addWidget(self._row(row))
        fit_scroll_content(self.body_scroll)
        if not self._rows:
            tip = Label("还没有执行记录；执行一次重命名或文件提取后会出现在这里。"
                        if not self._root else "这个目录里还没有执行记录。",
                        12, 400, C["t3"])
            tip.setAlignment(Qt.AlignCenter)
            self.body_lay.addWidget(tip, 1)
        else:
            self.body_lay.addStretch(1)
        n_ren = sum(1 for r in self._rows if r.get("record_type") == "rename")
        n_ext = len(self._rows) - n_ren
        scope = "全部目录" if not self._root else "当前目录"
        self.meta.setText("%s · 共 %d 条（重命名 %d · 提取 %d）"
                          % (scope, len(self._rows), n_ren, n_ext))
        self.meta.setToolTip("重命名日志：%s\n提取日志：%s"
                             % (A.backup_dir(), EA.rollback_dir()))
        self.foot_left.setText(
            "共 %d 条记录（重命名 %d · 文件提取 %d）· 撤销各自走对应引擎，不互相覆盖"
            % (len(self._rows), n_ren, n_ext))
        self.foot_right.setText("Name_backup 与 Extract/Rollback_Log")

    def _row(self, row):
        is_extract = row.get("record_type") == "extract"
        pairs = row.get("pairs") or []
        w = QWidget()
        w.setObjectName("LRow")
        w.setAttribute(Qt.WA_StyledBackground, True)
        w.setFixedHeight(62)
        lay = hbox(w, (14, 0, 14, 0), 0)

        when = Label(str(row.get("time") or "未记录"), 12, 400, C["t1"])
        when.setFixedWidth(150)
        # 类型徽章：一眼区分两类记录，不靠颜色单独表意
        kind = Badge("文件提取" if is_extract else "文件重命名",
                     "info" if is_extract else "muted")
        kind.setFixedWidth(76)
        # 目标目录：日志现在全存在一个文件夹里，这一列必须看得出是哪个目录
        root = str(row.get("root") or "")
        target = Elided(root, 12, 400, C["t1"])
        target.setToolTip(root)
        target.setMinimumWidth(0)

        count = Label("%d 项" % (row.get("count") or 0), 12, 400, C["t2"])
        count.setFixedWidth(80)
        count.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        count.setContentsMargins(0, 0, 10, 0)

        res_w = QWidget()
        rl = hbox(res_w, (8, 0, 0, 0), 0)
        rl.addWidget(self._result(row))
        rl.addStretch(1)

        log = Elided(str(row.get("name") or ""), 11, 400, C["t2"])
        log.setFixedWidth(200)
        log.setToolTip("日志文件：%s" % row.get("path", ""))

        act_w = QWidget()
        act_w.setFixedWidth(110)
        al = hbox(act_w, 0, 0)
        al.addStretch(1)
        # 提取日志已撤销 / 重命名日志无 pairs → 不显示撤销按钮
        already_undone = is_extract and row.get("state") == "undone"
        if pairs and not already_undone:
            b = QPushButton("撤销")
            b.setFont(F(12))
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet("QPushButton{border:0;background:transparent;color:%s;padding:0;}"
                            "QPushButton:hover{text-decoration:underline;}" % C["t1"])
            b.clicked.connect(lambda: self._undo(row))
            al.addWidget(b, 0, Qt.AlignRight | Qt.AlignVCenter)
        else:
            al.addWidget(Label("—", 12, 400, C["t4"]), 0, Qt.AlignRight | Qt.AlignVCenter)

        lay.addWidget(when, 0)
        lay.addWidget(kind, 0, Qt.AlignVCenter)
        lay.addWidget(target, 1)
        lay.addWidget(count, 0)
        lay.addWidget(res_w, 1)
        lay.addWidget(log, 0)
        lay.addWidget(act_w, 0)
        return w

    @staticmethod
    def _result(row):
        # 文件提取日志：result 是引擎写实的结果串（completed / stopped /
        # completed_with_errors:N/M / undone / undo_partial:…），state 是撤销状态。
        if row.get("record_type") == "extract":
            result = str(row.get("result") or "")
            state = str(row.get("state") or "active")
            parts = []
            if result.startswith("completed"):
                parts.append("提取完成")
            elif result == "stopped":
                parts.append("已停止")
            elif result == "undone":
                parts.append("已撤销")
            elif result.startswith("undo_partial"):
                parts.append("部分撤销")
            elif result == "running":
                parts.append("执行中")
            else:
                parts.append(result or "无结果")
            if "completed_with_errors" in result:
                parts.append("有失败")
            if state == "partial":
                parts.append("部分")
            elif state == "undone":
                parts.append("已撤销")
            text = " · ".join(parts)
            tone = C["err"] if "有失败" in parts else (
                C["warn"] if state in ("partial",) or result == "stopped" else C["ok"])
            return Label(text, 12, 500, tone)
        # 重命名日志：优先显示**执行阶段**的真实成败（schema 5 起写进日志的
        # exec 段）；老日志没有这一段，才回落到校验阶段的 summary。
        ex = row.get("exec") or {}
        if isinstance(ex, dict) and ("done" in ex or "failed" in ex):
            done = int(ex.get("done") or 0)
            failed = int(ex.get("failed") or 0)
            text = "成功 %d · 失败 %d" % (done, failed) if failed else "成功 %d" % done
            return Label(text, 12, 500, C["err"] if failed else C["ok"])
        summary = row.get("summary") or {}
        parts = []
        tone = C["t2"]
        labels = {"ok": "成功", "same": "名称未变", "conflict": "冲突",
                  "error": "失败", "skip": "跳过"}
        for key, color in (("ok", C["ok"]), ("same", C["t2"]), ("conflict", C["warn"]),
                           ("error", C["err"]), ("skip", C["t2"])):
            n = int(summary.get(key, 0) or 0)
            if not n:
                continue
            if key in ("conflict", "error"):
                tone = color
            elif key == "ok" and tone == C["t2"]:
                tone = C["ok"]
            parts.append("%s %d" % (labels[key], n))
        text = " · ".join(parts) if parts else "无统计信息"
        return Label(text, 12, 500 if parts else 400, tone)

    # ---------------- 撤销 ----------------
    def _undo(self, row):
        is_extract = row.get("record_type") == "extract"
        pairs = row.get("pairs") or []
        if not pairs:
            return
        if is_extract:
            msg = ("类型：文件提取\n目标目录：%s\n日志：%s\n执行时间：%s\n可还原 %d 项。\n\n"
                   "只撤销实际执行成功的项；目标被后续修改或原位置已有文件时跳过并报告，"
                   "不会强制覆盖。撤销失败会保留日志与备份。"
                   % (row.get("root") or "未记录", row.get("name", ""),
                      row.get("time") or "未记录", len(pairs)))
            title = "撤销文件提取"
        else:
            msg = ("类型：文件重命名\n目标目录：%s\n日志：%s\n执行时间：%s\n可还原 %d 项。\n\n"
                   "撤销会把这些对象改回原名；目标已存在时会跳过而不是覆盖。"
                   % (row.get("root") or "未记录", row.get("name", ""),
                      row.get("time") or "未记录", len(pairs)))
            title = "撤销重命名"
        if QMessageBox.question(self, title, msg,
                                QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) != QMessageBox.Yes:
            return
        if self.runner.busy():
            return
        if is_extract:
            task = W.history_extract_undo_task(str(row.get("path") or ""))
        else:
            task = W.undo_task(str(row.get("path") or ""))
        task.signals.done.connect(lambda r, ext=is_extract: self._undo_done(r, ext))
        task.signals.failed.connect(
            lambda m: (self.notice.set_text("撤销失败：%s" % m, "error"), self.notice.show()))
        self.runner.submit(task)

    def _undo_done(self, result, is_extract=False):
        if is_extract:
            r = result if isinstance(result, dict) else {}
            complete = bool(r.get("complete"))
            text = ("文件提取撤销完成：成功 %d · 跳过 %d · 失败 %d · 恢复目录 %d"
                    % (r.get("succeeded", 0), r.get("skipped", 0),
                       r.get("failed", 0), r.get("restored_dirs", 0)))
            if not complete:
                text += "（日志与备份已保留，可再次撤销）"
            tone = "success" if complete else "warn"
        else:
            ok, errors = result if isinstance(result, tuple) and len(result) == 2 else (False, [])
            errors = list(errors or [])
            text = "重命名撤销完成" if ok else "重命名撤销有 %d 处未完成" % len(errors)
            tone = "success" if ok else "error"
        self.notice.set_text(text, tone)
        self.notice.show()
        self.status_message.emit(text)
        # 撤销改了磁盘：两类页面各自的旧清单都失效，通知它们刷新。
        win = self.window()
        ws = getattr(win, "page_ws", None)
        if ws is not None and hasattr(ws, "mark_scan_outdated"):
            ws.mark_scan_outdated("撤销完成，正在按磁盘现状刷新预览…")
        ep = getattr(win, "page_extract", None)
        if ep is not None and hasattr(ep, "bundle"):
            ep.bundle = None
            ep._refresh_ui()
        self.reload()

    def open_dir(self):
        path = A.backup_dir()
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)  # noqa: S606
        except Exception as exc:
            self.notice.set_text("打不开资源管理器：%s" % exc, "warn")
            self.notice.show()

    def shutdown(self):
        self.runner.wait(2000)
