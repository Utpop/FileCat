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

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import (APP_NAME, APP_VER, LOG_DIR_NAME, LOG_SCHEMA, MAX_LOGS,
               STATUS_TEXT, ST_ERROR, ST_OK, ST_SAME, ST_SKIP, ST_CONFLICT,
               TEMP_PREFIX)
from .rules import INVALID_NAME_CHARS, MAX_NAME_LEN, RESERVED_NAMES
from .scan import nkey, natural_key

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
            it.new_name = old
            it.status = ST_SAME
            it.note = "规则未改变名称"
            continue
        if new == old:
            it.status = ST_SAME
            it.note = "规则未改变名称"
            continue
        prob = name_problem(new, getattr(it, "is_dir", False))
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
                # 先撤本组已落位的，再把本组还没落位的临时名撤回原名
                for s2, t2, d2 in reversed(final):
                    try:
                        if os.path.exists(d2):
                            rename_with_retry(d2, t2)
                    except OSError:
                        s2.error = "回滚失败：%s" % d2
                landed = {id(f[0]) for f in final}
                _rollback_temps([p for p in moved if id(p[0]) not in landed])
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
    """把还没落位的临时名挪回原名。

    moved 的元素是 (RenameStep, 中转前的真实路径, 临时名) 或旧式
    (RenameStep, 临时名)，两种都认，避免调用点各自转换。
    """
    for entry in reversed(list(moved)):
        step, src, tmp = (entry if len(entry) == 3
                          else (entry[0], entry[0].old, entry[1]))
        try:
            if os.path.exists(tmp):
                rename_with_retry(tmp, src)
                step.ok = False
                step.error = "已回滚"
        except OSError:
            step.error = "回滚失败：%s" % tmp


def find_temp_leftovers(root: str, max_depth: int = 8) -> List[str]:
    """找出上次崩溃遗留的临时名文件（BatchRenamer 的抢救功能）。"""
    found: List[str] = []
    if not os.path.isdir(root):
        return found
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.count(os.sep) - root.count(os.sep) >= max_depth:
            dirnames[:] = []
        if LOG_DIR_NAME in dirnames:
            dirnames.remove(LOG_DIR_NAME)
        for name in dirnames + filenames:
            if name.startswith(TEMP_PREFIX):
                found.append(os.path.join(dirpath, name))
    found.sort()
    return found


# --------------------------------------------------------------------------- #
# 日志与撤销
# --------------------------------------------------------------------------- #

def log_dir(root: str) -> str:
    return os.path.join(os.path.abspath(root), LOG_DIR_NAME)


def save_log(root: str, pairs: List[Tuple[str, str]], kind: str = "mixed",
             summary: Optional[dict] = None, options: Optional[dict] = None,
             rules: Optional[list] = None, note: str = "") -> str:
    """写撤销日志。pairs 是 [(旧绝对路径, 新绝对路径)]。"""
    directory = log_dir(root)
    os.makedirs(directory, exist_ok=True)
    # 时间戳只到微秒，同一微秒内两次落盘会互相覆盖 —— 加随机尾码兜底
    stamp = "%s_%s" % (time.strftime("%Y%m%d_%H%M%S"),
                       uuid.uuid4().hex[:6])
    path = os.path.join(directory, "rename_%s.json" % stamp)
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
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    _trim_logs(directory)
    return path


def _trim_logs(directory: str, keep: int = MAX_LOGS) -> None:
    try:
        files = sorted(f for f in os.listdir(directory)
                       if f.startswith("rename_") and f.endswith(".json"))
    except OSError:
        return
    for name in files[:-keep] if len(files) > keep else []:
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            pass


def list_logs(root: str) -> List[str]:
    directory = log_dir(root)
    if not os.path.isdir(directory):
        return []
    files = [os.path.join(directory, f) for f in os.listdir(directory)
             if f.startswith("rename_") and f.endswith(".json")]
    files.sort()
    return files


def load_log(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def log_pairs(data: dict) -> List[Tuple[str, str]]:
    out = []
    for p in data.get("pairs") or []:
        old, new = p.get("old"), p.get("new")
        if old and new:
            out.append((old, new))
    return out


def undo(log_path: str, progress: Optional[Callable[[int, int, str], None]] = None
         ) -> Tuple[bool, List[str]]:
    """按日志逆序还原。绝不覆盖已存在的文件（沿用 BatchRenamer 的保守策略）。

    任一步失败都保留日志，便于人工处理——宁可少删一次日志，也不丢信息。
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
    total = len(ordered)
    for i, (old, new) in enumerate(ordered):
        if progress:
            progress(i + 1, total, "还原 %s" % os.path.basename(new))
        if not os.path.exists(new):
            errors.append("找不到：%s" % new)
            continue
        if os.path.exists(old):
            errors.append("已存在，跳过：%s" % old)
            continue
        try:
            os.makedirs(os.path.dirname(old), exist_ok=True)
            rename_with_retry(new, old)
        except OSError as exc:
            errors.append("%s -> %s：%s" % (new, old, exc))
    if not errors:
        try:
            os.remove(log_path)
        except OSError:
            pass
    return (not errors), errors
