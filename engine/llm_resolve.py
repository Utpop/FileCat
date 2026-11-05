# -*- coding: utf-8 -*-
"""
engine.llm_resolve —— LLM 规则的"先批量问一次模型"阶段。

从 `engine/plan.py` 里搬出来的（P2 换壳时）：plan 的计算已经在 Rust，
但这一步要发网络请求、要读用户可编辑的要求文件，属于 P4 的范围，
暂时留成一行 Python 桥，由 Rust 侧在链里出现 llm 规则时回调。

约定（与 Rust 侧的调用一一对应）：
    resolve(items, ctxs, chain, opts, client, notes) -> notes
        items  : Item 列表
        ctxs   : 与 items 一一对应的 NameContext
        chain  : 已归一化的规则列表
        opts   : PlanOptions
        client : LLM 客户端或 None（None 时自己建一个）
        notes  : 已有提示，返回值是"追加后"的新列表

只在这个阶段碰网络；规则变换本身仍是纯函数（只读 ctx.extra 里的缓存）。
没有可用客户端时不会失败：规则原样放行，并在提示里说清原因。
"""

from __future__ import annotations

from . import llm as LLM
from . import rules as R
from . import template as tpl
from .context import split_ext


def resolve(items, ctxs, chain, opts, client, notes):
    notes = list(notes or [])
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
