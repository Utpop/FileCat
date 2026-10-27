# -*- coding: utf-8 -*-
"""
engine.context —— 命名上下文（供模板变量使用）。

合并两边之后暴露的变量是两边变量表的并集：
    ReNamer 侧：#、$Counter、$Date、Date、$MD5、$SHA1、$FilePath、$FilePath1 等
    BatchRenamer 侧：{n} {name} {ext} {index} {total} {size} {mtime} {year} …

变量命名一律小写、下划线分隔；$Counter 与 # 等价，都是「本组编号」。
template.py 负责把两种写法都映射到这里。
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime


def split_ext(name: str):
    """拆成 (主干, 扩展名含点)。点开头的文件（.gitignore）视为无扩展名。

    全项目只此一处实现，rules/template/context 都复用它，
    保证「大小写扩展名原样保留」这一条在各层一致。
    """
    if not name or name.startswith(".") or "." not in name:
        return name, ""
    stem, ext = name.rsplit(".", 1)
    return stem, "." + ext


def fmt_date(ts: float, style: str) -> str:
    """按 ReNamer 的日期风格输出；未知风格回退到 yyyy-mm-dd。"""
    try:
        dt = datetime.fromtimestamp(ts)
    except (OSError, ValueError, OverflowError, TypeError):
        dt = datetime.now()
    s = (style or "yyyy-mm-dd").strip()
    table = [
        ("yyyy", "%Y"), ("yy", "%y"), ("mmmm", "%B"), ("mmm", "%b"),
        ("mm", "%m"), ("dd", "%d"), ("hh", "%H"), ("nn", "%M"), ("ss", "%S"),
    ]
    out = s
    for key, fmt in table:
        val = dt.strftime(fmt)
        out = out.replace(key, val)
        if key.islower():
            out = out.replace(key.upper(), val.upper())
    return out


def human_size(num: int) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return ("%d %s" % (size, unit)) if unit == "B" else ("%.1f %s" % (size, unit))
        size /= 1024.0
    return "%d B" % num


class NameContext:
    """一个条目的变量表：只读、缓存一次。"""

    __slots__ = ("path", "name", "is_dir", "index", "total", "num", "width",
                 "_st", "_vars", "_extra", "_digests", "_split", "_dirname")

    def __init__(self, path, name=None, is_dir=None, index=1, total=1, num=None,
                 width=1, st=None, extra=None):
        self.path = path
        self.name = name if name is not None else os.path.basename(path)
        self.is_dir = os.path.isdir(path) if is_dir is None else is_dir
        self.index = index
        self.total = total
        self.num = index if num is None else num
        self.width = width
        self._st = st
        self._vars = None
        self._extra = extra or {}
        self._digests = {}
        self._split = None
        self._dirname = None

    # ---- 基本属性 ----
    @property
    def stat(self):
        if self._st is None:
            try:
                self._st = os.stat(self.path)
            except OSError:
                self._st = False
        return self._st or None

    @property
    def split(self):
        """(主干, 扩展名) 缓存：模板一次渲染会多次问到同一对值。"""
        if self._split is None:
            self._split = (self.name, "") if self.is_dir else split_ext(self.name)
        return self._split

    @property
    def ext(self) -> str:
        """扩展名（含点，保留原大小写）；文件夹无扩展名。

        与大写扩展名保持一致：`.JPG` 原样返回，不做小写化
        （BatchRenamer 的 PlanStats 依赖这一点）。
        """
        return self.split[1]

    @property
    def e(self) -> str:
        return self.ext.lstrip(".")

    @property
    def stem(self) -> str:
        return self.split[0]

    @property
    def dirname(self) -> str:
        if self._dirname is None:
            self._dirname = os.path.dirname(self.path)
        return self._dirname

    @property
    def kind(self) -> str:
        """file / image / video / dir；以真实文件系统为准，不信任外部传值。"""
        if self.is_dir:
            return "dir"
        from .scan import kind_of
        return kind_of(self.name, False)

    @property
    def extra(self) -> dict:
        """批量阶段预存的附加数据（LLM 精简结果等）。"""
        return self._extra

    @extra.setter
    def extra(self, value) -> None:
        self._extra = dict(value or {})

    @property
    def new_path_name(self) -> str:
        return self.name

    def counter(self) -> str:
        n = self.num if self.num is not None else self.index
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 1
        w = int(self.width or 0)
        return str(n).zfill(w) if w > 0 else str(n)

    # ---- 变量表 ----
    # 常用键直接算，绕开整张变量表（表里 10 次 strftime 是渲染时的热点）。
    _FAST = {
        "n": "counter", "num": "counter",
        "index": "index", "total": "total",
        "name": "stem", "stem": "stem",
        "fullname": "name", "filename": "name",
        "ext": "e", "extension": "e", "extdot": "ext",
        "path": "path", "fullpath": "path",
        "parentdir": "parentdir", "filepath": "parentdir",
    }

    def variables(self) -> dict:
        if self._vars is not None:
            return self._vars
        st = self.stat
        size = getattr(st, "st_size", 0) if st else 0
        mtime = getattr(st, "st_mtime", 0.0) if st else 0.0
        ctime = getattr(st, "st_ctime", 0.0) if st else 0.0
        atime = getattr(st, "st_atime", 0.0) if st else 0.0
        parent = self.dirname
        base = os.path.basename(parent) or parent
        v = {
            "n": self.counter(),
            "num": self.counter(),
            "index": str(self.index),
            "total": str(self.total),
            "name": self.stem,
            "stem": self.stem,
            "fullname": self.name,
            "ext": self.e,
            "extdot": self.ext,
            "dir": base,
            "parent": base,
            "parentdir": parent,
            "filepath": parent,
            "path": self.path,
            "fullpath": self.path,
            "size": str(size),
            "size_kb": str(size // 1024),
            "size_h": human_size(size),
            "mtime": fmt_date(mtime, "yyyy-mm-dd"),
            "ctime": fmt_date(ctime, "yyyy-mm-dd"),
            "atime": fmt_date(atime, "yyyy-mm-dd"),
            "year": fmt_date(mtime, "yyyy"),
            "month": fmt_date(mtime, "mm"),
            "day": fmt_date(mtime, "dd"),
            "time": fmt_date(mtime, "hh-nn-ss"),
            "now": datetime.now().strftime("%Y-%m-%d"),
            "date": datetime.now().strftime("%Y%m%d"),
            "kind": self.kind,
        }
        self._vars = v
        return v

    def var(self, key: str) -> str:
        if key is None:
            return ""
        low = str(key).strip().strip("{}$").lower()
        if not low:
            return ""
        fast = self._FAST.get(low)
        if fast is not None:
            if fast == "counter":
                return self.counter()
            if fast == "index":
                return str(self.index)
            if fast == "total":
                return str(self.total)
            return str(getattr(self, fast))
        if low.startswith("date:"):
            return fmt_date(getattr(self.stat, "st_mtime", 0.0) or 0.0, low[5:])
        if low in ("md5", "sha1", "sha256", "crc32"):
            return self.digest(low)
        if self._vars is not None:
            return self._vars.get(low, "")
        return self.variables().get(low, "")

    def digest(self, algo: str) -> str:
        """文件校验值；同一上下文里同一算法只读一次文件（hash 规则常用）。"""
        algo = (algo or "").lower()
        if algo in self._digests:
            return self._digests[algo]
        val = self._digest_uncached(algo)
        self._digests[algo] = val
        return val

    def _digest_uncached(self, algo: str) -> str:
        if algo == "crc32":
            import zlib
            crc = 0
            try:
                with open(self.path, "rb") as fh:
                    while True:
                        block = fh.read(1 << 20)
                        if not block:
                            break
                        crc = zlib.crc32(block, crc)
            except OSError:
                return ""
            return "%08x" % (crc & 0xFFFFFFFF)
        try:
            h = hashlib.new(algo)
        except (ValueError, TypeError):
            return ""
        try:
            with open(self.path, "rb") as fh:
                while True:
                    block = fh.read(1 << 20)
                    if not block:
                        break
                    h.update(block)
        except OSError:
            return ""
        return h.hexdigest()


class Item:
    """engine.plan / engine.scan 使用的条目：文件元数据 + 命名上下文。"""

    __slots__ = ("path", "name", "is_dir", "index", "total", "num", "width",
                 "_ctx", "st", "kind", "new_name", "status", "note", "_dir",
                 "__dict__")

    def __init__(self, path, name=None, is_dir=None, index=1, total=1, num=None,
                 width=1, st=None, kind=None):
        self.path = path
        self.name = name if name is not None else os.path.basename(path)
        self.is_dir = os.path.isdir(path) if is_dir is None else is_dir
        self.index = index
        self.total = total
        self.num = index if num is None else num
        self.width = width
        self._ctx = None
        self.st = st
        self.kind = kind or ""
        self.new_name = ""
        self.status = "ok"
        self.note = ""
        self._dir = None

    # 让引擎内部可以随手挂临时字段（例如 stat 缓存）
    def _ctx_obj(self) -> NameContext:
        if self._ctx is None:
            self._ctx = NameContext(self.path, self.name, self.is_dir,
                                    self.index, self.total, self.num, self.width,
                                    st=self.st)
        else:
            self._ctx.index = self.index
            self._ctx.total = self.total
            self._ctx.num = self.num
            self._ctx.width = self.width
        return self._ctx

    @property
    def ext(self) -> str:
        return self._ctx_obj().ext

    @property
    def stem(self) -> str:
        return self._ctx_obj().stem

    @property
    def dirname(self) -> str:
        if self._dir is None:
            self._dir = os.path.dirname(self.path)
        return self._dir

    @property
    def new_path(self) -> str:
        """改名后的完整路径（父目录若也改名，这里跟着走）。"""
        if not self.new_name:
            return self.path
        new_dir = getattr(self, "_new_dir", "")
        return os.path.join(new_dir or self.dirname, self.new_name)

    @property
    def changed(self) -> bool:
        return bool(self.new_name) and self.new_name != self.name

    def variables(self):
        return self._ctx_obj().variables()

    def var(self, key):
        return self._ctx_obj().var(key)

    def digest(self, algo):
        return self._ctx_obj().digest(algo)
