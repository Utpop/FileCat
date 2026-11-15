# -*- coding: utf-8 -*-
"""ui.pages.presets —— 「加载预设」弹出的独立窗口（选择 / 编辑 / 删除预设）。

原先是主窗口的第 3 页；3.0.0 起改为由「文件重命名」页的「加载预设」按钮
弹出 QDialog，主窗口不再保留预设页。数据读写仍全走 engine_adapter（A），
不碰引擎内部结构。
"""

from __future__ import annotations

import os
from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QInputDialog, QMessageBox, QPushButton,
                                QWidget)

from adapters import engine_adapter as A
from ui import prefs
from ui.base import (C, F, Button, Card, Elided, Label, NoticeBar, Strip,
                      fit_scroll_content, grid_row, hbox, hline, scroll_area, vbox)
from ui.pages.workspace import Item


class PresetOps(QWidget):
    """操作列：应用 / 编辑 / 删除（或打开目录）右对齐。"""

    def __init__(self, row, parent=None):
        super().__init__(parent)
        self.setFixedWidth(190)
        lay = hbox(self, 0, 14)
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

        def link(text, slot, color=None, enabled=True):
            b = QPushButton(text)
            b.setFont(F(12, 500 if text == "应用" else 400))
            b.setCursor(Qt.PointingHandCursor if enabled else Qt.ArrowCursor)
            b.setEnabled(enabled)
            b.setStyleSheet("QPushButton{border:0;background:transparent;color:%s;padding:0;}"
                            "QPushButton:hover{text-decoration:%s;}"
                            % (color or C["t1"], "underline" if enabled else "none"))
            b.clicked.connect(slot)
            return b

        lay.addWidget(link("应用", row["apply"]), 0, Qt.AlignRight | Qt.AlignVCenter)
        # 内置预设也能「编辑」：改名 + 说明后另存为自定义，不改内置数据
        lay.addWidget(link("编辑", row["edit"]), 0, Qt.AlignRight | Qt.AlignVCenter)
        can_del = row["kind"] in ("custom", "builtin")
        lay.addWidget(link("删除", row["delete"],
                           C["err"] if can_del else C["t4"], can_del),
                      0, Qt.AlignRight | Qt.AlignVCenter)


class PresetDialog(QDialog):
    """选择 / 编辑预设的额外窗口。

    applied(preset) 信号：选中「应用」时把 Preset 交给调用方（主窗口），
    由它调用 WorkspacePage.apply_preset()，对话框随后关闭。
    """

    applied = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("加载预设")
        self.setMinimumSize(760, 520)
        self.setObjectName("Root")
        self.setStyleSheet("#Root{background:%s;}" % C["white"])
        self._rows: List[dict] = []

        outer = vbox(self, (18, 16, 18, 12), 12)
        title = Label("加载预设", 20, 600, C["t1"])
        outer.addWidget(title)
        outer.addWidget(Label("规则链 + 扫描 / 编号选项的命名组合；应用只替换配置，不改动任何文件",
                              12, 400, C["t2"]))

        bar = hbox(None, 0, 8)
        restore_btn = Button("恢复内置预设", "refresh", "mid")
        restore_btn.clicked.connect(self._restore_builtin)
        self.restore_btn = restore_btn
        bar.addWidget(restore_btn)
        reload_btn = Button("刷新", "refresh", "mid")
        reload_btn.clicked.connect(self.reload)
        bar.addWidget(reload_btn)
        open_btn = Button("打开预设目录", "open", "mid")
        open_btn.clicked.connect(self.open_dir)
        bar.addWidget(open_btn)
        bar.addWidget(QWidget(), 1)
        close_btn = Button("关闭", "x", "mid")
        close_btn.clicked.connect(self.reject)
        bar.addWidget(close_btn)
        outer.addLayout(bar)

        self.notice = NoticeBar("内置预设可删除（只是隐藏，可恢复）；自定义预设为 .json，"
                                "「编辑」可改名与说明")
        outer.addWidget(self.notice)

        card = Card()
        lay = vbox(card, 0, 0)
        lay.addWidget(grid_row([Item(("预设名称", 240)), Item(("来源", 90)),
                                Item(("规则数", 70), align_right=True), Item("说明"),
                                Item(("操作", 190), align_right=True)],
                               36, pad=(0, 14), holder=Strip(C["s1"])))
        lay.addWidget(hline())

        self.body = QWidget()
        self.body_lay = vbox(self.body, 0, 0)
        self.body_scroll = scroll_area(self.body, min_height=56 * 3)
        lay.addWidget(self.body_scroll, 1)
        lay.addWidget(hline())

        foot = Strip(C["s1"])
        foot.setFixedHeight(40)
        fl = hbox(foot, (14, 0, 14, 0), 8)
        self.foot_left = Label("", 12, 400, C["t2"])
        fl.addWidget(self.foot_left, 1)
        fl.addWidget(Label("双击一行也可直接应用", 11, 400, C["t3"]))
        lay.addWidget(foot)
        outer.addWidget(card, 1)
        self.reload()

    # ---------------- 数据 ----------------
    def _hidden_builtins(self) -> List[str]:
        return [n for n in (prefs.load().get("deleted_builtin") or []) if n]

    def _set_hidden_builtins(self, names: List[str]):
        data = prefs.load()
        data["deleted_builtin"] = sorted(set(names))
        prefs.save(data)

    def reload(self):
        rows: List[dict] = []
        hidden = set(self._hidden_builtins())
        for name, preset in A.builtin_presets():
            if name in hidden:
                continue
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
        self.restore_btn.setVisible(bool(hidden))

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
        w.setCursor(Qt.PointingHandCursor)
        lay = hbox(w, (14, 0, 14, 0), 0)

        name = Elided(row["name"], 13, 500, C["t1"])
        name.setFixedWidth(240)
        color = {"custom": C["info"], "bad": C["err"]}.get(row["kind"], C["t2"])
        src = Elided(row["source"], 12, 500 if row["kind"] != "builtin" else 400, color)
        src.setFixedWidth(90)
        num = Label(str(row["count"]) if row["count"] else "—", 12, 400, C["t2"])
        num.setFixedWidth(70)
        num.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        num.setContentsMargins(0, 0, 12, 0)
        note = Elided(row["note"], 12, 400, C["err"] if row["kind"] == "bad" else C["t2"])
        note.setContentsMargins(8, 0, 0, 0)

        bind = dict(row)
        bind.update({"apply": lambda: self._apply(row),
                     "edit": lambda: self._edit(row),
                     "delete": lambda: self._delete(row),
                     "open_dir": self.open_dir})
        lay.addWidget(name, 0)
        lay.addWidget(src, 0)
        lay.addWidget(num, 0)
        lay.addWidget(note, 1)
        lay.addWidget(PresetOps(bind), 0)
        # 双击整行 = 应用
        w.mouseDoubleClickEvent = lambda _e, r=row: self._apply(r)
        return w

    # ---------------- 动作 ----------------
    def _apply(self, row):
        if row["preset"] is None:
            return
        self.applied.emit(row["preset"])
        self.accept()

    def _edit(self, row):
        """编辑预设：改名 + 说明。

        自定义预设覆盖原文件；内置预设另存为一份新的自定义预设（内置数据不动）。
        规则链本身仍在工作台里编辑，这里只管元数据。
        """
        if row["preset"] is None:
            return
        name, ok = QInputDialog.getText(self, "编辑预设名称", "预设名称：",
                                        text=row["name"])
        if not ok or not name.strip():
            return
        note, ok2 = QInputDialog.getText(self, "编辑预设说明", "说明：",
                                         text=row["note"] or "")
        if not ok2:
            return
        preset = row["preset"]
        preset.name = name.strip()
        preset.note = note.strip()
        try:
            if row["kind"] == "custom" and row["path"]:
                path = A.save_preset(preset, row["path"])
            else:
                path = A.save_preset(preset)   # 内置 → 另存为自定义
        except Exception as exc:
            self.notice.set_text("保存失败：%s" % exc, "error")
            self.notice.show()
            return
        self.notice.set_text("已保存预设：%s" % os.path.basename(path), "success")
        self.notice.show()
        self.reload()

    def _delete(self, row):
        if row["kind"] not in ("custom", "builtin"):
            return
        if row["kind"] == "builtin":
            if QMessageBox.question(
                    self, "删除内置预设",
                    "确认删除内置预设「%s」？\n\n它不会被真正删除，随时可用"
                    "「恢复内置预设」找回。" % row["name"],
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No) != QMessageBox.Yes:
                return
            self._set_hidden_builtins(self._hidden_builtins() + [row["name"]])
            self.notice.set_text("已隐藏内置预设「%s」" % row["name"], "success")
            self.notice.show()
            self.reload()
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

    def _restore_builtin(self):
        self._set_hidden_builtins([])
        self.notice.set_text("已恢复全部内置预设", "success")
        self.notice.show()
        self.reload()

    def open_dir(self):
        path = A.preset_dir()
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)  # noqa: S606
        except Exception as exc:
            self.notice.set_text("打不开预设目录：%s" % exc, "warn")
            self.notice.show()
