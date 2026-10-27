# -*- coding: utf-8 -*-
"""
ui.window —— 主窗口：顶栏 + 侧栏 + 页面栈 + 状态栏。

这里只做四件事：装页面、连线、跨页契约、偏好读写。
业务都在 pages/* 与 adapters/engine_adapter 里。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QInputDialog, QLabel, QMainWindow,
                               QPushButton, QStackedWidget, QVBoxLayout,
                               QWidget)

from adapters import engine_adapter as A
from ui import prefs as P
from ui.base import (C, F, HEADER_H, STATUS_H, SIDEBAR_W, WIN_H, WIN_W, Button,
                     Elided, Label, NavButton, hbox, icon, svg_pixmap, vbox)
from ui.pages.history import HistoryPage
from ui.pages.llm_settings import LlmPage
from ui.pages.presets import PresetsPage
from ui.pages.workspace import WorkspacePage

NAV = [("workspace", "重命名工作台"), ("preset", "预设"),
       ("history", "执行记录"), ("spark", "LLM 设置")]

QSS = """
#Root { background: %(white)s; }
#TopBar { background: %(white)s; border-bottom: 1px solid %(line)s; }
#SideBar { background: %(s1)s; border-right: 1px solid %(line)s; }
#StatusBar { background: %(s1)s; border-top: 1px solid %(line)s; }

QPushButton { border: 1px solid %(line)s; border-radius: 6px; background: %(white)s;
              color: %(t1)s; padding: 0 12px; }
QPushButton:hover { background: %(s1)s; }
QPushButton:pressed { background: %(s2)s; }
QPushButton:disabled { color: %(t4)s; background: %(white)s; }
QPushButton#BtnPrimary { background: %(t1)s; border-color: %(t1)s; color: #FFFFFF; }
QPushButton#BtnPrimary:hover { background: #2A2A2A; border-color: #2A2A2A; }
QPushButton#BtnPrimary:disabled { background: %(t4)s; border-color: %(t4)s; color: #FFFFFF; }
QPushButton#BtnSm { border-radius: 5px; padding: 0 9px; }
QPushButton#BtnMid { border-radius: 5px; padding: 0 10px; }

#Card { background: %(white)s; border: 1px solid %(line)s; border-radius: 6px; }
#OptBar { background: %(s1)s; border: 1px solid %(line)s; border-radius: 6px; }
#Input { background: %(white)s; border: 1px solid %(line2)s; border-radius: 4px; }
#Select { background: %(white)s; border: 1px solid %(line2)s; border-radius: 4px; }
#Select:hover { border-color: %(t3)s; }
#LRow { background: transparent; border-bottom: 1px solid #F0F0F0; }
#LRow:hover { background: %(s1)s; }
#PRow { background: transparent; }
#PRow:hover { background: %(s1)s; }
#RuleRow { background: transparent; border-radius: 5px; }
#RuleRow:hover { background: %(s1)s; }
#RuleRow[selected="true"] { background: %(s2)s; }
#RuleRow[selected="true"]:hover { background: #EFEFEF; }
#DirBar { background: %(s1)s; border: 1px solid %(line)s; border-radius: 6px; }
#HelpBtn { border: 0; background: transparent; border-radius: 15px; padding: 0; }
#HelpBtn:hover { background: %(s2)s; }
#SrcNow { background: %(s2)s; border-radius: 4px; }
""" % C


class MainWindow(QMainWindow):

    def __init__(self, prefs: dict | None = None):
        super().__init__()
        self.setWindowTitle("%s %s" % (A.APP_NAME, A.APP_VER))
        self.setObjectName("Root")
        self.resize(WIN_W, WIN_H)
        self.setMinimumSize(1240, 720)
        self.setStyleSheet(QSS)

        self._prefs = dict(P.DEFAULTS)
        self._prefs.update(prefs or {})

        central = QWidget()
        central.setObjectName("Root")
        root = vbox(central, 0, 0)
        root.addWidget(self._header())

        body = QWidget()
        body.setObjectName("Root")
        row = hbox(body, 0, 0)
        row.addWidget(self._sidebar())

        self.stack = QStackedWidget()
        self.stack.setObjectName("Root")
        self.page_ws = WorkspacePage()
        self.page_presets = PresetsPage()
        self.page_history = HistoryPage()
        self.page_llm = LlmPage()
        for page in (self.page_ws, self.page_presets, self.page_history, self.page_llm):
            self.stack.addWidget(page)

        # 页面本身不滚动：列表都在各自卡片内滚动（见 ui/pages/*）
        row.addWidget(self.stack, 1)
        root.addWidget(body, 1)
        root.addWidget(self._statusbar())
        self.setCentralWidget(central)

        self._wire()
        self._restore()
        self._goto(0)

    # ---------------- 顶栏 ----------------
    def _header(self):
        head = QWidget()
        head.setObjectName("TopBar")
        head.setAttribute(Qt.WA_StyledBackground, True)
        head.setFixedHeight(HEADER_H)
        lay = hbox(head, (20, 0, 14, 0), 8)
        logo = QLabel()
        logo.setPixmap(svg_pixmap("cat", C["t1"], 26))
        logo.setFixedSize(26, 26)
        lay.addWidget(logo, 0, Qt.AlignVCenter)
        lay.addWidget(Label("NameCat", 15, 600, C["t1"], -0.1), 0, Qt.AlignVCenter)
        ver = QLabel("v%s" % A.APP_VER)
        ver.setFont(F(11, 500))
        ver.setFixedHeight(20)
        ver.setContentsMargins(6, 0, 6, 0)
        ver.setStyleSheet("QLabel{background:%s;color:%s;border-radius:4px;}"
                          % (C["s2"], C["t2"]))
        lay.addWidget(ver, 0, Qt.AlignVCenter)
        lay.addWidget(QWidget(), 1)

        help_btn = QPushButton()
        help_btn.setObjectName("HelpBtn")
        help_btn.setIcon(icon("help", C["t2"], 17))
        help_btn.setIconSize(QSize(17, 17))
        help_btn.setFixedSize(30, 30)
        help_btn.setCursor(Qt.PointingHandCursor)
        help_btn.clicked.connect(self.show_help)
        lay.addWidget(help_btn, 0, Qt.AlignVCenter)
        return head

    # ---------------- 侧栏 ----------------
    def _sidebar(self):
        side = QWidget()
        side.setObjectName("SideBar")
        side.setAttribute(Qt.WA_StyledBackground, True)
        side.setFixedWidth(SIDEBAR_W)
        lay = vbox(side, (12, 10, 12, 10), 2)
        self.nav = []
        for i, (ic, text) in enumerate(NAV):
            b = NavButton(ic, text)
            b.clicked.connect(lambda _c=False, idx=i: self._goto(idx))
            lay.addWidget(b)
            self.nav.append(b)
        lay.addStretch(1)
        lay.addWidget(NavButton("left", "收起侧栏", height=32, size=12, icon_size=16,
                                muted=True, checkable=False))
        return side

    # ---------------- 状态栏 ----------------
    def _statusbar(self):
        bar = QWidget()
        bar.setObjectName("StatusBar")
        bar.setAttribute(Qt.WA_StyledBackground, True)
        bar.setFixedHeight(STATUS_H)
        lay = hbox(bar, (14, 0, 14, 0), 8)
        self.status = Label("就绪", 11, 400, C["t2"])
        lay.addWidget(self.status, 1)
        self.status_path = Label("", 11, 400, C["t3"])
        lay.addWidget(self.status_path)
        return bar

    # ---------------- 连线 ----------------
    def _wire(self):
        for page in (self.page_ws, self.page_presets, self.page_history, self.page_llm):
            page.status_message.connect(self.show_status)
        self.page_presets.apply_requested.connect(self.apply_preset)
        self.page_ws.load_preset_requested.connect(lambda: self._goto(1))
        self.page_ws.save_preset_requested.connect(self.save_preset)
        self.page_llm.saved.connect(self._remember)

    def _goto(self, index):
        self.stack.setCurrentIndex(index)
        for i, b in enumerate(self.nav):
            b.setChecked(i == index)
        if index == 1:
            self.page_presets.reload()
        elif index == 2:
            if not self.page_history._root:
                self.page_history.set_root(self.workspace_root())
        elif index == 3:
            self.page_llm.reload(False)
        self._remember()

    # ---------------- 跨页契约 ----------------
    def llm_overrides(self):
        return self.page_llm.overrides()

    def workspace_root(self) -> str:
        return str(getattr(self.page_ws.state, "root", "") or "")

    def show_status(self, text):
        self.status.setText(text or "就绪")
        self.status_path.setText(self.workspace_root())

    def apply_preset(self, preset):
        self.page_ws.apply_preset(preset)
        self._goto(0)
        self.show_status("已应用预设：%s" % getattr(preset, "name", ""))

    def save_preset(self):
        name, ok = QInputDialog.getText(self, "保存为预设", "预设名称：", text="我的预设")
        if not ok or not name.strip():
            return
        try:
            path = A.save_preset(self.page_ws.collect_preset(name.strip()))
        except Exception as exc:
            self.show_status("保存预设失败：%s" % exc)
            return
        self.show_status("已保存预设：%s" % path)
        self.page_presets.reload()

    def show_help(self):
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(
            self, "帮助",
            "NameCat %s —— 批量重命名\n\n"
            "工作流：选择目录 → 扫描 → 加规则 → 生成预览 → 执行。\n"
            "预览只算新名字，不落盘；执行会写撤销日志，可在「执行记录」撤回。\n\n"
            "快捷键：F5 重新扫描 · Ctrl+Enter 生成预览 · Ctrl+1…4 切页\n\n"
            "预设目录：%s\n撤销日志：<目标目录>\\_rename_logs\n要求文件：%s"
            % (A.APP_VER, A.preset_dir(), A.llm_instruction_path()))

    # ---------------- 偏好 ----------------
    def _restore(self):
        root = str(self._prefs.get("last_root") or "")
        if root:
            self.page_ws.state.root = root
            self.page_ws.path.setText(root)
        self.page_llm.set_prefs(self._prefs)
        self.show_status("就绪")

    def _remember(self):
        provider, model, batch = self.llm_overrides()
        self._prefs.update({
            "last_root": self.workspace_root(),
            "llm_provider": provider,
            "llm_model": model,
            "llm_batch": int(batch or 0),
        })
        P.save(self._prefs)

    def closeEvent(self, event):  # noqa: N802
        self._remember()
        for page in (self.page_ws, self.page_history):
            shutdown = getattr(page, "shutdown", None)
            if callable(shutdown):
                shutdown()
        super().closeEvent(event)

    # ---------------- 快捷键 ----------------
    def keyPressEvent(self, event):  # noqa: N802
        key = event.key()
        mods = event.modifiers()
        if mods & Qt.ControlModifier and Qt.Key_1 <= key <= Qt.Key_4:
            self._goto(key - Qt.Key_1)
            return
        if mods & Qt.ControlModifier and key in (Qt.Key_Return, Qt.Key_Enter):
            self._goto(0)
            self.page_ws.request_preview()
            return
        if key == Qt.Key_F5:
            self._goto(0)
            self.page_ws.scan()
            return
        super().keyPressEvent(event)
