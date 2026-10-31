# -*- coding: utf-8 -*-
"""
engine.common —— 名字校验、两遍冲突检测、落盘改名、日志与撤销。

这段逻辑照搬 BatchRenamer（core/common.py）的成熟做法并做三点扩展：
    1. 名字校验对「文件夹」与「文件」用同一套规则，但文件夹不需要扩展名保护
    2. 冲突检测的「盘上占用」判断现在能识别「本批次会挪走的路径」，
       因此 A→B、B→C 这种链式改名不会被误判成冲突
    3. 落盘执行支持三级策略：直改、临时名中转、回滚；并记录每一步的成败

纯 IO 模块，不依赖任何界面。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import (APP_NAME, APP_VER, BACKUP_DIR_NAME, LOG_DIR_NAME, LOG_SCHEMA,
               MAX_LOGS, MAX_LOGS_TOTAL, STATUS_TEXT, ST_ERROR, ST_OK, ST_SAME,
               ST_SKIP, ST_CONFLICT, TEMP_PREFIX)
from .rules import INVALID_NAME_CHARS, MAX_NAME_LEN, RESERVED_NAMES
from .scan import nkey, natural_key
from .context import split_ext

# 名字合法性只有一处定义：engine/rules.py（规则与校验共用同一张表）
ILLEGAL_CHARS = INVALID_NAME_CHARS


# --------------------------------------------------------------------------- #
# 名字合法性
# --------------------------------------------------------------------------- #

def name_problem(name: str, is_dir: bool = False) -> str:
    """返回不合法原因；合法返回空串。"""
    if name is None:
        return "名字为空"
    if not name.strip():
        return "名字为空"
    for ch in name:
        if ch in ILLEGAL_CHARS:
            return "含非法字符 %s" % ch
        if ord(ch) < 32:
            return "含控制字符"
    if name in (".", ".."):
        return "不能是 . 或 .."
    if name.endswith(" "):
        return "结尾不能是空格"
    if name.endswith("."):
        return "结尾不能是点"
    if not is_dir:
        stem = name.split(".")[0] if not name.startswith(".") else name[1:].split(".")[0]
        if stem.strip().lower() in RESERVED_NAMES:
            return "是 Windows 保留名"
    else:
        if name.split(".")[0].strip().lower() in RESERVED_NAMES:
            return "是 Windows 保留名"
    if len(name) > MAX_NAME_LEN:
        return "名字过长（%d > %d）" % (len(name), MAX_NAME_LEN)
    return ""


def nameless_problem(new: str, old: str, is_dir: bool = False) -> str:
    """规则把**文件**的名字改没了？（返回原因，空串表示没问题）

    单看新名字是看不出来的：`abc.txt` 被删掉主干后是 `.txt`，
    而 `.txt` 本身在 Windows 上合法（`.gitignore` 这类点文件也合法）。
    所以只能拿原名做对照：新名字恰好只剩**原来的扩展名**时，才算"名字被删空"。
    """
    if is_dir or not new:
        return ""
    _stem, ext = split_ext(old or "")
    if ext and os.path.normcase(new) == os.path.normcase(ext):
        return "名字不能为空（只剩扩展名 %s）" % ext
    return ""


def safe_rel(rel: str) -> str:
    return os.path.normcase(rel or "")


# --------------------------------------------------------------------------- #
# 两遍校验
# --------------------------------------------------------------------------- #

class ValidateReport:
    """校验结果汇总（界面/命令行展示用）。"""

    def __init__(self):
        self.ok = 0
        self.same = 0
        self.conflict = 0
        self.error = 0
        self.skip = 0

    def count(self, item) -> None:
        st = getattr(item, "status", ST_OK)
        if st == ST_OK:
            self.ok += 1
        elif st == ST_SAME:
            self.same += 1
        elif st == ST_CONFLICT:
            self.conflict += 1
        elif st == ST_ERROR:
            self.error += 1
        else:
            self.skip += 1

    def __str__(self) -> str:
        return "可改 %d / 未变 %d / 冲突 %d / 无效 %d / 跳过 %d" % (
            self.ok, self.same, self.conflict, self.error, self.skip)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "same": self.same, "conflict": self.conflict,
                "error": self.error, "skip": self.skip}


def validate_items(items: Iterable, dir_keys: Optional[set] = None,
                   progress: Optional[Callable[[int, int, str], None]] = None
                   ) -> ValidateReport:
    """两遍校验，就地改 item.status / item.note。

    dir_keys：本批次「文件夹」条目的新路径集合（归一化）。
    父子同时改名时，目标路径要由父项落地后才存在，需要提前放行，
    否则会被误判成「目标已存在/所在目录不存在」。
    """
    items = list(items)
    report = ValidateReport()
    # 目录在整批里高度重复：normcase/abspath 与 isdir 都按目录缓存一次，
    # 1500 个文件实测能省掉大半的路径系统调用
    dir_key_cache: Dict[str, str] = {}
    dir_ok_cache: Dict[str, bool] = {}

    def dir_key(d: str) -> str:
        got = dir_key_cache.get(d)
        if got is None:
            got = dir_key_cache[d] = nkey(d)
        return got

    def target_key(it) -> str:
        # 等价于 nkey(os.path.join(it.dirname, it.new_name))，但目录部分走缓存
        return os.path.join(dir_key(it.dirname), os.path.normcase(it.new_name))

    # ---- 第一遍：语法 + 批内重名 ----
    live: List = []
    for idx, it in enumerate(items):
        if it.status == ST_SKIP:
            continue
        it.status = ST_OK
        it.note = ""
        old, new = it.name, it.new_name
        if not new:
            # 规则把名字整个删空（文件夹最常见）：这是"规则产出非法名字"，
            # 不是"规则没生效"。旧实现标成"名称未变"，用户会以为规则没起作用。
            it.new_name = old
            it.status = ST_ERROR
            it.note = "规则把名字删空了"
            continue
        if new == old:
            it.status = ST_SAME
            it.note = "规则未改变名称"
            continue
        prob = name_problem(new, getattr(it, "is_dir", False)) \
            or nameless_problem(new, old, getattr(it, "is_dir", False))
        if prob:
            it.status = ST_ERROR
            it.note = prob
            continue
        live.append(it)

    # 批内重名：按归一化目标路径分组，落在同一目录下才算冲突
    groups: Dict[str, List] = {}
    for it in live:
        key = target_key(it)
        groups.setdefault(key, []).append(it)
    for key, same in groups.items():
        if len(same) <= 1:
            continue
        for it in same:
            it.status = ST_CONFLICT
            it.note = "与本批次其它 %d 项同名" % (len(same) - 1)

    # ---- 第二遍：盘上占用 ----
    leaving = {nkey(it.path) for it in live}
    new_dirs = dir_keys or set()
    for it in live:
        if it.status == ST_CONFLICT:
            continue
        target = os.path.join(it.dirname, it.new_name)
        key = target_key(it)
        own = nkey(it.path)
        if key in leaving and key != own:
            continue                     # 目标就是本批次里另一项，会被腾出来
        if key in new_dirs:
            continue                     # 目标是本批次的文件夹，由执行阶段负责
        exists = os.path.exists(target)
        if exists and key == own:
            continue                     # 只改大小写
        if exists:
            it.status = ST_CONFLICT
            it.note = "目标已存在"
        else:
            okdir = dir_ok_cache.get(it.dirname)
            if okdir is None:
                okdir = dir_ok_cache[it.dirname] = os.path.isdir(it.dirname)
            if not okdir:
                it.status = ST_ERROR
                it.note = "所在目录不存在"

    for it in items:
        report.count(it)
    if progress:
        progress(len(items), len(items), "校验完成")
    return report


# --------------------------------------------------------------------------- #
# 落盘执行
# --------------------------------------------------------------------------- #

@dataclass
class RenameStep:
    item: object
    old: str
    new: str
    ok: bool = False
    error: str = ""


def rename_with_retry(src: str, dst: str, attempts: int = 3) -> None:
    """Windows 上被杀软/索引器短暂占用时重试（沿用 BatchRenamer 做法）。"""
    last = None
    for i in range(attempts):
        try:
            os.rename(src, dst)
            return
        except OSError as exc:
            last = exc
            code = getattr(exc, "winerror", None)
            if code in (5, 32) or getattr(exc, "errno", None) in (13, 16):
                time.sleep(0.15 * (i + 1))
                continue
            raise
    if last:
        raise last


def execute(items: Iterable, progress: Optional[Callable[[int, int, str], None]] = None
            ) -> List[RenameStep]:
    """把校验通过的条目改名到位。

    BatchRenamer 的策略是「只处理链式冲突才走临时名」。这里做得更稳：
    只要某一步的目标当前还被本批次另一项占着，就先自己挪到临时名，
    再统一从临时名落到目标名；任一步失败则把已做的全部回滚。
    文件夹（尤其在父子同时改名时）永远走中转，因为子项路径随着父项变化。
    """
    todo = [it for it in items if getattr(it, "status", ST_OK) == ST_OK
            and getattr(it, "new_name", "") and it.changed]
    steps: List[RenameStep] = [RenameStep(it, it.path, os.path.join(it.dirname, it.new_name))
                               for it in todo]
    if not steps:
        return steps

    # 文件夹一律走中转：父项改名会让子项路径失效，中转能把这两件事解耦。
    # 文件只有在目标被本批次另一项占着时才需要中转。
    sources = {nkey(it.path) for it in todo}
    needs_chain = any(
        it.is_dir or (nkey(os.path.join(it.dirname, it.new_name)) in sources
                      and nkey(os.path.join(it.dirname, it.new_name)) != nkey(it.path))
        for it in todo)

    if not needs_chain:
        # 排成「最深的先改」，避免父目录先改名后子项路径失效
        steps.sort(key=lambda s: -s.old.count(os.sep))
        done: List[RenameStep] = []
        for i, step in enumerate(steps):
            if progress:
                progress(i + 1, len(steps), "正在重命名 %s" % os.path.basename(step.old))
            try:
                rename_with_retry(step.old, step.new)
                step.ok = True
            except OSError as exc:
                step.error = str(exc)
                for back in reversed(done):
                    try:
                        rename_with_retry(back.new, back.old)
                        back.ok = False
                        back.error = "已回滚"
                    except OSError:
                        back.error = "回滚失败：%s" % back.new
                return steps
            done.append(step)
        if progress:
            progress(len(steps), len(steps), "完成")
        return steps

    # ---- 中转方案：按「所在目录从深到浅」一组一组处理 ----
    # 同一目录内的项先全部挪到临时名（此时目录本身还没动，路径都有效），
    # 再从临时名落到目标名，然后把临时名并入目标名的前缀映射，
    # 于是父目录稍后改名时，子项已知的临时路径会自动跟着修正。
    ordered = sorted(steps, key=lambda s: -os.path.dirname(s.old).count(os.sep))
    dir_map: List[Tuple[str, str]] = []      # (旧前缀, 新前缀)，长的在前
    done: List[RenameStep] = []
    total = len(steps) * 2
    cur = 0

    def resolve(path: str) -> str:
        for old_pref, new_pref in dir_map:
            np, op = nkey(path), nkey(old_pref)
            if np == op:
                return new_pref
            if np.startswith(op + os.sep):
                return new_pref + path[len(old_pref):]
        return path

    groups: List[Tuple[str, List[RenameStep]]] = []
    for step in ordered:
        key = nkey(os.path.dirname(step.old))
        if groups and groups[-1][0] == key:
            groups[-1][1].append(step)
        else:
            groups.append((key, [step]))

    for _key, group in groups:
        moved: List[Tuple[RenameStep, str, str]] = []   # (步骤, 中转前的真实路径, 临时名)
        for step in group:
            src = resolve(step.old)
            tmp = os.path.join(os.path.dirname(src),
                               TEMP_PREFIX + uuid.uuid4().hex[:8])
            cur += 1
            if progress:
                progress(cur, total, "临时移动 %s" % os.path.basename(src))
            try:
                rename_with_retry(src, tmp)
            except OSError as exc:
                step.error = str(exc)
                _rollback_temps(moved)
                return steps
            moved.append((step, src, tmp))

        final: List[Tuple[RenameStep, str, str]] = []
        for step, src, tmp in moved:
            dst = resolve(step.new)
            cur += 1
            if progress:
                progress(cur, total, "落位 %s" % os.path.basename(dst))
            try:
                rename_with_retry(tmp, dst)
            except OSError as exc:
                step.error = str(exc)
                # 已落位的先退回临时名，再让 _rollback_temps 把它们连同还没落位的
                # 一起挪回**原名**。旧实现把已落位的从回滚列表里过滤掉了，于是这些
                # 文件永远停在 __nk_tmp_xxxx 上（内容在、名字没了），却报告"已回滚"。
                for s2, t2, d2 in reversed(final):
                    try:
                        if os.path.exists(d2):
                            rename_with_retry(d2, t2)
                    except OSError:
                        s2.error = "回滚失败：%s" % d2
                _rollback_temps(moved)
                for s2, _t2, _d2 in final:
                    s2.ok = False
                    s2.error = s2.error or "已回滚"
                return steps
            # 中转链路里真实落位的是 src（可能已随父目录改名）与 dst，
            # 记进 step 才能让日志/撤销用对路径
            step.old, step.new = src, dst
            final.append((step, tmp, dst))
            if step.item.is_dir:
                dir_map.append((tmp, dst))
                dir_map.sort(key=lambda p: -len(p[0]))
            step.ok = True
            done.append(step)

    if progress:
        progress(total, total, "完成")
    return steps


def _rollback_temps(moved) -> None:
    """把临时名挪回原名。

    moved 的元素是 (RenameStep, 中转前的真实路径, 临时名) 或旧式
    (RenameStep, 临时名)，两种都认，避免调用点各自转换。

    失败项的 error 里已经有真正的原因（哪个文件被占用/没权限），
    别用"已回滚"盖掉它 —— 那会让用户彻底查不到为什么失败。
    """
    for entry in reversed(list(moved)):
        step, src, tmp = (entry if len(entry) == 3
                          else (entry[0], entry[0].old, entry[1]))
        try:
            if os.path.exists(tmp):
                rename_with_retry(tmp, src)
                step.ok = False
                if not step.error:
                    step.error = "已回滚"
        except OSError:
            step.error = "回滚失败：%s" % tmp


def find_temp_leftovers(root: str, max_depth: int = 8) -> List[str]:
    """找出上次崩溃遗留的临时名文件（BatchRenamer 的抢救功能）。

    这些名字里没有原始文件名的任何线索（`__nk_tmp_` + 随机串），
    所以只能"找出来给人看"，不能自动还原 —— 界面/命令行都只负责列清单。
    """
    found: List[str] = []
    if not os.path.isdir(root):
        return found
    root = os.path.abspath(root)
    skip_dirs = {name.lower() for name in (LOG_DIR_NAME, BACKUP_DIR_NAME)}
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.count(os.sep) - root.count(os.sep) >= max_depth:
            dirnames[:] = []
        # 与 scan.LOG_DIR_SKIP 同一套口径：Windows 目录名大小写不敏感，
        # 直接比对 "Name_backup" 会漏掉 "name_backup"。
        dirnames[:] = [d for d in dirnames if d.lower() not in skip_dirs]
        for name in dirnames + filenames:
            if name.startswith(TEMP_PREFIX):
                found.append(os.path.join(dirpath, name))
    found.sort()
    return found


# --------------------------------------------------------------------------- #
# 日志与撤销
# --------------------------------------------------------------------------- #
#
# 日志统一放在**程序目录**下的 `Name_backup` 文件夹里（不再塞进被改名的
# 目标目录 —— 那样会在用户的数据目录里到处留垃圾）。
#
# 同一个文件夹里混着各个目标目录的日志，靠两样东西区分：
#   · 文件名里的 8 位目录指纹（`_root_hash`）—— 列表与清理都靠它，不用读 JSON；
#   · 日志 JSON 里的 `root` 字段 —— 界面上显示"这是哪个目录的记录"。
# 旧版本写在 `<目标目录>/_rename_logs/` 里的日志仍然认（只读兼容），
# 这样升级不会让老用户丢掉可撤销的机会。

def _root_hash(root: str) -> str:
    """目标目录的 8 位指纹（Windows 下大小写不敏感）。"""
    key = nkey(root or "")
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:8]


def _log_name(root: str, stamp: str) -> str:
    """日志文件名：指纹在前，时间戳在后 —— 排序按时间戳走。

    段数刻意比旧格式多一段（5 段），这样新旧两种文件名绝不会互相误认：
        新： rename_<8位指纹>_<日期>_<时分秒>_<随机>.json
        旧： rename_<日期>_<时分秒>_<随机>.json
    """
    return "rename_%s_%s.json" % (_root_hash(root), stamp)


def _split_log_name(name: str) -> Tuple[str, str]:
    """拆出 (目录指纹, 排序键)。不是本程序写的日志返回 ("", "")。

    指纹必须是 8 位十六进制 —— 旧格式（`rename_<日期>_<时分秒>_<随机>.json`）
    段数对不上，会被判成"认不出来"，免得被误分到某个目录下再被清理掉。
    """
    if not (name.startswith("rename_") and name.endswith(".json")):
        return ("", "")
    parts = name[:-len(".json")].split("_")
    if len(parts) != 5:
        return ("", "")
    tag, date, clock = parts[1], parts[2], parts[3]
    if len(tag) != 8 or any(c not in "0123456789abcdef" for c in tag.lower()):
        return ("", "")
    if len(date) != 8 or not date.isdigit():
        return ("", "")
    if len(clock) != 6 or not clock.isdigit():
        return ("", "")
    return (tag, date + clock)


def log_dir(root: str = "") -> str:
    """撤销日志的存放目录（统一的一个，跟 root 无关）。"""
    from . import backup_dir
    return backup_dir()


def legacy_log_dir(root: str) -> str:
    """旧版本把日志放在目标目录下的 `_rename_logs`（只读兼容用）。"""
    return os.path.join(os.path.abspath(root or ""), LOG_DIR_NAME)


def save_log(root: str, pairs: List[Tuple[str, str]], kind: str = "mixed",
             summary: Optional[dict] = None, options: Optional[dict] = None,
             rules: Optional[list] = None, note: str = "") -> str:
    """写撤销日志。pairs 是 [(旧绝对路径, 新绝对路径)]。"""
    directory = log_dir(root)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return ""
    # 时间戳只到微秒，同一微秒内两次落盘会互相覆盖 —— 加随机尾码兜底
    stamp = "%s_%s" % (time.strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:6])
    path = os.path.join(directory, _log_name(root, stamp))
    data = {
        "app": APP_NAME,
        "version": APP_VER,
        "schema": LOG_SCHEMA,
        "kind": kind,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "root": os.path.abspath(root),
        "summary": summary or {},
        "options": options or {},
        "rules": rules or [],
        "note": note,
        "pairs": [{"old": o, "new": n} for o, n in pairs],
    }
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError:
        return ""
    _trim_logs(directory)
    return path


def _legacy_sort_key(name: str) -> str:
    """旧格式日志（`rename_<日期>_<时分秒>_<随机>.json`）的时间排序键。"""
    if not (name.startswith("rename_") and name.endswith(".json")):
        return ""
    parts = name[:-len(".json")].split("_")
    if len(parts) != 4:
        return ""
    return parts[1] + parts[2] if parts[1].isdigit() and parts[2].isdigit() else ""


def _trim_logs(directory: str, keep: int = MAX_LOGS,
               keep_total: int = MAX_LOGS_TOTAL) -> None:
    """清理旧日志。

    同一个文件夹里混着各个目标目录的日志，所以**按目录**各留 keep 份，
    再对总量压一道上限 —— 否则 A 目录的日志会把 B 目录的挤掉。
    认不出来的文件（不是本程序写的）一律不动。
    """
    try:
        files = sorted(f for f in os.listdir(directory)
                       if f.startswith("rename_") and f.endswith(".json"))
    except OSError:
        return
    by_root: Dict[str, List[Tuple[str, str]]] = {}
    known: List[str] = []
    for name in files:
        tag, order = _split_log_name(name)
        if not tag:
            continue                        # 认不出来的（老格式/别的程序）不参与清理
        by_root.setdefault(tag, []).append((order, name))
        known.append(name)
    drop = set()
    for _tag, items in by_root.items():
        items.sort()
        for _order, name in items[:-keep] if len(items) > keep else []:
            drop.add(name)
    # 总量兜底：超出上限时按时间从旧到新删
    alive = [n for n in known if n not in drop]
    if len(alive) > keep_total:
        def order_of(name):
            _tag, order = _split_log_name(name)
            return order or name
        for name in sorted(alive, key=order_of)[:len(alive) - keep_total]:
            drop.add(name)
    for name in drop:
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            pass


def list_logs(root: str = "") -> List[str]:
    """列出撤销日志（按时间从旧到新排）。

    root 给了 = 只列那个目标目录的；root 空 = 列出全部（各目录混在一起）。
    旧版本留在 `<目标目录>/_rename_logs/` 里的日志也一并认，
    这样升级不会让老用户丢掉可撤销的机会。
    """
    picked: List[Tuple[str, str]] = []
    directory = log_dir(root)
    tag = _root_hash(root) if root else ""
    try:
        files = os.listdir(directory)
    except OSError:
        files = []
    for name in files:
        if not (name.startswith("rename_") and name.endswith(".json")):
            continue
        ftag, order = _split_log_name(name)
        if tag and ftag != tag:
            continue
        picked.append((order or _legacy_sort_key(name) or name,
                       os.path.join(directory, name)))

    if root:
        legacy = legacy_log_dir(root)
        try:
            for name in os.listdir(legacy):
                if name.startswith("rename_") and name.endswith(".json"):
                    picked.append((_legacy_sort_key(name) or name,
                                   os.path.join(legacy, name)))
        except OSError:
            pass
    picked.sort()
    return [path for _order, path in picked]


def load_log(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def log_pairs(data: dict) -> List[Tuple[str, str]]:
    """从日志里取 [(旧路径, 新路径)]。

    日志文件是用户可编辑的普通 JSON，结构可能被改坏（数组、字符串、
    甚至 `"pairs": [1, 2]`）。这里一律按"能读的读、读不了的跳过"处理 ——
    一份坏日志不该让整份记录（乃至整个「执行记录」页）都读不出来。
    """
    out: List[Tuple[str, str]] = []
    if not isinstance(data, dict):
        return out
    items = data.get("pairs")
    if not isinstance(items, list):
        return out
    for p in items:
        if not isinstance(p, dict):
            continue
        old, new = p.get("old"), p.get("new")
        if isinstance(old, str) and isinstance(new, str) and old and new:
            out.append((old, new))
    return out


def _rewrite_log(path: str, data: dict, pairs: List[Tuple[str, str]],
                 missing: List[str]) -> None:
    """把"还没还原成功的项"写回日志，其余字段原样保留。

    这样重复点撤销时只会重试剩下的那几项，而不会把已经还原过的
    条目又算成"找不到"（旧实现因此永远删不掉日志，每次都报"未完成"）。
    """
    payload = dict(data)
    payload["pairs"] = [{"old": o, "new": n} for o, n in pairs]
    note = "仍有 %d 项未还原" % len(pairs)
    if missing:
        note += "（%d 项原文件已不存在，已跳过）" % len(missing)
    payload["note"] = note
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def undo(log_path: str, progress: Optional[Callable[[int, int, str], None]] = None
         ) -> Tuple[bool, List[str]]:
    """按日志逆序还原。绝不覆盖已存在的文件（沿用 BatchRenamer 的保守策略）。

    幂等：还原成功的条目不会再参与下一次撤销。
    还有没还原成的项时，把**剩下的项**写回日志（便于人工处理 / 下次重试）；
    全部还原完（或只剩"原文件已不存在"）时删掉日志。
    """
    data = load_log(log_path)
    pairs = log_pairs(data)
    # 日志里的路径记的是「当时所在的父目录」：父目录改名之前，子项记的是
    # 老父目录下的路径（d1\d2\X_d3）。所以还原必须**父项先还原**——父项
    # 回到原名之后，子项日志里那条路径才重新成立；反过来先还原子项，
    # 子项的目标路径还不存在，只能靠 os.makedirs 造出一个同名空目录，
    # 父项随后还原时就撞名失败（多层嵌套 a/b/c 同时改名时必然踩到）。
    # 深度升序 = 先父后子；同深度按自然序保证结果稳定。
    ordered = sorted(pairs, key=lambda p: (p[1].count(os.sep),
                                           natural_key(os.path.basename(p[1]))))
    errors: List[str] = []
    missing: List[str] = []
    pending: List[Tuple[str, str]] = []
    total = len(ordered)
    for i, (old, new) in enumerate(ordered):
        if progress:
            progress(i + 1, total, "还原 %s" % os.path.basename(new))
        if not os.path.exists(new):
            # 目标已经不在了（用户删了/挪了）：没什么可还原的，跳过即可。
            # 这不算失败，否则"撤销过一次"就会让日志永远删不掉。
            missing.append(new)
            continue
        if os.path.exists(old):
            errors.append("已存在，跳过：%s" % old)
            pending.append((old, new))
            continue
        try:
            os.makedirs(os.path.dirname(old), exist_ok=True)
            rename_with_retry(new, old)
        except OSError as exc:
            errors.append("%s -> %s：%s" % (new, old, exc))
            pending.append((old, new))
    if pending:
        _rewrite_log(log_path, data, pending, missing)
        return False, errors
    # 该还原的都还原了（其余是"原文件已不存在"）：日志没有继续存在的意义
    try:
        os.remove(log_path)
    except OSError:
        pass
    return True, []
