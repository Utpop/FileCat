# -*- coding: utf-8 -*-
"""ui.pages.llm_settings —— LLM 供应商配置（OpenAI 兼容接口为主）。

页面结构（照着「供应商配置」那份设计稿来）：
    供应商      名称 / 协议 / 供应商预设 / 基础 URL / API Key / 测试连接
    供应商模型  手动添加模型 / 获取并保存模型列表
    高级        模型列表 URL / 自定义请求头
右侧保留「整理要求」编辑器与生效明细。

密钥规矩
    · 界面**永不回显**密钥：输入框是遮蔽的，留空表示"沿用已保存的密钥"；
    · 密钥单独存 %APPDATA%\\NameCat\\llm_credential.dat（Windows 用 DPAPI 加密），
      普通配置文件里只有地址、模型这些非敏感字段。
"""

from __future__ import annotations

import os
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QLineEdit, QPlainTextEdit, QWidget

from adapters import engine_adapter as A
from ui.base import (C, F, R_XS, Badge, Button, Card, Elided, Label, NoticeBar,
                     PageHead, fit_scroll_content, hbox, scroll_area,
                     svg_pixmap, vbox)
from ui.pages.workspace import Combo, FieldEdit
from workers import task_workers as W


class Row(QWidget):
    """一行设置：左边标题（可带说明），右边控件。

    控件高度必须 ≤ 行高，否则会被容器裁掉（按钮下半截会看不见）。
    """

    def __init__(self, title, control=None, desc="", height=52, title_width=112):
        super().__init__(parent=None)
        self.setFixedHeight(height)
        lay = hbox(self, (0, 0, 0, 0), 12)
        self._left = vbox(None, 0, 2)
        self._left.addWidget(Label(title, 13, 500, C["t1"]))
        # 说明文字总是建出来（可能先空着），这样 set_desc 随时能改
        self._desc = Elided("", 11, 400, C["t3"])
        self._desc.setMinimumWidth(0)
        self._left.addWidget(self._desc)
        self._desc.setText(desc or "")
        lay.addLayout(self._left, 1)
        if control is not None:
            lay.addWidget(control, 0, Qt.AlignVCenter)

    def set_desc(self, text):
        """换掉说明文字（第二行）。"""
        self._desc.setText(text or "")


class Field(QWidget):
    """标题在上、控件在下的一组（多行输入框用这个更好看）。"""

    def __init__(self, title, control, desc="", height=132):
        super().__init__(parent=None)
        self.setFixedHeight(height)
        lay = vbox(self, 0, 6)
        head = vbox(None, 0, 2)
        head.addWidget(Label(title, 13, 500, C["t1"]))
        self._desc = Elided("", 11, 400, C["t3"])
        self._desc.setMinimumWidth(0)
        head.addWidget(self._desc)
        self._desc.setText(desc or "")
        lay.addLayout(head)
        lay.addWidget(control, 1)

    def set_desc(self, text):
        self._desc.setText(text or "")


class Section(QWidget):
    """一段小标题 + 若干行（卡片由调用方给）。"""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        lay = vbox(self, 0, 8)
        lay.addWidget(Label(title, 13, 600, C["t1"]))


class LlmPage(QWidget):

    status_message = Signal(str)
    saved = Signal()
    # 连通性状态变了（测完 / 保存完）——窗口用它刷新左下角的气泡
    status_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Root")
        self._cfg = None
        self._client = None
        self._key_input = ""          # 用户这次输入的密钥（空 = 沿用已保存的）
        self._custom_kind = ""        # 选回"自定义供应商"时强制用的客户端类型
        self._dirty = False
        self.runner = W.TaskRunner(self, max_threads=1)

        outer = vbox(self, (20, 18, 20, 12), 12)
        self.btn_open_cfg = Button("打开配置目录", "open")
        self.btn_open_cfg.clicked.connect(self.open_config_dir)
        self.btn_save = Button("保存配置", kind="primary")
        self.btn_save.clicked.connect(self.save)
        self.dirty_label = Label("与磁盘一致", 11, 400, C["t3"])
        # PageHead 里的控件一律顶对齐，这行字要跟按钮**垂直居中**才好看：
        # 给它按钮同样的高度（34），字再在框里居中，两边的字才在一条基线上。
        self.dirty_label.setFixedHeight(34)
        self.dirty_label.setFixedWidth(80)
        self.dirty_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        outer.addWidget(PageHead(
            "供应商配置", "OpenAI 兼容接口为主：填好地址与密钥，工作台里的「智能精简」就能用",
            [self.dirty_label, self.btn_open_cfg, self.btn_save]))

        self.notice = NoticeBar("密钥只保存在本机，不进预设、不进日志")
        outer.addWidget(self.notice)

        grid = hbox(None, 0, 12)
        left = vbox(None, 0, 12)
        left.addWidget(self._provider_section())
        left.addWidget(self._model_section())
        left.addWidget(self._advanced_section())
        right = vbox(None, 0, 12)
        right.addWidget(self._state_card())
        right.addWidget(self._require_card(), 1)

        # 左列内容比视口高（供应商 + 模型 + 高级），装进滚动区，别把行压扁
        lw = QWidget()
        lw.setLayout(left)
        lw.setMinimumWidth(600)
        left_scroll = scroll_area(lw)
        left_scroll.setMinimumHeight(200)
        rw = QWidget()
        rw.setLayout(right)
        rw.setMinimumWidth(330)
        from PySide6.QtWidgets import QSplitter
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(12)
        split.addWidget(left_scroll)
        split.addWidget(rw)
        split.setSizes([792, 392])
        split.setStyleSheet("QSplitter::handle{background:transparent;}")
        grid.addWidget(split, 1)
        outer.addLayout(grid, 1)
        self.reload()
        fit_scroll_content(left_scroll)

    # ===================================================================== #
    # 供应商
    # ===================================================================== #
    def _provider_section(self):
        box = Section("供应商")

        card = Card()
        lay = vbox(card, 16, 0)

        # 供应商名称
        self.name_edit = FieldEdit("", width=260, height=30)
        self.name_edit.edit.setPlaceholderText("例如 OpenAI Compatible")
        self.name_edit.edited.connect(self._mark_dirty)
        lay.addWidget(Row("供应商名称", self.name_edit,
                          "只是给这个配置起个名字，不影响连接"))
        lay.addSpacing(12)

        # 协议
        self.protocol = Combo(A.llm_protocols(), "", width=230, height=30)
        self.protocol.changed.connect(self._on_protocol_changed)
        lay.addWidget(Row("协议", self.protocol,
                          "决定请求打到哪里、怎么组织请求体"))
        lay.addSpacing(12)

        # 供应商预设
        self.preset = Combo(A.llm_provider_presets(), "", width=230, height=30)
        self.preset.changed.connect(self._on_preset_changed)
        self.preset_row = Row("供应商预设", self.preset, "")
        lay.addWidget(self.preset_row)
        lay.addSpacing(12)

        # 基础 URL
        self.base_url = FieldEdit("", width=300, height=30)
        self.base_url.edit.setPlaceholderText("https://api.openai.com/v1")
        self.base_url.edited.connect(self._mark_dirty)
        lay.addWidget(Row("基础 URL", self.base_url,
                          "接口根地址；一般不带 /chat/completions"))
        lay.addSpacing(12)

        # API Key
        self.key_edit = FieldEdit("", width=300, height=30)
        self.key_edit.edit.setEchoMode(QLineEdit.Password)
        self.key_edit.edit.setPlaceholderText("留空会保留已保存的密钥")
        self.key_edit.edited.connect(self._on_key_edited)
        key_row = Row("API Key", self.key_edit, "保存在本地。留空会保留已保存的密钥。")
        lay.addWidget(key_row)
        self.key_row = key_row
        lay.addSpacing(12)

        # 测试连接
        self.test_btn = Button("测试连接", "check")
        self.test_btn.clicked.connect(self.test)
        self.test_row = Row("测试连接", self.test_btn,
                            "真发一个请求，验证地址、密钥与模型是否可用")
        lay.addWidget(self.test_row)

        box.layout().addWidget(card)
        return box

    # ===================================================================== #
    # 供应商模型
    # ===================================================================== #
    def _model_section(self):
        box = Section("供应商模型")

        card = Card()
        lay = vbox(card, 16, 0)

        self.model = Combo([], "", width=260, height=30)
        self.model.changed.connect(self._mark_dirty)
        lay.addWidget(Row("模型", self.model, "实际用来干活的模型名"))
        lay.addSpacing(12)

        self.manual_model = FieldEdit("", width=200, height=30)
        self.manual_model.edit.setPlaceholderText("手动输入模型名")
        add_btn = Button("添加", "plus", kind="sm_brand")
        add_btn.clicked.connect(self.add_model)
        add_box = hbox(None, 0, 8)
        add_box.addWidget(self.manual_model, 0)
        add_box.addWidget(add_btn, 0)
        add_holder = QWidget()
        add_holder.setLayout(add_box)
        add_holder.setFixedHeight(30)
        lay.addWidget(Row("手动添加模型", add_holder,
                          "接口不提供模型列表时，自己把模型名加进来"))
        lay.addSpacing(12)

        self.fetch_btn = Button("获取模型列表", "refresh", kind="mid")
        self.fetch_btn.clicked.connect(self.fetch_models)
        self.fetch_row = Row("获取并保存模型列表", self.fetch_btn,
                             "从接口拉一份模型清单存到本地")
        lay.addWidget(self.fetch_row)

        box.layout().addWidget(card)
        return box

    # ===================================================================== #
    # 高级
    # ===================================================================== #
    def _advanced_section(self):
        box = Section("高级")

        card = Card()
        lay = vbox(card, 16, 0)

        self.models_url = FieldEdit("", width=300, height=30)
        self.models_url.edit.setPlaceholderText("留空 = 按基础 URL 自动推算")
        self.models_url.edited.connect(self._mark_dirty)
        lay.addWidget(Row("模型列表 URL", self.models_url,
                          "拉模型清单用的地址；留空按基础 URL 推算"))
        lay.addSpacing(12)

        self.headers = QPlainTextEdit()
        self.headers.setObjectName("ReqBox")
        self.headers.setFont(F(11))
        self.headers.setPlaceholderText("每行一条，例如：\nX-Trace-Id: namecat")
        self.headers.setStyleSheet(
            "QPlainTextEdit{background:%s;border:1px solid %s;border-radius:%dpx;"
            "padding:8px;color:%s;}" % (C["s1"], C["line"], R_XS, C["t2"]))
        lay.addWidget(Field("自定义请求头", self.headers,
                            "可选。随每次请求发送的额外 HTTP 请求头；同名时以这里为准"))

        box.layout().addWidget(card)
        return box

    # ===================================================================== #
    # 右侧：生效明细
    # ===================================================================== #
    STATE_ROWS = (("供应商", "名称"), ("协议", "协议"), ("基础 URL", "基础 URL"),
                  ("API Key", "API Key"), ("模型", "模型"), ("模型列表", "模型列表"),
                  ("配置来源", "来源"))

    def _state_card(self):
        card = Card()
        lay = vbox(card, 16, 0)

        head = hbox(None, 0, 8)
        head.addWidget(Label("当前生效", 13, 600))
        head.addStretch(1)
        self.state_badge = Badge("未配置", "muted")
        head.addWidget(self.state_badge, 0, Qt.AlignVCenter)
        lay.addLayout(head)
        lay.addSpacing(10)

        # 明细行**建一次就不再重建**：反复 deleteLater + 重建会留下没被真正
        # 删掉的旧控件（deleteLater 要事件循环才会执行），几行文字会叠在一起。
        self._state_values = {}
        for key, _label in self.STATE_ROWS:
            r = hbox(None, 0, 10)
            kl = Label(key, 12, 400, C["t2"])
            kl.setFixedWidth(76)
            r.addWidget(kl, 0, Qt.AlignVCenter)
            val = Elided("", 12, 400, C["t1"])
            r.addWidget(val, 1)
            holder = QWidget()
            holder.setLayout(r)
            holder.setFixedHeight(26)
            lay.addWidget(holder)
            self._state_values[key] = val
        lay.addSpacing(10)
        lay.addWidget(Label("改完要点「保存配置」；密钥不会写进预设与日志",
                            11, 400, C["t3"]))
        return card

    def _fill_state(self, rows):
        """更新生效明细的文字（行是固定的，只改值）。"""
        for key, value in rows:
            widget = self._state_values.get(key)
            if widget is not None:
                widget.setText(str(value))

    # ===================================================================== #
    # 右侧：整理要求
    # ===================================================================== #
    def _require_card(self):
        card = Card()
        lay = vbox(card, 14, 0)
        head = hbox(None, 0, 8)
        head.addWidget(Label("整理要求", 13, 600))
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

        foot = hbox(None, 0, 8)
        reset = Button("恢复默认", kind="sm")
        reset.clicked.connect(self.reset_instruction)
        foot.addWidget(reset, 0, Qt.AlignVCenter)
        foot.addStretch(1)
        save_req = Button("保存要求", kind="mid")
        save_req.clicked.connect(self.save_instruction)
        foot.addWidget(save_req, 0, Qt.AlignVCenter)
        holder = QWidget()
        holder.setLayout(foot)
        holder.setFixedHeight(30)
        lay.addWidget(holder)
        return card

    # ===================================================================== #
    # 数据
    # ===================================================================== #
    def _current_config(self):
        """界面上这一份配置（含未保存的修改）。"""
        cfg = A.llm_provider_config()
        cfg.name = self.name_edit.text().strip() or cfg.name
        cfg.protocol = self.protocol.value() or cfg.protocol
        cfg.preset = self.preset.value() or cfg.preset
        cfg.base_url = self.base_url.text().strip()
        # 「自定义供应商」不改变客户端类型：地址自己填，用哪种协议还是原来那个
        if cfg.preset and cfg.preset != "custom":
            cfg.kind = A.PRV_KIND.get(cfg.preset, cfg.kind)
        elif self._custom_kind:
            cfg.kind = self._custom_kind
        cfg.model = self.model.value()
        cfg.models_url = self.models_url.text().strip()
        cfg.headers = A.llm_save_headers(self.headers.toPlainText())
        # 密钥以输入框为准、_key_input 兜「刚输入还没失焦」的那一瞬：
        # 其它字段都是直接读控件的，密钥没道理例外（editingFinished 要等失焦，
        # 粘贴完直接点「测试连接」的那一瞬读不到就白输了）。
        key = self._key_input or self.key_edit.text().strip()
        if key:
            cfg.api_key = key
        return cfg

    def _mark_dirty(self):
        self._dirty = True
        self._refresh_state()
        self._refresh_dirty()

    def _refresh_dirty(self):
        """页头右上角提示"有未保存的修改 / 与磁盘一致"。"""
        if self._dirty:
            self.dirty_label.setText("有未保存的修改")
            self.dirty_label.setStyleSheet("color:%s;background:transparent;" % C["warn"])
        else:
            self.dirty_label.setText("与磁盘一致")
            self.dirty_label.setStyleSheet("color:%s;background:transparent;" % C["t3"])

    def _sync_dirty_after_write(self):
        """「添加模型 / 获取模型列表」会顺手把当前表单一起写盘，脏标记要跟着回落。

        这两个动作不带密钥（密钥只认「保存配置」），所以只要用户刚输入的密钥
        还没落盘，就仍然算「有未保存的修改」，别骗人说已经存好了。
        """
        self._dirty = bool(self._key_input)
        self._refresh_dirty()

    def _on_key_edited(self):
        self._key_input = self.key_edit.text().strip()
        self._mark_dirty()

    def _on_protocol_changed(self, value):
        self._mark_dirty()

    def _on_preset_changed(self, value):
        """套预设：把地址、模型列表 URL、协议一次填好。"""
        self._apply_preset_to_ui(value)
        self._mark_dirty()

    def _apply_preset_to_ui(self, preset_key):
        notes = A.llm_provider_preset_notes()
        note = notes.get(preset_key, "")
        if preset_key == "custom":
            # 回到自定义：地址与协议都留着自己填，客户端类型回到 OpenAI 兼容
            self._custom_kind = "openai"
            self.preset_row.set_desc(note or "地址、密钥、模型全部自己填")
            return
        self._custom_kind = ""
        cfg = A.llm_apply_preset(A.llm_provider_config(), preset_key)
        self.base_url.setText(cfg.base_url)
        self.models_url.setText(cfg.models_url)
        self.protocol.set_value(cfg.protocol)
        self.name_edit.setText(cfg.name)
        self.preset_row.set_desc(note or cfg.base_url)

    def set_prefs(self, prefs: dict):
        """窗口启动时调用：读一遍已保存的配置。"""
        self.reload(read_instruction=True)

    def reload(self, read_instruction=False, force=False):
        """从磁盘重新读一遍配置。

        force=False（默认）：已有未保存的修改时**不覆盖** —— 用户粘完密钥
        切到别的页再切回来，输入框不该被悄悄清空。
        """
        if self._dirty and not force:
            return
        cfg = A.llm_provider_config()
        self._cfg = cfg
        self._key_input = ""
        self._dirty = False
        # 模型名同步给状态记录器：气泡里要显示"用的哪个模型"
        A.llm_status_set_model(cfg.model)

        self.name_edit.setText(cfg.name)
        self.protocol.set_value(cfg.protocol)
        self.preset.set_value(cfg.preset)
        self.base_url.setText(cfg.base_url)
        self.models_url.setText(cfg.models_url)
        self.headers.setPlainText(A.llm_headers_to_text(cfg.headers))
        self.key_edit.setText("")
        self.key_edit.edit.setPlaceholderText(
            "已保存（留空即沿用）" if A.llm_provider_key_saved()
            else "粘贴你的密钥")
        self._set_model_options(cfg)
        self._refresh_dirty()

        path = A.llm_instruction_path()
        self.req_path.setText(path)
        self.req_path.setToolTip(path)
        if read_instruction:
            self.load_instruction()
        self._refresh_state()

    def _set_model_options(self, cfg):
        names = list(cfg.models or [])
        if cfg.model and cfg.model not in names:
            names.insert(0, cfg.model)
        options = [(n, n) for n in names] or [("", "（还没有模型）")]
        self.model.set_options(options)
        self.model.set_value(cfg.model or (names[0] if names else ""))

    def _refresh_state(self):
        cfg = self._current_config()
        # 密钥状态有三种来源，得分开说：
        #   本次输入（还没存）/ 本机已保存（沿用）/ 环境变量兜底。
        # 不能只看 cfg.api_key —— 它已经把三种情况混成"有没有值"了，
        # 结果输入框明明是空的，明细里却写着「已填写」。
        if self._key_input:
            key_state = "已填写（未保存）"
        elif A.llm_provider_key_saved():
            key_state = "已保存（沿用）"
        elif cfg.api_key:
            key_state = "来自环境变量"
        else:
            key_state = "未填写"
        tone = "ok" if cfg.api_key else "muted"
        self.state_badge.setText("已就绪" if cfg.base_url and cfg.api_key else "未就绪")
        self.state_badge.setStyleSheet(
            "QLabel{background:%s;color:%s;border-radius:%dpx;}"
            % (C["ok_bg"] if tone == "ok" else C["s2"],
               C["ok"] if tone == "ok" else C["t3"], R_XS))
        self._fill_state([
            ("供应商", cfg.name),
            ("协议", A.llm_protocol_text(cfg.protocol)),
            ("基础 URL", cfg.base_url or "未填写"),
            ("API Key", key_state),
            ("模型", cfg.model or "未选择"),
            ("模型列表", "%d 个" % len(cfg.models or [])),
            ("配置来源", "本机配置" if A.llm_provider_config_saved()
             else "环境变量（还没保存过本机配置）"),
        ])

    # ===================================================================== #
    # 动作
    # ===================================================================== #
    def test(self):
        """真发一个请求测连通性（后台线程，不卡界面）。"""
        cfg = self._current_config()
        if not cfg.base_url:
            self.notice.set_text("还没填基础 URL，先填地址再测", "warn")
            self.notice.show()
            return
        self.test_btn.setEnabled(False)
        self.test_btn.setText("测试中…")
        task = W.llm_test_task(cfg)
        task.signals.done.connect(self._test_done)
        task.signals.failed.connect(self._test_failed)
        task.signals.finished.connect(self._test_finished)
        self.runner.submit(task)

    def _test_done(self, result):
        ok = bool(result.get("ok"))
        msg = result.get("message") or ""
        ms = result.get("latency_ms") or 0
        self.notice.set_text("%s（%d ms）" % (msg, ms),
                             "success" if ok else "error")
        self.notice.show()
        self.status_message.emit(("连接正常：" if ok else "连接失败：") + msg)
        if ok:
            self.test_row.set_desc("连接正常 · 上次测试 %d ms" % ms)
        else:
            self.test_row.set_desc(msg)
        self.status_changed.emit()

    def _test_failed(self, message):
        self.notice.set_text("测试失败：%s" % message, "error")
        self.notice.show()

    def _test_finished(self):
        self.test_btn.setEnabled(True)
        self.test_btn.setText("测试连接")

    def fetch_models(self):
        """拉模型清单并存到本地。"""
        cfg = self._current_config()
        if not cfg.base_url:
            self.notice.set_text("还没填基础 URL，先填地址再拉列表", "warn")
            self.notice.show()
            return
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText("拉取中…")
        task = W.llm_fetch_models_task(cfg)
        task.signals.done.connect(self._models_done)
        task.signals.failed.connect(self._test_failed)
        task.signals.finished.connect(self._models_finished)
        self.runner.submit(task)

    def _models_done(self, result):
        names, err = result if isinstance(result, tuple) else ([], "返回格式不对")
        if err or not names:
            self.notice.set_text(err or "没拉到模型", "warn")
            self.notice.show()
            self.fetch_row.set_desc(err or "没拉到模型")
            return
        cfg = self._current_config()
        cfg.models = names
        A.llm_save_provider_config(cfg, None)
        self._set_model_options(cfg)
        self.notice.set_text("已保存 %d 个模型" % len(names), "success")
        self.notice.show()
        self.fetch_row.set_desc("已保存 %d 个模型" % len(names))
        self._sync_dirty_after_write()
        self._refresh_state()
        self.status_message.emit("已保存 %d 个模型" % len(names))

    def _models_finished(self):
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("获取模型列表")

    def add_model(self):
        """手动往模型列表里加一个名字。"""
        name = self.manual_model.text().strip()
        if not name:
            self.notice.set_text("先填一个模型名", "warn")
            self.notice.show()
            return
        cfg = self._current_config()
        names = list(cfg.models or [])
        if name in names:
            self.notice.set_text("「%s」已经在列表里了" % name, "info")
            self.notice.show()
            return
        names.insert(0, name)
        cfg.models = names
        cfg.model = name
        A.llm_save_provider_config(cfg, None)
        self.manual_model.setText("")
        self._set_model_options(cfg)
        self.notice.set_text("已添加模型「%s」" % name, "success")
        self.notice.show()
        self._sync_dirty_after_write()
        self._refresh_state()

    def save(self):
        """把供应商配置写盘（密钥另行加密保存）。"""
        cfg = self._current_config()
        # 「手动输入模型名」填了却没点「添加」就保存：那是用户想要的模型，
        # 不能因为少点一次按钮就把它悄悄丢掉。
        manual = self.manual_model.text().strip()
        if manual:
            names = list(cfg.models or [])
            if manual not in names:
                names.insert(0, manual)
            cfg.models = names
            cfg.model = manual
        key = self._key_input or self.key_edit.text().strip()
        path = A.llm_save_provider_config(cfg, key if key else None)
        if not path:
            self.notice.set_text("保存失败：配置目录写不进去", "error")
            self.notice.show()
            return
        self.manual_model.setText("")
        self._key_input = ""
        self.key_edit.setText("")
        self.key_edit.edit.setPlaceholderText(
            "已保存（留空即沿用）" if A.llm_provider_key_saved() else "粘贴你的密钥")
        # 手填的模型名已经进列表了，下拉框跟上，别让「模型」还显示旧值
        self._set_model_options(cfg)
        self._dirty = False
        self.notice.set_text("已保存供应商配置：%s" % path, "success")
        self.notice.show()
        self.saved.emit()
        self.status_message.emit("供应商配置已保存")
        # 配置变了 → 之前那次测试结果不再作数，气泡回到"尚未测试"
        A.llm_status_mark_unknown(cfg.model)
        self._refresh_state()
        self._refresh_dirty()
        self.status_changed.emit()

    def load_instruction(self):
        text = A.llm_read_instruction()
        self.editor.blockSignals(True)
        self.editor.setPlainText(text)
        self.editor.blockSignals(False)
        self.req_meta.setText("%d 行" % (len(text.splitlines()) if text else 0))
        self.status_message.emit("已读取整理要求")

    def reset_instruction(self):
        self.editor.setPlainText(A.llm_default_instruction())
        self.notice.set_text("已载入内置默认要求，点「保存要求」才会写盘", "info")
        self.notice.show()

    def save_instruction(self):
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
        self.status_message.emit("整理要求已保存")

    def open_config_dir(self):
        try:
            path = A.llm_provider_config_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            os.startfile(os.path.dirname(path))  # noqa: S606 - 交给系统文件管理器
        except Exception as exc:
            self.notice.set_text("打不开配置目录：%s" % exc, "warn")
            self.notice.show()

    # 兼容旧接口：窗口与工作台通过它拿"本次运行"的覆盖参数
    def overrides(self):
        cfg = self._current_config()
        return cfg.kind, cfg.model, cfg.batch

    def client_config(self):
        return self._current_config()
