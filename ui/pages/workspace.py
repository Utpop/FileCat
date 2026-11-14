# -*- coding: utf-8 -*-
"""
ui.pages.workspace —— 重命名工作台（新版界面）

三段状态：未扫描 / 只扫描 / 已算名。
所有数据来自 adapters.engine_adapter：界面不自己算名字，也不自己数统计。
"""

from __future__ import annotations

import math
import os
from typing import List, Optional

from PySide6.QtCore import (QAbstractAnimation, QPoint, QRect, QSize, Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen, QPolygonF, QTransform
from PySide6.QtWidgets import (QDialog, QFileDialog, QFrame, QGraphicsOpacityEffect,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu,
                               QMessageBox, QPushButton, QSizePolicy, QVBoxLayout,
                               QWidget)

from adapters import engine_adapter as A
from namecat_py import rules as R
from ui.base import (C, F, R_XS, Badge, Button, Card, Check, Clickable, Chip,
                     Elided, EmptyState, Label, PageHead, Select, Strip,
                     fit_scroll_content, grid_row, hbox, hline, icon, scroll_area,
                     spacer, svg_pixmap, vbox)
from workers import task_workers as W

STATUS_TONE = {"ok": C["info"], "same": C["t2"], "conflict": C["warn"],
               "error": C["err"], "skip": C["t2"]}

# 扫描栏右侧状态文案的三种颜色：灰 = 默认/淡出后，蓝 = 扫描中，深灰 = 刚扫完
SCAN_GRAY = C["t3"]        # 色板里的 #999 档灰
SCAN_DARK = C["t2"]        # 深灰文字（扫完 5 秒内）


class Item(QLabel):
    """表头单元格：定宽元组或 1fr。"""

    def __init__(self, spec, align_right=False, parent=None):
        if isinstance(spec, tuple):
            text, width = spec
            super().__init__(text, parent)
            self.setFixedWidth(width)
        else:
            super().__init__(spec, parent)
        self.setFont(F(11, 500))
        self.setStyleSheet("color:%s;background:transparent;" % C["t2"])
        self.setMinimumWidth(0)
        if align_right:
            self.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.setContentsMargins(0, 0, 10, 0)


class Combo(Select):
    """带下拉菜单的只读选择器（外观沿用设计稿）。"""

    changed = Signal(str)

    def __init__(self, options, value="", width=148, height=32, size=12, parent=None):
        self._opts = list(options)
        text = dict(self._opts).get(value, value or "")
        super().__init__(text, width, height, size, parent)
        self._value = value

    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        self._value = value
        text = dict(self._opts).get(value, value)
        self.label.setText(text)
        self.label.setToolTip(text)

    def set_options(self, options) -> None:
        """换掉整组选项（模型列表变了以后用），并把当前值重新贴上去。"""
        self._opts = list(options)
        self.set_value(self._value)

    def mousePressEvent(self, _e):  # noqa: N802
        menu = QMenu(self)
        menu.setFont(F(12))
        for val, text in self._opts:
            act = menu.addAction(text)
            act.setCheckable(True)
            act.setChecked(val == self._value)
            act.triggered.connect(lambda _c=False, v=val: self._pick(v))
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def _pick(self, value):
        self.set_value(value)
        self.changed.emit(value)


class PathInput(QFrame):
    """36 高可编辑路径输入框。"""

    edited = Signal()

    def __init__(self, text="", icon_name="folder", height=36, size=13, parent=None):
        super().__init__(parent)
        self.setObjectName("Input")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(height)
        lay = hbox(self, (10, 0, 10, 0), 8)
        if icon_name:
            ic = QLabel()
            ic.setPixmap(svg_pixmap(icon_name, C["t3"], 15))
            ic.setFixedSize(15, 15)
            lay.addWidget(ic, 0, Qt.AlignVCenter)
        self.edit = QLineEdit(text)
        self.edit.setFont(F(size))
        self.edit.setFrame(False)
        self.edit.setStyleSheet("QLineEdit{background:transparent;border:0;color:%s;}" % C["t1"])
        self.edit.setPlaceholderText("选择要处理的文件夹…")
        self.edit.editingFinished.connect(self.edited)
        lay.addWidget(self.edit, 1, Qt.AlignVCenter)

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, value: str) -> None:  # noqa: N802
        self.edit.setText(value or "")


class FieldEdit(QFrame):
    """30 高紧凑输入框。"""

    edited = Signal()

    def __init__(self, text="", width=0, height=30, size=12, parent=None):
        super().__init__(parent)
        self.setObjectName("Input")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(height)
        if width:
            self.setFixedWidth(width)
        lay = hbox(self, (8, 0, 8, 0), 0)
        self.edit = QLineEdit(str(text))
        self.edit.setFont(F(size))
        self.edit.setFrame(False)
        self.edit.setStyleSheet("QLineEdit{background:transparent;border:0;color:%s;}" % C["t1"])
        self.edit.editingFinished.connect(self.edited)
        lay.addWidget(self.edit, 1)

    def text(self) -> str:
        return self.edit.text()

    def setText(self, value) -> None:  # noqa: N802
        self.edit.setText(str(value or ""))


def op_button(name, color, tip, slot):
    b = QPushButton()
    b.setIcon(icon(name, color, 14))
    b.setIconSize(QSize(14, 14))
    b.setFixedSize(20, 20)
    b.setCursor(Qt.PointingHandCursor)
    b.setToolTip(tip)
    b.setStyleSheet("QPushButton{border:0;background:transparent;border-radius:4px;}"
                    "QPushButton:hover{background:%s;}" % C["sel"])
    b.clicked.connect(lambda: slot())
    return b


class RuleRow(QWidget):
    picked = Signal(str)
    toggled_on = Signal(str, bool)
    moved = Signal(str, int)
    cloned = Signal(str)
    removed = Signal(str)

    def __init__(self, rule, index, selected=False, parent=None):
        super().__init__(parent)
        self.rule = rule
        self.setObjectName("RuleRow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setProperty("selected", "true" if selected else "false")
        # 名称和效果横排：列表很宽，竖排两行既占高度又浪费横向面积
        self.setFixedHeight(36)
        self.setCursor(Qt.PointingHandCursor)
        lay = hbox(self, (8, 0, 8, 0), 8)

        self.cb = Check(bool(rule.enabled))
        self.cb.toggled.connect(lambda on: self.toggled_on.emit(rule.id, on))
        lay.addWidget(self.cb, 0, Qt.AlignVCenter)

        no = Label("%02d" % index, 11, 400, C["t3"])
        no.setFixedWidth(10)
        no.setAlignment(Qt.AlignCenter)
        no.setText("%d" % index)
        lay.addWidget(no, 0, Qt.AlignVCenter)

        # 选中行：标题用品牌蓝（底色由 QSS 的 #RuleRow[selected] 给）
        main = hbox(None, 0, 8)
        self.name = Elided(A.rule_title(rule), 12, 500,
                           C["t3"] if not rule.enabled else
                           (C["brand"] if selected else C["t1"]))
        self.name.setMinimumWidth(48)     # 名称先占住一小段，效果占剩下的全部宽度
        main.addWidget(self.name, 0, Qt.AlignVCenter)
        self.sub = Elided(A.rule_summary(rule), 11, 400,
                          C["t2"] if rule.enabled else "#C4C4C4")
        main.addWidget(self.sub, 1, Qt.AlignVCenter)
        lay.addLayout(main, 1)

        if not rule.enabled:
            lay.addWidget(Badge("已停用", "mut2"), 0, Qt.AlignVCenter)

        ops = hbox(None, 0, 2)
        col = C["t3"] if rule.enabled else "#EBEBEB"
        ops.addWidget(op_button("up", col, "上移", lambda: self.moved.emit(rule.id, -1)))
        ops.addWidget(op_button("arrdown", col, "下移", lambda: self.moved.emit(rule.id, 1)))
        ops.addWidget(op_button("copy", col, "复制", lambda: self.cloned.emit(rule.id)))
        ops.addWidget(op_button("trash", col, "删除", lambda: self.removed.emit(rule.id)))
        lay.addLayout(ops, 0)

        grip = QLabel()
        grip.setPixmap(svg_pixmap("grip", C["t4"], 12))
        grip.setFixedSize(12, 14)
        lay.addWidget(grip, 0, Qt.AlignVCenter)

    def mousePressEvent(self, _e):  # noqa: N802
        self.picked.emit(self.rule.id)


class RulePicker(QDialog):
    """添加规则：按分组列出全部规则类型。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加规则")
        self.setMinimumSize(520, 560)
        self.setStyleSheet("QDialog{background:%s;}" % C["white"])
        self._kind = ""
        root = vbox(self, 16, 10)

        search = QLineEdit()
        search.setPlaceholderText("搜索规则，例如「正则」「编号」")
        search.setFont(F(12))
        search.setFixedHeight(34)
        search.setStyleSheet("QLineEdit{border:1px solid %s;border-radius:4px;padding:0 10px;"
                             "background:%s;color:%s;}" % (C["line2"], C["white"], C["t1"]))
        root.addWidget(search)

        body = QWidget()
        body.setStyleSheet("background:%s;" % C["white"])
        self.list_lay = vbox(body, 0, 4)
        self.list_lay.addStretch(1)
        # 规则一多（现在 28 种）就装不进固定高度的弹窗，必须内滚：
        # 否则 Qt 会把每个分组压扁，行与行互相重叠
        self._scroll = scroll_area(body)
        root.addWidget(self._scroll, 1)

        row = hbox(None, 0, 8)
        row.addStretch(1)
        cancel = Button("取消")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        root.addLayout(row)

        self._rows = []
        self._groups = []
        for group, kinds in A.RULE_GROUPS:
            holder = QWidget()
            gl = vbox(holder, 0, 2)
            gl.addWidget(Label(group, 11, 500, C["t3"]))
            for kind in kinds:
                b = QPushButton(A.rule_kind_text(kind))
                b.setFont(F(12))
                b.setFixedHeight(28)
                b.setCursor(Qt.PointingHandCursor)
                b.setToolTip(A.rule_kind_note(kind))
                b.setStyleSheet("QPushButton{border:0;background:transparent;text-align:left;"
                                "padding-left:8px;color:%s;}"
                                "QPushButton:hover{background:%s;border-radius:4px;}"
                                % (C["t1"], C["s2"]))
                b.clicked.connect(lambda _c=False, k=kind: self._take(k))
                gl.addWidget(b)
                self._rows.append((kind, b))
            self.list_lay.insertWidget(self.list_lay.count() - 1, holder)
            self._groups.append((holder, list(kinds)))
        search.textChanged.connect(self._filter)
        fit_scroll_content(self._scroll)

    def _filter(self, text):
        text = (text or "").strip().lower()
        hits = {}
        for kind, b in self._rows:
            hit = (not text) or text in A.rule_kind_text(kind).lower() \
                or text in kind.lower() or text in A.rule_kind_note(kind).lower()
            b.setVisible(hit)
            hits[kind] = hit
        # 一个分组里一条都没命中时，连分组标题一起收起，别留空标题
        for holder, kinds in self._groups:
            holder.setVisible(any(hits.get(k) for k in kinds))

    def _take(self, kind):
        self._kind = kind
        self.accept()

    def chosen(self) -> str:
        return self._kind


class AdvancedDialog(QDialog):
    """高级筛选：名称 / 扩展名 / 深度 / 文件头校验（排序方式在预览卡表头）。"""

    def __init__(self, state: A.FormState, parent=None):
        super().__init__(parent)
        self.setWindowTitle("高级筛选")
        self.setMinimumWidth(560)
        self.setStyleSheet("QDialog{background:%s;}" % C["white"])
        self.state = state
        root = vbox(self, 18, 10)

        def line(label, value, hint=""):
            row = hbox(None, 0, 10)
            lb = Label(label, 12, 400, C["t2"])
            lb.setFixedWidth(96)
            row.addWidget(lb, 0, Qt.AlignVCenter)
            ed = FieldEdit(value, height=32, size=12)
            row.addWidget(ed, 1)
            root.addLayout(row)
            if hint:
                root.addWidget(Label(hint, 11, 400, C["t3"]))
            return ed

        self.only_match = line("名称包含", state.only_match)
        self.exclude_match = line("名称排除", state.exclude_match)
        self.include_exts = line("仅这些扩展名", state.include_exts, "逗号分隔，例如 jpg, png")
        self.exclude_exts = line("排除扩展名", state.exclude_exts)

        row2 = hbox(None, 0, 10)
        lb2 = Label("递归深度", 12, 400, C["t2"])
        lb2.setFixedWidth(96)
        row2.addWidget(lb2, 0, Qt.AlignVCenter)
        self.depth = FieldEdit(str(state.max_depth), width=80, height=32)
        row2.addWidget(self.depth, 0)
        row2.addWidget(Label("上限 %d 层" % A.max_depth(), 11, 400, C["t3"]), 0)
        row2.addStretch(1)
        self.verify = Check(bool(state.verify_content))
        row2.addWidget(self.verify, 0, Qt.AlignVCenter)
        row2.addWidget(Label("校验文件头（魔数）", 12, 400, C["t1"]), 0, Qt.AlignVCenter)
        root.addLayout(row2)

        # 这两项不属于任何一条规则：扩展名由谁管、文件夹改不改名，作用在整条链上
        row3 = hbox(None, 0, 10)
        self.split_ext = Check(bool(state.split_ext))
        self.split_ext.setToolTip("关掉后，除「扩展名」规则外，其它规则只改名字主干")
        row3.addWidget(self.split_ext, 0, Qt.AlignVCenter)
        row3.addWidget(Label("扩展名也参与规则链", 12, 400, C["t1"]), 0, Qt.AlignVCenter)
        row3.addSpacing(20)
        self.rename_dirs = Check(bool(state.rename_dirs))
        self.rename_dirs.setToolTip("选「文件夹 / 全部」时自动打开")
        row3.addWidget(self.rename_dirs, 0, Qt.AlignVCenter)
        row3.addWidget(Label("同时重命名文件夹", 12, 400, C["t1"]), 0, Qt.AlignVCenter)
        row3.addStretch(1)
        root.addLayout(row3)

        root.addWidget(Label("图片：" + A.image_exts_text(), 11, 400, C["t3"]))
        root.addWidget(Label("视频：" + A.video_exts_text(), 11, 400, C["t3"]))

        btns = hbox(None, 0, 8)
        btns.addStretch(1)
        cancel = Button("取消")
        cancel.clicked.connect(self.reject)
        ok = Button("应用", kind="primary")
        ok.clicked.connect(self._apply)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        root.addLayout(btns)

    def _apply(self):
        s = self.state
        s.only_match = self.only_match.text().strip()
        s.exclude_match = self.exclude_match.text().strip()
        s.include_exts = self.include_exts.text().strip()
        s.exclude_exts = self.exclude_exts.text().strip()
        try:
            s.max_depth = max(1, min(int(self.depth.text() or 12), A.max_depth()))
        except ValueError:
            s.max_depth = A.max_depth()
        s.verify_content = self.verify.isChecked()
        s.split_ext = self.split_ext.isChecked()
        s.rename_dirs = self.rename_dirs.isChecked()
        self.accept()


class DropCard(Card):
    """预览卡：整块接住拖进来的文件 / 文件夹。

    空状态写着「拖入文件或文件夹开始」，所以拖放必须真的能用——卡片自己
    收 drop，子控件不收时会冒泡上来。
    """

    dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    @staticmethod
    def _paths(event):
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]

    def _highlight(self, on):
        self.setProperty("dropActive", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event):  # noqa: N802
        if self._paths(event):
            event.acceptProposedAction()
            self._highlight(True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event):  # noqa: N802
        self._highlight(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):  # noqa: N802
        self._highlight(False)
        paths = self._paths(event)
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.dropped.emit(paths)


# --------------------------------------------------------------------------- #
# 预览表：自绘 + 只画可见行
# --------------------------------------------------------------------------- #

_QCOL = {k: QColor(v) for k, v in C.items()}


def _qc(tone, default="#171717"):
    """语气色：既接受 C 里的 token 名，也接受 "#RRGGBB" 直给。"""
    if not tone:
        return _QCOL.get(default) or QColor(default)
    col = _QCOL.get(tone)
    if col is not None:
        return col
    return QColor(tone)


class RowHandle:
    """一行的轻量句柄（给「按行操作」的老调用点用，不再是控件树）。

    以前这里是一整行 13 个 QWidget，9000 行就是十几万个 QObject：建表要 100 秒，
    勾选/全选/重绘全部陪跑成秒级卡顿。现在一行只是一条数据，这个句柄只暴露
    `_cb` / `_item`，让「取勾选状态」「取消某一行」这类写法继续可用。
    """

    __slots__ = ("_table", "_index", "_cb", "_item", "_st")

    def __init__(self, table, index, item):
        self._table = table
        self._index = index
        self._item = item
        self._st = None                      # 状态格不再是控件（保留属性兼容）
        self._cb = _HandleCheck(table, index)


class _HandleCheck:
    """RowHandle._cb：形状像 QAbstractButton，实际只改表里的一个 bool。"""

    __slots__ = ("_table", "_index")

    def __init__(self, table, index):
        self._table = table
        self._index = index

    def isChecked(self):  # noqa: N802
        return self._table.is_checked(self._index)

    def setChecked(self, on):  # noqa: N802
        self._table.set_checked(self._index, bool(on))

    def blockSignals(self, _on):  # noqa: N802
        return False                     # 惰性行没有信号，这里什么都不用做

    def property(self, name):  # noqa: N802
        return self._table.row_key(self._index) if name == "nkey" else None


class PreviewTable(QWidget):
    """预览表格：数据驱动 + 自绘，只画视口里那十几行。

    这一层是 9000 文件卡顿的根治点：行不再是控件，而是预计算好的字符串与颜色；
    滚动、勾选、换预览都由「重画一小段矩形」完成，成本与总行数基本无关。
    """

    ROW_H = 34
    PAD = 8
    COL_NO, COL_KIND, COL_OLD, COL_NEW, COL_DIR, COL_ST = 56, 74, 0, 0, 200, 104

    toggled = Signal(int, bool)      # 行下标, 勾选后的状态

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self._header = None          # 表头行：列几何直接照它取，保证对齐
        self._rows = []              # 每行 7 段文本
        self._keys = []              # 每行的 normcase 路径
        self._on = []                # 每行勾选状态
        self._dim = []               # 每行是否置灰（跳过）
        self._new_tones = []         # 新名称/状态两列的语气色（token 名）
        self._st_tones = []
        self._hover = -1
        self._interactive = False    # 还没有计划时勾选框是灰的、点不动
        self._f11 = F(11)
        self._f12 = F(12)
        self._f12b = F(12, 500)
        self._m11 = QFontMetrics(self._f11)
        self._m12 = QFontMetrics(self._f12)
        self._m12b = QFontMetrics(self._f12b)

    # ---------------- 数据 ----------------
    def set_rows(self, rows, keys, checked, dim, new_tones, st_tones, interactive):
        """整表换数据。rows 的每项是 7 段文本，语气色单独按行存。"""
        self._rows = rows
        self._keys = keys
        self._on = list(checked)
        self._dim = dim
        self._new_tones = new_tones
        self._st_tones = st_tones
        self._interactive = bool(interactive)
        self._hover = -1
        self.setMinimumHeight(max(1, len(rows) * self.ROW_H))
        self.update()

    def row_count(self):
        return len(self._rows)

    def row_key(self, index):
        return self._keys[index] if 0 <= index < len(self._keys) else ""

    def is_checked(self, index):
        return bool(self._on[index]) if 0 <= index < len(self._on) else False

    def checked_count(self):
        return sum(1 for v in self._on if v)

    def checked_keys(self):
        return {self._keys[i] for i, on in enumerate(self._on) if on}

    def excluded_keys(self):
        return {self._keys[i] for i, on in enumerate(self._on) if not on}

    def set_checked(self, index, on, emit=True):
        if not (0 <= index < len(self._on)) or not self._interactive:
            return
        if self._on[index] == bool(on):
            return
        self._on[index] = bool(on)
        self._repaint_row(index)
        if emit:
            self.toggled.emit(index, bool(on))

    def set_all(self, on):
        if not self._interactive:
            return
        on = bool(on)
        if all(v == on for v in self._on):
            return
        self._on = [on] * len(self._on)
        self.update()

    def is_all(self):
        return bool(self._on) and all(self._on)

    def handles(self, items):
        """按行取轻量句柄（老调用点用）；每行一个对象，不建控件。"""
        return [RowHandle(self, i, it) for i, it in enumerate(items)]

    def refresh_tones(self, items):
        """条目状态被外部改过（勾选排除）后，只更新受影响两列的语气与文字。

        逐项读 status/note 是纯 Python 的 O(n) 字符串操作（9000 项约 10ms），
        而重画只发生视口内那十几行 —— 这是「勾选一行不再卡」的关键。
        """
        changed = False
        for i, it in enumerate(items):
            if i >= len(self._rows):
                break
            changed = self._apply_tone(i, it) or changed
        if changed:
            self.update()

    def refresh_tones_at(self, index, it):
        """只刷新一行（单行勾选走这里，O(1)）。"""
        if self._apply_tone(index, it):
            self._repaint_row(index)

    def _apply_tone(self, i, it):
        status = getattr(it, "status", "ok")
        tone = STATUS_TONE.get(status, C["t2"])
        skipped = status == "skip"
        no, kind, old, new, folder, _cur_st, cur_note = self._rows[i]
        # note 也要跟着刷新：被勾选排除后引擎会把 note 改成「按勾选排除」，
        # 只改状态列会让同一行两条信息互相矛盾。
        note = getattr(it, "note", "") or ("将按预览名落盘" if status == "ok" else "")
        if self._dim[i] == skipped and self._st_tones[i] == tone and cur_note == note:
            return False
        self._dim[i] = skipped
        self._st_tones[i] = tone
        self._rows[i] = (no, kind, old, new, folder, A.status_text(status), note)
        return True

    def repaint_row(self, index):
        self._repaint_row(index)

    def _repaint_row(self, index):
        if 0 <= index < len(self._rows):
            self.update(0, index * self.ROW_H, self.width(), self.ROW_H)
    # ---------------- 绘制 ----------------
    def _cols(self):
        """列几何：**直接取表头控件的真实几何**，不再自己重算一遍。

        表头是 8 个 QLabel（5 个定宽 + 3 个 1fr），由 Qt 布局分配宽度；自己按
        公式再算一遍总会差几个像素（1fr 的取整口径不同），长文件名就会在
        「新名称」左边的表头网格外面多吃一个字。对着表头读，永远严丝合缝。
        """
        hdr = self._header
        if hdr is not None:
            lay = hdr.layout()
            if lay is not None and lay.count() >= 8:
                xs, widths = [], []
                for i in range(8):
                    g = lay.itemAt(i).geometry()
                    xs.append(g.x())
                    widths.append(g.width())
                return xs, widths
        # 没有表头（自检里直接建表）时退回内部公式
        total_w = self.width() - 2 * self.PAD
        fixed = self.COL_NO + self.COL_KIND + self.COL_DIR + self.COL_ST
        flex = max(1, (total_w - fixed) // 3)
        box = 34
        xs = [self.PAD, self.PAD + box, 0, 0, 0, 0, 0, 0]
        xs[2] = xs[1] + self.COL_NO
        xs[3] = xs[2] + self.COL_KIND
        xs[4] = xs[3] + flex
        xs[5] = xs[4] + flex
        xs[6] = xs[5] + self.COL_DIR
        xs[7] = xs[6] + self.COL_ST
        end = self.width() - self.PAD
        widths = [box, self.COL_NO, self.COL_KIND, flex, flex, self.COL_DIR,
                  self.COL_ST, max(1, end - xs[7])]
        return xs, widths

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        # 控件是 opaque 的：不自己铺底，滚出可视区的空白会留残影
        p.fillRect(self.rect(), _QCOL["white"])
        if not self._rows:
            p.end()
            return
        xs, widths = self._cols()
        # 只画视口覆盖的那一段：range 范围由可见矩形算，与总行数无关
        vis = self.visibleRegion().boundingRect()
        if vis.isEmpty():
            vis = self.rect()
        first = max(0, vis.top() // self.ROW_H)
        last = min(len(self._rows), vis.bottom() // self.ROW_H + 1)
        hover_bg = _QCOL["s1"]
        for i in range(first, last):
            y = i * self.ROW_H
            if i == self._hover:
                p.fillRect(0, y, self.width(), self.ROW_H, hover_bg)
            no, kind, old, new, folder, st, note = self._rows[i]
            dim = self._dim[i]
            self._draw_check(p, xs[0], y, self._on[i], dim)
            self._text(p, self._f11, self._m11, no, xs[1], y, widths[1],
                       "t3" if dim else "t2")
            self._text(p, self._f12, self._m12, kind, xs[2], y, widths[2],
                       "t3" if dim else "t1")
            self._text(p, self._f12, self._m12, old, xs[3], y, widths[3],
                       "t3" if dim else "t2")
            self._text(p, self._f12b, self._m12b, new, xs[4], y, widths[4],
                       self._new_tone(i, dim))
            self._text(p, self._f12, self._m12, folder, xs[5], y, widths[5],
                       "t3" if dim else "t2")
            self._text(p, self._f12, self._m12, st, xs[6], y, widths[6],
                       self._st_tone(i))
            self._text(p, self._f11, self._m11, note, xs[7], y, widths[7], "t3")
        p.end()

    def _new_tone(self, index, dim):
        if dim:
            return "#C4C4C4"
        return self._new_tones[index] if index < len(self._new_tones) else "t1"

    def _st_tone(self, index):
        return self._st_tones[index] if index < len(self._st_tones) else "t2"

    def _text(self, p, font, metrics, text, x, y, w, tone):
        if not text or w <= 0:
            return
        txt = metrics.elidedText(text, Qt.ElideRight, max(0, w - 4))
        p.setFont(font)
        p.setPen(_qc(tone))
        p.drawText(QRect(x, y, max(0, w - 4), self.ROW_H),
                   Qt.AlignLeft | Qt.AlignVCenter, txt)

    def _draw_check(self, p, x, y, on, dim):
        """自绘 14×14 复选框（与 ui.base.Check 同一套画法）。"""
        cy = y + (self.ROW_H - 14) // 2
        r = QRect(x + 8, cy, 14, 14)
        p.setRenderHint(QPainter.Antialiasing, True)
        if on:
            p.setPen(Qt.NoPen)
            p.setBrush(_QCOL["t1"] if not dim else _QCOL["line2"])
            p.drawRoundedRect(r, R_XS, R_XS)
            pen = QPen(_QCOL["white"])
            pen.setWidthF(1.6)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.drawPolyline(QPolygonF([QPoint(r.x() + 3.6, r.y() + 7.6),
                                      QPoint(r.x() + 6.0, r.y() + 10.0),
                                      QPoint(r.x() + 10.6, r.y() + 4.8)]))
            p.setPen(Qt.NoPen)
        else:
            p.setPen(_QCOL["line2"])
            p.setBrush(_QCOL["white"])
            p.drawRoundedRect(r, R_XS, R_XS)

    # ---------------- 交互 ----------------
    def _row_at(self, pos):
        if not (0 <= pos.x() <= self.width()):
            return -1
        # 表本身被滚动到负坐标里，行号要用表内坐标算
        idx = pos.y() // self.ROW_H
        return int(idx) if 0 <= idx < len(self._rows) else -1

    def _in_check(self, pos):
        xs, _ws = self._cols()
        return xs[0] + 8 <= pos.x() <= xs[0] + 26

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() == Qt.LeftButton and self._interactive:
            idx = self._row_at(e.position().toPoint())
            if idx >= 0 and self._in_check(e.position().toPoint()):
                self.set_checked(idx, not self._on[idx])
                return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):  # noqa: N802
        pos = e.position().toPoint()
        idx = self._row_at(pos)
        if idx != self._hover:
            old, self._hover = self._hover, idx
            self._repaint_row(old)
            self._repaint_row(idx)
            if self._interactive and idx >= 0 and self._in_check(pos):
                self.setCursor(Qt.PointingHandCursor)
            else:
                self.unsetCursor()
        # 老版每个单元格的全文放在各自 tooltip 里；自绘后统一在这里给
        self.setToolTip(self._tooltip_at(idx, pos) if idx >= 0 else "")
        super().mouseMoveEvent(e)

    def _tooltip_at(self, idx, pos):
        if not (0 <= idx < len(self._rows)):
            return ""
        xs, widths = self._cols()
        for col in range(3, 8):
            if xs[col] <= pos.x() <= xs[col] + widths[col]:
                text = self._rows[idx][col - 1]
                return text or ""
        return ""

    def leaveEvent(self, e):  # noqa: N802
        old, self._hover = self._hover, -1
        self._repaint_row(old)
        self.unsetCursor()
        super().leaveEvent(e)

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self.update()


# --------------------------------------------------------------------------- #
# 工作台
# --------------------------------------------------------------------------- #

class WorkspacePage(QWidget):

    status_message = Signal(str)
    save_preset_requested = Signal()
    load_preset_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Root")
        self.state = A.FormState()
        self.rules: List[R.Rule] = []
        self.bundle: Optional[A.PlanBundle] = None
        self.selected_id = ""
        self.stale = False
        self._busy = False
        self._plan_after_scan = False
        # 落盘 / 撤销会改磁盘上的名字，但 bundle 里的 Item 还是动作前那批。
        # stale 只表示「规则改过、清单还能复用」；_scan_outdated 表示清单本身
        # 已经和磁盘不一致，必须重新扫描，绝不能拿旧 Item 再算名 / 再落盘。
        self._scan_outdated = False
        self._rescan_after_apply = False       # 磁盘变更后待重扫
        self._refresh_is_apply = False         # 本次扫描是不是「落盘后刷新」
        self._rescan_tries = 0
        self._apply_result_message = ""        # 重扫完把执行结果贴回状态槽
        self._apply_result_tone = "success"
        self.runner = W.TaskRunner(self, max_threads=2)

        outer = vbox(self, (20, 18, 20, 12), 12)
        self.head_widget = self._head()
        outer.addWidget(self.head_widget)
        self.toolbar_card = self._toolbar()
        outer.addWidget(self.toolbar_card)
        self.rules_wrap = self._rules()
        outer.addWidget(self.rules_wrap)
        self.preview_card = self._preview()
        outer.addWidget(self.preview_card, 1)
        outer.addWidget(self._summary())
        self._focus = False
        self._refresh_all()
        self._scan_status_idle()
        # 启动引导：原来的「现在可以添加规则了」横幅退场，改成按钮自己亮一次
        if not self.rules:
            QTimer.singleShot(900, self._pulse_add_rule)

    # ---------------- 页头 ----------------
    def _head(self):
        self.btn_load = Button("加载预设", "arrdown")
        self.btn_load.clicked.connect(lambda: self.load_preset_requested.emit())
        self.btn_save = Button("保存为预设", "save")
        self.btn_save.clicked.connect(lambda: self.save_preset_requested.emit())
        return PageHead("重命名工作台",
                        "扫描 → 编排规则链 → 预览校验 → 执行落盘，全程可撤销",
                        [self.btn_load, self.btn_save])

    # ---------------- 工具栏 ----------------
    def _toolbar(self):
        card = Card()
        lay = vbox(card, 14, 12)

        row1 = hbox(None, 0, 8)
        self.path = PathInput(self.state.root)
        self.path.edited.connect(self._on_root_changed)
        row1.addWidget(self.path, 1)
        pick = Button("选择目录…")
        pick.setToolTip("选一个文件夹作为扫描根目录（要精确挑几个文件，直接拖进下面的预览卡）")
        pick.clicked.connect(self._pick)
        row1.addWidget(pick)
        self.btn_scan = Button("开始扫描", "scan", "cta", icon_px=16)
        self.btn_scan.clicked.connect(lambda: self.scan())
        row1.addWidget(self.btn_scan)
        lay.addLayout(row1)

        row2 = hbox(None, 0, 16)
        self.only = Combo([(k, A.ONLY_LABELS[k]) for k in A.ONLY_ORDER],
                          self.state.only, width=148, height=32)
        self.only.changed.connect(self._on_only)
        row2.addWidget(self.only, 0)
        self.cb_recursive = self._check("包含子文件夹", self.state.recursive,
                                        self._on_recursive)
        row2.addWidget(self.cb_recursive)
        self.cb_hidden = self._check("包含隐藏项", self.state.include_hidden,
                                     self._on_hidden)
        row2.addWidget(self.cb_hidden)

        adv_holder = Clickable()
        adv = hbox(adv_holder, 0, 4)
        adv.addWidget(Label("高级筛选", 12, 400, C["t2"]))
        ic = QLabel()
        ic.setPixmap(svg_pixmap("right", C["t2"], 12))
        ic.setFixedSize(12, 12)
        adv.addWidget(ic, 0, Qt.AlignVCenter)
        adv_holder.clicked.connect(self._advanced)
        self.adv_holder = adv_holder
        row2.addWidget(adv_holder, 0)

        row2.addWidget(spacer(), 1)
        # 扫描状态并入扫描栏（唯一显示处，底部统计条另有汇总）：
        # 未扫描 = 灰字「已扫描 0 项」→ 扫描中 = 蓝字 + 转动图标 →
        # 扫完 = 绿勾 + 深灰字，5 秒后淡回灰色默认态。
        # 状态一变就套同色系的胶囊浅底（ok_bg / info_bg / err_bg…），
        # 让「这里有新消息」一眼可见；灰色默认态无底色。
        self.scan_status = QWidget()
        self.scan_status.setObjectName("ScanStatus")
        self.scan_status.setAttribute(Qt.WA_StyledBackground, True)
        self.scan_status.setFixedHeight(22)
        sst = hbox(self.scan_status, (10, 0, 10, 0), 5)
        self.scan_icon = QLabel()
        self.scan_icon.setFixedSize(13, 13)
        self.scan_icon.hide()
        sst.addWidget(self.scan_icon, 0, Qt.AlignVCenter)
        self.count_label = Label("已扫描 0 项", 11, 400, SCAN_GRAY)
        sst.addWidget(self.count_label, 0, Qt.AlignVCenter)
        row2.addWidget(self.scan_status, 0, Qt.AlignVCenter)
        lay.addLayout(row2)
        return card

    def _check(self, text, checked, slot):
        w = QWidget()
        lay = hbox(w, 0, 6)
        cb = Check(checked)
        cb.toggled.connect(slot)
        lay.addWidget(cb, 0, Qt.AlignVCenter)
        lay.addWidget(Label(text, 12, 400, C["t1"]), 0, Qt.AlignVCenter)
        w._cb = cb          # 套预设时要能把勾选状态同步回来
        return w

    # ---------------- 规则区 ----------------
    def _rules(self):
        wrap = QWidget()
        grid = QGridLayout(wrap)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        # 240：让 5 行字段的规则（LLM / 插入文字等）不用滚动就能显示全，
        # 差几个像素的场景再由 _props_fit 收行距兜底
        grid.setRowMinimumHeight(0, 240)
        wrap.setFixedHeight(240)

        self.rules_card = Card()
        rc = vbox(self.rules_card, 12, 0)
        head = hbox(None, 0, 8)
        head.addWidget(Label("规则链", 13, 600))
        self.rule_badge = Badge("0 条")
        head.addWidget(self.rule_badge, 0, Qt.AlignVCenter)
        head.addWidget(spacer(), 1)
        add = Button("添加规则", "plus", "sm_brand", icon_px=12)
        add.clicked.connect(lambda: self.add_rule())
        self.btn_add_rule = add          # 启动引导的 pulse 动画要用
        head.addWidget(add)
        rc.addLayout(head)
        rc.addSpacing(8)
        # 列表装在同款圆角容器里（和预览的拖放区一个样式），行与边框之间有 6px 呼吸
        self.rule_zone = QFrame()
        self.rule_zone.setObjectName("ListZone")
        self.rule_zone.setAttribute(Qt.WA_StyledBackground, True)
        zone = vbox(self.rule_zone, 6, 0)
        self.rule_holder = QWidget()
        self.rule_lay = vbox(self.rule_holder, 0, 6)
        # 规则一多必须内滚：否则固定高度的卡片装不下，行会互相重叠/溢出卡片
        self.rule_scroll = scroll_area(self.rule_holder, min_height=120)
        zone.addWidget(self.rule_scroll, 1)
        self.rule_empty = EmptyState("doc", "还没有规则",
                                     "点右上角「添加规则」，或从预设加载一组规则。")
        self.rule_empty.hide()
        zone.addWidget(self.rule_empty, 1)
        rc.addWidget(self.rule_zone, 1)
        grid.addWidget(self.rules_card, 0, 0)

        self.props_card = Card()
        pc = vbox(self.props_card, 12, 0)
        ph = hbox(None, 0, 8)
        ph.addWidget(Label("规则属性", 13, 600))
        ph.addWidget(spacer(), 1)
        self.props_meta = Label("未选择规则", 11, 400, C["t3"])
        ph.addWidget(self.props_meta)
        pc.addLayout(ph)
        pc.addSpacing(10)
        self.props_holder = QWidget()
        self.props_lay = vbox(self.props_holder, 0, 0)
        # 字段多的规则（LLM 三项 + 作用范围）会超过卡片高度：内滚，别让布局去压扁每一行
        self.props_scroll = scroll_area(self.props_holder)
        pc.addWidget(self.props_scroll, 1)
        grid.addWidget(self.props_card, 0, 1)
        return wrap

    # ---------------- 预览 ----------------
    def _preview(self):
        card = DropCard()
        card.dropped.connect(self.add_paths)
        lay = vbox(card, (12, 12, 12, 12), 0)
        head = hbox(None, 0, 8)
        head.addWidget(Label("预览", 13, 600))
        self.preview_hint = Elided("", 11, 400, C["t2"])   # 未扫描时留空，别和拖放区重复
        self.preview_hint.setMinimumWidth(120)
        head.addWidget(self.preview_hint, 1)
        # 排序方式放在表头正中（发号顺序 = 扫描排序结果，值得常驻可见）：
        # 换档立即重扫，省得再开高级筛选。
        self.sort_lb = Label("排序方式", 11, 400, C["t2"])
        head.addWidget(self.sort_lb, 0, Qt.AlignVCenter)
        self.sort = Combo(A.sort_modes(), self.state.sort_mode, width=200, height=26)
        self.sort.changed.connect(self._on_sort_changed)
        head.addWidget(self.sort, 0, Qt.AlignVCenter)
        head.addSpacing(8)
        self.btn_all = Button("全选", "check_square", "sm_soft", icon_px=12)
        self.btn_all.clicked.connect(lambda: self._check_all(True))
        self.btn_none = Button("全不选", "square", "sm_soft", icon_px=12)
        self.btn_none.clicked.connect(lambda: self._check_all(False))
        head.addWidget(self.btn_all)
        head.addWidget(self.btn_none)

        self.btn_expand = QPushButton()
        self.btn_expand.setFixedSize(26, 26)
        self.btn_expand.setCursor(Qt.PointingHandCursor)
        self.btn_expand.setIcon(icon("expand", C["t1"], 14))
        self.btn_expand.setIconSize(QSize(14, 14))
        self.btn_expand.setToolTip("放大预览（占满整页）")
        self.btn_expand.setStyleSheet(
            "QPushButton{border:1px solid %s;border-radius:5px;background:%s;}"
            "QPushButton:hover{background:%s;}" % (C["line"], C["white"], C["s1"]))
        self.btn_expand.clicked.connect(lambda: self._toggle_focus())
        head.addWidget(self.btn_expand)
        lay.addLayout(head)
        lay.addSpacing(4)

        cols = [("", 34), ("序号", 56), ("类型", 74), "原名称", "新名称",
                ("所在文件夹", 200), ("状态", 104), "说明"]
        self.phead = grid_row([Item(c) for c in cols], 30, holder=Strip(C["s1"], 4))
        lay.addWidget(self.phead)
        lay.addSpacing(1)
        # 表头下面这条分隔线要跟着表头一起显隐，否则空状态下会留一条孤零零的横线
        self.phead_line = hline()
        lay.addWidget(self.phead_line)
        # 表格体：**一块自绘控件**，不是「一行一个控件」。
        # 9000 个文件时旧做法要建十几万个 QObject，光建表就 100 秒；
        # PreviewTable 只画视口里的十几行，成本与总行数基本无关。
        self.rows_table = PreviewTable()
        self.rows_table._header = self.phead
        self.rows_table.toggled.connect(self._on_row_toggle)
        self.rows_scroll = scroll_area(self.rows_table, min_height=34 * 3)
        self.rows_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        lay.addWidget(self.rows_scroll, 1)
        # 空状态：虚线拖放区（形状本身就是"把东西放进来"的暗示）
        self.drop_panel = QFrame()
        self.drop_panel.setObjectName("DropZone")
        self.drop_panel.setAttribute(Qt.WA_StyledBackground, True)
        dz = vbox(self.drop_panel, (16, 10, 16, 10), 0)
        dz.addWidget(EmptyState(
            "inbox", "拖入文件或文件夹开始",
            "把文件/文件夹拖到这里，或点工具栏的「选择目录…」。"
            "然后 F5 先扫描清单，再回来加规则。"), 1)
        lay.addWidget(self.drop_panel, 1)
        return card

    def _preview_rows(self, items):
        """把条目摊平成 7 段文本 + 语气色，一次性喂给 PreviewTable。

        以前这里每个条目建 13 个控件（9000 条 ≈ 12 万个 QObject），现在只算字符串：
        这条路是「导入大量文件后卡住」的主因，别再退回控件树。
        """
        plan_ready = self.bundle is not None and self.bundle.has_plan
        root = self.state.root
        cut = root.rstrip("\\/") if root else ""
        low_r = cut.lower()
        sep = os.sep
        status_text = A.status_text
        kind_text = A.kind_text
        rows, keys, checked, dim, new_tones, st_tones = [], [], [], [], [], []
        for idx, it in enumerate(items, 1):
            skipped = plan_ready and getattr(it, "status", "") == "skip"
            status = getattr(it, "status", "ok") if plan_ready else "ok"
            if not plan_ready:
                new_name, note = "—", "尚未应用规则"
            else:
                new_name = getattr(it, "new_name", "") or ""
                note = getattr(it, "note", "") or ("将按预览名落盘" if status == "ok" else "")

            path = getattr(it, "path", "")
            # ntpath.dirname 会做整套规范化（9000 次约 30ms），这里只做「砍掉
            # 最后一段」——预览要的就是"所在文件夹"这一句，用不到 dirname 的语义。
            folder = path.rpartition(sep)[0]
            # 必须按"目录边界"比前缀：`D:\photo` 不能把 `D:\photo2\x` 截成 "2\x"
            if cut:
                low_f = folder.lower()
                if low_f == low_r:
                    folder = "."
                elif low_f.startswith(low_r + sep):
                    folder = folder[len(cut) + 1:]

            if not plan_ready:
                new_tone = "t2"
            elif status == "same":
                new_tone = "t3"
            elif skipped:
                new_tone = "#C4C4C4"
            else:
                new_tone = "t1"

            rows.append(("%02d" % idx, kind_text(getattr(it, "kind", "")),
                         getattr(it, "name", ""), new_name, folder,
                         status_text(status) if plan_ready else "已扫描", note))
            keys.append(os.path.normcase(getattr(it, "path", "")))
            checked.append(not skipped)
            dim.append(bool(skipped))
            new_tones.append(new_tone)
            st_tones.append(STATUS_TONE.get(status, C["t2"]))
        return rows, keys, checked, dim, new_tones, st_tones

    # ---------------- 摘要 ----------------
    def _summary(self):
        card = Card()
        card.setFixedHeight(50)
        lay = hbox(card, (12, 0, 12, 0), 12)
        self.chips = {}
        chips = hbox(None, 0, 6)
        for key, text, tone in (("total", "对象总数", "muted"), ("ok", "待重命名", "ok"),
                                ("same", "名称未变", "muted"), ("conflict", "冲突", "warn"),
                                ("error", "无效", "err"), ("skip", "已跳过", "info")):
            chip = Chip(text, 0, tone)
            self.chips[key] = chip
            chips.addWidget(chip)
        lay.addLayout(chips)
        self.sum_text = Elided("", 12, 400, C["t2"])       # 同上：扫描之后才说话
        self.sum_text.setMinimumWidth(60)
        lay.addWidget(self.sum_text, 1)
        self.btn_preview = Button("预览变更")
        self.btn_preview.clicked.connect(self.request_preview)
        self.btn_csv = Button("导出 CSV")
        self.btn_csv.clicked.connect(self.export_csv)
        self.btn_apply = Button("执行重命名", "play", "cta", icon_px=16)
        self.btn_apply.clicked.connect(self.confirm_apply)
        lay.addWidget(self.btn_preview)
        lay.addWidget(self.btn_csv)
        lay.addWidget(self.btn_apply)
        return card

    # ================= 放大预览 =================
    def _toggle_focus(self, on=None):
        """放大预览：隐藏页头/工具栏/规则区，让预览卡占满整页。"""
        self._focus = not getattr(self, "_focus", False)
        for w in (self.head_widget, self.toolbar_card, self.rules_wrap):
            w.setVisible(not self._focus)
        self.btn_expand.setIcon(icon("shrink" if self._focus else "expand", C["t1"], 14))
        self.btn_expand.setToolTip("还原布局" if self._focus else "放大预览（占满整页）")
        if self._focus:
            self.status_message.emit("已放大预览；点右上角箭头或按 Esc 还原")

    def _notice_msg(self, text, tone="info"):
        """页面内消息统一入口：全部并入扫描栏右侧的状态槽（横幅已整体退场）。

        专注模式下警告/错误先还原布局（工具栏回来了状态槽才可见），其余不打断、
        只走底部状态栏。
        """
        flat = " · ".join(line.strip() for line in str(text or "").splitlines()
                          if line.strip())
        if getattr(self, "_focus", False):
            if tone in ("warn", "error"):
                self._toggle_focus()
            else:
                self.status_message.emit(flat)
                return
        self._scan_status_message(flat, tone)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key_Escape and getattr(self, "_focus", False):
            self._toggle_focus()
            return
        super().keyPressEvent(event)

    # ================= 刷新 =================
    def _clear(self, lay):
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            elif item.layout() is not None:
                self._clear(item.layout())

    def _refresh_all(self):
        self._refresh_rules()
        self._refresh_preview()
        self._refresh_summary()

    def _refresh_rules(self):
        if not hasattr(self, "rule_lay"):
            return
        # 编辑参数会重建整个列表：滚动位置必须自己记住再放回去，
        # 否则改一条靠后的规则，列表"啪"地跳回顶部。
        bar = self.rule_scroll.verticalScrollBar()
        keep_scroll = bar.value()
        self._clear(self.rule_lay)
        self.rule_badge.setText("%d 条" % len(self.rules))
        # 没有规则时容器退回白底：灰底是"这里有内容"的暗示，空盒子别抢戏
        self.rule_zone.setProperty("empty", "true" if not self.rules else "false")
        st = self.rule_zone.style()
        st.unpolish(self.rule_zone)
        st.polish(self.rule_zone)
        if not self.rules:
            self.rule_scroll.hide()
            self.rule_empty.show()
            self.selected_id = ""
            self._refresh_props()
            return
        self.rule_empty.hide()
        self.rule_scroll.show()
        if self.selected_id not in [r.id for r in self.rules]:
            self.selected_id = self.rules[0].id
        for i, rule in enumerate(self.rules, 1):
            row = RuleRow(rule, i, rule.id == self.selected_id)
            row.picked.connect(self._select_rule)
            row.toggled_on.connect(self._toggle_rule)
            row.moved.connect(self._move_rule)
            row.cloned.connect(self._clone_rule)
            row.removed.connect(self._remove_rule)
            self.rule_lay.addWidget(row)
        self.rule_lay.addStretch(1)
        fit_scroll_content(self.rule_scroll)
        bar.setValue(min(keep_scroll, bar.maximum()))
        self._refresh_props()

    def _current_rule(self):
        for r in self.rules:
            if r.id == self.selected_id:
                return r
        return None

    def _refresh_props(self):
        if not hasattr(self, "props_lay"):
            return
        self._clear(self.props_lay)
        rule = self._current_rule()
        if rule is None:
            self.props_meta.setText("")     # 空状态自己会说明，标题栏不再重复一遍
            self.props_lay.addWidget(EmptyState(
                "pencil", "还没有选中规则", "在左边点一条规则，这里就会展示它的参数。"), 1)
            self._props_fit()
            return
        idx = self.rules.index(rule) + 1
        self.props_meta.setText("第 %d 条 / 共 %d 条" % (idx, len(self.rules)))
        # 标签列宽按当前规则最长的那个标签算：固定 62 会把「名字最大长度」裁掉
        fm = QFontMetrics(F(11))
        self._label_w = max(62, max(fm.horizontalAdvance(t) + 4 for t in
                                    [lb for _k, lb, _c, _o in A.rule_fields(rule.kind)]
                                    + ["作用范围"]))

        # 不再放「规则类型」一行：它和左侧选中行的名称是同一句话，纯属重复；
        # 少了这 40px，字段多的规则也基本不用滚动
        if rule.kind in ("number", "renumber"):
            self._number_props(rule)
            self.props_lay.addStretch(1)
            self._props_fit()
            return
        for key, label, ctl, opt in A.rule_fields(rule.kind):
            value = A.get_field(rule, key)
            if ctl == "combo" and isinstance(opt, list):
                control = Combo(opt, str(value), width=0, height=30)
                control.setMinimumWidth(130)
                control.changed.connect(lambda v, r=rule, k=key: self._set_field(r, k, v))
            else:
                control = FieldEdit(str(value), height=30)
                control.edited.connect(
                    lambda r=rule, k=key, w=control: self._set_field(r, k, w.text()))
                if ctl == "template":
                    # 模板变量的真实清单来自引擎（A.template_variables 用真实
                    # NameContext 渲染一遍），挂在 tooltip 上，不占版面。
                    variables = A.template_variables()
                    control.edit.setToolTip("可用变量（示例值）：\n" + "\n".join(
                        "%s → %s" % (name, val) for name, val in variables))
                    control.edit.setPlaceholderText(
                        opt or "例如 {n}-{name}；可用 {ext} {date} {parent} 等")
                elif opt:
                    control.edit.setPlaceholderText(str(opt)[:40])
            self.props_lay.addWidget(self._field_row(label, control, grow=True))

        if "selector" in A.rule_flags(rule.kind):
            opts = [(s, A.selector_text(s)) for s in A.selector_kinds()]
            sel = Combo(opts, rule.selector or "text", width=0, height=30)
            sel.setMinimumWidth(130)
            sel.changed.connect(lambda v, r=rule: self._set_selector(r, v))
            self.props_lay.addWidget(self._field_row("作用范围", sel, grow=True))
        self._flag_props(rule)
        self._advisory_label(rule)
        self.props_lay.addStretch(1)
        self._props_fit()

    # ---------------- 通用开关（引擎支持、以前界面没有入口） ----------------
    def _flag_props(self, rule):
        """按引擎声明的 KIND_FLAGS 渲染通用开关。

        `skip_ext` / `occurrence` / `keep_sep` 一直是引擎能力，但属性面板只渲染了
        「作用范围」，于是 README 宣传的「不含扩展名」「第 N 次出现」在界面上
        根本设不了。这里按同一张表补齐，以后引擎加开关也会自动出现。
        """
        flags = A.rule_flags(rule.kind)
        if "occurrence" in flags and (rule.selector or "text") != "text":
            occ = FieldEdit(str(rule.occurrence or 1), width=70, height=30)
            occ.edit.setToolTip("1 = 第一次出现，-1 = 最后一次，N = 第 N 次")
            occ.edited.connect(lambda r=rule, w=occ: self._set_occurrence(r, w.text()))
            self.props_lay.addWidget(self._field_row("第几次出现", occ, grow=False))
        if "skip_ext" in flags:
            self.props_lay.addWidget(self._flag_row(
                "不作用于扩展名", bool(rule.skip_ext),
                lambda on, r=rule: self._set_flag(r, "skip_ext", on)))
        if "keep_sep" in flags:
            self.props_lay.addWidget(self._flag_row(
                "保留分隔符", bool(rule.keep_sep),
                lambda on, r=rule: self._set_flag(r, "keep_sep", on)))

    def _flag_row(self, text, checked, slot):
        holder = QWidget()
        row = hbox(holder, 0, 6)
        cb = Check(checked)
        cb.toggled.connect(slot)
        row.addWidget(cb, 0, Qt.AlignVCenter)
        row.addWidget(Label(text, 12, 400, C["t1"]), 0, Qt.AlignVCenter)
        row.addStretch(1)
        holder.setFixedHeight(24)
        return self._wrap_row(holder)

    def _advisory_label(self, rule):
        """把不进 rule_problems 的提醒挂在面板底部（灰色小字，不阻断预览）。

        用中性灰而不是警告橙：这些是"引擎会这样处理"的说明，不是错误。
        标成橙色会让每一条模板规则都像出了问题。
        """
        tips = A.rule_advisories(rule)
        if not tips:
            return
        lab = Label("提示：" + "；".join(tips), 11, 400, C["t2"])
        lab.setWordWrap(True)
        self.props_lay.addSpacing(4)
        self.props_lay.addWidget(lab)

    def _props_fit(self):
        """装完再量一次：内容超出卡片就出滚动条，而不是把每一行压扁。

        重建会换掉所有控件，所以滚到哪儿要自己记住再放回去。
        """
        bar = self.props_scroll.verticalScrollBar()
        keep = bar.value()
        fit_scroll_content(self.props_scroll)
        # 只差几个像素也出滚动条，最难看：行间距收紧 1~3px 把这点溢出换回来
        if 0 < bar.maximum() <= 6:
            self.props_lay.setSpacing(max(3, 6 - bar.maximum()))
            fit_scroll_content(self.props_scroll)
        else:
            self.props_lay.setSpacing(6)
        bar.setValue(min(keep, bar.maximum()))

    # ---------------- 编号规则：发号 + 插号同一条 ----------------
    def _number_props(self, rule):
        """编号规则的属性：起始 / 步长 / 补零 一行，编号范围、倒序、位置各一行。

        发号参数就长在这条规则上，链里不再有第二个「编号入口」。
        """
        f = {key: (label, ctl, opt) for key, label, ctl, opt in A.rule_fields(rule.kind)}

        group = QWidget()
        row = hbox(group, 0, 6)
        for key in ("a", "extras.step", "b"):
            if key not in f:
                continue
            label = f[key][0]
            lb = Label(label, 11, 400, C["t2"])
            lb.setFixedWidth(max(32, 11 * len(label) + 4))
            row.addWidget(lb, 0, Qt.AlignVCenter)
            box = FieldEdit(A.get_field(rule, key), width=56, height=28)
            box.edited.connect(lambda r=rule, k=key, w=box: self._set_field(r, k, w.text()))
            row.addWidget(box, 0, Qt.AlignVCenter)
            row.addSpacing(10)
        row.addStretch(1)
        group.setFixedHeight(28)
        self.props_lay.addWidget(self._wrap_row(group))

        if "extras.scope" in f:
            scope = Combo(list(A.scope_text().items()),
                          A.get_field(rule, "extras.scope") or "global",
                          width=0, height=30)
            scope.setMinimumWidth(150)
            scope.changed.connect(lambda v, r=rule: self._set_field(r, "extras.scope", v))
            self.props_lay.addWidget(self._field_row("编号范围", scope, grow=True))

        if "extras.reverse" in f:
            rev = QWidget()
            rrow = hbox(rev, 0, 6)
            cb = Check(A.get_field(rule, "extras.reverse") not in ("", "0"))
            cb.toggled.connect(lambda on, r=rule: self._set_field(
                r, "extras.reverse", "1" if on else ""))
            rrow.addWidget(cb, 0, Qt.AlignVCenter)
            rrow.addWidget(Label(f["extras.reverse"][0], 12, 400, C["t1"]), 0,
                          Qt.AlignVCenter)
            rrow.addStretch(1)
            rev.setFixedHeight(24)
            self.props_lay.addWidget(rev)

        if "c" in f:
            pos = Combo(f["c"][2], A.get_field(rule, "c") or "prefix", width=0, height=30)
            pos.setMinimumWidth(130)
            pos.changed.connect(lambda v, r=rule: self._set_field(r, "c", v))
            self.props_lay.addWidget(self._field_row("位置", pos, grow=True))
            # 锚点之前/之后必须有个地方填锚点：引擎读的是 rule.charset，
            # 面板以前不渲染它，于是这两个选项选了等于没选（静默退回前缀）。
            if (A.get_field(rule, "c") or "prefix").lower() in ("before", "after"):
                anchor = FieldEdit(rule.charset or "", height=30)
                anchor.edit.setPlaceholderText("用哪个字符定位编号")
                anchor.edited.connect(
                    lambda r=rule, w=anchor: self._set_field(r, "charset", w.text()))
                self.props_lay.addWidget(self._field_row("锚点字符", anchor, grow=True))

        self._flag_props(rule)
        self._advisory_label(rule)

        tip = Label("号码按扫描顺序发号；整个名字换成序号，扩展名原样保留。"
                    if rule.kind == "renumber" else
                    "号码按扫描顺序发号；这条规则同时负责发号与摆放位置。",
                    11, 400, C["t3"])
        tip.setWordWrap(True)
        self.props_lay.addSpacing(4)
        self.props_lay.addWidget(tip)

    def _wrap_row(self, control):
        wrap = QWidget()
        wl = vbox(wrap, 0, 0)
        wl.addWidget(control)
        wl.addSpacing(6)
        return wrap

    def _field_row(self, label, control, grow=True):
        holder = QWidget()
        row = hbox(holder, 0, 10)
        lb = Label(label, 11, 400, C["t2"])
        lb.setFixedWidth(getattr(self, "_label_w", 62))
        row.addWidget(lb, 0, Qt.AlignVCenter)
        if grow:
            row.addWidget(control, 1)
        else:
            row.addWidget(control, 0, Qt.AlignVCenter)
            row.addStretch(1)
        # 容器必须容得下控件本身：30 高的输入框放进 28 高的 holder，底边框就被裁掉了
        holder.setFixedHeight(max(28, control.height() or control.sizeHint().height()))
        wrap = QWidget()
        wl = vbox(wrap, 0, 0)
        wl.addWidget(holder)
        wl.addSpacing(6)
        return wrap

    @staticmethod
    def _pill(text):
        w = QLabel(text)
        w.setFont(F(12, 500))
        w.setFixedHeight(26)
        w.setContentsMargins(8, 0, 8, 0)
        w.setAlignment(Qt.AlignCenter)
        w.setStyleSheet("QLabel{background:%s;border-radius:%dpx;color:%s;}"
                        % (C["s2"], R_XS, C["t1"]))
        w.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        return w

    def _refresh_summary(self):
        has_scan = self.bundle is not None and self.bundle.has_scan
        has_plan = self.bundle is not None and self.bundle.has_plan
        stats = self.bundle.stats() if self.bundle else {}
        for key, chip in self.chips.items():
            chip.set_value(int(stats.get(key, 0)))
        if not has_scan:
            self.sum_text.setText("")                    # 空状态的信息由拖放区说
        elif not has_plan:
            self.sum_text.setText("已扫描 %d 项，尚未应用规则" % stats.get("total", 0))
        elif self._scan_outdated:
            self.sum_text.setText("文件已变更，正在按磁盘现状刷新预览…")
        elif self.stale:
            self.sum_text.setText("配置已改动，预览已过期 — 请重新生成")
        else:
            ok = int(stats.get("ok", 0))
            conflict = int(stats.get("conflict", 0))
            text = "%d 项将被重命名" % ok
            if conflict:
                text += " · %d 项冲突将跳过" % conflict
            self.sum_text.setText(text)
        can_apply = bool(has_plan and stats.get("ok") and not self.stale
                         and not self._scan_outdated and not self._busy)
        self.btn_csv.setEnabled(bool(has_plan) and not self._scan_outdated
                                and not self._busy)
        self.btn_apply.setEnabled(can_apply)
        self.btn_preview.setText("重新生成预览" if has_plan else
                                 ("生成预览" if has_scan else "预览变更"))
        self.btn_preview.setEnabled(not self._busy)
        self.btn_all.setEnabled(has_scan)
        self.btn_none.setEnabled(has_scan)

    def _refresh_preview(self):
        # 一张自绘表换数据，不再逐行建控件：9000 行从「100 秒」降到「几十毫秒」
        items = self.bundle.items() if self.bundle else []
        has = bool(items)
        self.phead.setVisible(has)
        self.phead_line.setVisible(has)
        self.rows_scroll.setVisible(has)
        self.drop_panel.setVisible(not has)
        plan_ready = self.bundle is not None and self.bundle.has_plan
        rows, keys, checked, dim, new_tones, st_tones = self._preview_rows(items)
        self.rows_table.set_rows(rows, keys, checked, dim, new_tones, st_tones,
                                 plan_ready)
        self.rows_scroll.verticalScrollBar().setValue(0)
        if self.bundle is not None:
            n = len(items)
            self.preview_hint.setText(
                ("%d 项 · 已勾选 %d 项" % (n, self.rows_table.checked_count()))
                if plan_ready else ("%d 项 · 尚未生成预览" % n))

    # ---- 扫描栏右侧的状态机（颜色见 SCAN_GRAY / SCAN_DARK） ----
    def _scan_status_bg(self, color):
        """状态槽的胶囊底色；「transparent」= 灰色默认态无胶囊。"""
        self.scan_status.setStyleSheet(
            "#ScanStatus{background:%s;border-radius:11px;}" % color)

    def _scan_status_text(self, text):
        """状态槽的单行文本：超过 420px 用省略号截断，全文进 tooltip。

        手动定宽而不是用 Elided——省略后的 sizeHint 会反过来把布局挤窄，
        宽度越算越小（反馈塌缩），这里宽度只由全文算一次，稳定。
        """
        fm = QFontMetrics(self.count_label.font())
        full = str(text or "")
        if fm.horizontalAdvance(full) > 420:
            self.count_label.setText(fm.elidedText(full, Qt.ElideRight, 420))
            self.count_label.setToolTip(full)
        else:
            self.count_label.setText(full)
            self.count_label.setToolTip("")
        self.count_label.setFixedWidth(min(fm.horizontalAdvance(full) + 2, 422))

    def _scan_status_idle(self):
        """灰色默认态：未扫描时是「已扫描 0 项」，扫完淡出后回到这里（数字保留）。"""
        self._scan_spin_stop()
        self._scan_fade_stop()
        self.scan_icon.hide()
        n = len(self.bundle.items()) if (
            self.bundle is not None and self.bundle.has_scan) else 0
        self._scan_status_text("已扫描 %d 项" % n)
        self.count_label.setStyleSheet(
            "color:%s;background:transparent;" % SCAN_GRAY)
        self._scan_status_bg("transparent")

    def _scan_status_busy(self):
        """扫描中：蓝字「正在扫描…」+ 转动的刷新图标，蓝胶囊底。"""
        self._scan_fade_stop()
        self._spin_base = svg_pixmap("refresh", C["info"], 13)
        self.scan_icon.setPixmap(self._spin_base)
        self.scan_icon.setGraphicsEffect(None)
        self.scan_icon.show()
        self._scan_status_text("正在扫描…")
        self.count_label.setStyleSheet(
            "color:%s;background:transparent;" % C["info"])
        self._scan_status_bg(C["info_bg"])
        self._scan_spin_start()

    def _scan_status_done(self):
        """扫完：绿勾 + 深灰字 + 绿胶囊底，挂一个 5 秒后淡回灰色的定时器。"""
        self._scan_spin_stop()
        self.scan_icon.setPixmap(svg_pixmap("check", C["ok"], 13))
        self.scan_icon.setGraphicsEffect(None)
        self.scan_icon.show()
        n = len(self.bundle.items()) if self.bundle is not None else 0
        self._scan_status_text("已扫描 %d 项" % n)
        self.count_label.setStyleSheet(
            "color:%s;background:transparent;" % SCAN_DARK)
        self._scan_status_bg(C["ok_bg"])
        self._scan_fade_schedule()

    def _scan_status_message(self, text, tone):
        """警告 / 错误 / 结果类消息：同样走扫描栏右侧，语气色文字 + 同色胶囊底。

        与扫描三态互斥：新消息顶掉旧状态，下一次扫描/淡出再恢复正常三态。
        消息不自动淡出——错误和结果需要停留到用户下一个动作。
        """
        self._scan_spin_stop()
        self._scan_fade_stop()
        ic, fg, bg = {
            "success": ("check", C["ok"], C["ok_bg"]),
            "warn": ("info", C["warn"], C["warn_bg"]),
            "error": ("x", C["err"], C["err_bg"]),
            "info": ("info", C["info"], C["info_bg"]),
        }.get(tone, ("info", C["info"], C["info_bg"]))
        self.scan_icon.setPixmap(svg_pixmap(ic, fg, 13))
        self.scan_icon.setGraphicsEffect(None)
        self.scan_icon.show()
        self._scan_status_text(text)
        self.count_label.setStyleSheet(
            "color:%s;background:transparent;" % fg)
        self._scan_status_bg(bg)

    def _scan_spin_start(self):
        if getattr(self, "_spin_timer", None) is not None:
            return
        self._spin_angle = 0
        timer = QTimer(self)
        timer.setInterval(40)
        timer.timeout.connect(self._scan_spin_tick)
        self._spin_timer = timer
        timer.start()

    def _scan_spin_tick(self):
        self._spin_angle = (self._spin_angle + 30) % 360
        self.scan_icon.setPixmap(self._spin_base.transformed(
            QTransform().rotate(self._spin_angle), Qt.SmoothTransformation))

    def _scan_spin_stop(self):
        timer = getattr(self, "_spin_timer", None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
            self._spin_timer = None

    def _scan_fade_schedule(self):
        self._scan_fade_stop()
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self._scan_fade_start)
        self._fade_timer = timer
        timer.start(5000)

    def _scan_fade_start(self):
        self._fade_timer = None
        icon_eff = QGraphicsOpacityEffect(self.scan_icon)
        self.scan_icon.setGraphicsEffect(icon_eff)
        anim = QVariantAnimation(self)
        anim.setDuration(450)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        dark, gray = QColor(SCAN_DARK), QColor(SCAN_GRAY)
        bg = QColor(C["ok_bg"])

        def mix(c1, c2, t):
            return QColor(int(c1.red() + (c2.red() - c1.red()) * t),
                          int(c1.green() + (c2.green() - c1.green()) * t),
                          int(c1.blue() + (c2.blue() - c1.blue()) * t)).name()

        def step(v):
            t = float(v)
            icon_eff.setOpacity(t)
            self.count_label.setStyleSheet(
                "color:%s;background:transparent;" % mix(dark, gray, t))
            # 胶囊底色同步淡出（QSS 用 rgba 表达透明度）
            self.scan_status.setStyleSheet(
                "#ScanStatus{background:rgba(%d,%d,%d,%.2f);border-radius:11px;}"
                % (bg.red(), bg.green(), bg.blue(), t))

        def done():
            if getattr(self, "_fade_anim", None) is not anim:
                return          # 中途被新扫描打断（_scan_fade_stop 已置空），别覆盖新状态
            self._fade_anim = None
            self._scan_status_idle()

        anim.valueChanged.connect(step)
        anim.finished.connect(done)
        self._fade_anim = anim
        anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

    def _scan_fade_stop(self):
        timer = getattr(self, "_fade_timer", None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
            self._fade_timer = None
        anim = getattr(self, "_fade_anim", None)
        if anim is not None:
            self._fade_anim = None   # 先摘引用，finished 里就不会回写灰色默认态
            anim.stop()

    def _pulse_add_rule(self):
        """「添加规则」按钮 pulse 一次（约 1.5s）：底色与描边向品牌蓝鼓一下再收回。

        取代旧的引导横幅——语义还在（可以添加规则了），但不占一整行布局。
        已经有规则、按钮不可见或动画正在进行时直接跳过。
        """
        btn = getattr(self, "btn_add_rule", None)
        if btn is None or self.rules or not btn.isVisible():
            return
        if getattr(self, "_pulse_anim", None) is not None:
            return
        white, soft = QColor("#FFFFFF"), QColor(C["brand_soft"])
        off, brand = QColor(C["brand_off"]), QColor(C["brand"])

        def mix(c1, c2, t):
            return QColor(int(c1.red() + (c2.red() - c1.red()) * t),
                          int(c1.green() + (c2.green() - c1.green()) * t),
                          int(c1.blue() + (c2.blue() - c1.blue()) * t)).name()

        anim = QVariantAnimation(btn)
        anim.setDuration(1500)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)

        def step(v):
            k = math.sin(math.pi * float(v))     # 0 → 1 → 0，单次鼓起
            btn.setStyleSheet(
                "QPushButton#BtnSmBrand{background:%s;border-color:%s;}"
                % (mix(white, soft, k), mix(off, brand, k)))

        anim.valueChanged.connect(step)

        def done():
            btn.setStyleSheet("")                # 回到全局 QSS 的常态
            self._pulse_anim = None

        anim.finished.connect(done)
        self._pulse_anim = anim
        anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

    def _row_widgets(self):
        """按行取轻量句柄（不再是控件）。

        保留这个方法名与 `_cb` / `_item` 的属性面，是为了不惊动已有的调用点与
        自检脚本；实现已经与控件无关，所以 9000 行也不会再造出十几万个 QObject。
        """
        items = self.bundle.items() if self.bundle is not None else []
        return self.rows_table.handles(items)

    def _mark_stale(self):
        if self.bundle is not None and self.bundle.has_plan:
            self.stale = True
        self._refresh_summary()

    def mark_scan_outdated(self, message=""):
        """外部动作（执行记录页撤销）改了磁盘：按同一套流程刷新工作台。"""
        self._apply_result_message = str(message or "")
        self._apply_result_tone = "info"
        # 工作台还没选过目录 / 目录已不存在：别弹「还没有选择文件夹」，
        # 只把旧清单标成失效，等用户下次扫描自然重建。
        root = str(self.state.root or "")
        if not root or not os.path.isdir(root):
            self.stale = True
            self._refresh_summary()
            return
        self._scan_outdated = True
        self._rescan_after_apply = True
        self.stale = True
        self._schedule_apply_refresh()

    # ================= 规则操作 =================
    def add_rule(self, kind=""):
        if not kind:
            dlg = RulePicker(self)
            if dlg.exec() != QDialog.Accepted:
                return
            kind = dlg.chosen()
        if not kind:
            return
        rule = A.new_rule(kind)
        self.rules.append(rule)
        self.selected_id = rule.id
        self._refresh_rules()
        self._mark_stale()
        self.status_message.emit("已添加规则：%s" % A.rule_title(rule))

    def _select_rule(self, rule_id):
        if rule_id == self.selected_id:
            return
        self.selected_id = rule_id
        self._refresh_rules()

    def _toggle_rule(self, rule_id, enabled):
        for r in self.rules:
            if r.id == rule_id:
                r.enabled = bool(enabled)
                break
        self._refresh_rules()
        self._mark_stale()

    def _move_rule(self, rule_id, delta):
        ids = [r.id for r in self.rules]
        if rule_id not in ids:
            return
        src = ids.index(rule_id)
        dst = max(0, min(len(self.rules) - 1, src + delta))
        if src == dst:
            return
        self.rules = A.move_rule(self.rules, src, dst)
        self._refresh_rules()
        self._mark_stale()

    def _clone_rule(self, rule_id):
        rule = next((r for r in self.rules if r.id == rule_id), None)
        if rule is None:
            return
        copy = A.clone_rule(rule)
        self.rules.insert(self.rules.index(rule) + 1, copy)
        self.selected_id = copy.id
        self._refresh_rules()
        self._mark_stale()

    def _remove_rule(self, rule_id):
        self.rules = [r for r in self.rules if r.id != rule_id]
        if self.selected_id == rule_id:
            self.selected_id = self.rules[0].id if self.rules else ""
        self._refresh_rules()
        self._mark_stale()

    def _set_field(self, rule, key, value):
        if A.get_field(rule, key) == value:
            return
        A.set_field(rule, key, value)
        self._refresh_rules()
        self._mark_stale()

    def _set_selector(self, rule, value):
        rule.selector = value
        self._refresh_rules()
        self._mark_stale()

    def _set_flag(self, rule, key, on):
        if bool(getattr(rule, key, False)) == bool(on):
            return
        setattr(rule, key, bool(on))
        self._refresh_rules()
        self._mark_stale()

    def _set_occurrence(self, rule, text):
        """第几次出现：空/0 按首次处理，-1 表示最后一次。"""
        try:
            n = int(str(text).strip() or 1)
        except (TypeError, ValueError):
            return
        if n == 0:
            n = 1
        if n == int(rule.occurrence or 1):
            return
        rule.occurrence = n
        self._refresh_rules()
        self._mark_stale()

    # ================= 扫描参数 =================
    def _on_root_changed(self):
        root = self.path.text()
        if root == self.state.root:
            return
        self.state.root = root
        self.state.selected = ()
        self.state.strict_selected = False
        self.bundle = None
        self._scan_outdated = False
        self._rescan_after_apply = False
        self._refresh_is_apply = False
        self._refresh_preview()
        self._refresh_summary()

    def _on_only(self, value):
        self.state.only = value
        if value in ("dir", "all"):
            self.state.rename_dirs = True
        self._mark_stale()

    def _on_recursive(self, value):
        self.state.recursive = bool(value)
        self._mark_stale()

    def _on_hidden(self, value):
        self.state.include_hidden = bool(value)
        self._mark_stale()

    def _on_sort_changed(self, value):
        """排序方式决定发号顺序（先排序再编号）：换档立即重扫。"""
        if value == self.state.sort_mode:
            return
        self.state.sort_mode = value
        self._mark_stale()
        if self.bundle is not None and self.bundle.has_scan:
            self.scan(plan_after=True)

    def _pick(self):
        """「选择目录…」只选文件夹，选到的就是扫描根目录（扫里面全部）。

        「只要这几个文件」这个用法**不放在按钮上**：Qt 的文件对话框要么只选文件、
        要么只选目录，非原生对话框理论上能混选，实测在 Windows 上选不了文件夹。
        所以精确挑文件走**拖放**：把文件拖进预览卡即可，逻辑与按钮同源
        （add_paths：单文件夹→根目录，多个→「只要这几个」）。
        """
        start = self.state.root or os.path.expanduser("~")
        path = QFileDialog.getExistingDirectory(self, "选择要处理的文件夹", start)
        if not path:
            return
        self.path.setText(path)
        self.state.selected = ()
        self._on_root_changed()
        self.scan()

    def add_paths(self, paths):
        """拖进来 / 选进来的路径：单个文件夹当根目录扫里面，其余当「扫描对象」。

        这一路是「导入大量文件」的入口，**在主线程里**跑：9000 个路径时逐个
        `os.path.exists` 要 60ms、`os.path.commonpath` 要 100ms，都实打实地
        卡在点击后那一帧上。这里改成：唯一性去重（normcase）+ 只核实少量样本的
        存在性 —— 真正不存在的路径引擎扫描时会自然落空，不需要先全量 stat 一遍。
        """
        raw = [p for p in (paths or []) if p]
        if not raw:
            return
        seen, items = set(), []
        for p in raw:
            key = os.path.normcase(p)
            if key in seen:
                continue
            seen.add(key)
            items.append(p)
        # 存在性核实：全量 stat 是 O(n) 次系统调用（9000 个约 60ms，卡在点击后
        # 那一帧上）。拖放来的路径本来就存在，只需抽样挡掉"拖了个空壳"的情况；
        # 真不存在的路径，引擎扫描时自然会落空。
        if len(items) <= 64:
            items = [p for p in items if os.path.exists(p)]
        else:
            sample = items[:8] + items[-8:]
            if not any(os.path.exists(p) for p in sample):
                return
        if not items:
            return
        if len(items) == 1 and os.path.isdir(items[0]):
            self.state.selected = ()
            self.state.strict_selected = False
            root = items[0]
        else:
            self.state.selected = tuple(items)
            # 9000 个文件通常只来自少数几个目录：先按父目录去重，再对那一小撮
            # 目录算 commonpath（对着 9000 条路径算要 106ms，这样只要几毫秒）。
            # 目录本身也要算进去（拖进来的文件夹代表它自己那一段），所以对
            # 候选里"可能是目录"的少数几项做一次 isdir 核实。
            sep = os.sep
            folders = {p.rpartition(sep)[0] for p in items} - {""}
            dirs = [p for p in items if os.path.isdir(p)]
            parts = sorted(folders | set(dirs))
            try:
                root = os.path.commonpath(parts) if len(parts) > 1 else (
                    parts[0] if parts else "")
                self.state.strict_selected = False
            except ValueError:
                # 跨盘符：没有公共父目录。根目录取第一项的父目录（只用来显示与
                # 记日志），同时打开"严格按选中项收"，免得把父目录里其它无关
                # 条目一起拉进待改名列表。
                root = items[0].rpartition(sep)[0]
                self.state.strict_selected = True
        if root:
            self.path.setText(root)
            self.state.root = root
        self.scan()

    def _advanced(self):
        if AdvancedDialog(self.state, self).exec() == QDialog.Accepted:
            self._mark_stale()
            self.scan()

    # ================= 流程 =================
    def _busy_set(self, busy, text=""):
        self._busy = busy
        self.btn_scan.setEnabled(not busy)
        self.btn_scan.setText("处理中…" if busy else "开始扫描")
        if text:
            self.preview_hint.setText(text)
        self._refresh_summary()

    def _collect(self, need_rules=False) -> bool:
        self.state.root = self.path.text()
        if not self.state.root:
            QMessageBox.information(self, "还没有选择文件夹",
                                    "点「选择目录…」选一个文件夹，或把文件直接拖进预览卡。")
            return False
        if not os.path.isdir(self.state.root):
            QMessageBox.warning(self, "目录不存在", self.state.root)
            return False
        if need_rules:
            if not self.rules:
                QMessageBox.information(self, "还没有规则", "先添加一条规则，再生成预览。")
                return False
            bad = []
            for rule in self.rules:
                if not rule.enabled:
                    continue
                problems = A.rule_problems(rule)
                if problems:
                    bad.append("%s：%s" % (A.rule_title(rule), problems[0]))
            if bad:
                QMessageBox.warning(self, "规则参数不完整", "\n".join(bad))
                return False
        return True

    def scan(self, plan_after=False):
        if self.runner.busy():
            return
        if not self._collect(need_rules=False):
            return
        self._plan_after_scan = bool(plan_after and self.rules)
        self._refresh_is_apply = bool(self._rescan_after_apply)
        self._busy_set(True, "正在扫描…")
        self._scan_status_busy()
        task = W.scan_task(self.state)
        task.signals.done.connect(self._scan_done)
        task.signals.failed.connect(self._failed)
        task.signals.finished.connect(self._task_finished)
        self.runner.submit(task)

    def _scan_done(self, bundle):
        self.bundle = bundle
        self.stale = False
        refresh = self._refresh_is_apply
        self._refresh_is_apply = False
        self._scan_outdated = False
        self._rescan_after_apply = False
        self._rescan_tries = 0
        self._refresh_preview()
        self._refresh_summary()
        n = len(bundle.items())
        if refresh:
            # 落盘结果比「已扫描 N 项」重要：重扫完把执行结果贴回状态槽，
            # 别让刷新过程把成功/失败提示冲掉。
            self._scan_status_message(self._apply_result_message,
                                      self._apply_result_tone)
            self.status_message.emit(self._apply_result_message)
        else:
            self._scan_status_done()
            self.status_message.emit("已扫描 %d 项" % n)
        if not self.rules:
            self._pulse_add_rule()

    def _task_finished(self):
        self._busy_set(False)
        # 执行 / 撤销改过磁盘：等任务线程彻底退出后，按磁盘现状重扫一次。
        # 只扫清单、不自动再套规则（用户看到的应是刚落盘的名字）。
        if self._rescan_after_apply:
            self._schedule_apply_refresh()
            return
        if self._plan_after_scan:
            self._plan_after_scan = False
            QTimer.singleShot(0, self.generate_plan)

    def _schedule_apply_refresh(self, _tries=0):
        """执行完一次落盘 / 撤销后排队重扫：成功即刷新，线程池还忙就等下一轮。"""
        if not self._rescan_after_apply:
            return
        if _tries > 40:                       # 2 秒还没排上就放弃，别无限打转
            self._rescan_after_apply = False
            self._rescan_tries = 0
            self._refresh_summary()
            return
        if self.runner.busy() or self._busy:
            self._rescan_tries = _tries
            QTimer.singleShot(50, lambda: self._schedule_apply_refresh(_tries + 1))
            return
        self._rescan_tries = 0
        self.scan()

    def generate_plan(self):
        if self.runner.busy():
            return
        # 清单已和磁盘不一致（执行过 / 撤销过）：拿旧 Item 再算名会给出
        # 与磁盘脱节的预览，甚至再次落盘撞 WinError 183。先重扫再说。
        if self._scan_outdated:
            self.scan(plan_after=True)
            return
        if self.bundle is None or not self.bundle.has_scan:
            self.scan(plan_after=True)
            return
        if not self._collect(need_rules=True):
            return
        self._busy_set(True, "正在按规则生成预览…")
        task = W.build_plan_task(self.bundle, self.rules, self._client())
        task.signals.done.connect(self._plan_done)
        task.signals.failed.connect(self._failed)
        task.signals.finished.connect(self._task_finished)
        self.runner.submit(task)

    def _plan_done(self, bundle):
        self.bundle = bundle
        self.stale = False
        self._refresh_preview()
        self._refresh_summary()
        # 这一步可能真发了 LLM 请求 —— 让左下角的气泡刷新延迟与状态
        self._notify_llm_status()
        warns = list(getattr(bundle.plan, "warnings", []) or [])
        if warns:
            self._notice_msg("生成预览时有提示：" + "；".join(warns[:3]), "warn")
        else:
            # 无警告：状态槽回到扫描栏的灰色默认态，别让上一次的错误挂着不走
            self._scan_status_idle()
        self.status_message.emit("预览已更新：%s" % bundle.plan.summary())

    def _failed(self, message):
        self._busy_set(False)
        self._refresh_is_apply = False
        # 重扫失败时保留 _scan_outdated（清单仍然不可用），但别让
        # _task_finished 再排一次队、也别无限重试；用户点预览会重新触发重扫。
        self._rescan_after_apply = False
        self._rescan_tries = 0
        if self.bundle is None or not self.bundle.has_scan:
            self._scan_status_idle()   # 没扫成：状态回灰色默认，别停在「正在扫描…」
        self._notice_msg("操作失败：%s" % message, "error")
        self.status_message.emit("操作失败：%s" % message)
        self._refresh_summary()

    def request_preview(self):
        # 清单与磁盘脱节时先重扫：既不能拿旧 Item 算名，也不能重复排队重扫。
        if self._rescan_after_apply:
            return
        if self.bundle is None or not self.bundle.has_scan or self._scan_outdated:
            self.scan(plan_after=True)
        else:
            self.generate_plan()

    def _client(self):
        if not A.llm_has_rule(self.rules):
            return None
        main = self.window()
        getter = getattr(main, "llm_client", None)
        if callable(getter):
            return getter()
        return A.llm_client()

    def _notify_llm_status(self):
        """通知窗口刷新左下角的 LLM 状态气泡（真跑过请求后延迟才有意义）。"""
        main = self.window()
        refresh = getattr(main, "refresh_llm_bubble", None)
        if callable(refresh):
            refresh()

    # ================= 勾选 / 导出 / 执行 =================
    def _on_row_toggle(self, index, checked):
        """单行勾选：只改这一条，代价是常数（不再全表遍历 + 整表重画）。

        旧实现每次点击都跑一遍 O(n) 的排除集合并逐个改行控件样式，9000 行
        一次要 700ms；现在单点一行 ≈ 1ms。
        """
        if self.bundle is None or not self.bundle.has_plan:
            return
        items = self.bundle.plan.items
        if 0 <= index < len(items):
            A.set_excluded_one(items[index], not checked)
            self.rows_table.refresh_tones_at(index, items[index])
        self._after_check_change()

    def _after_check_change(self):
        """勾选变化后统一收尾：提示文案 + 底部统计 + 按钮可用性。"""
        if self.bundle is None or not self.bundle.has_plan:
            return
        n = len(self.bundle.plan.items)
        self.preview_hint.setText(
            "%d 项 · 已勾选 %d 项" % (n, self.rows_table.checked_count()))
        self._refresh_summary()

    def _on_row_check(self):
        """按当前勾选状态整体对齐引擎（全选/全不选、批量操作走这里）。"""
        if self.bundle is None or not self.bundle.has_plan:
            return
        A.set_excluded(self.bundle.plan, self.rows_table.excluded_keys())
        self.rows_table.refresh_tones(self.bundle.plan.items)
        self._after_check_change()

    def _check_all(self, on):
        # 先把引擎里的状态一次性刷成 skip / 恢复，再让表整片重画（视口范围内）
        self.rows_table.set_all(on)
        self._on_row_check()

    def export_csv(self):
        if self.bundle is None or not self.bundle.has_plan:
            return
        start = os.path.join(self.state.root, "NameCat_plan.csv")
        path, _ = QFileDialog.getSaveFileName(self, "导出预览为 CSV", start, "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            out = A.export_csv(self.bundle.plan, path)
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", "%s: %s" % (type(exc).__name__, exc))
            return
        self._notice_msg("已导出：%s" % out, "success")

    def confirm_apply(self):
        if (self.bundle is None or not self.bundle.has_plan or self._busy
                or self._scan_outdated):
            return
        stats = self.bundle.stats()
        if not stats.get("ok"):
            QMessageBox.information(self, "没有需要改名的项",
                                    "所有对象要么名称未变，要么被排除或存在冲突。")
            return
        msg = ("目录：%s\n待重命名 %d 项；名称未变 %d 项；冲突 %d 项；无效 %d 项；已跳过 %d 项。\n\n"
               "目标已存在时会跳过而不是覆盖；执行后写入撤销日志，可在「执行记录」页撤销。"
               % (self.state.root, stats.get("ok", 0), stats.get("same", 0),
                  stats.get("conflict", 0), stats.get("error", 0), stats.get("skip", 0)))
        if QMessageBox.question(self, "执行重命名", msg,
                                QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) != QMessageBox.Yes:
            return
        self._busy_set(True, "正在执行重命名…")
        task = W.apply_task(self.bundle, self.rules, self._client())
        task.signals.done.connect(self._apply_done)
        task.signals.failed.connect(self._failed)
        task.signals.finished.connect(self._task_finished)
        self.runner.submit(task)

    def _apply_done(self, result):
        ok = bool(getattr(result, "ok", False))
        msg = str(getattr(result, "message", "") or "")
        # 回滚万一没做干净（例如文件被别的程序锁住），至少要让用户知道
        # 有东西停在了 __nk_tmp_* 上，而不是只看到一句"失败 N 项"。
        if not ok:
            hint = self._leftover_hint()
            if hint:
                msg += hint
        # 撤销日志路径不再进状态槽（执行记录页有完整列表），只报结果本身
        self._notice_msg(msg, "success" if ok else "error")
        self.status_message.emit(msg)
        # 磁盘上的名字已经变了，旧 bundle 里的 Item 不能再用来算名 / 落盘。
        # 任务 finished 之后排队重扫，预览会跟着磁盘现状一起更新。
        self._apply_result_message = msg
        self._apply_result_tone = "success" if ok else "error"
        self._scan_outdated = True
        self._rescan_after_apply = True
        self.stale = True
        self._refresh_summary()

    def _leftover_hint(self) -> str:
        """失败后检查一下目录里有没有遗留的临时名文件（取前几个做提示）。"""
        root = str(self.state.root or "")
        if not root or not os.path.isdir(root):
            return ""
        try:
            left = A.temp_leftovers(root)
        except Exception:                       # noqa: BLE001 - 提示失败不该影响主流程
            return ""
        if not left:
            return ""
        sample = "、".join(os.path.basename(p) for p in left[:3])
        more = "…" if len(left) > 3 else ""
        return ("；另发现 %d 个临时名残留（%s%s），"
                "可用 `main.py leftovers \"%s\"` 查看后手工处理"
                % (len(left), sample, more, root))

    # ================= 预设互通 =================
    def collect_preset(self, name: str):
        return A.preset_from_state(name, self.rules, self.state)

    def apply_preset(self, preset):
        # 预设里不含根目录（`FormState.scan_dict()` 不写 root，页面文案也写着
        # "不含根目录"），所以套预设必须**保住当前目录**：重建 FormState 会把
        # 用户刚选好的目录一起清空，还得重新选一次。
        root = str(self.state.root or "")
        self.rules = list(preset.rules or [])
        self.state = A.FormState(root=root)
        if preset.scan:
            self.state.apply_scan_dict(preset.scan)
        if preset.plan:
            self.state.apply_plan_dict(preset.plan)
        self.selected_id = self.rules[0].id if self.rules else ""
        self.bundle = None
        self._scan_outdated = False
        self._rescan_after_apply = False
        self._refresh_is_apply = False
        self.path.setText(self.state.root)
        self.only.set_value(self.state.only)
        self.sort.set_value(self.state.sort_mode)
        # 工具栏的两个勾选框也要跟着预设走，否则界面显示的扫描范围与
        # state 里的实际值不一致（预设 recursive=true 时框还是空的）。
        for holder, on in ((getattr(self, "cb_recursive", None), self.state.recursive),
                           (getattr(self, "cb_hidden", None), self.state.include_hidden)):
            if holder is None or not hasattr(holder, "_cb"):
                continue
            holder._cb.blockSignals(True)
            holder._cb.setChecked(bool(on))
            holder._cb.blockSignals(False)
        self.sort.blockSignals(True)
        self.sort.set_value(self.state.sort_mode)
        self.sort.blockSignals(False)
        self._refresh_all()
        self.status_message.emit("已应用预设：%s" % preset.name)
        # 目录还在就直接按新预设重扫一遍，省掉"再点一次扫描"这一步
        if self.rules and root and os.path.isdir(root):
            self.scan(plan_after=True)

    def shutdown(self):
        self.runner.wait(3000)
