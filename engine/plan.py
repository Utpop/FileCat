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

—— 已换壳（P2）——
实现迁到 Rust（crates/namecat-core/src/plan.rs + namecat-py/src/py_plan.rs），
公开面保持不变（19 个符号）。两处仍然回调 Python：
    · LLM 规则 → `engine.llm_resolve`（P4 才迁）。
落盘前的两遍校验自 P3 起直接在 Rust 侧调用（不再绕回 Python 的
`engine.common.validate_items`）。
`_common_root` 是私有函数但被 verify/test_edges.py 直接调用，显式 import。
"""

from __future__ import annotations

from namecat_py.plan import *            # noqa: F401,F403
from namecat_py.plan import _common_root  # noqa: F401  test_edges 直接用
