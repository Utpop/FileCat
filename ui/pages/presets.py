# -*- coding: utf-8 -*-
"""ui.pages.presets —— 预设页（真实读写 %APPDATA%\\NameCat\\presets）。"""

from __future__ import annotations

import os
from typing import List

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QMessageBox, QPushButton, QWidget

from adapters import engine_adapter as A
from ui.base import (C, F, Button, Card, Elided, Label, NoticeBar, PageHead, Strip,
                     fit_scroll_content, grid_row, hbox, hline, scroll_area, vbox)
from ui.pages.workspace import Item


class PresetOps(QWidget):
    """操作列：应用右边缘共线，删除/打开目录右对齐。"""

    def __init__(self, row, parent=None):
        super().__init__(parent)
        self.setFixedWidth(150)
        lay = hbox(self, 0, 18)
        lay.addStretch(1)
        if row["kind"] == "bad":
            b = QPushButton("打开目录")
            b.setFont(F(12))
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet("QPushButton{border:0;background:transparent;color:%s;padding:0;}"
                            "QPushButton:hover{text-decoration:underline;}" % C["t2"])
            b.clicked.connect(row["open_dir"])
            lay.addWidget(b, 0, Qt.AlignRight | Qt.AlignVCenter)
            return

        apply_btn = QPushButton("应用")
        apply_btn.setFont(F(12, 500))
        apply_btn.setFixedWidth(24)
        apply_btn.setCursor(Qt.PointingHandCursor)
        apply_btn.setStyleSheet("QPushButton{border:0;background:transparent;color:%s;padding:0;}"
                                "QPushButton:hover{text-decoration:underline;}" % C["t1"])
        apply_btn.clicked.connect(row["apply"])
        lay.addWidget(apply_btn, 0, Qt.AlignRight | Qt.AlignVCenter)

        color = C["err"] if row["kind"] == "custom" else C["t4"]
        del_btn = QPushButton("删除")
        del_btn.setFont(F(12))
        del_btn.setFixedWidth(24)
        del_btn.setStyleSheet("QPushButton{border:0;background:transparent;color:%s;padding:0;}"
                              "QPushButton:hover{text-decoration:%s;}"
                              % (color, "underline" if row["kind"] == "custom" else "none"))
        del_btn.setEnabled(row["kind"] == "custom")
        del_btn.setCursor(Qt.PointingHandCursor if row["kind"] == "custom" else Qt.ArrowCursor)
        del_btn.clicked.connect(row["delete"])
        lay.addWidget(del_btn, 0, Qt.AlignRight | Qt.AlignVCenter)


class PresetsPage(QWidget):

    status_message = Signal(str)
    apply_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Root")
        self._rows: List[dict] = []

        outer = vbox(self, (20, 18, 20, 12), 12)
        reload_btn = Button("刷新", "refresh")
        reload_btn.clicked.connect(self.reload)
        open_btn = Button("打开预设目录", "open")
        open_btn.clicked.connect(self.open_dir)
        outer.addWidget(PageHead(
            "预设", "规则链 + 扫描 / 编号选项的命名组合，不含根目录与任何密钥",
            [reload_btn, open_btn]))

        self.notice = NoticeBar("内置预设不可删除或覆盖；自定义预设保存为 .json，"
                                "文件损坏时该行标记「读取失败」")
        outer.addWidget(self.notice)

        card = Card()
        lay = vbox(card, 0, 0)
        lay.addWidget(grid_row([Item(("预设名称", 280)), Item(("来源", 110)),
                                Item(("规则数", 76), align_right=True), Item("说明"),
                                Item(("操作", 150), align_right=True)],
                               36, pad=(0, 14), holder=Strip(C["s1"])))
        lay.addWidget(hline())

        self.body = QWidget()
        self.body_lay = vbox(self.body, 0, 0)
        self.body_scroll = scroll_area(self.body, min_height=56 * 3)
        lay.addWidget(self.body_scroll, 1)
        lay.addWidget(hline())

        foot = Strip(C["s1"])
        foot.setFixedHeight(44)
        fl = hbox(foot, (14, 0, 14, 0), 8)
        self.foot_left = Label("", 12, 400, C["t2"])
        fl.addWidget(self.foot_left, 1)
        fl.addWidget(Label("应用预设只替换规则链与扫描选项，不改动任何文件", 11, 400, C["t3"]))
        lay.addWidget(foot)
        outer.addWidget(card, 1)
        self.reload()

    # ---------------- 数据 ----------------
    def reload(self):
        rows: List[dict] = []
        for name, preset in A.builtin_presets():
            rows.append({"name": name, "source": "内置", "kind": "builtin",
                         "count": len(preset.rules or []),
                         "note": preset.note or "内置预设", "preset": preset, "path": ""})
        broken = 0
        for path in A.preset_paths():
            try:
                preset = A.load_preset(path)
            except Exception as exc:
                broken += 1
                rows.append({"name": os.path.basename(path), "source": "读取失败",
                             "kind": "bad", "count": 0,
                             "note": "预设文件解析失败：%s" % type(exc).__name__,
                             "preset": None, "path": path})
                continue
            rows.append({"name": preset.name or os.path.splitext(os.path.basename(path))[0],
                         "source": "自定义", "kind": "custom",
                         "count": len(preset.rules or []),
                         "note": preset.note or "自定义预设", "preset": preset, "path": path})
        self._rows = rows
        self._fill()
        custom = sum(1 for r in rows if r["kind"] == "custom")
        builtin = sum(1 for r in rows if r["kind"] == "builtin")
        self.foot_left.setText("共 %d 个预设 · 内置 %d / 自定义 %d / 读取失败 %d"
                               % (len(rows), builtin, custom, broken))

    def _fill(self):
        while self.body_lay.count():
            item = self.body_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for row in self._rows:
            self.body_lay.addWidget(self._row(row))
        self.body_lay.addStretch(1)
        fit_scroll_content(self.body_scroll)

    def _row(self, row):
        w = QWidget()
        w.setObjectName("LRow")
        w.setAttribute(Qt.WA_StyledBackground, True)
        w.setFixedHeight(56)
        lay = hbox(w, (14, 0, 14, 0), 0)

        name = Elided(row["name"], 13, 500, C["t1"])
        name.setFixedWidth(280)
        color = {"custom": C["info"], "bad": C["err"]}.get(row["kind"], C["t2"])
        src = Elided(row["source"], 12, 500 if row["kind"] != "builtin" else 400, color)
        src.setFixedWidth(110)
        num = Label(str(row["count"]) if row["count"] else "—", 12, 400, C["t2"])
        num.setFixedWidth(76)
        num.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        num.setContentsMargins(0, 0, 16, 0)
        note = Elided(row["note"], 12, 400, C["err"] if row["kind"] == "bad" else C["t2"])
        note.setContentsMargins(8, 0, 0, 0)

        bind = dict(row)
        bind.update({"apply": lambda: self._apply(row),
                     "delete": lambda: self._delete(row),
                     "open_dir": self.open_dir})
        lay.addWidget(name, 0)
        lay.addWidget(src, 0)
        lay.addWidget(num, 0)
        lay.addWidget(note, 1)
        lay.addWidget(PresetOps(bind), 0)
        return w

    # ---------------- 动作 ----------------
    def _apply(self, row):
        if row["preset"] is None:
            return
        self.apply_requested.emit(row["preset"])
        self.status_message.emit("已应用预设：%s" % row["name"])

    def _delete(self, row):
        if row["kind"] != "custom":
            return
        if QMessageBox.question(self, "删除预设", "确认删除「%s」？" % row["name"],
                                QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) != QMessageBox.Yes:
            return
        ok = A.delete_preset(row["path"])
        self.notice.set_text("已删除预设「%s」" % row["name"] if ok
                             else "没有删掉这个预设：%s" % row["path"],
                             "success" if ok else "warn")
        self.notice.show()
        self.reload()

    def open_dir(self):
        path = A.preset_dir()
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)  # noqa: S606 - Windows 资源管理器
        except Exception as exc:
            self.notice.set_text("打不开预设目录：%s" % exc, "warn")
            self.notice.show()
