# -*- coding: utf-8 -*-
"""ui.pages.history —— 执行记录与撤销（读 _rename_logs，撤销走引擎日志）。"""

from __future__ import annotations

import os
from typing import List

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QMessageBox, QPushButton, QWidget

from adapters import engine_adapter as A
from ui.base import (C, F, Button, Card, Elided, Label, NoticeBar, PageHead, Strip,
                     fit_scroll_content, grid_row, hbox, hline, scroll_area,
                     svg_pixmap, vbox)
from ui.pages.workspace import Item
from workers import task_workers as W


class HistoryPage(QWidget):

    status_message = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Root")
        self._root = ""
        self._rows: List[dict] = []
        self.runner = W.TaskRunner(self, max_threads=1)

        outer = vbox(self, (20, 18, 20, 12), 12)
        refresh = Button("刷新", "refresh")
        refresh.clicked.connect(self.reload)
        open_btn = Button("打开日志目录", "open")
        open_btn.clicked.connect(self.open_dir)
        outer.addWidget(PageHead(
            "执行记录", "每次落盘都会生成撤销日志；撤销只在日志仍存在时可用",
            [refresh, open_btn]))

        self.notice = NoticeBar("日志按目录存放；先在「重命名工作台」选一个目录")
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
        self.path_label = Elided("尚未选择目录", 12, 500, C["t1"])
        self.path_label.setMinimumWidth(240)
        bl.addWidget(self.path_label, 0, Qt.AlignVCenter)
        self.meta = Elided("", 11, 400, C["t3"])
        self.meta.setMinimumWidth(0)
        bl.addWidget(self.meta, 1, Qt.AlignVCenter)
        bl.addWidget(QWidget(), 1)
        browse = Button("选择目录", kind="mid")
        browse.clicked.connect(self._browse)
        bl.addWidget(browse, 0, Qt.AlignVCenter)
        use_ws = Button("用工作台目录", kind="mid")
        use_ws.clicked.connect(self._use_workspace)
        bl.addWidget(use_ws, 0, Qt.AlignVCenter)
        outer.addWidget(bar)

        card = Card()
        lay = vbox(card, 0, 0)
        lay.addWidget(grid_row([Item(("执行时间", 150)), Item("使用的预设"),
                                Item(("数量", 80), align_right=True), Item("结果"),
                                Item(("日志状态", 230)),
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
        fl.addWidget(Label("日志保留 30 天", 11, 400, C["t3"]))
        lay.addWidget(foot)
        outer.addWidget(card, 1)
        self._fill()

    # ---------------- 目录 ----------------
    def set_root(self, root: str, reload: bool = True):
        root = (root or "").strip()
        if root == self._root and self._rows:
            return
        self._root = root
        self.path_label.setText(root or "尚未选择目录")
        if reload and root:
            self.reload()

    def _browse(self):
        from PySide6.QtWidgets import QFileDialog
        start = self._root or os.path.expanduser("~")
        path = QFileDialog.getExistingDirectory(self, "选择要查看执行记录的目录", start)
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

    # ---------------- 读取 ----------------
    def reload(self):
        if not self._root:
            self._rows = []
            self._fill()
            self.notice.set_text("还没有选择目录", "warn")
            self.notice.show()
            return
        if self.runner.busy():
            return
        task = W.logs_task(self._root)
        task.signals.done.connect(self._done)
        task.signals.failed.connect(self._failed)
        self.runner.submit(task)

    def _done(self, rows):
        self._rows = list(rows or [])
        self._fill()
        self.notice.set_text("已读取 %d 份日志" % len(self._rows), "info")
        if self._rows:
            self.notice.hide()
        self.status_message.emit("已读取 %d 份日志" % len(self._rows))

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
            tip = Label("这个目录里还没有撤销日志；执行一次重命名后会出现在这里。"
                        if self._root else "先选择一个目录。", 12, 400, C["t3"])
            tip.setAlignment(Qt.AlignCenter)
            self.body_lay.addWidget(tip, 1)
        else:
            self.body_lay.addStretch(1)
        if self._root:
            self.meta.setText("共 %d 条记录 · 保留最近 30 条" % len(self._rows))
            self.meta.setToolTip("日志目录：%s" % A.log_dir(self._root))
        else:
            self.meta.setText("")
        self.foot_left.setText("共 %d 条记录 · 撤销按日志逐条还原，已不存在的文件自动忽略"
                               % len(self._rows))

    def _row(self, row):
        pairs = row.get("pairs") or []
        w = QWidget()
        w.setObjectName("LRow")
        w.setAttribute(Qt.WA_StyledBackground, True)
        w.setFixedHeight(62)
        lay = hbox(w, (14, 0, 14, 0), 0)

        when = Label(str(row.get("time") or "未记录"), 12, 400, C["t1"])
        when.setFixedWidth(150)
        preset = Elided(str(row.get("note") or "—"), 13, 500, C["t1"])

        count = Label("%d 项" % (row.get("count") or 0), 12, 400, C["t2"])
        count.setFixedWidth(80)
        count.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        count.setContentsMargins(0, 0, 10, 0)

        res_w = QWidget()
        rl = hbox(res_w, (8, 0, 0, 0), 0)
        rl.addWidget(self._result(row))
        rl.addStretch(1)

        log = Elided(str(row.get("name") or ""), 11, 400, C["t2"])
        log.setFixedWidth(230)

        act_w = QWidget()
        act_w.setFixedWidth(110)
        al = hbox(act_w, 0, 0)
        al.addStretch(1)
        if pairs:
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
        lay.addWidget(preset, 1)
        lay.addWidget(count, 0)
        lay.addWidget(res_w, 1)
        lay.addWidget(log, 0)
        lay.addWidget(act_w, 0)
        return w

    @staticmethod
    def _result(row):
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
        pairs = row.get("pairs") or []
        if not pairs:
            return
        msg = ("日志：%s\n执行时间：%s\n可还原 %d 项。\n\n"
               "撤销会把这些对象改回原名；目标已存在时会跳过而不是覆盖。"
               % (row.get("name", ""), row.get("time") or "未记录", len(pairs)))
        if QMessageBox.question(self, "撤销重命名", msg,
                                QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) != QMessageBox.Yes:
            return
        if self.runner.busy():
            return
        task = W.undo_task(str(row.get("path") or ""))
        task.signals.done.connect(self._undo_done)
        task.signals.failed.connect(
            lambda m: (self.notice.set_text("撤销失败：%s" % m, "error"), self.notice.show()))
        self.runner.submit(task)

    def _undo_done(self, result):
        ok, errors = result if isinstance(result, tuple) and len(result) == 2 else (False, [])
        errors = list(errors or [])
        self.notice.set_text("撤销完成" if ok else "撤销有 %d 处未完成" % len(errors),
                             "success" if ok else "error")
        self.notice.show()
        self.status_message.emit("撤销完成" if ok else "撤销未完成")
        self.reload()

    def open_dir(self):
        path = A.log_dir(self._root) if self._root else ""
        if not path or not os.path.isdir(path):
            self.notice.set_text("这个目录还没有日志文件夹", "info")
            self.notice.show()
            return
        try:
            os.startfile(path)  # noqa: S606
        except Exception as exc:
            self.notice.set_text("打不开资源管理器：%s" % exc, "warn")
            self.notice.show()

    def shutdown(self):
        self.runner.wait(2000)
