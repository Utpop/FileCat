# -*- coding: utf-8 -*-
"""
ui.base —— 设计令牌、字体、图标与基础控件（新界面的公共层）

设计基准
    · 逻辑画布 1440×900，全部尺寸为逻辑像素（dp），由 Qt 按屏幕 DPI 换算成物理像素；
    · 字体为仓库自带的 HarmonyOS Sans SC 可变字体，400/500/600 三档取真实字形；
    · 颜色、圆角、字号只在本文件顶部 TOKENS 区定义一次。

这里只放“长什么样”的东西：令牌、字体、图标、基础控件。
业务与数据在 ui/pages/*，窗口骨架在 ui/window.py。
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
VENDOR = os.path.join(PROJECT, "_vendor")
for _p in (PROJECT, VENDOR):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from PySide6.QtCore import QByteArray, QPointF, QRectF, QSize, Qt, Signal  # noqa: E402
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetrics,  # noqa: E402
                           QGuiApplication, QIcon, QPainter, QPen, QPixmap, QPolygonF)
from PySide6.QtSvg import QSvgRenderer  # noqa: E402
from PySide6.QtWidgets import (QAbstractButton, QApplication, QFrame, QGridLayout,  # noqa: E402
                               QHBoxLayout, QLabel, QMainWindow, QPushButton,
                               QScrollArea, QSizePolicy, QSplitter, QStackedWidget,
                               QVBoxLayout, QWidget)

# --------------------------------------------------------------------------- #
# 设计令牌
# --------------------------------------------------------------------------- #

C = {
    "white": "#FFFFFF",
    "s1": "#FAFAFA",
    "s2": "#F5F5F5",
    "sel": "#F0F0F0",
    "t1": "#171717",
    "t2": "#737373",
    "t3": "#A3A3A3",
    "t4": "#D4D4D4",
    "line": "#E5E5E5",
    "line2": "#D4D4D4",
    "ok": "#15803D", "ok_bg": "#F0FDF4",
    "warn": "#B45309", "warn_bg": "#FFFBEB",
    "err": "#DC2626", "err_bg": "#FEF2F2",
    "info": "#2563EB", "info_bg": "#EFF6FF",
}

R_CARD, R_SM, R_XS = 6, 5, 4
FONT_FILE = os.path.join(HERE, "fonts", "HarmonyOS_Sans_SC.ttf")
FAMILY = "HarmonyOS Sans SC"

WIN_W, WIN_H = 1440, 900
HEADER_H, SIDEBAR_W, STATUS_H = 56, 204, 28
MARGIN_X, MARGIN_T, MARGIN_B, GAP = 20, 18, 12, 12

APP_TITLE = "NameCat 1.2.0"


# --------------------------------------------------------------------------- #
# 字体与图标
# --------------------------------------------------------------------------- #

def load_fonts() -> str:
    """注册随包字体；失败时退回系统无衬线，界面不会崩。"""
    fid = QFontDatabase.addApplicationFont(FONT_FILE)
    if fid >= 0:
        fams = QFontDatabase.applicationFontFamilies(fid)
        if fams:
            return fams[0]
    return "Microsoft YaHei UI"


def F(size: int, weight: int = 400, spacing: float = 0.0) -> QFont:
    f = QFont(FAMILY)
    f.setPixelSize(size)
    f.setWeight(QFont.Weight(weight))
    f.setStyleStrategy(QFont.PreferAntialias)
    if spacing:
        f.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
    return f


# 24×24 线性图标（1.6 描边），与设计稿同一套路径
ICONS = {
    "cat": '<path d="M5.4 8.4 4.9 4.3l4.1 2.3"/><path d="M18.6 8.4l.5-4.1-4.1 2.3"/>'
           '<path d="M12 6.5c4.3 0 7.3 2.9 7.3 6.8 0 3.9-3.2 6.5-7.3 6.5s-7.3-2.6-7.3-6.5c0-3.9 3-6.8 7.3-6.8Z"/>'
           '<path d="M9.5 12.6h.01M14.5 12.6h.01" stroke-width="2.2"/>'
           '<path d="M12 14.3v1.2M10.6 16.9c.4.3.9.5 1.4.5s1-.2 1.4-.5"/>',
    "help": '<circle cx="12" cy="12" r="8.6"/>'
            '<path d="M9.6 9.5a2.5 2.5 0 1 1 3.4 2.4c-.6.2-1 .8-1 1.5v.5"/>'
            '<path d="M12 17h.01" stroke-width="2.2"/>',
    "workspace": '<rect x="3.2" y="4.2" width="17.6" height="15.6" rx="2.4"/>'
                 '<path d="M3.2 9.2h17.6M9.4 9.2v10.6"/>',
    "preset": '<path d="M6.6 4.3h10.8a1 1 0 0 1 1 1v14.4l-6.4-4-6.4 4V5.3a1 1 0 0 1 1-1Z"/>',
    "history": '<circle cx="12" cy="12" r="8.4"/><path d="M12 7.4V12l3.1 1.9"/>',
    "spark": '<path d="M12 3.4c.6 4.3 2.9 6.6 7.2 7.2-4.3.6-6.6 2.9-7.2 7.2-.6-4.3-2.9-6.6-7.2-7.2'
             ' 4.3-.6 6.6-2.9 7.2-7.2Z"/>'
             '<path d="M17.6 15.4c.3 1.6 1 2.3 2.6 2.6-1.6.3-2.3 1-2.6 2.6-.3-1.6-1-2.3-2.6-2.6'
             ' 1.6-.3 2.3-1 2.6-2.6Z"/>',
    "left": '<path d="M14.4 6 8.6 12l5.8 6"/>',
    "right": '<path d="M9.6 6l5.8 6-5.8 6"/>',
    "down": '<path d="M6.2 9.4 12 15l5.8-5.6"/>',
    "plus": '<path d="M12 5.4v13.2M5.4 12h13.2"/>',
    "folder": '<path d="M3.4 7.6A1.6 1.6 0 0 1 5 6h3.9l2 2h8.1a1.6 1.6 0 0 1 1.6 1.6v7.8'
              'A1.6 1.6 0 0 1 19 19H5a1.6 1.6 0 0 1-1.6-1.6Z"/>',
    "scan": '<path d="M4.2 8.6V6.4A2.2 2.2 0 0 1 6.4 4.2h2.2M15.4 4.2h2.2a2.2 2.2 0 0 1 2.2 2.2v2.2'
            'M19.8 15.4v2.2a2.2 2.2 0 0 1-2.2 2.2h-2.2M8.6 19.8H6.4a2.2 2.2 0 0 1-2.2-2.2v-2.2"/>'
            '<path d="M4.6 12h14.8"/>',
    "up": '<path d="M12 18.4V5.6M6.4 11.2 12 5.6l5.6 5.6"/>',
    "arrdown": '<path d="M12 18.4V5.6M6.4 12.8 12 18.4l5.6-5.6"/>',
    "copy": '<rect x="9" y="9" width="11.4" height="11.4" rx="2"/>'
            '<path d="M5.6 15.2V6.4A1.8 1.8 0 0 1 7.4 4.6h8.8"/>',
    "trash": '<path d="M4.6 7.2h14.8M9.6 7.2V5.6a1.4 1.4 0 0 1 1.4-1.4h2a1.4 1.4 0 0 1 1.4 1.4v1.6"/>'
             '<path d="M6.6 7.2l.8 11.1a1.5 1.5 0 0 0 1.5 1.4h6.2a1.5 1.5 0 0 0 1.5-1.4l.8-11.1"/>',
    "grip": '<g fill="{c}" stroke="none"><circle cx="9.2" cy="7" r="1.15"/><circle cx="14.8" cy="7" r="1.15"/>'
            '<circle cx="9.2" cy="12" r="1.15"/><circle cx="14.8" cy="12" r="1.15"/>'
            '<circle cx="9.2" cy="17" r="1.15"/><circle cx="14.8" cy="17" r="1.15"/></g>',
    "check": '<path d="M4.6 12.6 9.4 17.4 19.4 6.6"/>',
    "info": '<circle cx="12" cy="12" r="8.6"/><path d="M12 11.2v5.4"/>'
            '<path d="M12 7.8h.01" stroke-width="2.2"/>',
    "x": '<path d="M6.4 6.4l11.2 11.2M17.6 6.4 6.4 17.6"/>',
    "play": '<path d="M8.4 5.6 18.6 12 8.4 18.4Z" fill="{c}" stroke="none"/>',
    "refresh": '<path d="M19.4 12a7.4 7.4 0 1 1-2.2-5.2"/><path d="M19.4 4.6v4.6h-4.6"/>',
    "open": '<path d="M3.4 8.4A1.6 1.6 0 0 1 5 6.8h3.6l1.9 2h7.5a1.6 1.6 0 0 1 1.6 1.6v1"/>'
            '<path d="M3.4 17.4V8.4"/><path d="M3.6 17.6 6 12.4h15l-2.4 5.2Z"/>'
            '<path d="M14.6 13.4h4.2v4.2"/>',
    "save": '<path d="M5.2 5.2h10.9l2.9 2.9v10.7H5.2Z"/><path d="M8.6 5.2v5h6.8v-5"/>'
            '<path d="M8.6 14.6h6.8"/>',
    "shield": '<path d="M12 4.2 5.4 6.9v5.3c0 4.1 2.8 6.8 6.6 7.6 3.8-.8 6.6-3.5 6.6-7.6V6.9Z"/>'
              '<path d="M9.3 12.2 11.4 14.3 15 10.5"/>',
    "file": '<path d="M6.2 4.4h7.4l4.2 4.2v11H6.2Z"/><path d="M13.4 4.4v4.4h4.4"/>',
    "expand": '<path d="M9.2 4.6H4.6v4.6"/><path d="M14.8 4.6h4.6v4.6"/>'
              '<path d="M14.8 19.4h4.6v-4.6"/><path d="M9.2 19.4H4.6v-4.6"/>',
    "shrink": '<path d="M4.6 9.2h4.6V4.6"/><path d="M19.4 9.2h-4.6V4.6"/>'
              '<path d="M19.4 14.8h-4.6v4.6"/><path d="M4.6 14.8h4.6v4.6"/>',
}


def svg_pixmap(name: str, color: str, size: int, weight: float = 1.6) -> QPixmap:
    body = ICONS[name].replace("{c}", color)
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
           'stroke="%s" stroke-width="%s" stroke-linecap="round" stroke-linejoin="round">%s</svg>'
           % (color, weight, body))
    dpr = QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1.0
    pm = QPixmap(int(round(size * dpr)), int(round(size * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    QSvgRenderer(QByteArray(svg.encode("utf-8"))).render(p, QRectF(0, 0, size, size))
    p.end()
    return pm


def icon(name: str, color: str, size: int, weight: float = 1.6) -> QIcon:
    return QIcon(svg_pixmap(name, color, size, weight))


# --------------------------------------------------------------------------- #
# 基础控件
# --------------------------------------------------------------------------- #

class Label(QLabel):
    def __init__(self, text="", size=12, weight=400, color=None, ls=0.0, parent=None):
        super().__init__(text, parent)
        self.setFont(F(size, weight, ls))
        self.setStyleSheet("color: %s; background: transparent;" % (color or C["t1"]))
        self.setTextFormat(Qt.PlainText)
        self.setMinimumWidth(0)


class Elided(Label):
    """单行省略：空间不足时尾部省略，全文放 tooltip。"""

    def __init__(self, text="", size=12, weight=400, color=None, parent=None):
        super().__init__("", size, weight, color, parent=parent)
        self._full = ""
        self.setText(text)

    def setText(self, text):  # noqa: N802 - Qt 命名
        self._full = text or ""
        super().setText(self._elided())
        self.setToolTip(self._full)

    def fullText(self):  # noqa: N802
        return self._full

    def _elided(self):
        w = self.contentsRect().width()
        if w <= 0:
            return self._full
        return self.fontMetrics().elidedText(self._full, Qt.ElideRight, w)

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        super().setText(self._elided())


class Card(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setAttribute(Qt.WA_StyledBackground, True)


def hline(color=None) -> QFrame:
    f = QFrame()
    f.setFixedHeight(1)
    f.setStyleSheet("background: %s;" % (color or C["line"]))
    return f


def spacer() -> QWidget:
    w = QWidget()
    w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    return w


def vbox(target=None, m=0, s=0):
    """在 widget 上取（或新建）竖直布局；target 为 None 时返回裸布局。"""
    if isinstance(target, QWidget):
        lay = QVBoxLayout(target)
    elif target is None:
        lay = QVBoxLayout()
    else:
        lay = target
    if isinstance(m, int):
        lay.setContentsMargins(m, m, m, m)
    else:
        lay.setContentsMargins(*m)
    lay.setSpacing(s)
    return lay


def hbox(target=None, m=0, s=0):
    if isinstance(target, QWidget):
        lay = QHBoxLayout(target)
    elif target is None:
        lay = QHBoxLayout()
    else:
        lay = target
    if isinstance(m, int):
        lay.setContentsMargins(m, m, m, m)
    else:
        lay.setContentsMargins(*m)
    lay.setSpacing(s)
    return lay


class Button(QPushButton):
    """34 高普通按钮 / primary 主按钮 / sm 26 / mid 30。"""

    def __init__(self, text="", name="", kind="normal", icon_color=None, parent=None, icon_px=14):
        super().__init__(text, parent)
        self._kind = kind
        self.setCursor(Qt.PointingHandCursor)
        self.setFont(F(12 if kind in ("sm", "mid") else 13, 400 if kind == "normal" else 500))
        obj = {"normal": "Btn", "primary": "BtnPrimary", "sm": "BtnSm",
               "mid": "BtnMid", "text": "BtnText"}[kind]
        self.setObjectName(obj)
        if name:
            col = "#FFFFFF" if kind == "primary" else (icon_color or C["t1"])
            self.setIcon(icon(name, col, icon_px))
            self.setIconSize(QSize(icon_px, icon_px))
        h = {"normal": 34, "primary": 34, "sm": 26, "mid": 30, "text": 18}[kind]
        self.setFixedHeight(h)
        if kind == "text":
            self.setFlat(True)


class Check(QAbstractButton):
    """14×14 自绘复选框（圆角 4）。"""

    def __init__(self, checked=True, enabled=True, parent=None, size=14):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setEnabled(enabled)
        self.setCursor(Qt.PointingHandCursor)
        self._size = size
        self.setFixedSize(size, size)

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(0.5, 0.5, self._size - 1, self._size - 1)
        if self.isChecked():
            bg = C["t1"] if self.isEnabled() else C["line2"]
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(bg))
            p.drawRoundedRect(r, R_XS, R_XS)
            # 注意：这里必须新建 QPen —— setPen(Qt.NoPen) 之后 p.pen() 仍是 NoPen，
            # 直接改它的颜色/宽度不会恢复描边，勾就会画不出来。
            pen = QPen(QColor("#FFFFFF"))
            pen.setWidthF(max(1.4, self._size / 9.0))
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            k = self._size / 14.0
            poly = QPolygonF([QPointF(3.6 * k, 7.6 * k), QPointF(6.0 * k, 10.0 * k),
                              QPointF(10.6 * k, 4.8 * k)])
            p.drawPolyline(poly)
            p.setPen(Qt.NoPen)
        else:
            p.setPen(QColor(C["line2"]))
            p.setBrush(QColor(C["white"]))
            p.drawRoundedRect(r, R_XS, R_XS)
        p.end()

    @staticmethod
    def _pt(x, y):
        from PySide6.QtCore import QPointF
        return QPointF(x, y)


class Badge(QLabel):
    def __init__(self, text="", tone="muted", parent=None):
        super().__init__(text, parent)
        self.setFont(F(11, 500))
        self.setAlignment(Qt.AlignCenter)
        self.setFixedHeight(18)
        self.setContentsMargins(6, 0, 6, 0)
        bg, fg = {
            "muted": (C["s2"], C["t2"]), "mut2": (C["s2"], C["t3"]),
            "ok": (C["ok_bg"], C["ok"]), "warn": (C["warn_bg"], C["warn"]),
            "err": (C["err_bg"], C["err"]), "info": (C["info_bg"], C["info"]),
        }[tone]
        self.setStyleSheet("QLabel{background:%s;color:%s;border-radius:%dpx;}"
                           % (bg, fg, R_XS))


class Chip(QWidget):
    def __init__(self, label, value, tone="muted", parent=None):
        super().__init__(parent)
        bg, fg = {
            "muted": (C["s2"], C["t2"]), "ok": (C["ok_bg"], C["ok"]),
            "warn": (C["warn_bg"], C["warn"]), "err": (C["err_bg"], C["err"]),
            "info": (C["info_bg"], C["info"]),
        }[tone]
        self.setStyleSheet("QWidget{background:%s;border-radius:%dpx;}" % (bg, R_XS))
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(26)
        lay = hbox(self, (8, 0, 8, 0), 6)
        lay.addWidget(Label(label, 11, 400, fg), 0, Qt.AlignVCenter)
        self.val = Label(str(value), 12, 600, fg if tone != "muted" else C["t1"])
        lay.addWidget(self.val, 0, Qt.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)


class NoticeBar(QFrame):
    """页内提示条：info / warn / error / success 四种语气。"""

    def __init__(self, text="", tone="info", parent=None):
        super().__init__(parent)
        self.setObjectName("Notice")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(34)
        lay = hbox(self, (12, 0, 12, 0), 8)
        self._icon = QLabel()
        self._icon.setFixedSize(15, 15)
        lay.addWidget(self._icon, 0, Qt.AlignVCenter)
        self.label = Elided(text, 12, 400, C["info"])
        lay.addWidget(self.label, 1, Qt.AlignVCenter)
        self._close = QPushButton()
        self._close.setIconSize(QSize(13, 13))
        self._close.setFixedSize(20, 20)
        self._close.setCursor(Qt.PointingHandCursor)
        self._close.clicked.connect(self.hide)
        lay.addWidget(self._close, 0, Qt.AlignVCenter)
        self.set_text(text, tone)

    def set_text(self, text, tone="info"):
        bg, fg = {
            "info": (C["info_bg"], C["info"]),
            "warn": (C["warn_bg"], C["warn"]),
            "error": (C["err_bg"], C["err"]),
            "success": (C["ok_bg"], C["ok"]),
        }.get(tone, (C["info_bg"], C["info"]))
        self.setStyleSheet("#Notice{background:%s;border-radius:%dpx;}" % (bg, R_CARD))
        self._icon.setPixmap(svg_pixmap("info", fg, 15))
        self.label.setStyleSheet("color:%s;background:transparent;" % fg)
        flat = " · ".join(line.strip() for line in str(text or "").splitlines() if line.strip())
        self.label.setText(flat)
        self.label.setToolTip(flat)
        self._close.setIcon(icon("x", fg, 13))
        self._close.setStyleSheet(
            "QPushButton{border:0;background:transparent;border-radius:4px;}"
            "QPushButton:hover{background:rgba(0,0,0,0.04);}")


class Select(QWidget):
    """只读下拉外观（点击弹出菜单）。"""

    def __init__(self, text, width=148, height=32, size=12, parent=None):
        super().__init__(parent)
        self.setFixedHeight(height)
        if width:
            self.setFixedWidth(width)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("Select")
        lay = hbox(self, (10, 0, 8, 0), 8)
        self.label = Elided(text, size, 400, C["t1"])
        lay.addWidget(self.label, 1, Qt.AlignVCenter)
        ic = QLabel()
        ic.setPixmap(svg_pixmap("down", C["t2"], 13))
        ic.setFixedSize(13, 13)
        lay.addWidget(ic, 0, Qt.AlignVCenter)


class Input(QFrame):
    """36 高输入框外观（带前置图标）。"""

    def __init__(self, text, icon_name="", height=36, size=13, width=0, parent=None):
        super().__init__(parent)
        self.setObjectName("Input")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(height)
        if width:
            self.setFixedWidth(width)
        lay = hbox(self, (10, 0, 10, 0), 8)
        if icon_name:
            ic = QLabel()
            ic.setPixmap(svg_pixmap(icon_name, C["t3"], 15))
            ic.setFixedSize(15, 15)
            lay.addWidget(ic, 0, Qt.AlignVCenter)
        self.label = Elided(text, size, 400, C["t1"])
        lay.addWidget(self.label, 1, Qt.AlignVCenter)


class StatRow(QWidget):
    def __init__(self, cols, parent=None):
        super().__init__(parent)
        self._lay = hbox(self, 0, 0)
        for c in cols:
            self._lay.addWidget(c)


class Strip(QWidget):
    """纯色底条（表头 / 页脚）：自绘，避免依赖样式表继承。"""

    def __init__(self, color, radius=0, parent=None):
        super().__init__(parent)
        self._color = color
        self._radius = radius

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self._color))
        r = QRectF(self.rect())
        if self._radius:
            p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), self._radius, self._radius)
        else:
            p.drawRect(r)
        p.end()


def grid_row(items, height, pad=(0, 8), spacing=0, holder=None):
    """一行等距栅格：int 表示定宽，控件表示 1fr。"""
    w = holder if holder is not None else QWidget()
    lay = hbox(w, (pad[1], 0, pad[1], 0), spacing)
    for it in items:
        if isinstance(it, tuple):
            widget, width = it
            widget.setFixedWidth(width)
            lay.addWidget(widget, 0)
        else:
            it.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            lay.addWidget(it, 1)
    w.setFixedHeight(height)
    return w


# --------------------------------------------------------------------------- #
# 页面通用：页头
# --------------------------------------------------------------------------- #

class PageHead(QWidget):
    def __init__(self, title, subtitle, actions, parent=None):
        super().__init__(parent)
        lay = hbox(self, 0, 16)
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(2)
        left.addWidget(Label(title, 22, 600, C["t1"], -0.2))
        sub = Elided(subtitle, 12, 400, C["t2"])
        sub.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        sub.setMinimumWidth(120)
        left.addWidget(sub)
        lay.addLayout(left, 1)
        right = QHBoxLayout()
        right.setContentsMargins(0, 2, 0, 0)
        right.setSpacing(8)
        for a in actions:
            right.addWidget(a, 0, Qt.AlignTop)
        lay.addLayout(right, 0)
        self.setFixedHeight(49)


def fit_scroll_content(area):
    """把滚动容器的内容高度补到真实值。

    widgetResizable 只在滚动区域自身 resize 时重算内层大小；放在固定高度
    卡片里的列表加行后拿不到 resize 事件，内容就会被压扁/溢出。刷新完
    列表后调用本函数手动补一刀。
    """
    w = area.widget()
    if w is None:
        return
    # 新加入的行在第一次 show 之前 isHidden() 为真，布局会把它们当不存在
    # （sizeHint 全是 0），量出来的内容高度就是错的。先显式显示一遍。
    for child in w.findChildren(QWidget):
        if child.isHidden():
            child.show()
    need = max(w.minimumSizeHint().height(), w.sizeHint().height())
    vp_h = area.viewport().height()
    # 关键：只 resize 会被下一次布局压回视口高度；把最小高度设为内容高度，
    # Qt 就不可能再把内层压扁（行变少时会自动恢复成较小值）。
    w.setMinimumHeight(max(need, 1))
    w.resize(area.viewport().width(), max(need, vp_h))


class Clickable(QWidget):
    """可点击的透明容器：把左键点击变成 clicked 信号。

    重要：PySide6 不能用 `widget.mousePressEvent = fn` 这种实例属性去覆盖
    事件处理 —— C++ 侧按类分发虚函数，实例属性根本不会被调用，点击毫无反应。
    需要可点的地方必须子类化（本类）或用 QPushButton。
    """

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


class OptBar(Strip):
    """「编号与执行选项」折叠条：整条可点，点击发 clicked。"""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(C["s1"], R_CARD, parent)

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


SCROLL_QSS = """
QScrollArea { background: transparent; border: 0; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: %s; border-radius: 5px; min-height: 32px; }
QScrollBar::handle:vertical:hover { background: %s; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
""" % (C["line2"], C["t3"])


def scroll_area(widget, min_height=0, padding_right=0):
    """把内容装进带细滚动条的容器：只纵向滚动，横向关掉。"""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    area.setStyleSheet(SCROLL_QSS)
    if padding_right:
        area.setViewportMargins(0, 0, padding_right, 0)
    area.setWidget(widget)
    if min_height:
        area.setMinimumHeight(min_height)
    return area


class NavButton(QAbstractButton):
    """侧栏导航项：自绘，保证图标/文字基线与设计稿一致。"""

    def __init__(self, icon_name, text, height=40, size=13, icon_size=17,
                 pad=10, gap=10, muted=False, checkable=True, parent=None):
        super().__init__(parent)
        self._ic = icon_name
        self._txt = text
        self._fs = size
        self._isz = icon_size
        self._pad = pad
        self._gap = gap
        self._muted = muted
        self._hover = False
        self.setCheckable(checkable)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def enterEvent(self, e):  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, e):  # noqa: N802
        self._hover = False
        self.update()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        on = self.isChecked()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if on:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(C["sel"]))
            p.drawRoundedRect(rect, R_CARD, R_CARD)
        elif self._hover:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(C["s2"]))
            p.drawRoundedRect(rect, R_CARD, R_CARD)
        if self._muted:
            color = C["t3"] if not self._hover else C["t2"]
        else:
            color = C["t1"] if on else C["t2"]
        y = (self.height() - self._isz) / 2.0
        p.drawPixmap(int(self._pad), int(y), svg_pixmap(self._ic, color, self._isz))
        p.setPen(QColor(color))
        p.setFont(F(self._fs, 500 if on else 400))
        left = self._pad + self._isz + self._gap
        p.drawText(QRectF(left, 0, self.width() - left - 6, self.height()),
                   Qt.AlignLeft | Qt.AlignVCenter, self._txt)
        p.end()
