# -*- coding: utf-8 -*-
"""
adapters.engine_adapter —— 界面与引擎之间的唯一通道。

设计原则（对应任务书 03 节「推荐代码分层」）：
    · 界面层只调用本模块，不直接碰 engine 的内部结构；
    · 本模块不实现任何重命名算法，只做两件事：
        1) 把界面状态翻译成引擎需要的 ScanOptions / PlanOptions / Rule；
        2) 把引擎的返回值翻译成界面要展示的东西（统计、状态文案、日志行…）；
    · 引擎没有的能力一律不在这里"补"出来——缺什么就老实标成待接入。

所有对外行为都以 engine 现有实现为准，参数名、默认值、语义都取自源码，
不从设计稿反推。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from engine import APP_NAME, APP_VER, STATUS_TEXT
from engine import common as C
from engine import llm as LLM
from engine import ops as OPS
from engine import plan as P
from engine import presets as PS
from engine import provider as PRV
from engine import rules as R
from engine import template as TPL
from engine.cli import ONLY_CHOICES          # 与命令行共用同一张映射表
from engine.scan import (IMAGE_EXTS, KIND_DIR, KIND_FILE, KIND_IMAGE, KIND_TEXT,
                         KIND_VIDEO, MAX_DEPTH, SORT_MODES, ScanOptions, VIDEO_EXTS,
                         nkey, scan)

# 状态常量
ST_OK = "ok"
ST_SAME = "same"
ST_CONFLICT = "conflict"
ST_ERROR = "error"
ST_SKIP = "skip"


# --------------------------------------------------------------------------- #
# 对象类型
# --------------------------------------------------------------------------- #

ONLY_LABELS = {
    "all": "文件 + 文件夹",
    "file": "仅文件",
    "dir": "仅文件夹",
    "image": "仅图片",
    "video": "仅视频",
    "media": "图片 + 视频",
}
ONLY_ORDER = ["all", "file", "dir", "image", "video", "media"]


def kinds_for(only: str) -> Tuple[str, ...]:
    return ONLY_CHOICES.get(only, (KIND_FILE,))


# --------------------------------------------------------------------------- #
# 表单状态 -> 引擎选项
# --------------------------------------------------------------------------- #

def _split_ext_list(text: str) -> Optional[set]:
    """`jpg, .png png` -> {'.jpg', '.png'}；空串表示不限（None）。"""
    raw = (text or "").replace(";", ",").replace(" ", ",")
    parts = [p.strip().lower() for p in raw.split(",")]
    out = set()
    for p in parts:
        if not p:
            continue
        out.add(p if p.startswith(".") else "." + p)
    return out or None


def _join_ext_list(values) -> str:
    if not values:
        return ""
    return ", ".join(sorted(str(v) for v in values))


@dataclass
class FormState:
    """界面上的全部可调参数。纯数据，不含 Qt，方便测试与预设序列化。"""

    root: str = ""
    only: str = "file"
    recursive: bool = False
    include_hidden: bool = False
    only_match: str = ""
    exclude_match: str = ""
    include_exts: str = ""
    exclude_exts: str = ""
    verify_content: bool = False
    sort_mode: str = "name_asc"
    max_depth: int = MAX_DEPTH
    # 「扫描对象…」直接指定的条目（绝对路径）。空 = 扫整个 root。
    # 对应引擎 ScanOptions.selected，界面不自作过滤。
    selected: Tuple[str, ...] = ()
    # 计划
    start: int = 1
    step: int = 1
    width: int = 0
    scope: str = P.SCOPE_GLOBAL
    reverse: bool = False
    split_ext: bool = True
    rename_dirs: bool = False

    # ---- 转成引擎对象 ----
    def scan_options(self) -> ScanOptions:
        return ScanOptions(
            root=self.root,
            kinds=kinds_for(self.only),
            recursive=bool(self.recursive),
            include_hidden=bool(self.include_hidden),
            only_match=self.only_match or "",
            exclude_match=self.exclude_match or "",
            include_exts=_split_ext_list(self.include_exts),
            exclude_exts=_split_ext_list(self.exclude_exts),
            verify_content=bool(self.verify_content),
            sort_mode=self.sort_mode or "name_asc",
            max_depth=max(1, min(int(self.max_depth or MAX_DEPTH), MAX_DEPTH)),
            selected=(set(nkey(p) for p in self.selected) if self.selected else None),
        )

    def plan_options(self, dry_run: bool = True) -> P.PlanOptions:
        return P.PlanOptions(
            start=int(self.start),
            step=int(self.step) if int(self.step) else 1,
            width=max(0, int(self.width)),
            scope=self.scope if self.scope in P.SCOPES else P.SCOPE_GLOBAL,
            reverse=bool(self.reverse),
            split_ext=bool(self.split_ext),
            rename_dirs=bool(self.rename_dirs),
            dry_run=bool(dry_run),
        )

    # ---- 预设序列化（只保留引擎认识的字段） ----
    def scan_dict(self) -> dict:
        return {
            "kinds": list(kinds_for(self.only)),
            "recursive": self.recursive,
            "include_hidden": self.include_hidden,
            "only_match": self.only_match,
            "exclude_match": self.exclude_match,
            "include_exts": _split_ext_list(self.include_exts),
            "exclude_exts": _split_ext_list(self.exclude_exts),
            "verify_content": self.verify_content,
            "sort_mode": self.sort_mode,
            "max_depth": self.max_depth,
        }

    def plan_dict(self) -> dict:
        return {
            "start": self.start, "step": self.step, "width": self.width,
            "scope": self.scope, "reverse": self.reverse,
            "split_ext": self.split_ext, "rename_dirs": self.rename_dirs,
        }

    def apply_scan_dict(self, data: dict) -> None:
        """把预设里的 scan 字典套回表单（只认引擎 ScanOptions 的字段）。"""
        if not data:
            return
        kinds = tuple(data.get("kinds") or ())
        for only, ks in ONLY_CHOICES.items():
            if tuple(ks) == kinds:
                self.only = only
                break
        for key in ("recursive", "include_hidden", "verify_content"):
            if key in data:
                setattr(self, key, bool(data[key]))
        for key in ("only_match", "exclude_match"):
            if key in data:
                setattr(self, key, str(data[key] or ""))
        if "sort_mode" in data and data["sort_mode"] in dict(SORT_MODES):
            self.sort_mode = str(data["sort_mode"])
        if "max_depth" in data:
            try:
                self.max_depth = max(1, min(int(data["max_depth"]), MAX_DEPTH))
            except (TypeError, ValueError):
                pass
        for key in ("include_exts", "exclude_exts"):
            if key in data:
                setattr(self, key, _join_ext_list(data[key]))
        # 文件夹类型自带「改名文件夹」语义，保持与命令行一致
        self.rename_dirs = self.only in ("dir", "all")

    def apply_plan_dict(self, data: dict) -> None:
        if not data:
            return
        for key in ("start", "step", "width"):
            if key in data:
                try:
                    setattr(self, key, int(data[key]))
                except (TypeError, ValueError):
                    pass
        if data.get("scope") in P.SCOPES:
            self.scope = data["scope"]
        for key in ("reverse", "split_ext", "rename_dirs"):
            if key in data:
                setattr(self, key, bool(data[key]))


# --------------------------------------------------------------------------- #
# 一次「扫描 + 计划」的结果包
# --------------------------------------------------------------------------- #

@dataclass
class PlanBundle:
    scan: object = None          # engine.scan.ScanResult
    plan: object = None          # engine.plan.Plan（只扫描阶段为 None）
    state: FormState = field(default_factory=FormState)

    @property
    def has_scan(self) -> bool:
        return self.scan is not None

    @property
    def has_plan(self) -> bool:
        return self.plan is not None

    def items(self) -> list:
        """表格该显示的东西：有计划时是计划项，只有扫描结果时是扫描项。"""
        if self.plan is not None:
            return list(self.plan.items)
        if self.scan is not None:
            return list(self.scan.items)
        return []

    def stats(self) -> dict:
        """按有效状态计数（勾选排除的项已置为 skip，这里如实反映）。

        只有扫描结果、还没生成计划时，各状态都是 0——没算过就不能假装有结果，
        只有总数照实给（界面据此显示「已扫描 N 项，尚未应用规则」）。
        """
        counts = {ST_OK: 0, ST_SAME: 0, ST_CONFLICT: 0, ST_ERROR: 0, ST_SKIP: 0}
        if self.plan is None:
            counts["total"] = len(self.scan.items) if self.scan else 0
            return counts
        for it in self.plan.items:
            counts[getattr(it, "status", ST_OK)] = \
                counts.get(getattr(it, "status", ST_OK), 0) + 1
        counts["total"] = len(self.plan.items)
        return counts


def scan_only(state: FormState, progress=None) -> PlanBundle:
    """第一阶段：只扫出文件清单，不碰规则。

    先扫描、再逐步加规则时走这里——扫描不再需要「至少一条规则」。
    """
    scan_res = scan(state.scan_options(), progress)
    return PlanBundle(scan=scan_res, plan=None, state=state)


def build_plan(bundle: PlanBundle, rules: Sequence,
               progress=None, client=None) -> PlanBundle:
    """第二阶段：在已扫出的清单上算名 + 校验（绝不重新扫描文件系统）。

    复用 `bundle.scan.items` 里的 Item 对象，跳过顺序与结果跟一次走完完全一致。
    """
    items = list(bundle.scan.items) if bundle.scan is not None else []
    clear_exclusions(items)          # 上一轮勾选排除的痕迹不能带进新计划
    plan = P.build(items, list(rules or []), bundle.state.plan_options(True),
                   progress, client)
    return PlanBundle(scan=bundle.scan, plan=plan, state=bundle.state)


def build(state: FormState, rules: Sequence, progress=None, client=None) -> PlanBundle:
    """扫描 + 算名 + 校验，一次走完（命令行与旧路径用；界面已拆成两阶段）。"""
    return build_plan(scan_only(state, progress), rules, progress, client)


def apply_plan(bundle: PlanBundle, rules: Sequence, progress=None, client=None):
    """把已算好的计划交给引擎执行器落盘（绝不重算，避免预览与落盘不一致）。"""
    if bundle.plan is None:
        raise ValueError("还没有生成计划，无法执行")
    return OPS.apply(bundle.plan.items, list(rules or []),
                     bundle.state.plan_options(dry_run=False),
                     root=bundle.state.root, kind=bundle.state.only,
                     progress=progress, client=client, plan=bundle.plan)


# --------------------------------------------------------------------------- #
# 勾选排除：用引擎自己的 skip 语义，不另造一套
# --------------------------------------------------------------------------- #

def set_excluded(plan, excluded_keys) -> int:
    """把未勾选的条目置为 skip（引擎的「已跳过」是真实状态，执行器会跳过它）。

    还原时恢复成被排除之前的原状态，不会把真实冲突"洗白"。
    返回被排除的条目数。
    """
    if plan is None:
        return 0
    excluded_keys = set(excluded_keys or ())
    n = 0
    for it in plan.items:
        excluded = os.path.normcase(it.path) in excluded_keys
        if excluded:
            if not it.__dict__.get("_excluded"):
                it.__dict__["_excluded"] = True
                it.__dict__["_status_before"] = it.status
                it.__dict__["_note_before"] = it.note
                it.status = ST_SKIP
                it.note = "按勾选排除"
                n += 1
            else:
                n += 1
        elif it.__dict__.pop("_excluded", False):
            it.status = it.__dict__.pop("_status_before", it.status)
            it.note = it.__dict__.pop("_note_before", "")
    return n


def is_excluded(it) -> bool:
    return bool(it.__dict__.get("_excluded"))


def clear_exclusions(items) -> None:
    """抹掉勾选排除留下的痕迹。

    两阶段改造后，重新生成计划会复用同一批 Item 对象；上一次「按勾选排除」
    打的 skip 标记会跟着 Item 活下来，让新计划莫名其妙少几项。算名前先清掉。
    """
    for it in items or ():
        it.__dict__.pop("_excluded", None)
        it.__dict__.pop("_status_before", None)
        it.__dict__.pop("_note_before", None)
        it.status = ST_OK
        it.note = ""


# --------------------------------------------------------------------------- #
# 状态文案 / 颜色语义
# --------------------------------------------------------------------------- #

STATUS_ORDER = [ST_OK, ST_SAME, ST_CONFLICT, ST_ERROR, ST_SKIP]


def status_text(status: str) -> str:
    return STATUS_TEXT.get(status, status or "")


def status_tone(status: str) -> str:
    """给界面用：info / muted / warn / error。"""
    return {ST_OK: "info", ST_SAME: "muted", ST_CONFLICT: "warn",
            ST_ERROR: "error", ST_SKIP: "muted"}.get(status, "muted")


def kind_text(kind: str) -> str:
    return KIND_TEXT.get(kind, kind or "")


# --------------------------------------------------------------------------- #
# 规则元数据（界面表单要用的标签与控件类型；参数本身仍来自引擎）
# --------------------------------------------------------------------------- #

# (字段, 标签, 控件, 选项/提示)
# 控件：line / int / combo / text(多行) / bool
#
# 编号的标量参数（起始值 / 步长 / 补零宽度 / 编号范围）只有一处归属：规则链的
# 链级参数节点（计划层的 PlanOptions）。「编号 / 序列化编号」规则只决定号码插在
# 哪个位置——曾经两边各有一套起始值与补零，谁生效全看哪边非空，属于同一件事的
# 两套输入。a / b 仍在 Rule 上保留，只用于读回旧配置（见 number_legacy）。
_NUM_POS = [("prefix", "名字前面"), ("suffix", "名字后面"),
            ("before", "锚点之前"), ("after", "锚点之后")]
_SCOPES = [(s, P.SCOPE_TEXT[s]) for s in P.SCOPES]

KIND_FIELDS: Dict[str, List[Tuple[str, str, str, object]]] = {
    "insert": [("a", "位置", "combo", [("prefix", "名字前面"), ("suffix", "名字后面"),
                                   ("before", "某字符之前"), ("after", "某字符之后")]),
               ("b", "锚点字符", "line", "before/after 时用来定位，可留空"),
               ("c", "插入的文字", "line", "")],
    "delete": [("a", "要删除的文字", "line", "删除所有出现")],
    "remove_chars": [("a", "要删掉的字符集", "line", "例如 -_. 会去掉这三种分隔符")],
    "replace": [("a", "查找", "line", ""), ("b", "替换为", "line", "留空表示删除")],
    "regex": [("a", "正则表达式", "line", r"例如 (\d+)"),
              ("b", "替换为", "line", r"支持 \1 与 {1} {name} {n}")],
    "keep_after": [("a", "分隔符", "line", "只保留它之后的内容；留空则按长度对半切")],
    "keep_before": [("a", "分隔符", "line", "只保留它之前的内容")],
    "remove_after": [("a", "分隔符", "line", "删除它之后的内容")],
    "remove_before": [("a", "分隔符", "line", "删除它之前的内容")],
    "prefix": [("a", "前缀", "line", "")],
    "suffix": [("a", "后缀", "line", "文件加在扩展名之前")],
    "case": [("a", "转换方式", "combo", [("lower", "全部小写"), ("upper", "全部大写"),
                                     ("title", "每个词首字母大写"),
                                     ("sentence", "句首大写"),
                                     ("capitalize", "整个名字首字母大写")])],
    "trim": [("a", "要剥掉的字符集", "line", "留空 = 剥空白，并去掉结尾的点")],
    "collapse": [("a", "要合并的字符", "line", "例如一个空格")],
    "number": [("c", "位置", "combo", _NUM_POS)],
    "template": [("a", "模板原文", "template", "点击右侧按钮打开模板编辑器")],
    "slug": [("a", "分隔符", "line", "默认 _")],
    "translit": [],
    "hash": [("a", "算法", "combo", [("md5", "MD5"), ("sha1", "SHA-1"),
                                  ("sha256", "SHA-256"), ("crc32", "CRC32")]),
             ("b", "取前 N 位", "int", "0 = 全取"),
             ("c", "位置", "combo", [("suffix", "名字后面"), ("prefix", "名字前面")])],
    "filename_clean": [("b", "非法字符替换为", "line", "默认 _")],
    "move": [("a", "从第几个字符起", "int", "从 1 开始"),
             ("b", "取几个字符", "int", ""),
             ("c", "插到第几个位置", "int", "")],
    "pad_numbers": [("a", "目标宽度", "int", "例如 3 会把 7 补成 007"),
                    ("b", "填充字符", "line", "默认 0")],
    # 编号：发号与插号在同一条规则里（序列化编号已并入，选择器里不再重复列出）
    "number": [("a", "起始值", "int", "第一个对象拿到的编号"),
               ("extras.step", "步长", "int", "1 = 1、2、3；10 = 1、11、21"),
               ("b", "补零宽度", "int", "0 = 不补零"),
               ("extras.scope", "编号范围", "combo", _SCOPES),
               ("extras.reverse", "倒序编号", "check", "从后往前发号"),
               ("c", "位置", "combo", _NUM_POS)],
    # 序列化：整个名字换成序号（扩展名不动），默认就是「四位 / 每文件夹从 0001 起」
    "renumber": [("a", "起始值", "int", "第一个对象拿到的序号"),
                 ("extras.step", "步长", "int", "1 = 0001、0002、0003"),
                 ("b", "补零宽度", "int", "默认 4 位"),
                 ("extras.scope", "编号范围", "combo", _SCOPES),
                 ("extras.reverse", "倒序编号", "check", "从后往前发号")],
    "randomize": [("a", "随机串长度", "int", "默认 8"),
                  ("b", "字符集", "line", "默认 0-9a-z"),
                  ("c", "位置", "combo", [("prefix", "名字前面"), ("suffix", "名字后面")])],
    "date_insert": [("a", "日期风格", "combo", [("yyyy-mm-dd", "2026-10-19"),
                                            ("yyyy年mm月dd日", "2026年10月19日"),
                                            ("yyyymmdd", "20261019"),
                                            ("hh-nn-ss", "21:44:42")]),
                    ("c", "位置", "combo", [("prefix", "名字前面"), ("suffix", "名字后面")])],
    "extension": [("a", "改成这个扩展名", "line", "含点，例如 .md；留空则看下面"),
                  ("b", "特殊处理", "combo", [("", "不改（除非上面填了）"),
                                          ("remove", "去掉扩展名"),
                                          ("lower", "扩展名转小写"),
                                          ("upper", "扩展名转大写")])],
    "llm": [("extras.instruction", "整理要求", "text", "留空 = 使用要求文件"),
            ("extras.lang", "输出语言", "combo", [("zh", "中文"), ("en", "英文"),
                                               ("ja", "日文")]),
            ("extras.max_len", "名字最大长度", "int", "默认 24")],
}

# 哪些规则支持哪些通用开关（依据 apply_rule / apply_chain 的真实分支）
KIND_FLAGS: Dict[str, set] = {
    kind: {"selector", "skip_ext", "occurrence"} for kind in R.RULE_KIND_IDS
}
# 扩展名严格由模板/扩展名规则/LLM 自己管，skip_ext 没有意义
for _k in ("template", "extension", "llm"):
    KIND_FLAGS[_k] = {"selector", "occurrence"}
# number 的分隔符是「锚点」，选择器会让两者抢同一个字段
KIND_FLAGS["number"] = {"skip_ext", "occurrence"}
# 序列化整名就是号码，选择器与「不含扩展名」都无从谈起
KIND_FLAGS["renumber"] = {"occurrence"}
KIND_FLAGS["extension"] = set()
KIND_FLAGS["remove_after"] = {"selector", "skip_ext", "occurrence", "keep_sep"}
KIND_FLAGS["remove_before"] = {"selector", "skip_ext", "occurrence", "keep_sep"}

RULE_GROUPS: List[Tuple[str, List[str]]] = [
    ("增删", ["insert", "delete", "remove_chars", "replace", "regex"]),
    ("切取", ["keep_after", "keep_before", "remove_after", "remove_before"]),
    ("修饰", ["prefix", "suffix", "case", "trim", "collapse"]),
    ("编号与序列", ["number", "renumber", "pad_numbers", "randomize", "date_insert"]),
    ("模板与规范化", ["template", "slug", "translit", "filename_clean", "move",
                 "hash", "extension"]),
    ("智能", ["llm"]),
]

# 常用规则（工具条上的快捷入口，全部是引擎真实 kind）
COMMON_RULES = ["template", "replace", "regex", "case", "number", "trim", "llm"]

DEFAULTS: Dict[str, dict] = {
    "keep_after": {"a": "-"}, "keep_before": {"a": "-"},
    "remove_after": {"a": "-"}, "remove_before": {"a": "-"},
    "prefix": {"a": "新_"}, "suffix": {"a": "_备份"},
    "insert": {"a": "suffix", "b": "", "c": "补充"},
    "number": {"a": "1", "extras.step": "1", "b": "", "extras.scope": "global",
               "extras.reverse": "", "c": "prefix"},
    # 与内置预设「照片序号（四位）」一致：四位、每个文件夹各自从 0001 起
    "renumber": {"a": "1", "extras.step": "1", "b": "4", "extras.scope": "folder",
                 "extras.reverse": ""},
    "template": {"a": "{n}"},
    "case": {"a": "lower"},
    "pad_numbers": {"a": "3"},
    "hash": {"a": "md5", "b": "8", "c": "suffix"},
    "move": {"a": "1", "b": "2", "c": "1"},
    "randomize": {"a": "8", "b": "", "c": "prefix"},
    "date_insert": {"a": "yyyy-mm-dd", "c": "suffix"},
    "slug": {"a": "_"},
    "extension": {"a": ".txt"},
    "collapse": {"a": " "},
    "llm": {},
}


def new_rule(kind: str) -> R.Rule:
    """按真实默认值造一条规则；id 由引擎生成（跨进程稳定）。"""
    d = DEFAULTS.get(kind, {})
    rule = R.make_rule(kind, d.get("a", ""), d.get("b", ""), d.get("c", ""))
    # 带点号的键落到 extras（编号规则的步长 / 编号范围 / 倒序就住在这里）
    for key, value in d.items():
        if "." in key:
            set_field(rule, key, value)
    if kind == "llm":
        rule.extras.setdefault("lang", "zh")
        rule.extras.setdefault("max_len", "24")
        rule.extras.setdefault("instruction", "")
    return rule


def clone_rule(rule: R.Rule) -> R.Rule:
    """复制一条规则（新 id，其余字段照抄）。"""
    data = rule.to_dict()
    data["id"] = ""
    return R.normalize(R.Rule(**data))


def rule_title(rule: R.Rule) -> str:
    return R.RULE_KIND_TEXT.get(rule.kind, rule.kind)


def rule_kind_text(kind: str) -> str:
    """按规则类型取中文名（还没造出 Rule 对象时用，例如规则选择弹窗）。"""
    return R.RULE_KIND_TEXT.get(kind, kind or "")


def rule_kind_note(kind: str) -> str:
    """按规则类型取一句话说明（弹窗 tooltip）。"""
    return R.RULE_NOTE.get(kind, "")


def rule_summary(rule: R.Rule) -> str:
    """列表右侧那句「简短参数摘要」，直接取引擎的 describe()。"""
    return rule.describe()


def rule_hint(rule: R.Rule) -> str:
    return R.RULE_NOTE.get(rule.kind, "")


def rule_fields(kind: str) -> List[Tuple[str, str, str, object]]:
    return KIND_FIELDS.get(kind, [])


def rule_flags(kind: str) -> set:
    return KIND_FLAGS.get(kind, set())


def get_field(rule: R.Rule, key: str) -> str:
    if key.startswith("extras."):
        return str((rule.extras or {}).get(key[7:], "") or "")
    return str(getattr(rule, key, "") or "")


def set_field(rule: R.Rule, key: str, value) -> None:
    if key.startswith("extras."):
        ex = rule.extras if isinstance(rule.extras, dict) else {}
        ex[key[7:]] = value
        rule.extras = ex
    else:
        setattr(rule, key, value)


def rule_problems(rule: R.Rule) -> List[str]:
    """就地输入校验（只查真实会被引擎用到、且能立刻判断的部分）。"""
    probs: List[str] = []
    kind = rule.kind
    a, b, c = rule.a, rule.b, rule.c

    def need(value: str, label: str) -> None:
        if not str(value or "").strip():
            probs.append("「%s」不能为空" % label)

    if kind in ("delete", "remove_chars", "keep_after", "prefix", "suffix"):
        need(a, R.RULE_PARAM_HINT.get(kind, {}).get("a", "参数"))
    if kind in ("replace", "regex"):
        need(a, "查找" if kind == "replace" else "正则表达式")
        if kind == "regex" and a:
            try:
                re.compile(a)
            except re.error as exc:
                probs.append("正则不合法：%s" % exc)
    if kind == "template":
        need(a, "模板原文")
    if kind in ("number", "renumber"):
        for key, val, label in (("a", a, "起始值"), ("b", b, "补零宽度"),
                                ("extras.step", get_field(rule, "extras.step"), "步长")):
            if str(val).strip():
                try:
                    int(val)
                except (TypeError, ValueError):
                    probs.append("「%s」要是整数" % label)
    if kind == "insert":
        if a in ("before", "after") and not str(b).strip():
            probs.append("选了「锚点之前/之后」就需要填锚点字符")
        need(c, "插入的文字")
    if kind == "extension":
        if not a and not b:
            probs.append("要么填新扩展名，要么选一种特殊处理")
        if a and not a.startswith("."):
            probs.append("扩展名建议带上点，例如 .md")
    if kind == "llm":
        try:
            int(str(get_field(rule, "extras.max_len") or 24))
        except (TypeError, ValueError):
            probs.append("「名字最大长度」要是整数")
    return probs


def rule_warnings(rule: R.Rule) -> List[str]:
    """不阻断保存、但值得提醒的情况。"""
    out: List[str] = []
    if rule.kind == "regex" and rule.a:
        try:
            re.compile(rule.a)
        except re.error:
            return out
        if "\\1" in rule.b or "{1}" in rule.b:
            out.append("替换串引用了第 1 个分组：只有正则里有括号时才有效")
    if rule.kind == "template" and rule.a and "{ext" not in rule.a \
            and "{扩展名" not in rule.a:
        out.append("模板没写扩展名变量：引擎会在末尾自动找回原扩展名")
    return out


def move_rule(rules: List[R.Rule], src: int, dst: int) -> List[R.Rule]:
    """调整顺序（列表拖拽与上下按钮共用）。"""
    out = list(rules)
    if not (0 <= src < len(out)):
        return out
    item = out.pop(src)
    dst = max(0, min(dst, len(out)))
    out.insert(dst, item)
    return out


def describe_chain(rules: Sequence) -> str:
    return R.describe_chain(list(rules or []))


# --------------------------------------------------------------------------- #
# 模板参考（全部来自真实实现）
# --------------------------------------------------------------------------- #

def template_variables() -> List[Tuple[str, str]]:
    """模板可用变量：以 NameContext.variables() 的真实键为准。"""
    from engine.context import NameContext

    ctx = NameContext(os.path.join(os.getcwd(), "示例-2026-10-19.txt"),
                      "示例-2026-10-19.txt", False, 1, 1, 1, 4,
                      st=_sample_stat())
    try:
        names = list(ctx.variables())
    except Exception:
        names = []
    out: List[Tuple[str, str]] = []
    for key in names:
        try:
            val = ctx.var(key)
        except Exception:
            val = ""
        out.append(("{%s}" % key, val))
    return out


def _sample_stat():
    class _St:
        st_size = 123456
        st_mtime = 1760000000.0
        st_ctime = 1760000000.0
        st_atime = 1760000000.0
    return _St()


def selector_kinds() -> List[str]:
    return list(TPL.SELECTOR_KINDS)


def selector_text(sel: str) -> str:
    return TPL.SELECTOR_TEXT.get(sel, sel)


def reference_keys() -> Tuple[str, ...]:
    return tuple(TPL.REF_KEYS)


def render_preview(template: str, item=None) -> str:
    """用真实 render() 渲染一段模板；没有真实条目时用样例上下文。

    返回的是引擎算出来的字符串，不是界面自己拼接的示例。
    """
    if item is not None:
        ctx = item._ctx_obj()
        num, width = item.num, item.width
    else:
        from engine.context import NameContext
        ctx = NameContext(os.path.join(os.getcwd(), "示例-2026-10-19.txt"),
                          "示例-2026-10-19.txt", False, 1, 1, 1, 4,
                          st=_sample_stat())
        num, width = 1, 4
    return TPL.render(template, ctx, num=num, width=width)


# --------------------------------------------------------------------------- #
# 预设
# --------------------------------------------------------------------------- #

def builtin_presets() -> List[Tuple[str, PS.Preset]]:
    return [(name, PS.BUILTIN[name]) for name in PS.builtin_list()]


def preset_paths() -> List[str]:
    return PS.list_presets()


def load_preset(path: str) -> PS.Preset:
    return PS.load(path)


def preset_from_state(name: str, rules: Sequence, state: FormState,
                      note: str = "") -> PS.Preset:
    return PS.Preset(name=name, rules=list(rules or []),
                     scan=state.scan_dict(), plan=state.plan_dict(), note=note)


def save_preset(preset: PS.Preset, path: Optional[str] = None) -> str:
    return PS.save(preset, path)


def delete_preset(name_or_path: str) -> bool:
    return PS.delete(name_or_path)


def preset_dir() -> str:
    return PS.preset_dir()


# --------------------------------------------------------------------------- #
# 日志 / 撤销 / CSV / 残留
# --------------------------------------------------------------------------- #

def logs(root: str) -> List[dict]:
    """把撤销日志读成界面要用的行。字段全部来自真实 schema。

    root 给了 = 只看那个目标目录的；root 空 = 全部（所有目录的日志混在一起）。
    """
    rows = []
    for path in C.list_logs(root or ""):
        try:
            data = C.load_log(path)
        except (OSError, ValueError):
            continue
        pairs = C.log_pairs(data)
        rows.append({
            "path": path,
            "time": str(data.get("time") or ""),
            "kind": str(data.get("kind") or ""),
            "schema": data.get("schema"),
            "note": str(data.get("note") or ""),
            "root": str(data.get("root") or ""),
            "count": len(pairs),
            "summary": data.get("summary") or {},
            "pairs": pairs,
            "name": os.path.basename(path),
        })
    rows.sort(key=lambda r: r["time"], reverse=True)
    return rows


def undo(log_path: str, progress=None):
    return C.undo(log_path, progress)


def export_csv(plan, path: str) -> str:
    return OPS.export_csv(plan, path)


def temp_leftovers(root: str) -> List[str]:
    return C.find_temp_leftovers(root or "")


def log_dir(root: str) -> str:
    """撤销日志的存放目录（统一的一个，与目标目录无关）。"""
    return C.log_dir(root or "")


def backup_dir() -> str:
    """撤销日志文件夹的完整路径（`<程序目录>\\Name_backup`）。"""
    return C.log_dir("")


# --------------------------------------------------------------------------- #
# LLM
# --------------------------------------------------------------------------- #

def llm_config(provider: str = "", model: str = "", batch: int = 0) -> "LLM.LLMConfig":
    """最终生效的接入配置。

    优先级：界面保存的供应商配置 > 环境变量 > 内置默认值。
    界面上保存过一次供应商配置之后，就完全以那份配置为准；没保存过则走
    环境变量，老用户的行为不变。
    """
    cfg = PRV.effective_config()
    if provider:
        cfg.provider = provider
    if model:
        cfg.model = model
    if batch and int(batch) > 0:
        cfg.batch = int(batch)
    return cfg


def llm_client(provider: str = "", model: str = "", batch: int = 0):
    return LLM.make_client(llm_config(provider, model, batch))


def llm_client_config(cfg):
    """按界面上的供应商配置造客户端（cfg 为 None 时用当前生效的配置）。"""
    if cfg is None:
        return LLM.make_client(llm_config())
    return LLM.make_client(llm_config_from_provider(cfg))


# --------------------------------------------------------------------------- #
# 供应商配置（界面「LLM 设置」页的全部读写都走这里）
# --------------------------------------------------------------------------- #

def llm_provider_config() -> "PRV.ProviderConfig":
    """界面上显示的那一份供应商配置。

    保存过就以保存的为准；从没保存过则用环境变量兜底，老用户看到的还是熟悉的那套。
    """
    return PRV.effective_provider_config()


def llm_save_provider_config(cfg, api_key: Optional[str] = None) -> str:
    """保存供应商配置；api_key 为 None 表示不动已保存的密钥。"""
    return PRV.save_config(cfg, api_key)


def llm_provider_presets() -> List[Tuple[str, str]]:
    """供应商预设（界面下拉框用）。"""
    return [(p.key, p.label) for p in PRV.PROVIDER_PRESETS]


def llm_provider_preset_notes() -> Dict[str, str]:
    """每个预设的一句话说明。"""
    return {p.key: p.note for p in PRV.PROVIDER_PRESETS}


def llm_protocols() -> List[Tuple[str, str]]:
    """协议列表（界面下拉框用）。"""
    return list(PRV.PROTOCOLS)


def llm_protocol_text(protocol: str) -> str:
    """协议代号 → 人话名字。"""
    return PRV.PROTOCOL_TEXT.get(protocol or "", protocol or "")


# 预设 → 引擎客户端类型（界面按预设换客户端）
PRV_KIND = {p.key: p.kind for p in PRV.PROVIDER_PRESETS}


def llm_config_from_provider(cfg) -> "LLM.LLMConfig":
    """把供应商配置翻译成引擎的接入配置。"""
    return LLM.LLMConfig(
        provider=(cfg.kind or "openai"),
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        model=cfg.model,
        timeout=int(cfg.timeout),
        batch=int(cfg.batch),
        temperature=float(cfg.temperature),
        protocol=cfg.protocol,
        headers=dict(cfg.headers or {}),
        max_tokens=int(cfg.max_tokens),
    )


def llm_apply_preset(cfg, preset_key: str):
    """套用供应商预设，返回新的配置对象（不落盘）。"""
    return PRV.apply_preset(cfg, preset_key)


def llm_models_url(cfg) -> str:
    return PRV.models_url_for(cfg)


def llm_fetch_models(cfg) -> Tuple[List[str], str]:
    """拉取模型列表。返回 (模型名, 错误原因)。"""
    return PRV.list_models(cfg)


def llm_test_connection(cfg) -> dict:
    """测试供应商连通性：真发一个请求，返回耗时与原因。"""
    return PRV.test_connection(cfg)


def llm_status() -> dict:
    """供应商连接状态（界面左下角的气泡显示的就是它）。"""
    snap = PRV.STATUS.snapshot()
    if not snap.get("model"):
        snap["model"] = llm_provider_config().model
    return snap


def llm_status_set_model(model: str):
    """把当前生效的模型名记进状态（气泡第一行显示它）。"""
    PRV.STATUS.set_model(model)


def llm_status_mark_unknown(model: str = ""):
    """配置改了但还没重新测：状态回到"未知"，别拿旧结果说事。"""
    PRV.STATUS.mark_unknown(model)


def llm_provider_key_saved() -> bool:
    """本地是否已经保存过密钥（界面只显示"已保存"，绝不回显内容）。"""
    return PRV.secrets.has_secret()


def llm_provider_config_path() -> str:
    return PRV.config_path()


def llm_provider_config_saved() -> bool:
    """界面上是不是已经保存过本机配置（没保存过就还认环境变量）。"""
    return os.path.isfile(PRV.config_path())


def llm_save_headers(text: str) -> Dict[str, str]:
    """把"每行一条 Key: Value"解析成请求头字典。"""
    out: Dict[str, str] = {}
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def llm_headers_to_text(headers: Dict[str, str]) -> str:
    """请求头字典转回"每行一条 Key: Value"。"""
    return "\n".join("%s: %s" % (k, v) for k, v in (headers or {}).items())


def llm_env_sources() -> List[Tuple[str, str, bool]]:
    """真实生效的环境变量名与是否已设置（只展示名字，绝不展示值）。

    改名后主名是 `NAMECAT_LLM_*`，但引擎仍然认旧名 `NAMEKIT_LLM_*`；
    这里把两个名字合起来判断"是否已设置"，否则老用户明明配了却在界面上
    看到"未设置"，会误以为配置丢了。
    """
    groups = [
        (("NAMECAT_LLM_API_KEY", "NAMEKIT_LLM_API_KEY"), "接口密钥"),
        (("NAMECAT_LLM_BASE_URL", "NAMEKIT_LLM_BASE_URL"), "接口地址"),
        (("NAMECAT_LLM_MODEL", "NAMEKIT_LLM_MODEL"), "模型名"),
        (("NAMECAT_LLM_PROVIDER", "NAMEKIT_LLM_PROVIDER"), "客户端类型"),
        (("OPENAI_API_KEY",), "接口密钥（OpenAI 兼容）"),
        (("OPENAI_BASE_URL",), "接口地址（OpenAI 兼容）"),
        (("DEEPSEEK_API_KEY",), "接口密钥（DeepSeek）"),
        (("DEEPSEEK_BASE_URL",), "接口地址（DeepSeek）"),
        ((LLM.INSTRUCTION_ENV, LLM.LEGACY_INSTRUCTION_ENV), "要求文件位置"),
    ]
    out = []
    for names, label in groups:
        primary = names[0]
        is_set = any(os.environ.get(n) for n in names)
        note = label if len(names) == 1 else "%s（旧名 %s 仍然生效）" % (label, names[1])
        out.append((primary, note, is_set))
    return out


def llm_instruction_path(custom: str = "") -> str:
    return LLM.instruction_path(custom)


def llm_read_instruction(custom: str = "") -> str:
    return LLM.read_instruction(custom)


def llm_write_instruction(text: str, custom: str = "", with_header: bool = True) -> str:
    return LLM.write_instruction(text, custom, with_header)


def llm_ensure_instruction(custom: str = "") -> str:
    return LLM.ensure_instruction_file(custom)


def llm_default_instruction() -> str:
    return LLM.DEFAULT_INSTRUCTION


def llm_styles() -> List[Tuple[str, str]]:
    return list(LLM.STYLES.items())


def llm_has_rule(rules: Sequence) -> bool:
    return any(getattr(r, "kind", "") == "llm" for r in (rules or []))


# --------------------------------------------------------------------------- #
# 杂项
# --------------------------------------------------------------------------- #

def app_title() -> str:
    return "%s %s" % (APP_NAME, APP_VER)


def sort_modes() -> List[Tuple[str, str]]:
    return list(SORT_MODES)


def scope_text() -> Dict[str, str]:
    return dict(P.SCOPE_TEXT)


def image_exts_text() -> str:
    return ", ".join(sorted(e.lstrip(".") for e in IMAGE_EXTS))


def video_exts_text() -> str:
    return ", ".join(sorted(e.lstrip(".") for e in VIDEO_EXTS))


def max_depth() -> int:
    return MAX_DEPTH
