# -*- coding: utf-8 -*-
"""
engine.ops —— 高层动作：预览 / 落盘 / 撤销 / 导出。

界面层与命令行都只调这里，保证两条入口的行为完全一致（这也是合并的关键：
BatchRenamer 过去有「文件夹版」与「媒体版」两套流程，容易走岔）。
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from . import APP_NAME, APP_VER, STATUS_TEXT, ST_OK
from . import common as C
from . import plan as P
from . import rules as R
from .scan import KIND_DIR, KIND_TEXT, scan, ScanOptions


@dataclass
class Result:
    plan: object = None
    steps: List = field(default_factory=list)
    log_path: str = ""
    message: str = ""
    # 「这一步整体上算不算成功」。撤销没有 step 列表，只有 per-entry 的错误清单，
    # 所以成败必须由调用方显式给出——否则 steps 为空时 ok 恒为 True，
    # 撤销明明一项都没还原却被当成成功（`main.py undo` 因此错误地返回 0）。
    success: bool = True

    @property
    def ok(self) -> bool:
        steps_ok = all(s.ok for s in self.steps) if self.steps else True
        return self.success and steps_ok

    @property
    def failed(self) -> List:
        return [s for s in self.steps if not s.ok]


def preview(items, rules=(), opts: Optional[P.PlanOptions] = None,
            progress=None, client=None) -> P.Plan:
    """只算不改（默认行为，与 BatchRenamer 一致）。"""
    return P.build(items, rules, opts, progress, client)


def apply(items, rules=(), opts: Optional[P.PlanOptions] = None,
          root: str = "", kind: str = "mixed", progress=None,
          client=None, plan=None) -> Result:
    """落盘并写撤销日志。dry_run=True 时只预览。

    plan 可以直接传入已经算好的计划——命令行先用 scan_and_plan 预览、
    再调本函数落盘，传进来就不必重算。重算会带来两个真实缺陷：
    LLM 规则被请求两次（多花钱、可能给出不同名字），randomize 规则
    两次结果不同（预览显示的名字和真正落盘的名字不一致）。
    """
    opts = opts or P.PlanOptions()
    if plan is None:
        plan = P.build(items, rules, opts, progress, client)

    if opts.dry_run:
        return Result(plan=plan, message="预览：%s" % plan.summary())

    todo = plan.changing()
    if not todo:
        return Result(plan=plan, message="没有需要改名的项")

    steps = C.execute(todo, progress)
    done = [s for s in steps if s.ok]
    log_path = ""
    if done:
        base = root or _common_base([s.item.dirname for s in done])
        log_path = C.save_log(
            base, [(s.old, s.new) for s in done], kind=kind,
            summary=plan.report.as_dict() if plan.report else {},
            options=opts.as_dict(), rules=R.rules_to_json(rules))
    ok_n = len(done)
    bad = len(steps) - ok_n
    msg = "已重命名 %d 项" % ok_n
    if bad:
        msg += "，失败 %d 项（已回滚）" % bad
    if ok_n and not log_path:
        # 日志写不进去时绝不能只报"成功"：这批改名将无法撤销，
        # 而执行前的确认框明确承诺了"会写撤销日志"。
        msg += "；但撤销日志没能写入（日志目录不可写），本次改名无法撤销"
    return Result(plan=plan, steps=steps, log_path=log_path, message=msg)


def _common_base(dirs: Sequence[str]) -> str:
    """取共同的父目录当日志根。跨盘（commonpath 会抛 ValueError）时退到首项。"""
    dirs = [d for d in dirs if d]
    if not dirs:
        return os.getcwd()
    try:
        return os.path.commonpath(dirs)
    except (ValueError, OSError):
        return dirs[0]


def undo(log_path: str, progress=None) -> Result:
    ok, errors = C.undo(log_path, progress)
    msg = "撤销完成" if ok else "撤销有 %d 处未完成" % len(errors)
    if errors:
        msg += "；" + "；".join(errors)
    # success 必须显式给：撤销没有 step 列表，ok 不能靠 steps 推
    return Result(log_path=log_path, message=msg, success=ok)


def export_csv(plan, path: str) -> str:
    """导出预览结果。列与 BatchRenamer 的导出保持兼容并做了叠加。"""
    rows = []
    for i, it in enumerate(plan.items, 1):
        rows.append([
            i,
            KIND_TEXT.get(getattr(it, "kind", ""), ""),
            os.path.dirname(it.path),
            it.name,
            it.new_name or "",
            # BatchRenamer 导出的是中文状态（it.status_text），保持一致
            STATUS_TEXT.get(it.status, it.status or ""),
            it.note or "",
            getattr(getattr(it, "st", None), "st_size", "") if not it.is_dir else "",
        ])
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["序号", "类型", "所在文件夹", "原名称", "新名称",
                    "状态", "说明", "大小(字节)"])
        w.writerows(rows)
    return path


def scan_and_plan(root: str, rules=(), scan_opts: Optional[ScanOptions] = None,
                  plan_opts: Optional[P.PlanOptions] = None, progress=None,
                  client=None) -> P.Plan:
    """一次调用跑完 扫描 -> 排序编号 -> 算名 -> 校验。"""
    opts = scan_opts or ScanOptions(root=root)
    res = scan(opts, progress)
    return P.build(res.items, rules, plan_opts, progress, client)
