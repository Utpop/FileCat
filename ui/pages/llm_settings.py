# -*- coding: utf-8 -*-
"""ui.pages.llm —— LLM 设置（配置只读环境变量，界面不回显密钥）。"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QScrollArea, QWidget)

from adapters import engine_adapter as A
from ui.base import (C, F, R_XS, Button, Card, Elided, Label, NoticeBar, PageHead,
                     hbox, svg_pixmap, vbox)
from ui.pages.workspace import Combo, FieldEdit

PROVIDERS = [("", "跟随环境变量"), ("openai", "HTTP 接口"), ("ollama", "本地 Ollama"),
             ("echo", "离线替身")]


class LlmPage(QWidget):

    status_message = Signal(str)
    saved = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Root")
        self._cfg = None
        self._client = None

        outer = vbox(self, (20, 18, 20, 12), 12)
        self.btn_open_cfg = Button("打开配置文件", "file")
        self.btn_open_cfg.clicked.connect(self.open_instruction)
        outer.addWidget(PageHead(
            "LLM 设置", "只用于「整理要求」的自然语言解析；密钥在界面上始终遮蔽显示",
            [self.btn_open_cfg]))

        self.notice = NoticeBar("密钥只从环境变量读取，本页不落盘")
        outer.addWidget(self.notice)
        outer.addWidget(self._status())

        grid = QHBoxLayout()
        grid.setSpacing(12)
        left = vbox(None, 0, 12)
        left.addWidget(self._override())
        left.addWidget(self._detail(), 1)
        right = vbox(None, 0, 12)
        right.addWidget(self._sources())
        right.addWidget(self._require(), 1)

        lw = QWidget()
        lw.setLayout(left)
        lw.setMinimumWidth(560)
        rw = QWidget()
        rw.setLayout(right)
        rw.setMinimumWidth(320)
        from PySide6.QtWidgets import QSplitter
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(12)
        split.addWidget(lw)
        split.addWidget(rw)
        split.setSizes([756, 428])
        split.setStyleSheet("QSplitter::handle{background:transparent;}")
        grid.addWidget(split, 1)
        outer.addLayout(grid, 1)
        outer.addWidget(self._savebar())
        self.reload()

    # ---------------- 状态卡 ----------------
    def _status(self):
        card = Card()
        card.setMinimumHeight(70)
        lay = hbox(card, (16, 16, 16, 16), 16)

        self.badge = QLabel("未读取")
        self.badge.setFont(F(11, 500))
        self.badge.setFixedHeight(26)
        self.badge.setContentsMargins(9, 0, 9, 0)
        self.badge.setStyleSheet("QLabel{background:%s;color:%s;border-radius:%dpx;}"
                                 % (C["s2"], C["t2"], R_XS))
        lay.addWidget(self.badge, 0, Qt.AlignVCenter)

        stack = vbox(None, 0, 2)
        self.line1 = Elided("", 13, 500, C["t1"])
        self.line2 = Elided("", 11, 400, C["t3"])
        stack.addWidget(self.line1)
        stack.addWidget(self.line2)
        lay.addLayout(stack, 1)

        test = Button("测试连接", kind="mid")
        test.clicked.connect(self.test)
        lay.addWidget(test, 0, Qt.AlignVCenter)
        reload_btn = Button("重新加载配置", kind="mid")
        reload_btn.clicked.connect(lambda: self.reload(True))
        lay.addWidget(reload_btn, 0, Qt.AlignVCenter)
        return card

    # ---------------- 覆盖参数 ----------------
    def _override(self):
        card = Card()
        card.setFixedHeight(178)
        lay = vbox(card, 14, 0)
        head = hbox(None, 0, 8)
        head.addWidget(Label("本次运行覆盖参数", 13, 600))
        head.addStretch(1)
        head.addWidget(Label("只对本次运行生效，不写回环境变量", 11, 400, C["t3"]))
        lay.addLayout(head)
        lay.addSpacing(10)

        self.provider = Combo(PROVIDERS, "", width=260, height=30)
        self.provider.setMinimumWidth(200)
        self.provider.changed.connect(lambda _v: self.reload(False))
        lay.addWidget(self._row("提供商", self.provider))

        self.model = FieldEdit("", width=260, height=30)
        self.model.setMinimumWidth(200)
        self.model.edit.setPlaceholderText("留空 = 用环境变量里的模型名")
        self.model.edited.connect(lambda: self.reload(False))
        lay.addWidget(self._row("模型", self.model))

        self.batch = FieldEdit("", width=110, height=30)
        self.batch.edit.setPlaceholderText("默认 20")
        self.batch.edited.connect(lambda: self.reload(False))
        row = hbox(None, 0, 10)
        lb = Label("每批条数", 12, 400, C["t2"])
        lb.setFixedWidth(80)
        row.addWidget(lb, 0, Qt.AlignVCenter)
        row.addWidget(self.batch, 0)
        row.addWidget(Label("一次请求带多少条名字，太多模型容易漏项", 11, 400, C["t3"]), 0)
        row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        holder.setFixedHeight(32)
        lay.addWidget(holder)
        lay.addStretch(1)
        return card

    def _row(self, label, control):
        holder = QWidget()
        row = hbox(holder, 0, 10)
        lb = Label(label, 12, 400, C["t2"])
        lb.setFixedWidth(80)
        row.addWidget(lb, 0, Qt.AlignVCenter)
        row.addWidget(control, 0)
        row.addStretch(1)
        holder.setFixedHeight(32)
        return holder

    # ---------------- 明细 ----------------
    def _detail(self):
        card = Card()
        lay = vbox(card, 14, 0)
        lay.addWidget(Label("生效配置明细", 13, 600))
        lay.addSpacing(8)
        self.detail_lay = vbox(None, 0, 2)
        lay.addLayout(self.detail_lay)
        lay.addStretch(1)
        lay.addWidget(Label("改完配置需点「重新加载配置」；密钥不会写入日志", 11, 400, C["t3"]))
        return card

    def _fill_detail(self, rows):
        while self.detail_lay.count():
            item = self.detail_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for key, value in rows:
            r = hbox(None, 0, 10)
            kl = Label(key, 12, 400, C["t2"])
            kl.setFixedWidth(96)
            r.addWidget(kl, 0, Qt.AlignVCenter)
            r.addWidget(Elided(str(value), 12, 400, C["t1"]), 1)
            holder = QWidget()
            holder.setLayout(r)
            holder.setFixedHeight(30)
            self.detail_lay.addWidget(holder)

    # ---------------- 配置来源 ----------------
    def _sources(self):
        card = Card()
        card.setFixedHeight(256)
        lay = vbox(card, 14, 0)
        lay.addWidget(Label("配置来源（只读环境变量）", 13, 600))
        lay.addSpacing(8)
        self.src_lay = vbox(None, 0, 0)
        lay.addLayout(self.src_lay)
        lay.addSpacing(8)
        now = QFrame()
        now.setObjectName("SrcNow")
        now.setAttribute(Qt.WA_StyledBackground, True)
        now.setFixedHeight(34)
        nl = hbox(now, (8, 0, 8, 0), 8)
        nl.addWidget(Label("当前生效", 12, 500), 0, Qt.AlignVCenter)
        nl.addStretch(1)
        self.now_value = Label("", 11, 400, C["t2"])
        nl.addWidget(self.now_value, 0, Qt.AlignVCenter)
        lay.addWidget(now)
        lay.addStretch(1)
        return card

    def _fill_sources(self):
        while self.src_lay.count():
            item = self.src_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for name, label, is_set in list(A.llm_env_sources())[:5]:
            r = hbox(None, 0, 8)
            nm = Elided(name, 11, 400, C["t1"])
            nm.setToolTip(label)
            r.addWidget(nm, 1, Qt.AlignVCenter)
            badge = QLabel("已设置" if is_set else "未设置")
            badge.setFont(F(10, 500))
            badge.setStyleSheet("QLabel{background:%s;color:%s;border-radius:3px;padding:1px 5px;}"
                                % ((C["ok_bg"], C["ok"]) if is_set else (C["s2"], C["t3"])))
            r.addWidget(badge, 0, Qt.AlignVCenter)
            holder = QWidget()
            holder.setLayout(r)
            holder.setFixedHeight(30)
            self.src_lay.addWidget(holder)

    # ---------------- 要求文件 ----------------
    def _require(self):
        card = Card()
        lay = vbox(card, 14, 0)
        head = hbox(None, 0, 8)
        head.addWidget(Label("整理要求文件", 13, 600))
        head.addStretch(1)
        self.req_meta = Label("", 11, 400, C["t3"])
        head.addWidget(self.req_meta)
        lay.addLayout(head)
        lay.addSpacing(8)

        fr = hbox(None, 0, 8)
        ic = QLabel()
        ic.setPixmap(svg_pixmap("file", C["t2"], 14))
        ic.setFixedSize(14, 14)
        fr.addWidget(ic, 0, Qt.AlignVCenter)
        self.req_path = Elided("", 12, 400, C["t2"])
        fr.addWidget(self.req_path, 1)
        reopen = Button("重新加载", kind="sm")
        reopen.clicked.connect(self.load_instruction)
        fr.addWidget(reopen, 0)
        fh = QWidget()
        fh.setLayout(fr)
        fh.setFixedHeight(30)
        lay.addWidget(fh)

        self.editor = QPlainTextEdit()
        self.editor.setObjectName("ReqBox")
        self.editor.setFont(F(11))
        self.editor.setStyleSheet(
            "QPlainTextEdit{background:%s;border:1px solid %s;border-radius:%dpx;"
            "padding:8px;color:%s;}" % (C["s1"], C["line"], R_XS, C["t2"]))
        lay.addWidget(self.editor, 1)
        lay.addSpacing(6)
        lay.addWidget(Label("解析失败会保留上一条规则链，并在预览表顶部提示原因", 11, 400, C["t3"]))
        return card

    # ---------------- 保存栏 ----------------
    def _savebar(self):
        card = Card()
        card.setFixedHeight(56)
        lay = hbox(card, (14, 0, 14, 0), 12)
        ic = QLabel()
        ic.setPixmap(svg_pixmap("shield", C["t2"], 14))
        ic.setFixedSize(14, 14)
        lay.addWidget(ic, 0, Qt.AlignVCenter)
        lay.addWidget(Label("配置只影响整理要求的解析，不改动任何文件；保存后需重新加载才会生效",
                            12, 400, C["t2"]), 1)
        reset = Button("恢复默认")
        reset.clicked.connect(self.reset_instruction)
        lay.addWidget(reset, 0, Qt.AlignVCenter)
        save = Button("保存配置", kind="primary")
        save.clicked.connect(self.save)
        lay.addWidget(save, 0, Qt.AlignVCenter)
        return card

    # ---------------- 数据 ----------------
    def overrides(self):
        provider = self.provider.value()
        model = self.model.text().strip()
        try:
            batch = int(self.batch.text() or 0)
        except ValueError:
            batch = 0
        return provider, model, max(0, batch)

    def set_prefs(self, prefs: dict):
        provider = str(prefs.get("llm_provider") or "")
        self.provider.set_value(provider)
        self.model.setText(str(prefs.get("llm_model") or ""))
        batch = int(prefs.get("llm_batch") or 0)
        self.batch.setText(str(batch) if batch else "")
        self.reload(True)

    def reload(self, read_instruction=False):
        provider, model, batch = self.overrides()
        self._cfg = A.llm_config(provider, model, batch)
        self._client = A.llm_client(provider, model, batch)
        cfg, client = self._cfg, self._client
        name = str(getattr(client, "name", "") or "")
        available = bool(client.available())
        problem = client.problem()

        if name == "echo":
            badge, tone = "离线替身", ("warn_bg", "warn")
        elif name == "ollama":
            badge, tone = "本地模型", ("info_bg", "info") if available else ("warn_bg", "warn")
        elif name == "unavailable":
            badge, tone = "不可用", ("err_bg", "err")
        else:
            badge, tone = "HTTP 接口", ("ok_bg", "ok") if available else ("err_bg", "err")
        self.badge.setText(badge)
        self.badge.setStyleSheet("QLabel{background:%s;color:%s;border-radius:%dpx;}"
                                 % (C[tone[0]], C[tone[1]], R_XS))
        kind_text = {"echo": "离线机械精简替身（不联网）", "ollama": "本地 Ollama",
                     "unavailable": "没有可用客户端"}.get(name, "OpenAI 兼容（chat/completions）")
        self.line1.setText("%s · %s" % (kind_text, cfg.model or "未配置模型"))
        self.line2.setText("%s · 温度 %s · 每批 %d 条 · 超时 %ds · 重试 %d 次"
                           % (cfg.base_url or "未配置接口地址", cfg.temperature,
                              cfg.batch, cfg.timeout, cfg.max_retries))
        self.now_value.setText("已合并" if available else "不可用")
        if not available and problem:
            self.notice.set_text(problem, "warn")
            self.notice.show()

        self._fill_detail([
            ("提供商", kind_text),
            ("模型", cfg.model or "未配置"),
            ("接口地址", cfg.base_url or "未配置"),
            ("API Key", "已配置（已遮蔽）" if cfg.api_key else "未配置"),
            ("生成参数", "温度 %s · 每批 %d 条" % (cfg.temperature, cfg.batch)),
            ("网络", "超时 %ds · 重试 %d 次 · 失败不阻塞批量重命名"
             % (cfg.timeout, cfg.max_retries)),
        ])
        self._fill_sources()

        path = A.llm_instruction_path()
        self.req_path.setText(path)
        self.req_path.setToolTip(path)
        if read_instruction:
            self.load_instruction()

    def load_instruction(self):
        text = A.llm_read_instruction()
        self.editor.blockSignals(True)
        self.editor.setPlainText(text)
        self.editor.blockSignals(False)
        self.req_meta.setText("%d 行" % (len(text.splitlines()) if text else 0))
        self.status_message.emit("已读取整理要求")

    def reset_instruction(self):
        self.editor.setPlainText(A.llm_default_instruction())
        self.notice.set_text("已载入内置默认要求，点「保存配置」才会写盘", "info")
        self.notice.show()

    def save(self):
        text = self.editor.toPlainText()
        try:
            path = A.llm_write_instruction(text)
        except OSError as exc:
            self.notice.set_text("保存失败：%s" % exc, "error")
            self.notice.show()
            return
        self.req_path.setText(path)
        self.req_meta.setText("%d 行" % len(text.splitlines()))
        self.notice.set_text("已保存：%s" % path, "success")
        self.notice.show()
        self.saved.emit()
        self.status_message.emit("整理要求已保存")

    def test(self):
        client = A.llm_client(*self.overrides())
        ok = bool(client.available())
        self.notice.set_text(("可用：%s" % getattr(client, "name", "")) if ok
                             else ("不可用：%s" % client.problem()),
                             "success" if ok else "warn")
        self.notice.show()

    def open_instruction(self):
        try:
            path = A.llm_ensure_instruction()
            os.startfile(path)  # noqa: S606 - 交给系统默认编辑器
        except Exception as exc:
            self.notice.set_text("打不开要求文件：%s" % exc, "warn")
            self.notice.show()
