# -*- coding: utf-8 -*-
"""
engine.scan —— 扫描、过滤、排序、成链。

合并三处几乎重复的扫描逻辑：
    ReNamer 的「文件 + 文件夹 + 递归 + 只读/隐藏/系统过滤 + 自然序」
    BatchRenamer.folders 的「子文件夹 + only/exclude 文本过滤 + 按名/时间/大小排序」
    BatchRenamer.media 的「图片/视频扩展名 + 文件头校验 + 多种排序 + 每个子目录重置」

统一成一条 Options：想只看图/视频就设 kinds，想只看文件夹就设 kinds=("dir",)，
想混合处理就都开。排序键从 3 档扩展到 8 档（吸收 media.SORT_MODES）。

本模块只做 IO 扫描，不做改名计算（见 engine.plan）。
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .context import Item

# --------------------------------------------------------------------------- #
# 类型常量
# --------------------------------------------------------------------------- #

KIND_FILE = "file"
KIND_DIR = "dir"
KIND_IMAGE = "image"
KIND_VIDEO = "video"
ALL_KINDS = (KIND_FILE, KIND_DIR, KIND_IMAGE, KIND_VIDEO)
KIND_TEXT = {KIND_FILE: "文件", KIND_DIR: "文件夹", KIND_IMAGE: "图片", KIND_VIDEO: "视频"}

# 覆盖常见位图 / 相机 RAW（沿用 BatchRenamer 的清单，补了几个新容器）
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".bmp", ".webp",
    ".tif", ".tiff", ".heic", ".heif", ".avif", ".ico", ".svg", ".tga",
    ".dds", ".psd", ".jxl",
    ".raw", ".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2",
    ".raf", ".pef", ".sr2", ".srw",
}
VIDEO_EXTS = {
    ".mp4", ".m4v", ".mov", ".qt", ".avi", ".mkv", ".webm", ".wmv", ".asf",
    ".flv", ".f4v", ".mpg", ".mpeg", ".mpe", ".m2v", ".ts", ".m2ts", ".mts",
    ".3gp", ".3g2", ".vob", ".rm", ".rmvb", ".ogv", ".divx", ".mxf", ".dv",
}

# 文件头特征：仅用于「校验文件内容」。未列出的扩展名一律放行。
MAGIC = {
    ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",),
    ".jpe": (b"\xff\xd8\xff",), ".jfif": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".bmp": (b"BM",),
    ".tif": (b"II*\x00", b"MM\x00*"), ".tiff": (b"II*\x00", b"MM\x00*"),
    ".ico": (b"\x00\x00\x01\x00",),
    ".psd": (b"8BPS",),
    ".dds": (b"DDS ",),
    ".webp": None, ".heic": None, ".heif": None, ".avif": None,
    ".mp4": None, ".m4v": None, ".mov": None, ".qt": None,
    ".3gp": None, ".3g2": None, ".f4v": None, ".avi": None,
    ".mkv": (b"\x1a\x45\xdf\xa3",), ".webm": (b"\x1a\x45\xdf\xa3",),
    ".wmv": (b"0&\xb2u\x8ef\xcf\x11",), ".asf": (b"0&\xb2u\x8ef\xcf\x11",),
    ".flv": (b"FLV",),
    ".mpg": (b"\x00\x00\x01\xba",), ".mpeg": (b"\x00\x00\x01\xba",),
    ".mpe": (b"\x00\x00\x01\xba",), ".m2v": (b"\x00\x00\x01\xba",),
    ".vob": (b"\x00\x00\x01\xba",),
    ".ts": (b"\x47",), ".m2ts": (b"\x47",), ".mts": (b"\x47",),
    ".rm": (b".RMF",), ".rmvb": (b".RMF",),
    ".ogv": (b"OggS",),
}

SORT_MODES: List[Tuple[str, str]] = [
    ("name_asc", "名称（自然序）"),
    ("name_desc", "名称（倒序）"),
    ("mtime_asc", "修改时间（旧 → 新）"),
    ("mtime_desc", "修改时间（新 → 旧）"),
    ("ctime_asc", "创建时间（旧 → 新）"),
    ("ctime_desc", "创建时间（新 → 旧）"),
    ("size_asc", "大小（小 → 大）"),
    ("size_desc", "大小（大 → 小）"),
    ("path_asc", "路径（先目录后名称）"),
    ("none", "不改顺序（按扫描得到的顺序）"),
]
SORT_KEYS = [k for k, _t in SORT_MODES]
SORT_TEXT = dict(SORT_MODES)

MAX_DEPTH = 12
LOG_DIR_NAMES = ("_rename_logs",)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

_NUM_RE = re.compile(r"(\d+)")


def natural_key(text: str) -> List[Tuple[int, int, str]]:
    """自然序键：img2 排在 img10 前。"""
    key: List[Tuple[int, int, str]] = []
    for part in _NUM_RE.split(text or ""):
        if part.isdigit():
            key.append((1, int(part), ""))
        elif part:
            key.append((0, 0, part.lower()))
    return key


def nkey(path: str) -> str:
    """路径归一化键：Windows 大小写不敏感。"""
    return os.path.normcase(os.path.abspath(path))


def is_hidden_name(name: str) -> bool:
    return name.startswith(".")


def is_hidden_attrs(st) -> bool:
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & 0x2 or attrs & 0x4)


def kind_of(name: str, is_dir: bool,
            image_exts: Optional[set] = None,
            video_exts: Optional[set] = None) -> str:
    if is_dir:
        return KIND_DIR
    low = os.path.splitext(name)[1].lower()
    if low in (image_exts if image_exts is not None else IMAGE_EXTS):
        return KIND_IMAGE
    if low in (video_exts if video_exts is not None else VIDEO_EXTS):
        return KIND_VIDEO
    return KIND_FILE


def check_file_magic(path: str, ext: str) -> bool:
    """校验文件头。无法判断时一律放行，避免误伤 PSD/RAW 等未收录格式。"""
    ext = ext.lower()
    if ext not in MAGIC:
        return True
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return True
    if not head:
        return False
    spec = MAGIC[ext]
    if spec is None:
        if ext == ".webp":
            return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
        if ext == ".avi":
            return head[:4] == b"RIFF" and head[8:12] == b"AVI "
        return head[4:8] == b"ftyp"
    return any(head.startswith(sig) for sig in spec)


# --------------------------------------------------------------------------- #
# 扫描选项
# --------------------------------------------------------------------------- #

@dataclass
class ScanOptions:
    root: str = ""
    kinds: Tuple[str, ...] = (KIND_FILE,)      # 含 file / dir / image / video
    recursive: bool = False
    include_hidden: bool = False
    only_match: str = ""                        # 名字含该文本才处理
    exclude_match: str = ""                     # 名字含该文本就跳过
    include_exts: Optional[set] = None           # 只收这些扩展名（小写含点），None=不限
    exclude_exts: Optional[set] = None
    verify_content: bool = False                 # 校验文件头
    sort_mode: str = "name_asc"
    max_depth: int = MAX_DEPTH
    follow_log_dirs: bool = False                 # 是否把 _rename_logs 也当对象处理
    selected: Optional[set] = None                # 勾选的绝对路径集合；None=全部

    def wanted_kind(self, name: str, is_dir: bool) -> Optional[str]:
        k = kind_of(name, is_dir)
        if not self.kinds:
            return k
        if k in self.kinds:
            return k
        # 选「文件」时图片/视频也算文件；选图片/视频时不额外收普通文件
        if k in (KIND_IMAGE, KIND_VIDEO) and KIND_FILE in self.kinds:
            return k
        return None


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #

@dataclass
class ScanResult:
    items: List[Item] = field(default_factory=list)
    skipped: int = 0               # 因文件头校验被排除的数量
    dirs: int = 0
    files: int = 0

    def __len__(self) -> int:
        return len(self.items)

    @property
    def total(self) -> int:
        return len(self.items)


def scan(opts: ScanOptions,
         progress: Optional[Callable[[str], None]] = None) -> ScanResult:
    """扫描目标目录，按 kind 过滤并排序，返回 Item 列表。

    过滤顺序刻意安排成「廉价在前」：纯字符串（名字/扩展名）-> stat -> 读文件头。

    `opts.selected` 非空时语义是「只要这些条目」（界面的「扫描对象」与拖放都靠它）：
    - 选中的目录代表它**里面的整棵子树**（目录自身不算，只有其内容）；
    - 位于 root 之下但更深的选中项，会自动下探到它们所在目录——否则关掉
      「包含子文件夹」时，从子目录里拖进来的条目会被静默丢掉；
    - 不在 root 之下的选中项（例如跨盘符拖入）会各自作为扫描起点，不会被忽略；
    - 一旦有选中项在 root 之外，「只要这些」会放宽成「root 里全部 + 这些」，
      因为这时用户的意图明显是「往已有列表里再加几个」，而不是「只要这几个」。
    """
    root = os.path.abspath(opts.root) if opts.root else ""
    res = ScanResult()
    root_key = nkey(root) if root else ""
    selected_keys = set(opts.selected) if opts.selected else None

    def under(path_key: str, base_key: str) -> bool:
        return bool(base_key) and (path_key == base_key
                                   or path_key.startswith(base_key + os.sep))

    # 扫描起点 = root + 不在 root 之下的选中项（跨盘符拖入也能扫到）
    roots: List[str] = []
    if root and os.path.isdir(root):
        roots.append(root)
    if selected_keys:
        for path in sorted(selected_keys):
            if root_key and under(nkey(path), root_key):
                continue                 # root 之内，交给下探逻辑处理
            base = path if os.path.isdir(path) else os.path.dirname(path)
            if base and os.path.isdir(base):
                roots.append(base)
    if not roots:
        return res

    # 选中的条目里只要有一个在 root 之外（例如从别的文件夹拖进来），
    # 语义就从「只要这些」变成「root 全都要 + 额外这些」：否则往已扫出的
    # 列表里拖一个外部文件，会连原来的内容一起消失。
    mixed_scope = bool(selected_keys and root_key
                       and any(not under(key, root_key) for key in selected_keys))

    def hit(path: str) -> bool:
        """选中判定：命中自身，或命中某个被选中目录的子树。"""
        key = nkey(path)
        if key in selected_keys:
            return not os.path.isdir(key)      # 选中的目录只代表内容，不含自身
        parent = os.path.dirname(key)
        while parent:
            if parent in selected_keys:
                return True
            nxt = os.path.dirname(parent)
            if nxt == parent:
                return False
            parent = nxt
        return False

    def take(path: str, name: str, is_dir: bool) -> None:
        low = os.path.splitext(name)[1].lower() if not is_dir else ""
        if opts.include_exts is not None and not is_dir and low not in opts.include_exts:
            return
        if opts.exclude_exts and low in opts.exclude_exts:
            return
        if opts.only_match and opts.only_match not in name:
            return
        if opts.exclude_match and opts.exclude_match in name:
            return
        kind = opts.wanted_kind(name, is_dir)
        if kind is None:
            return
        if not opts.include_hidden and is_hidden_name(name):
            return
        try:
            st = os.stat(path)
        except OSError:
            return
        if is_dir and not stat.S_ISDIR(st.st_mode):
            return
        if not is_dir and not stat.S_ISREG(st.st_mode):
            return
        if not opts.include_hidden and is_hidden_attrs(st):
            return
        if opts.verify_content and not is_dir and not check_file_magic(path, low):
            res.skipped += 1
            return
        if selected_keys is not None and not hit(path):
            if not (mixed_scope and under(nkey(path), root_key)):
                return
        # 带上已经取得的 stat，后续取变量不必重复 stat
        it = Item(path=path, name=name, is_dir=is_dir, st=st, kind=kind)
        res.items.append(it)
        if is_dir:
            res.dirs += 1
        else:
            res.files += 1

    def in_selected_tree(dir_key: str) -> bool:
        """这个目录是不是「选中目录里的东西」（含选中目录自身）？"""
        if not selected_keys:
            return False
        for key in selected_keys:
            if key == dir_key or under(dir_key, key):
                return True
        return False

    def has_selected_below(dir_key: str) -> bool:
        """这个目录下面（更深处）还有选中项吗？有就必须下探去够它。"""
        if not selected_keys:
            return False
        for key in selected_keys:
            if key != dir_key and under(key, dir_key):
                return True
        return False

    def walk_dir(dir_path: str, depth: int, deep: bool = False) -> None:
        """deep=True 表示身处「选中的子树/起点」内，此时不受递归开关与深度上限约束。"""
        try:
            entries = list(os.scandir(dir_path))
        except OSError:
            return
        if progress:
            try:
                progress(dir_path)
            except TypeError:
                progress(0, 0, "扫描 %s" % dir_path)
        dirs_kept = []
        for ent in entries:
            try:
                is_dir = ent.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                low = ent.name.lower()
                if low in LOG_DIR_NAMES and not opts.follow_log_dirs:
                    continue
                dirs_kept.append(ent)
            take(ent.path, ent.name, is_dir)
        dirs_kept.sort(key=lambda e: natural_key(e.name))
        for ent in dirs_kept:
            child_key = nkey(ent.path)
            child_deep = deep or in_selected_tree(child_key)
            # 普通目录受「包含子文件夹」与深度上限约束；但选中项本身、
            # 选中项的子目录、以及通向选中项的祖先目录必须继续下探，
            # 否则关掉递归时用户明确拖进来的东西会被静默丢掉。
            if (opts.recursive and depth < opts.max_depth) or child_deep \
                    or has_selected_below(child_key):
                walk_dir(ent.path, depth + 1, child_deep)

    started: List[str] = []
    for start in roots:
        start_key = nkey(start)
        if any(under(start_key, nkey(seen)) for seen in started):
            continue                     # 已经包含在更上层的起点里
        started.append(start)
        # 从 root 出发时递归与否听用户的；从「root 之外的选中目录」出发时，
        # 那是用户明确拖进来的完整子树，一律扫全。
        walk_dir(start, 0, deep=not (root_key and under(start_key, root_key)))

    sort_items(res.items, opts.sort_mode)
    return res


def sort_items(items: List[Item], mode: str) -> None:
    """原地排序。先排次级键（名称）再排主键，稳定排序保证可复现。"""
    mode = mode or "name_asc"
    if mode == "none":
        return
    reverse = mode.endswith("_desc")
    base = mode.rsplit("_", 1)[0]
    items.sort(key=lambda it: natural_key(it.name))
    if base == "name":
        items.sort(key=lambda it: natural_key(it.name), reverse=reverse)
    elif base == "mtime":
        items.sort(key=lambda it: _st_val(it, "st_mtime"), reverse=reverse)
    elif base == "ctime":
        items.sort(key=lambda it: _st_val(it, "st_ctime"), reverse=reverse)
    elif base == "size":
        items.sort(key=lambda it: _st_val(it, "st_size"), reverse=reverse)
    elif base == "path":
        items.sort(key=lambda it: (natural_key(os.path.dirname(it.path)),
                                   natural_key(it.name)), reverse=reverse)


def _st_val(it, attr: str):
    st = getattr(it, "st", None)
    if not st:
        try:
            st = os.stat(it.path)
            it.st = st
        except OSError:
            return 0
    return getattr(st, attr, 0)
