# -*- coding: utf-8 -*-
"""
engine.template —— 模板渲染与两种旧写法兼容。

变量语法统一成 `{变量}`，同时认出两边的旧写法：
    ReNamer:          $Counter / $Date(yyyy-mm-dd) / $MD5 / $FilePath / <file.name> / &name& / #
    BatchRenamer:     #Name# / %name% / {n} / {date} / {parent}

另外补齐 ReNamer 的细节能力：
    {n:03}            补零宽度
    @@1000000@@       伪随机数字
    \\x41 / \\x{1F600}   转义

本模块纯函数，只依赖 engine.context 提供的变量表，不做 IO。
"""

from __future__ import annotations

import random
import re

# --------------------------------------------------------------------------- #
# selector：把规则限制在名字的某一段
# --------------------------------------------------------------------------- #

SELECTOR_KINDS = ["text", "after", "before", "from", "until", "range"]
SELECTOR_TEXT = {
    "text": "整个名字",
    "after": "某字符之后",
    "before": "某字符之前",
    "from": "从某字符起（含）",
    "until": "到某字符止（含）",
    "range": "字符区间（如 2-4）",
}


def cut_index(text: str, charset: str, occurrence: int = 1) -> int:
    """找分隔符位置。occurrence：1=第一次，-1=最后一次，N=第 N 次；找不到返回 -1。"""
    hits = [i for i, ch in enumerate(text or "") if ch in (charset or "")]
    if not hits:
        return -1
    try:
        occ = int(occurrence or 1)
    except (TypeError, ValueError):
        occ = 1
    if occ == -1:
        return hits[-1]
    if occ < -1:
        return hits[occ] if -occ <= len(hits) else -1
    if occ <= 1:
        return hits[0]
    return hits[occ - 1] if occ <= len(hits) else -1


def parse_range(expr: str, length: int):
    """'2-4' / '-3' / '5-' / '3' -> (lo, hi) 闭区间，越界自动夹紧。"""
    expr = (expr or "").strip()
    lo, hi = 1, length
    if not expr:
        return lo, hi
    if "-" in expr:
        left, _, right = expr.partition("-")
        left, right = left.strip(), right.strip()
        try:
            lo = int(left) if left else 1
            hi = int(right) if right else length
        except ValueError:
            return 1, length
    else:
        try:
            n = int(expr)
            lo, hi = n, n
        except ValueError:
            return 1, length
    if lo < 1:
        lo = 1
    if hi <= 0 or hi > length:
        hi = length
    if lo > hi:                    # '5-2' 这种手误：交换而不是产出空片段
        lo, hi = hi, lo
    return lo, hi


def select(text: str, mode: str, charset: str = "", occurrence: int = 1) -> str:
    """取出要作用的片段。"""
    if not text:
        return text
    mode = (mode or "text").strip().lower()
    if mode in ("text", ""):
        return text
    if mode == "range":
        lo, hi = parse_range(charset, len(text))
        return text[lo - 1:hi]
    idx = cut_index(text, charset, occurrence)
    if idx < 0:
        return text
    if mode == "after":
        return text[idx + 1:]
    if mode == "before":
        return text[:idx]
    if mode == "from":
        return text[idx:]
    if mode == "until":
        return text[:idx + 1]
    return text


def splice(text: str, mode: str, charset: str, occurrence: int,
           new: str) -> str:
    """把 select 出来的片段替换成 new，分隔符原样保留。"""
    mode = (mode or "text").strip().lower()
    if mode in ("text", ""):
        return new
    if mode == "range":
        lo, hi = parse_range(charset, len(text))
        return text[:lo - 1] + new + text[hi:]
    idx = cut_index(text, charset, occurrence)
    if idx < 0:
        return text
    if mode == "after":
        return text[: idx + 1] + new
    if mode == "before":
        return new + text[idx:]
    if mode == "until":
        return text[: idx + 1] + new
    return text[:idx] + new          # from


def split_half(text: str) -> str:
    """字符留空时的兜底：按长度对半截断（BatchRenamer 的老行为）。"""
    return text[: len(text) // 2]


# --------------------------------------------------------------------------- #
# 变量名归一
# --------------------------------------------------------------------------- #

_CANON = {
    "n": "n", "num": "n", "counter": "n", "#": "n", "序号": "n", "编号": "n",
    "index": "index", "idx": "index", "total": "total", "count": "total",
    "name": "name", "filename": "name", "basename": "name", "stem": "name",
    "原名": "name", "名字": "name",
    "fullname": "fullname", "full_name": "fullname", "filenameext": "fullname",
    "ext": "ext", "extension": "ext", "扩展名": "ext",
    "extdot": "extdot",
    "dir": "dir", "folder": "dir", "parent": "parent", "parentdir": "parent",
    "所在文件夹": "parent",
    "filepath": "filepath", "path": "path", "fullpath": "path",
    "size": "size", "sizebytes": "size", "size_kb": "size_kb", "sizekb": "size_kb",
    "size_h": "size_h", "sizeh": "size_h",
    "mtime": "mtime", "modified": "mtime", "date_modified": "mtime",
    "date": "now", "now": "now",
    "ctime": "ctime", "created": "ctime", "atime": "atime", "accessed": "atime",
    "time": "time", "year": "year", "month": "month", "day": "day",
    "kind": "kind",
}


def canon_key(name: str) -> str:
    raw = (name or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    if low.startswith("file."):          # <file.name> / <File.Ext>
        low = low[5:]
    if low.startswith("_"):
        low = low[1:]
    return _CANON.get(low, low)


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #

_ANGLE_RE = re.compile(r"<([A-Za-z_][A-Za-z0-9_.]*)>")
_PERCENT_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")
_AMP_RE = re.compile(r"&([A-Za-z_][A-Za-z0-9_]*)&")
_BRACE_RE = re.compile(r"\{([^{}]*)\}")
# `#Name#` 取变量 / 单个 `#` 取计数器。必须一次扫完原文，
# 否则变量值里自带的 `#` 会被当成计数器改写（例如 a#b.txt 变成 a03b.txt）。
_HASH_TOKEN_RE = re.compile(r"#([A-Za-z_][A-Za-z0-9_.]*)#")


def expand_escape(text: str) -> str:
    """展开 \\x41、\\x{1F600} 与 \\n\\t\\r。"""
    if not text or "\\" not in text:
        return text
    out = re.sub(r"\\x\{([0-9A-Fa-f]{1,6})\}",
                 lambda m: _chr_hex(m.group(1)), text)
    out = re.sub(r"\\x([0-9A-Fa-f]{2})", lambda m: _chr_hex(m.group(1)), out)
    return out.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")


def _chr_hex(h: str) -> str:
    try:
        return chr(int(h, 16))
    except (ValueError, OverflowError):
        return "\\x" + h


def _seed_of(ctx, num, text: str) -> str:
    """同一项在同一次计划里必须得到同一串随机字符。

    预览与落盘是两次渲染：只要种子只依赖「谁 + 第几号」，两次就一定相同，
    显示出来的名字就是真正会落盘的名字。路径拿不到时退回文本本身。
    """
    who = ""
    if ctx is not None:
        who = getattr(ctx, "path", "") or getattr(ctx, "name", "")
    return "%s|%s|%s" % (who, num, text)


def _pseudo_random(text: str, ctx=None, num=None) -> str:
    def fill(m):
        n = min(int(m.group(1)), 64)
        rnd = random.Random(_seed_of(ctx, num, text) + "|" + m.group(0))
        return "".join(rnd.choice("0123456789") for _ in range(n))

    return re.sub(r"@@(\d+)@@", fill, text)


def _num_text(num, width) -> str:
    try:
        n = int(num) if num is not None else 1
    except (TypeError, ValueError):
        n = 1
    try:
        w = int(width or 0)
    except (TypeError, ValueError):
        w = 0
    return str(n).zfill(w) if w > 0 else str(n)


def apply_num_format(value, spec: str) -> str:
    """{n:03} / {n:1}：冒号后是补零宽度（ReNamer 的格式）。"""
    spec = (spec or "").strip()
    if not spec:
        return str(value)
    m = re.fullmatch(r"0*(\d+)", spec)
    if not m:
        return str(value)
    return str(value).zfill(int(m.group(1)))


def _lookup(ctx, key: str) -> str:
    key = canon_key(key)
    if not key:
        return ""
    if ctx is None:
        return {"n": "1", "index": "1", "total": "1"}.get(key, "")
    try:
        return str(ctx.var(key))
    except Exception:
        return ""


def _find_close(text: str, start: int) -> int:
    """从 start 处的 '(' 找配对的 ')'，允许参数里再嵌括号。"""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _render_dollar(text: str, ctx, num, width) -> str:
    """处理 $约定：$Counter(起,步,宽) / $Date(风格) / $MD5 / $FilePath …"""
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != "$":
            out.append(ch)
            i += 1
            continue
        j = i + 1
        while j < n and (text[j].isalnum() or text[j] in "_"):
            j += 1
        name = text[i + 1:j]
        if not name:
            out.append(ch)
            i += 1
            continue
        args = ""
        if j < n and text[j] == "(":
            close = _find_close(text, j)
            if close > 0:
                args = text[j + 1:close]
                j = close + 1
        low = name.lower()
        if low in ("counter", "count", "index", "n", "num"):
            if args:
                parts = [p.strip() for p in args.split(",")]
                try:
                    start = int(parts[0]) if parts and parts[0] else 1
                    step = int(parts[1]) if len(parts) > 1 and parts[1] else 1
                    w = int(parts[2]) if len(parts) > 2 and parts[2] else (width or 0)
                except ValueError:
                    start, step, w = 1, 1, width or 0
                try:
                    base = int(num if num is not None else 1)
                except (TypeError, ValueError):
                    base = 1
                out.append(_num_text(start + (base - 1) * step, w))
            else:
                out.append(_num_text(num, width))
        elif low in ("date", "datetime"):
            style = args or "yyyy-mm-dd"
            if ctx is not None:
                try:
                    out.append(str(ctx.var("date:" + style)))
                except Exception:
                    out.append(style)
            else:
                out.append(style)
        else:
            out.append(_lookup(ctx, low))
        i = j
    return "".join(out)


def render(text: str, ctx=None, num=None, width=1) -> str:
    """把一个模板渲染成字符串；不含任何变量时原样返回（对普通文本安全）。

    num / width 的优先级：显式传参 > ctx 里的计划值 > 默认 1 / 0。
    ctx 里已有 num 时（规则链总会传），显式传入的 num 优先。
    """
    if text is None:
        return ""
    if num is None and ctx is not None:
        num = getattr(ctx, "num", None)
    if num is None:
        num = 1
    try:
        width = int(width or 0)
    except (TypeError, ValueError):
        width = 0
    if not width and ctx is not None:
        try:
            width = int(getattr(ctx, "width", 0) or 0)
        except (TypeError, ValueError):
            width = 0
    out = str(text)
    # 0) `#Name#` 取变量；整串只有一个 `#` 时当计数器（ReNamer 的老写法）。
    #    必须在任何变量替换之前处理，否则变量值里的 `#` 会被当成计数器改写
    #    （a#b.txt 曾变成 a03b.txt）。
    if "#" in out:
        if out == "#":
            out = _num_text(num, width)
        else:
            out = _HASH_TOKEN_RE.sub(
                lambda m: _lookup(ctx, m.group(1)), out)
    if "@@" in out:
        out = _pseudo_random(out, ctx, num)

    # 1) $Name(args) 与 $Name —— ReNamer 写法
    if "$" in out:
        out = _render_dollar(out, ctx, num, width)

    # 2) <file.name> / %name% / &name& —— 第三种旧写法
    #    先看有没有对应分隔符，普通文件名一次正则都不用跑（渲染是热点）
    if "<" in out:
        out = _ANGLE_RE.sub(lambda m: _lookup(ctx, m.group(1)), out)
    if "%" in out:
        out = _PERCENT_RE.sub(lambda m: _lookup(ctx, m.group(1)), out)
    if "&" in out:
        out = _AMP_RE.sub(lambda m: _lookup(ctx, m.group(1)), out)

    # 3) {变量} / {变量:格式} / {1}
    def brace(m):
        inner = (m.group(1) or "").strip()
        if not inner:
            return m.group(0)
        if ":" in inner:
            head, spec = inner.split(":", 1)
            head, spec = head.strip(), spec.strip()
            key = canon_key(head)
            if key in ("n", "num", "counter"):
                value = num if num is not None else 1
                return apply_num_format(value, spec) if spec else _num_text(value, width)
            if key in ("date", "mtime", "ctime", "atime") and ctx is not None:
                return str(ctx.var("date:" + (spec or "yyyy-mm-dd")))
            val = _lookup(ctx, head)
            return apply_num_format(val, spec) if val.isdigit() else val
        if inner.isdigit():
            return m.group(0)             # {1} 由正则替换负责，这里原样保留
        if canon_key(inner) in ("n", "num", "counter"):
            # 编号：显式传入的 num/width 优先（模板里 {n} 不该被 ctx 的旧值盖掉）
            return _num_text(num, width)
        return _lookup(ctx, inner)

    if "{" in out:
        out = _BRACE_RE.sub(brace, out)
    return out


# --------------------------------------------------------------------------- #
# 正则替换里的命名占位
# --------------------------------------------------------------------------- #

REF_KEYS = ("name", "stem", "fullname", "ext", "extdot", "n", "num", "index",
            "total", "dir", "parent", "size", "size_kb", "size_h", "mtime",
            "ctime", "atime", "year", "month", "day", "time", "now", "path",
            "filepath", "left", "right")


def expand_refs(repl: str, match, ctx=None, num=None, width=1,
                left: str = "", right: str = "") -> str:
    """展开 {1} {2} {name} {n} … 供 regex 规则使用。"""
    if not repl:
        return ""
    out = repl
    if "{@" in out or "{!" in out or "$F" in out:
        return out
    for gid in range(1, 10):
        if "{%d}" % gid in out:
            try:
                val = match.group(gid)
            except (IndexError, re.error):
                val = None
            if val is not None:
                out = out.replace("{%d}" % gid, val)
    for key in REF_KEYS:
        token = "{%s}" % key
        if token not in out:
            continue
        if key == "n":
            val = _num_text(num, width)
        elif ctx is None:
            val = ""
        else:
            val = _lookup(ctx, key)
        out = out.replace(token, val)
    if "{left}" in out:
        out = out.replace("{left}", left)
    if "{right}" in out:
        out = out.replace("{right}", right)
    return out
