# -*- coding: utf-8 -*-
"""
engine.rules —— 规则链。

合并原则
    · 吸收 ReNamer 的规则清单：种类多、可排序启停、可保存成预设
    · 吸收 BatchRenamer 的 13 种规则与其中文文案、`名称=参数` 命令行写法
    · 两边重复的（keep/remove after/before、replace、prefix、suffix、trim、
      template）合并成一条，用 selector 表达「只作用于某一段」

每条规则的结构：
    id          稳定标识（预设与撤销日志引用它）
    kind        规则类型（见 RULE_KINDS）
    a / b / c   三段参数
    enabled     是否启用
    selector    作用范围：text/after/before/from/until/range
    charset     selector 的分隔符或区间表达式
    occurrence  第几次出现（1 首次，-1 最后）
    skip_ext    是否排除扩展名（仅文件有意义）
    keep_sep    切分时是否保留分隔符（对照 BatchRenamer 的同名开关）

规则链按列表顺序串行生效，所以顺序本身就是语义。
本模块纯函数，无 IO（hash 规则要读文件，那是唯一例外，且失败即原样返回）。
"""

from __future__ import annotations

import hashlib
import random
import re
import unicodedata
import uuid
import zlib
from dataclasses import dataclass, field

from . import context
from . import template as tpl
from . import zhconv as _ZH

# --------------------------------------------------------------------------- #
# 规则类型表：kind -> 中文名 / 参数提示 / 说明
# --------------------------------------------------------------------------- #

RULE_KINDS = [
    ("insert", "插入文字",
     {"a": "位置 prefix/suffix/before/after", "b": "锚点字符（before/after 用）",
      "c": "要插入的文字"},
     "prefix=名字前，suffix=名字后，before/after=某字符之前/之后"),
    ("delete", "删除文字", {"a": "要删除的文字", "b": "", "c": ""}, "删除所有出现"),
    ("remove_chars", "删除指定字符集", {"a": "要删掉的字符集合（逐个删）", "b": "", "c": ""},
     "a=\"-_. \" 去掉这些分隔符"),
    ("replace", "文本替换", {"a": "查找", "b": "替换为", "c": ""}, "区分大小写"),
    ("regex", "正则替换", {"a": "正则表达式", "b": "替换为（\\1 或 {name} 引用）", "c": ""},
     "非法正则原样返回，不抛异常"),
    ("keep_after", "只保留某字符之后", {"a": "分隔符", "b": "", "c": ""}, ""),
    ("keep_before", "只保留某字符之前", {"a": "分隔符", "b": "", "c": ""}, ""),
    ("remove_after", "删除某字符之后", {"a": "分隔符", "b": "", "c": ""},
     "keep_sep=1 时连同分隔符保留"),
    ("remove_before", "删除某字符之前", {"a": "分隔符", "b": "", "c": ""}, ""),
    ("prefix", "添加前缀", {"a": "前缀", "b": "", "c": ""}, ""),
    ("suffix", "添加后缀", {"a": "后缀", "b": "", "c": ""}, "文件加在扩展名之前"),
    ("case", "大小写", {"a": "upper/lower/title/sentence/capitalize", "b": "", "c": ""}, ""),
    ("trim", "去首尾空格与尾点", {"a": "要剥掉的字符集（默认空白）", "b": "", "c": ""}, ""),
    ("collapse", "合并重复字符", {"a": "要合并的字符（如空格）", "b": "", "c": ""}, ""),
    ("number", "编号",
     {"a": "起始值（默认 1）", "step": "步长（默认 1）", "b": "补零宽度",
      "scope": "编号范围 global/folder/kind/folder_kind", "reverse": "倒序 1/0",
      "c": "位置 prefix/suffix/before/after"},
     "发号与插号都在这一条里：起始值 + 步长逐项递增，补零宽度、编号范围、"
     "倒序也挂在本规则上；before/after 的锚点字符用 ;sep= 指定"),
    ("template", "模板", {"a": "模板原文", "b": "", "c": ""},
     "可用 {n} {name} {ext} {date} {parent} 等，见 README"),
    ("slug", "规范化为 URL 安全名", {"a": "替换成什么（默认 _）", "b": "", "c": ""}, ""),
    ("translit", "音译（转拉丁字母）", {"a": "", "b": "", "c": ""}, ""),
    ("zhconv", "简繁转换", {"a": "s2t（简转繁）/ t2s（繁转简）", "b": "", "c": ""},
     "逐字转换，表外字符（数字、字母、符号…）原样保留"),
    ("hash", "追加文件校验值", {"a": "md5/sha1/sha256/crc32", "b": "取前 N 位（0=全取）",
                                "c": "位置 prefix/suffix"}, ""),
    ("filename_clean", "Windows 文件名清理", {"a": "", "b": "非法字符替换成（默认 _）", "c": ""},
     "去非法字符、去尾随空格与点、防保留名"),
    ("move", "移动字符位置", {"a": "从第几个字符起", "b": "取几个", "c": "插到第几个位置"}, ""),
    ("pad_numbers", "数字补零", {"a": "宽度", "b": "填充字符（默认 0）", "c": ""}, ""),
    ("renumber", "音视频文件序列化",
     {"a": "起始值（默认 1）", "step": "步长（默认 1）", "b": "补零宽度（默认 4）",
      "scope": "编号范围（默认 folder）", "reverse": "倒序 1/0"},
     "整个名字换成序号，扩展名原样保留：默认四位、每个文件夹各自从 0001 起，"
     "适合照片 / 音视频批量序列化"),
    ("randomize", "随机化", {"a": "随机串长度", "b": "字符集（默认 0-9a-z）", "c": "位置 prefix/suffix"}, ""),
    ("date_insert", "插入日期", {"a": "日期风格 yyyy-mm-dd", "b": "", "c": "位置 prefix/suffix"},
     "默认取文件修改时间"),
    ("extension", "扩展名", {"a": "改成什么扩展名（含点），b=remove/case 时见说明",
                             "b": "remove/lower/upper", "c": ""},
     "a 非空则替换扩展名；b=remove 去掉扩展名"),
    ("llm", "LLM 智能精简",
     {"a": "整理要求（留空=用要求文件）", "b": "输出语言（zh/en…）", "c": "名字最大长度"},
     "调用大模型把名字里的无意义片段（编号、日期戳、相机前缀、下载站后缀…）"
     "精简成可读短语；要求可在 %APPDATA%\\NameCat\\llm_instruction.txt 里改，"
     "也可由 --llm-instruction 临时指定；整批一次请求，离线/无 Key 时自动跳过"),
]

RULE_KIND_TEXT = {k: t for k, t, _p, _d in RULE_KINDS}
RULE_PARAM_HINT = {k: p for k, _t, p, _d in RULE_KINDS}
RULE_NOTE = {k: d for k, _t, _p, d in RULE_KINDS}
RULE_KIND_IDS = [k for k, _t, _p, _d in RULE_KINDS]

# 命令行别名：吸收 BatchRenamer 的 13 个规则名，让老命令与新命令都能跑
CLI_ALIASES = {
    "keep-after": "keep_after", "keep_after": "keep_after",
    "keep-before": "keep_before", "keep_before": "keep_before",
    "remove-after": "remove_after", "remove_after": "remove_after",
    "remove-before": "remove_before", "remove_before": "remove_before",
    "remove-char": "remove_chars", "remove-chars": "remove_chars",
    "remove_char": "remove_chars", "strip-chars": "remove_chars",
    "replace": "replace", "regex": "regex", "regexp": "regex",
    "template": "template", "prefix": "prefix", "suffix": "suffix",
    "lower": "case_lower", "upper": "case_upper", "title": "case_title",
    "case-sentence": "case_sentence", "sentence": "case_sentence",
    "trim": "trim", "insert": "insert", "delete": "delete",
    "number": "number", "serialize": "number", "seq": "number",   # serialize 已并入 number
    "slug": "slug", "translit": "translit", "hash": "hash",
    "zh": "zhconv", "zhconv": "zhconv", "s2t": "zhconv", "t2s": "zhconv",
    "简繁": "zhconv",
    "clean": "filename_clean", "filename-clean": "filename_clean",
    "move": "move", "pad": "pad_numbers", "pad-numbers": "pad_numbers",
    "collapse": "collapse", "randomize": "randomize", "random": "randomize",
    "date": "date_insert", "date-insert": "date_insert",
    "ext": "extension", "extension": "extension",
    "llm": "llm", "ai": "llm", "smart": "llm", "llm-clean": "llm",
}

INVALID_NAME_CHARS = '\\/:*?"<>|'
RESERVED_NAMES = {"con", "prn", "aux", "nul",
                  *(f"com{i}" for i in range(1, 10)),
                  *(f"lpt{i}" for i in range(1, 10))}
MAX_NAME_LEN = 255

# 编号范围的短说法（describe 用；完整说法在 plan.SCOPE_TEXT）
_SCOPE_SHORT = {"folder": "每文件夹重新编号", "kind": "每类型重新编号",
                "folder_kind": "每文件夹每类型"}


# --------------------------------------------------------------------------- #
# 规则对象
# --------------------------------------------------------------------------- #

@dataclass
class Rule:
    kind: str = "replace"
    a: str = ""
    b: str = ""
    c: str = ""
    enabled: bool = True
    id: str = ""
    selector: str = "text"
    charset: str = ""
    occurrence: int = 1
    skip_ext: bool = False
    keep_sep: bool = False
    extras: dict = field(default_factory=dict)

    # ---- 展示 ----
    def describe(self) -> str:
        name = RULE_KIND_TEXT.get(self.kind, self.kind)
        scope = ""
        if self.selector and self.selector != "text":
            scope = "（作用于%s%s）" % (
                tpl.SELECTOR_TEXT.get(self.selector, self.selector),
                ("「%s」" % self.charset) if self.charset else "")
        if self.occurrence not in (1, None) and self.selector in (
                "after", "before", "from", "until"):
            scope += "（第%s次出现）" % ("最后" if int(self.occurrence) == -1 else self.occurrence)
        if self.skip_ext:
            scope += "（不含扩展名）"
        if self.kind in ("insert",):
            head = "%s @%s" % (name, self.a or "suffix")
            params = [p for p in (self.b, self.c) if p]
        elif self.kind in ("number", "renumber"):
            # 号码的位置才是「编号」的语义；「序列化」整名就是号码
            if self.kind == "number":
                pos = {"prefix": "名字前面", "suffix": "名字后面",
                       "before": "锚点之前", "after": "锚点之后"}.get(
                           (self.c or "prefix").strip().lower(), "名字前面")
                head = "%s @%s" % (name, pos)
            else:
                head = ""      # 序列化：名字就在标题里，副标题只留给参数
            body = " ".join(self._number_bits())
            return (head + (" " + body if body else "") + scope).strip()
        else:
            head = name
            params = [str(p) for p in (self.a, self.b, self.c) if p != "" and p is not None]
        if self.kind == "zhconv":
            # 参数是 s2t/t2s 这种代号，直接摆出来没人看得懂，换成方向文案
            mode = (self.a or "s2t").strip().lower()
            head = "%s（%s）" % (
                name, "简→繁" if mode != "t2s" else "繁→简")
            params = []
        body = "  ".join("「%s」" % p for p in params)
        return (head + (" " + body if body else "") + scope).strip()

    # ---- 序列化 ----
    def _number_bits(self) -> list:
        """发号参数的一串短说明（编号 / 序列化共用）。"""
        ex = self.extras or {}
        bits = []
        if self.a not in ("", None):
            bits.append("起始 %s" % self.a)
        step = str(ex.get("step") or "").strip()
        if step and step != "1":
            bits.append("步长 %s" % step)
        if self.b not in ("", None, "0"):
            bits.append("补零 %s 位" % self.b)
        scope = str(ex.get("scope") or "").strip()
        if scope and scope != "global":
            bits.append(_SCOPE_SHORT.get(scope, scope))
        if str(ex.get("reverse") or "").strip().lower() not in ("", "0", "false"):
            bits.append("倒序")
        return bits

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "a": self.a, "b": self.b,
                "c": self.c, "enabled": bool(self.enabled),
                "selector": self.selector, "charset": self.charset,
                "occurrence": int(self.occurrence or 1),
                "skip_ext": bool(self.skip_ext), "keep_sep": bool(self.keep_sep),
                "extras": dict(self.extras or {})}


def new_id(seed: str = "") -> str:
    """规则 id。必须跨进程稳定：内置 hash() 受 PYTHONHASHSEED 影响，
    同一份配置在不同进程里会算出不同 id，让 randomize 的种子跟着漂。"""
    raw = "%s|%s" % (seed, uuid.uuid4().hex)
    return "r_" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def normalize(rule: Rule) -> Rule:
    """补齐缺省字段、把别名映射成规范 kind。原地修改并返回。"""
    kind = (rule.kind or "replace").strip()
    kind = CLI_ALIASES.get(kind.lower(), CLI_ALIASES.get(kind, kind))
    for prefix, mode in (("case_lower", "lower"), ("case_upper", "upper"),
                         ("case_title", "title"), ("case_sentence", "sentence")):
        if kind == prefix:
            kind, rule.a = "case", mode
    if kind not in RULE_KIND_TEXT:
        kind = "replace"
    rule.kind = kind
    if not rule.id:
        rule.id = new_id(kind + str(rule.a) + str(rule.b) + str(rule.c))
    rule.selector = (rule.selector or "text").strip().lower()
    if rule.selector not in tpl.SELECTOR_KINDS:
        rule.selector = "text"
    rule.a = "" if rule.a is None else str(rule.a)
    rule.b = "" if rule.b is None else str(rule.b)
    rule.c = "" if rule.c is None else str(rule.c)
    try:
        rule.occurrence = int(rule.occurrence or 1)
    except (TypeError, ValueError):
        rule.occurrence = 1
    rule.enabled = bool(rule.enabled)
    rule.skip_ext = bool(rule.skip_ext)
    rule.keep_sep = bool(rule.keep_sep)
    if rule.kind == "llm":
        # LLM 规则的 a/b/c 是「要求原文 / 语言 / 最大字数」，统一在归一化处落进 extras，
        # 手工构造、JSON 读取、命令行解析因此得到同一种规则（单一实现）。
        ex = rule.extras
        ex["instruction"] = str(ex.get("instruction") or rule.a or "")
        ex["lang"] = str(ex.get("lang") or rule.b or "zh")
        ex["max_len"] = str(ex.get("max_len") or rule.c or "24")
    return rule


def make_rule(kind: str, a: str = "", b: str = "", c: str = "", **kw) -> Rule:
    return normalize(Rule(kind=kind, a=a, b=b, c=c, **kw))


# --------------------------------------------------------------------------- #
# 编号规则的发号参数
# --------------------------------------------------------------------------- #

def number_options(rule) -> dict:
    """从「编号」规则上取出计划层要用的发号参数。

    起始值 / 步长 / 补零宽度 / 编号范围 / 倒序只挂在规则自己身上（同一条规则既
    发号又插号，没有第二个入口）。空值表示「不指定」，交给 PlanOptions 的默认值。
    """
    out: dict = {}
    a = str(getattr(rule, "a", "") or "").strip()
    if a:
        try:
            out["start"] = int(a)
        except ValueError:
            pass
    ex = getattr(rule, "extras", None) or {}
    step = str(ex.get("step") or "").strip()
    if step:
        try:
            out["step"] = int(step)
        except ValueError:
            pass
    b = str(getattr(rule, "b", "") or "").strip()
    if b:
        try:
            out["width"] = max(0, int(b))
        except ValueError:
            pass
    scope = str(ex.get("scope") or "").strip()
    if scope:
        out["scope"] = scope
    if str(ex.get("reverse") or "").strip().lower() not in ("", "0", "false", "no"):
        out["reverse"] = True
    return out


def number_rule(rules):
    """链里第一条启用的发号规则（编号 / 序列化）；没有就返回 None。"""
    for rule in rules or ():
        if getattr(rule, "kind", "") in ("number", "renumber") \
                and getattr(rule, "enabled", True):
            return rule
    return None


# --------------------------------------------------------------------------- #
# 各类变换（只处理传入的文本片段，selector 由 apply_rule 统一处理）
# --------------------------------------------------------------------------- #

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
    "α": "a", "β": "b", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i", "θ": "th",
    "ι": "i", "κ": "k", "λ": "l", "μ": "m", "ν": "n", "ξ": "x", "ο": "o", "π": "p",
    "ρ": "r", "σ": "s", "ς": "s", "τ": "t", "υ": "y", "φ": "f", "χ": "ch", "ψ": "ps",
    "ω": "o",
}


def translit(text: str) -> str:
    out = []
    for ch in text:
        low = ch.lower()
        if low in _TRANSLIT:
            mapped = _TRANSLIT[low]
            out.append(mapped.capitalize() if ch.isupper() and mapped else mapped)
            continue
        decomposed = unicodedata.normalize("NFKD", ch)
        out.append("".join(c for c in decomposed if not unicodedata.combining(c)))
    return "".join(out)


def slugify(text: str, sep: str = "_") -> str:
    sep = sep or "_"
    text = translit(text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^0-9A-Za-z]+", sep, text)
    text = re.sub(re.escape(sep) + "{2,}", sep, text)
    return text.strip(sep).lower()


def clean_filename(text: str, repl: str = "_") -> str:
    repl = (repl or "_")[:1]
    out = "".join(repl if (c in INVALID_NAME_CHARS or ord(c) < 32) else c for c in text)
    out = out.rstrip(" .")
    if out.split(".")[0].strip().lower() in RESERVED_NAMES:
        out = "_" + out
    return out[:MAX_NAME_LEN]


def move_chars(text: str, start, count, dest) -> str:
    try:
        s, n, d = int(start), int(count), int(dest)
    except (TypeError, ValueError):
        return text
    if s < 1 or n < 1 or s - 1 >= len(text):
        return text
    chunk = text[s - 1:s - 1 + n]
    rest = text[:s - 1] + text[s - 1 + n:]
    pos = max(0, min(len(rest), d - 1))
    return rest[:pos] + chunk + rest[pos:]


def pad_numbers(text: str, width, fill="0") -> str:
    try:
        w = int(width)
    except (TypeError, ValueError):
        return text
    if w <= 0:
        return text
    fill = (fill or "0")[:1]
    return re.sub(r"\d+", lambda m: m.group(0).rjust(w, fill), text)


def file_digest(path: str, algo: str) -> str:
    algo = (algo or "md5").lower()
    if algo == "crc32":
        crc = 0
        try:
            with open(path, "rb") as fh:
                while True:
                    block = fh.read(1 << 20)
                    if not block:
                        break
                    crc = zlib.crc32(block, crc)
        except OSError:
            return ""
        return "%08x" % (crc & 0xFFFFFFFF)
    import hashlib
    try:
        h = hashlib.new(algo)
    except (ValueError, TypeError):
        return ""
    try:
        with open(path, "rb") as fh:
            while True:
                block = fh.read(1 << 20)
                if not block:
                    break
                h.update(block)
    except OSError:
        return ""
    return h.hexdigest()


def _case(text: str, mode: str) -> str:
    mode = (mode or "lower").strip().lower()
    if mode == "upper":
        return text.upper()
    if mode == "lower":
        return text.lower()
    if mode in ("title",):
        return text.title()
    if mode == "sentence":
        return text[:1].upper() + text[1:].lower() if text else text
    if mode == "capitalize":
        return text.capitalize()
    if mode == "swap":
        return text.swapcase()
    return text


def _insert(text: str, where: str, anchor: str, payload: str) -> str:
    if not payload:
        return text
    where = (where or "suffix").strip().lower()
    if where == "prefix":
        return payload + text
    if where == "suffix":
        return text + payload
    if not anchor:
        return text + payload
    idx = text.find(anchor)
    if idx < 0:
        return text
    if where == "before":
        return text[:idx] + payload + text[idx:]
    if where == "after":
        j = idx + len(anchor)
        return text[:j] + payload + text[j:]
    return text


def _digits(rule, num, width) -> str:
    """号码 + 补零。

    号码（起始值 / 步长 / 编号范围 / 倒序）由计划层按本规则算好后传进来，
    这里只决定宽度——再叠加一次起始值就会把起始值算两遍。
    """
    try:
        k = int(num) if num is not None else 1
    except (TypeError, ValueError):
        k = 1
    b = str(getattr(rule, "b", "") or "").strip()
    try:
        w = int(b) if b else (width or 0)
    except (TypeError, ValueError):
        w = width or 0
    return tpl._num_text(k, max(0, w))


def transform(rule: Rule, text: str, num: int = 1, width: int = 1,
              ctx=None, index=None) -> str:
    """对已经切好的片段应用规则本身（不含 selector / 扩展名保护）。"""
    kind = rule.kind
    a, b, c = rule.a, rule.b, rule.c

    if kind == "insert":
        return _insert(text, a, b, c)
    if kind == "delete":
        return text.replace(a, "") if a else text
    if kind in ("remove_chars", "strip"):
        return "".join(ch for ch in text if ch not in a) if a else text
    if kind == "replace":
        if not a:
            return text
        if rule.extras.get("regex"):
            try:
                return re.sub(a, b, text)
            except re.error:
                return text
        return text.replace(a, b)
    if kind == "regex":
        if not a:
            return text
        try:
            if rule.extras.get("expand", True):
                # 替换串里的 \1 \g<名> 与 {1} {name} {n} {left} {right} 都展开；
                # ;expand=0 可退回纯 re.sub（替换串里想原样保留 \1 时用）
                return re.sub(a, lambda m: tpl.expand_refs(
                    m.expand(b), m, ctx, num, width,
                    text[:m.start()], text[m.end():]), text)
            return re.sub(a, b, text)
        except re.error:
            return text
    if kind == "keep_after":
        if not a:
            return tpl.split_half(text)
        idx = tpl.cut_index(text, a, rule.occurrence)
        return text if idx < 0 else text[idx + 1:]
    if kind == "keep_before":
        idx = tpl.cut_index(text, a, rule.occurrence)
        return text if idx < 0 else text[:idx]
    if kind == "remove_after":
        if not a:
            return tpl.split_half(text)
        idx = tpl.cut_index(text, a, rule.occurrence)
        if idx < 0:
            return text
        keep = rule.keep_sep or str(c) == "1"
        return text[: idx + 1] if keep else text[:idx]
    if kind == "remove_before":
        if not a:
            return tpl.split_half(text)
        idx = tpl.cut_index(text, a, rule.occurrence)
        if idx < 0:
            return text
        keep = rule.keep_sep or str(c) == "1"
        return text[idx:] if keep else text[idx + 1:]
    if kind == "prefix":
        return a + text
    if kind == "suffix":
        return text + a
    if kind == "case":
        return _case(text, a)
    if kind == "trim":
        out = text.strip(a if a else None)
        return out if a else out.rstrip(".")
    if kind == "collapse":
        if not a:
            return text
        return re.sub("(?:%s)+" % re.escape(a), a, text)
    if kind == "number":
        digits = _digits(rule, num, width)
        anchor = (rule.charset or "").strip()
        pos = (c or "prefix").strip().lower()
        if pos == "suffix":
            return text + digits
        if pos == "before" and anchor and text.find(anchor) >= 0:
            i = text.find(anchor)
            return text[:i] + digits + text[i:]
        if pos == "after" and anchor and text.find(anchor) >= 0:
            i = text.find(anchor) + len(anchor)
            return text[:i] + digits + text[i:]
        return digits + text
    if kind == "renumber":
        # 整个名字就是号码；扩展名由 apply_chain 原样找回
        return _digits(rule, num, width)
    if kind == "template":
        return tpl.render(a, ctx, num=num, width=width)
    if kind == "slug":
        return slugify(text, a)
    if kind == "translit":
        return translit(text)
    if kind == "zhconv":
        mode = (a or "s2t").strip().lower()
        if mode not in ("s2t", "t2s"):
            mode = "s2t"
        return _ZH.convert(text, mode)
    if kind == "hash":
        path = getattr(ctx, "path", "") if ctx is not None else ""
        if not path:
            return text
        h = file_digest(path, a or "md5")
        if not h:
            return text
        try:
            n = int(b or 0)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            h = h[:n]
        return (h + text) if (c or "suffix").strip().lower() == "prefix" else (text + h)
    if kind == "filename_clean":
        return clean_filename(text, b)
    if kind == "move":
        return move_chars(text, a, b, c)
    if kind == "pad_numbers":
        return pad_numbers(text, a, b)
    if kind == "randomize":
        try:
            n = max(0, int(a or 8))
        except (TypeError, ValueError):
            n = 8
        alpha = b or "0123456789abcdefghijklmnopqrstuvwxyz"
        # 随机必须可复现：预览与落盘是两次渲染，种子只取「谁 + 第几号 + 规则」，
        # 两次算出同一串，预览显示的名字就是真正落盘的名字。
        who = ""
        if ctx is not None:
            who = getattr(ctx, "path", "") or getattr(ctx, "name", "")
        rnd = random.Random("%s|%s|%s" % (who, num, rule.id or kind))
        chunk = "".join(rnd.choice(alpha) for _ in range(n))
        return (chunk + text) if (c or "prefix").lower() == "prefix" else (text + chunk)
    if kind == "date_insert":
        style = a or "yyyy-mm-dd"
        val = ""
        if ctx is not None:
            try:
                val = str(ctx.var("date:" + style))
            except Exception:
                val = ""
        if not val:
            return text
        return (val + text) if (c or "prefix").lower() == "prefix" else (text + val)
    if kind == "extension":
        mode = (b or "").strip().lower()
        if mode == "remove":
            stem, ext = _split_ext(text)
            return stem
        if mode in ("lower", "upper"):
            stem, ext = _split_ext(text)
            return stem + (ext.lower() if mode == "lower" else ext.upper())
        if not a:
            return text
        want = a if a.startswith(".") else "." + a
        stem, _ext = _split_ext(text)
        return stem + want
    if kind == "llm":
        # LLM 规则的素材在计划阶段批量算好，挂在 ctx.extra["llm_by_rule"][rule.id]；
        # 没拿到结果就原样保留（批量失败不影响其它规则继续生效）。
        if ctx is None:
            return text
        extra = getattr(ctx, "extra", None) or {}
        by_rule = extra.get("llm_by_rule") or {}
        val = by_rule.get(getattr(rule, "id", ""))
        if val is None:
            val = extra.get("llm")
        if val is None:
            try:
                val = ctx.var("llm")
            except Exception:
                val = None
        return str(val) if val else text
    return text


def _split_ext(text: str):
    """复用 context.split_ext，全项目扩展名语义只此一处。"""
    return context.split_ext(text)


# 模板里显式写了扩展名变量时，说明用户在自行管理扩展名
_TEMPLATE_EXT_RE = re.compile(
    r"\{[^{}]*(?:\bext\b|extdot|扩展名)[^{}]*\}|\$Ext\b", re.IGNORECASE)


def apply_rule(rule: Rule, text: str, num: int = 1, width: int = 1,
               ctx=None, index=None) -> str:
    """先按 selector 切出作用段，再变换，最后拼回去（分隔符不吃掉）。"""
    if not getattr(rule, "enabled", True):
        return text
    if rule.kind == "template":
        # 模板直接产出整段名字；selector 生效时只替换被选中的片段。
        part = tpl.select(text, rule.selector, rule.charset, rule.occurrence)
        new = transform(rule, part, num, width, ctx, index)
        if rule.selector in ("text", "", None):
            return new
        return tpl.splice(text, rule.selector, rule.charset, rule.occurrence, new)

    sel = rule.selector or "text"
    if sel == "text":
        return transform(rule, text, num, width, ctx, index)

    if sel == "range":
        # 区间选择器：只要区间非空就作用（哪怕区间正好覆盖整个名字——
        # 旧实现用「片段等于原文」判失败，导致 sel=range;sep=1-4 这类配置静默失效）
        lo, hi = tpl.parse_range(rule.charset, len(text))
        part = text[lo - 1:hi]
        if not part:
            return text
        new = transform(rule, part, num, width, ctx, index)
        return tpl.splice(text, sel, rule.charset, rule.occurrence, new)

    # 其余选择器靠分隔符定位：找不到分隔符就原样返回
    # （与 BatchRenamer「找不到字符就原样返回」一致）
    if tpl.cut_index(text, rule.charset, rule.occurrence) < 0:
        return text
    part = tpl.select(text, sel, rule.charset, rule.occurrence)
    new = transform(rule, part, num, width, ctx, index)
    return tpl.splice(text, sel, rule.charset, rule.occurrence, new)

def apply_chain(rules, text: str, num: int = 1, width: int = 1, ctx=None,
                index=None, split_ext: bool = True) -> str:
    """把整条规则链作用在一个名字上。

    split_ext=True 时，skip_ext 的规则只作用于主干，扩展名原样拼回；
    最终结果再拼上扩展名。文件夹没有扩展名，等价于整名处理。
    """
    is_dir = getattr(ctx, "is_dir", None)
    stem, ext = (text, "")
    if split_ext and is_dir is False:
        stem, ext = _split_ext(text)
    rule_list = [r for r in (rules or []) if getattr(r, "enabled", True)]

    for rule in rule_list:
        if rule.kind == "template" and rule.selector in ("text", "", None):
            # 模板产出的是完整名字。用户没写 {ext}/{extdot} 时，扩展名原样找回，
            # 这样 `{n}-{name}` 才能得到「0001-f000.txt」而不是丢掉扩展名。
            produced = tpl.render(rule.a, ctx, num=num, width=width)
            if is_dir or _TEMPLATE_EXT_RE.search(rule.a or ""):
                stem, ext = produced, ""
                continue
            e = _split_ext(produced)[1]
            stem, ext = (produced[: len(produced) - len(e)], e) if e else (produced, ext)
            continue
        if rule.kind == "extension":
            # 扩展名规则永远作用于整个名字
            joined = apply_rule(rule, stem + ext, num, width, ctx, index)
            stem, ext = _split_ext(joined)
            continue
        if rule.kind == "llm":
            # LLM 给的是「名字主干」，扩展名必须原样保留
            stem = apply_rule(rule, stem, num, width, ctx, index)
            continue
        if rule.kind == "renumber":
            # 序列化：整名换成号码，扩展名原样保留。没有 ctx 的兼容入口（以及
            # 文件夹）进来时扩展名还没拆过，这里补一次，否则 .mp4 会被一起吃
            if split_ext and is_dir is not False:
                core, tail = _split_ext(stem + ext)
                if core and tail:
                    stem, ext = core, tail
            stem = apply_rule(rule, stem, num, width, ctx, index)
            continue
        if rule.skip_ext and ext:
            stem = apply_rule(rule, stem, num, width, ctx, index)
            continue
        joined = stem + ext
        out = apply_rule(rule, joined, num, width, ctx, index)
        stem, ext = _split_ext(out)
    return stem + ext


def apply_rules(name: str, rules, num: int = 1, width: int = 1, **kw) -> str:
    """BatchRenamer 风格的兼容入口（无 ctx）。"""
    return apply_chain(rules, name, num, width, None, None)


# --------------------------------------------------------------------------- #
# 序列化 / 解析 / 展示
# --------------------------------------------------------------------------- #

def rules_to_json(rules) -> list:
    return [normalize(r).to_dict() for r in (rules or [])]


def _as_dict(value) -> dict:
    """只认真正的映射。手工改过的预设里 `plan: 3` 这类脏值一律当空字典，
    免得 json.load 成功却在构造对象时炸出一串看不懂的 traceback。"""
    return dict(value) if isinstance(value, dict) else {}


def _as_text(value, default: str = "") -> str:
    """非字符串的字段（JSON 里写成数字/null）统一转成文本。"""
    if value is None:
        return default
    return str(value)


def rules_from_json(data) -> list:
    out = []
    for d in data or []:
        if not isinstance(d, dict):
            continue
        try:
            occ = int(d.get("occurrence", 1) or 1)
        except (TypeError, ValueError):
            occ = 1
        out.append(normalize(Rule(
            kind=_as_text(d.get("kind", "replace"), "replace"),
            a=d.get("a", ""), b=d.get("b", ""), c=d.get("c", ""),
            enabled=bool(d.get("enabled", True)),
            id=_as_text(d.get("id", ""), ""),
            selector=_as_text(d.get("selector") or "text", "text"),
            charset=_as_text(d.get("charset") or "", ""),
            occurrence=occ,
            skip_ext=bool(d.get("skip_ext", False)),
            keep_sep=bool(d.get("keep_sep", False)),
            extras=_as_dict(d.get("extras") or {}))))
    return out


def parse_cli_rule(spec: str) -> Rule:
    """命令行规则解析：`名称=参数1=参数2=参数3`。

    兼容 BatchRenamer 的写法：`remove-after=-`、`replace=_:-`、`template=归档_{n}`。
    额外支持用 `;` 分隔 selector：`remove-after=-;occ=-1`。
    """
    text = (spec or "").strip()
    if not text:
        raise ValueError("空规则")
    sel_part = ""
    if ";" in text:
        text, _, sel_part = text.partition(";")
    head, _, rest = text.partition("=")
    name = head.strip()
    kind = CLI_ALIASES.get(name.lower(), name.lower())
    parts = rest.split("=") if rest else []
    a = parts[0] if len(parts) > 0 else ""
    b = parts[1] if len(parts) > 1 else ""
    c = parts[2] if len(parts) > 2 else ""
    if kind == "replace" and ":" in a and not b:
        a, b = a.split(":", 1)
    for prefix, mode in (("case_lower", "lower"), ("case_upper", "upper"),
                         ("case_title", "title"), ("case_sentence", "sentence")):
        if kind == prefix:
            kind, a = "case", mode
    if kind not in RULE_KIND_TEXT:
        raise ValueError("未知规则：%s（可用：%s）" % (
            name, "、".join(sorted(k for k in CLI_ALIASES))))
    if kind == "zhconv" and not a and name.lower() in ("s2t", "t2s"):
        # `t2s` 这种「别名即方向」的写法：规则名本身就是参数，别丢掉
        a = name.lower()
    rule = make_rule(kind, a, b, c)
    if sel_part:
        opts = {}
        for piece in sel_part.split(";"):
            if "=" in piece:
                k, v = piece.split("=", 1)
                opts[k.strip().lower()] = v.strip()
            elif piece.strip():
                rule.selector = piece.strip().lower()
        if "sel" in opts:
            rule.selector = opts["sel"].lower()
        if "sep" in opts:
            rule.charset = opts["sep"]
        if "occ" in opts:
            try:
                rule.occurrence = int(opts["occ"])
            except ValueError:
                pass
        if "keep_sep" in opts:
            rule.keep_sep = opts["keep_sep"] in ("1", "true", "yes")
        if "skip_ext" in opts:
            rule.skip_ext = opts["skip_ext"] in ("1", "true", "yes")
    return normalize(rule)


def describe_chain(rules) -> str:
    parts = [r.describe() for r in (rules or []) if getattr(r, "enabled", True)]
    return " → ".join(parts) if parts else "（无规则）"


def describe_rules_table() -> str:
    """给 --list-rules 用的说明表。"""
    lines = []
    for kind, text, params, note in RULE_KINDS:
        args = " ".join("%s=%s" % (k, v) for k, v in params.items() if v)
        lines.append("%-16s %-22s %s" % (kind, text, args))
        if note:
            lines.append("%-16s %s" % ("", note))
    return "\n".join(lines)
