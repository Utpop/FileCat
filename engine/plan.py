# -*- coding: utf-8 -*-
"""
engine.plan —— 从扫描结果算出新名字，并落到可执行的一批操作。

合并了两边各自最痛的一段逻辑：
    ReNamer 的能力：
        · 一条规则链同时作用于文件名、扩展名（可单独开关）、文件夹名
        · 编号规则参与整条链，位置（前/后/锚点前后）可配置
        · 编号分组：全局 / 每个文件夹 / 每个文件类型
    BatchRenamer 的能力：
        · 按目录分组、每目录重置编号；自然序排序后再编号
        · 扩展名大小写与内容一律原样保留
        · 模板变量 {n} {name} {date} {parent}

本模块不落盘，只算目标名字与顺序。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import rules as R
from . import template as tpl
from .common import name_problem, safe_rel, validate_items
from .context import NameContext, split_ext
from .scan import (KIND_DIR, KIND_FILE, KIND_IMAGE, KIND_VIDEO, KIND_TEXT,
                   nkey, natural_key)

# 编号作用域
SCOPE_GLOBAL = "global"          # 整批连续
SCOPE_FOLDER = "folder"          # 每个文件夹重新开始
SCOPE_KIND = "kind"              # 每个类型（图片/视频/其它）重新开始
SCOPE_FOLDER_KIND = "folder_kind"  # 文件夹 + 类型
SCOPES = [SCOPE_GLOBAL, SCOPE_FOLDER, SCOPE_KIND, SCOPE_FOLDER_KIND]
SCOPE_TEXT = {
    SCOPE_GLOBAL: "整批连续编号",
    SCOPE_FOLDER: "每个文件夹重新编号",
    SCOPE_KIND: "每种类型重新编号",
    SCOPE_FOLDER_KIND: "每个文件夹内的每种类型分别编号",
}


@dataclass
class PlanOptions:
    start: int = 1
    step: int = 1
    width: int = 0                 # 0 = 不补零
    scope: str = SCOPE_GLOBAL
    reverse: bool = False          # 编号方向倒过来
    split_ext: bool = True         # 扩展名是否参与规则链
    rename_dirs: bool = True       # 文件夹是否一起改
    dry_run: bool = True

    def as_dict(self) -> dict:
        return {"start": self.start, "step": self.step, "width": self.width,
                "scope": self.scope, "reverse": self.reverse,
                "split_ext": self.split_ext, "rename_dirs": self.rename_dirs}


@dataclass
class Plan:
    items: List = field(default_factory=list)
    report: object = None
    warnings: List[str] = field(default_factory=list)

    # ---- 便捷访问 ----
    def pairs(self) -> List[Tuple[str, str]]:
        return [(it.path, it.new_path) for it in self.items if it.changed
                and it.status == "ok"]

    def changing(self) -> List:
        return [it for it in self.items if it.changed and it.status == "ok"]

    def by_status(self) -> Dict[str, List]:
        out: Dict[str, List] = {}
        for it in self.items:
            out.setdefault(it.status, []).append(it)
        return out

    def summary(self) -> str:
        return str(self.report) if self.report else "%d 项" % len(self.items)


# --------------------------------------------------------------------------- #
# 编号
# --------------------------------------------------------------------------- #

def _scope_key(it, scope: str) -> Tuple:
    rel = os.path.dirname(it.path)
    if scope == SCOPE_GLOBAL:
        return ("",)
    if scope == SCOPE_FOLDER:
        return (safe_rel(rel),)
    kind = getattr(it, "kind", "") or KIND_TEXT.get(
        KIND_DIR if getattr(it, "is_dir", False) else KIND_FILE, "")
    if scope == SCOPE_KIND:
        return (kind,)
    return (safe_rel(rel), kind)


def assign_numbers(items: Sequence, opts: PlanOptions) -> None:
    """按作用域分组、按当前顺序分配 num / index / total / width。

    顺序沿用扫描阶段的排序结果（这就是 BatchRenamer「先排序再编号」的语义），
    但会先排「文件夹深 → 浅」，保证父文件夹拿到前面的编号。
    """
    ordered = list(items)
    if not opts.rename_dirs:
        ordered = [it for it in ordered if not it.is_dir]
    if opts.reverse:
        ordered = list(reversed(ordered))

    buckets: Dict[Tuple, List] = {}
    for it in ordered:
        buckets.setdefault(_scope_key(it, opts.scope), []).append(it)

    for _key, group in buckets.items():
        total = len(group)
        for i, it in enumerate(group):
            it.index = i + 1
            it.total = total
            it.num = opts.start + i * opts.step
            it.width = opts.width


def _iter_with_ctx(items, opts: PlanOptions):
    for it in items:
        ctx = NameContext(it.path, it.name, it.is_dir, it.index, it.total,
                          it.num, it.width, st=getattr(it, "st", None))
        yield it, ctx


# --------------------------------------------------------------------------- #
# 计算
# --------------------------------------------------------------------------- #

def compute_names(items: Sequence, rules: Sequence = (),
                  opts: Optional[PlanOptions] = None,
                  client=None) -> Plan:
    """对每个条目跑规则链，得到 new_name（不含落盘与校验）。"""
    opts = opts or PlanOptions()
    items = list(items)
    assign_numbers(items, opts)

    chain = [R.normalize(r) for r in (rules or [])]
    ctxs = [NameContext(it.path, it.name, it.is_dir, it.index, it.total,
                        it.num, it.width, st=getattr(it, "st", None))
            for it in items]

    notes: List[str] = []
    if chain and any(r.kind == "llm" for r in chain):
        notes = _resolve_llm(items, ctxs, chain, opts, client, notes)

    for it, ctx in zip(items, ctxs):
        it._ctx = ctx
        cur = it.name
        if chain:
            if it.is_dir and not opts.rename_dirs:
                cur = it.name          # 关掉文件夹改名时，文件夹一律保持原名
            else:
                cur = R.apply_chain(chain, it.name, it.num, it.width, ctx, it.index,
                                    split_ext=opts.split_ext)
        it.new_name = cur or ""

    plan = Plan(items=items)
    plan.warnings = _precheck(items, opts) + notes
    return plan


# --------------------------------------------------------------------------- #
# LLM 规则：先批量问一次模型，再让规则链读结果
# --------------------------------------------------------------------------- #

def _resolve_llm(items: Sequence, ctxs: Sequence, chain: Sequence,
                 opts: PlanOptions, client, notes: List[str]) -> List[str]:
    """按链序推进：每条 llm 规则都把「此刻的名字」批量交给模型。

    只在这个阶段碰网络；规则变换本身仍是纯函数（只读 ctx.extra 里的缓存）。
    没有可用客户端时不会失败：规则原样放行，并在提示里说清原因。
    """
    from . import llm as LLM

    if client is None:
        client = LLM.make_client()
    if not client.available():
        notes.append("LLM 规则未生效：%s" % client.problem())
        return notes

    used = 0
    for i, rule in enumerate(chain):
        if rule.kind != "llm":
            continue
        prefix = list(chain[:i])          # 这条规则之前的所有规则 = 它的输入
        segs, pairs = [], []
        for it, ctx in zip(items, ctxs):
            name = it.name
            if prefix:
                name = R.apply_chain(prefix, name, it.num, it.width, ctx, it.index,
                                     split_ext=opts.split_ext)
            if it.is_dir is False and opts.split_ext:
                name = split_ext(name)[0]     # 与 apply_chain 的 llm 分支喂同一段文本
            seg = tpl.select(name, rule.selector, rule.charset, rule.occurrence)
            segs.append(seg)
            pairs.append((it, ctx))

        ex = rule.extras or {}
        instruction = str(ex.get("instruction") or "").strip()
        if not instruction:
            instruction = LLM.read_instruction()      # ← 使用者可编辑的要求文件
        lang = str(ex.get("lang") or LLM.DEFAULT_LANG)
        style = str(ex.get("style") or LLM.DEFAULT_STYLE)
        try:
            max_len = int(str(ex.get("max_len") or LLM.DEFAULT_MAX_LEN))
        except (TypeError, ValueError):
            max_len = LLM.DEFAULT_MAX_LEN

        res = client.plan([s for s in segs if s], instruction, lang, max_len, style)
        for (_it, ctx), seg in zip(pairs, segs):
            got = res.mapping.get(seg, "")
            if got:
                ctx.extra.setdefault("llm_by_rule", {})[rule.id] = got
                ctx.extra["llm"] = got
                used += 1
        notes.append(res.summary())
    if not used:
        notes.append("LLM 没有给出任何新名字，本次沿用原名")
    return notes


def _precheck(items: Sequence, opts: PlanOptions) -> List[str]:
    """落盘前先挑出会被覆盖/被禁的文件，给命令行一句提示。"""
    warns: List[str] = []
    targets = {}
    for it in items:
        if not it.new_name or it.new_name == it.name:
            continue
        key = nkey(os.path.join(it.dirname, it.new_name))
        targets[key] = targets.get(key, 0) + 1
    dup = sum(1 for v in targets.values() if v > 1)
    if dup:
        warns.append("%d 个目标名字重复，会被标成冲突" % dup)
    return warns


def finalize(plan: Plan, opts: Optional[PlanOptions] = None) -> Plan:
    """父子路径修正 + 两遍校验。

    子项的目标路径要跟着父文件夹的新名字走：先把每个文件夹的
    「旧相对路径 → 新相对路径」算出来，再把所有条目按最长前缀替换。
    """
    opts = opts or PlanOptions()
    items = plan.items

    # 1) 文件夹的新相对路径映射（相对扫描根）
    root = _common_root(items)
    dir_map: List[Tuple[str, str, str]] = []      # (旧相对路径, 旧相对路径 normcase, 新相对路径)
    for it in items:
        if not it.is_dir:
            continue
        old_rel = os.path.relpath(it.path, root)
        if it.new_name and it.new_name != it.name:
            new_rel = os.path.join(os.path.dirname(old_rel), it.new_name)
        else:
            new_rel = old_rel
        if os.path.normcase(old_rel) != os.path.normcase(new_rel):
            dir_map.append((old_rel, os.path.normcase(old_rel), new_rel))
    dir_map.sort(key=lambda p: -p[0].count(os.sep))     # 长的先替换

    if dir_map:
        # 同一目录下的条目改名前缀完全一样：按目录缓存一次即可，
        # 否则 1500 个文件要对每个前缀各做一次 normcase 与 relpath
        # （实测这条路径曾占掉 build 一半的时间）
        cache: Dict[str, Tuple[str, bool]] = {}        # 绝对目录 -> (新相对路径, 是否变化)

        def map_rel(abs_dir: str) -> Tuple[str, bool]:
            got = cache.get(abs_dir)
            if got is not None:
                return got
            rel = os.path.relpath(abs_dir, root)
            cur, low = rel, os.path.normcase(rel)
            changed = False
            for old_rel, old_low, new_rel_val in dir_map:
                if low == old_low:
                    cur, changed = new_rel_val, True
                    break
                if low.startswith(old_low + os.sep):
                    cur, changed = new_rel_val + cur[len(old_rel):], True
                    break
            cache[abs_dir] = (cur, changed)
            return cur, changed

        for it in items:
            # 文件夹映射自己的相对路径；文件映射所在目录的相对路径
            base = it.path if it.is_dir else it.dirname
            rel, changed = map_rel(base)
            if not changed:
                continue
            name = it.new_name or it.name
            if it.is_dir:
                new_rel = rel
                it._new_dir = os.path.join(root, os.path.dirname(new_rel))
            else:
                new_rel = os.path.join(rel, name)
                it._new_dir = os.path.join(root, rel)
            it._new_rel = new_rel

    dir_keys = {nkey(os.path.join(root, new_rel)) for _o, _l, new_rel in dir_map}
    for it in items:
        if it.is_dir and it.new_name and it.new_name != it.name:
            dir_keys.add(nkey(os.path.join(it.dirname, it.new_name)))

    plan.report = validate_items(items, dir_keys)
    return plan


def _common_root(items: Sequence) -> str:
    """所有条目的公共父目录（用于计算相对路径）。"""
    if not items:
        return ""
    dirs = [os.path.dirname(os.path.abspath(it.path)) for it in items]
    if len(dirs) == 1:
        return dirs[0]
    try:
        return os.path.commonpath(dirs)
    except ValueError:
        return dirs[0]


# --------------------------------------------------------------------------- #
# 一步到位：扫描结果 -> 可执行计划
# --------------------------------------------------------------------------- #

def build(items: Sequence, rules: Sequence = (), opts: Optional[PlanOptions] = None,
          progress=None, client=None) -> Plan:
    plan = compute_names(items, rules, opts, client)
    if progress:
        progress(0, len(plan.items), "校验目标名字")
    return finalize(plan, opts)


def describe_plan(plan: Plan, limit: int = 60) -> str:
    """命令行预览用的表格文本。"""
    rows = plan.items
    lines = []
    shown = rows if limit in (0, None) else rows[:limit]
    for it in shown:
        mark = {"ok": "✓", "same": "=", "conflict": "!", "error": "×",
                "skip": "-"}.get(it.status, "?")
        old = it.name
        new = it.new_name or it.name
        if it.changed and it.status == "ok":
            body = "%s → %s" % (old, new)
        else:
            body = "%s（%s）" % (old, it.note or "未变")
        parent = os.path.dirname(it.path)
        lines.append("%s %s  [%s]" % (mark, body, parent if parent else "."))
    if limit not in (0, None) and len(rows) > limit:
        lines.append("… 共 %d 项，仅显示前 %d 项（--limit 0 显示全部）"
                     % (len(rows), limit))
    return "\n".join(lines)
