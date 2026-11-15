# -*- coding: utf-8 -*-
"""
adapters.extract_adapter —— 界面 ⇄ 文件提取引擎的唯一通道。

与 `engine_adapter.py` 平行、互不 import：
    · 界面只调本模块，不直接碰 `namecat_py.extract`；
    · 本模块不实现任何提取算法，只做两件事：
        1) 把界面表单翻译成引擎的 ExtractOptions / plan 字典；
        2) 把引擎返回的字典翻译成界面要展示的文案与统计；
    · 不含 Qt、不含业务状态机（状态机在 ui/pages/extract.py）。

所有行为以 `namecat_py.extract`（独立 Rust core）为准，不从设计稿反推。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from namecat_py import extract as X

# --------------------------------------------------------------------------- #
# 常量与文案（界面下拉框、状态列）
# --------------------------------------------------------------------------- #

MODES: List[tuple] = [
    ("flatten", "一次到底"),
    ("one_level", "逐级上移"),
]
POLICIES: List[tuple] = [
    ("rename", "自动改名"),
    ("skip", "跳过"),
    ("overwrite", "覆盖（危险）"),
]
DEPTHS: List[tuple] = [
    ("0", "不限深度"),
    ("1", "1 层"), ("2", "2 层"), ("3", "3 层"),
    ("4", "4 层"), ("5", "5 层"), ("6", "6 层"), ("8", "8 层"), ("10", "10 层"),
]

STATUS_TEXT: Dict[str, str] = {
    "pending": "待处理", "ready": "可执行", "renamed": "已改名",
    "conflict": "冲突", "skip": "跳过", "error": "失败",
    "done": "完成", "stopped": "已停止", "dry_run": "模拟",
}
STATUS_TONE: Dict[str, str] = {
    "pending": "muted", "ready": "info", "renamed": "info",
    "conflict": "warn", "skip": "muted", "error": "err",
    "done": "ok", "stopped": "muted", "dry_run": "info",
}
# 与全局状态胶囊色一致（ui/base.py 的 C 令牌）
STATUS_COLOR: Dict[str, str] = {
    "pending": "t2", "ready": "info", "renamed": "info",
    "conflict": "warn", "skip": "t3", "error": "err",
    "done": "ok", "stopped": "t3", "dry_run": "info",
}


def status_text(status: str) -> str:
    return STATUS_TEXT.get(status, status or "")


def status_color(status: str) -> str:
    return STATUS_COLOR.get(status, "t2")


def human_size(n: int) -> str:
    """文件大小的展示文案（引擎给的是字节数）。"""
    n = int(n or 0)
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            return "%.1f %s" % (n / div, unit)
    return "%d B" % n


# --------------------------------------------------------------------------- #
# 表单状态（纯数据，不含 Qt）
# --------------------------------------------------------------------------- #

@dataclass
class ExtractFormState:
    root: str = ""
    mode: str = "flatten"
    conflict: str = "rename"
    max_depth: int = 0
    exclude_dirs: str = ""           # 逗号分隔原文，交给引擎解析
    exclude_exts: str = ""
    clean_empty_dirs: bool = True
    overwrite_confirmed: bool = False

    def to_options(self, program_root: str) -> Dict[str, Any]:
        """表单 → 引擎 ExtractOptions 字典（唯一出处）。"""
        return {
            "root": self.root,
            "program_root": program_root,
            "mode": self.mode,
            "conflict": self.conflict,
            "max_depth": int(self.max_depth),
            "exclude_dirs": X.parse_list(self.exclude_dirs, False),
            "exclude_exts": X.parse_list(self.exclude_exts, True),
            "clean_empty_dirs": bool(self.clean_empty_dirs),
            "overwrite_confirmed": bool(self.overwrite_confirmed),
        }


@dataclass
class ExtractBundle:
    """一次「扫描 + 执行」的结果包。计划是纯 dict，方便 JSON/测试。"""
    options: Dict[str, Any] = field(default_factory=dict)
    plan: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    state: ExtractFormState = field(default_factory=ExtractFormState)

    @property
    def has_plan(self) -> bool:
        return self.plan is not None

    @property
    def items(self) -> List[Dict[str, Any]]:
        """界面清单该显示的东西：执行后优先执行结果，否则计划项。"""
        if self.result is not None:
            return list(self.result.get("items") or [])
        if self.plan is not None:
            return list(self.plan.get("items") or [])
        return []

    def stats(self) -> Dict[str, int]:
        """统计唯一来源（与引擎 plan_stats 同口径，界面不自己数数）。"""
        counts = {k: 0 for k in ("total", "pending", "done", "skipped",
                                  "conflict", "error", "renamed")}
        items = self.items
        counts["total"] = len(items)
        for it in items:
            st = str(it.get("status") or "pending")
            if st in ("pending", "ready", "dry_run"):
                counts["pending"] += 1
            elif st == "done":
                counts["done"] += 1
            elif st in ("skip", "stopped"):
                counts["skipped"] += 1
            elif st == "conflict":
                counts["conflict"] += 1
            elif st == "error":
                counts["error"] += 1
            elif st == "renamed":
                counts["renamed"] += 1
        return counts


# --------------------------------------------------------------------------- #
# 引擎调用（全部同步；后台线程由 workers 负责）
# --------------------------------------------------------------------------- #

def program_root() -> str:
    """程序根目录：源码 = 项目根，EXE = exe 所在目录。由 Rust 侧判定。"""
    return X.program_root_hint()


def rollback_dir() -> str:
    return X.rollback_dir(program_root())


def build_plan(state: ExtractFormState, progress=None) -> ExtractBundle:
    """扫描 + 生成计划（只读文件系统）。"""
    opts = state.to_options(program_root())
    plan = X.build_plan(opts, progress)
    return ExtractBundle(options=opts, plan=plan, state=state)


def execute(bundle: ExtractBundle, dry_run: bool = False,
            task_id: str = "", progress=None) -> ExtractBundle:
    """模拟 / 实际执行。严格复用 bundle.plan，绝不重新扫描或重算。"""
    if bundle.plan is None:
        raise ValueError("还没有生成计划，无法执行")
    result = X.execute(bundle.options, bundle.plan, dry_run, task_id, progress)
    return ExtractBundle(options=bundle.options, plan=bundle.plan,
                         result=result, state=bundle.state)


def stop_task(task_id: str) -> bool:
    return X.stop_task(task_id)


def undo_latest(progress=None) -> Dict[str, Any]:
    return X.undo_latest(program_root(), progress)


def undo_path(log_path: str, progress=None) -> Dict[str, Any]:
    return X.undo_path(log_path, progress)


def list_logs() -> List[Dict[str, Any]]:
    return list(X.list_logs(program_root()) or [])


def read_log(log_path: str) -> Dict[str, Any]:
    return X.read_log(log_path)


def export_csv(path: str, items: List[Dict[str, Any]]) -> None:
    X.export_csv(path, items)


def plan_stats(plan: Dict[str, Any]) -> Dict[str, int]:
    return X.plan_stats(plan)


# --------------------------------------------------------------------------- #
# 展示辅助（纯字符串，不碰 Qt）
# --------------------------------------------------------------------------- #

def relative_dir(path: str, root: str) -> str:
    """把绝对路径缩成相对目标根的文本，失败时原样返回。"""
    if not path or not root:
        return path or ""
    try:
        rel = os.path.relpath(path, root)
        return rel if rel != "." else os.path.basename(root) or root
    except (ValueError, TypeError):
        return path


def group_key(item: Dict[str, Any]) -> str:
    """目录分组的键：目标目录（一次到底时全在根，逐级上移时按父目录分）。"""
    return str(item.get("target_dir") or item.get("source_dir") or "")


def filter_items(items: List[Dict[str, Any]], text: str) -> List[Dict[str, Any]]:
    """文本筛选：文件名 / 来源 / 目标 / 状态 / 说明，大小写不敏感。"""
    q = (text or "").strip().lower()
    if not q:
        return list(items)
    out = []
    for it in items:
        hay = " ".join(str(it.get(k) or "") for k in
                       ("name", "source", "source_dir", "target", "status", "message"))
        if q in hay.lower():
            out.append(it)
    return out


def danger_note(conflict: str) -> str:
    if conflict == "overwrite":
        return "覆盖会替换目标位置的同名文件；执行前会先备份原目标，可在「撤销上次」恢复。"
    return ""
