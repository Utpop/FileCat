# -*- coding: utf-8 -*-
"""
engine.ops —— 高层动作：预览 / 落盘 / 撤销 / 导出。

界面层与命令行都只调这里，保证两条入口的行为完全一致（这也是合并的关键：
BatchRenamer 过去有「文件夹版」与「媒体版」两套流程，容易走岔）。

—— 已换壳（P3）——
实现迁到 Rust（crates/namecat-py/src/py_ops.rs），公开面保持不变（12 个符号）。
编排仍是"扫描 → 算名 → 校验 → 落盘 → 写日志"这条链，只是每一环都换成了
Rust 实现；`apply` 保留"传进来的计划不再重算"这条约束（LLM 规则不重复请求、
randomize 不会出现预览名与落盘名不一致）。
"""

from __future__ import annotations

from namecat_py.ops import *  # noqa: F401,F403
