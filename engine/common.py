# -*- coding: utf-8 -*-
"""
engine.common —— 名字校验、两遍冲突检测、落盘改名、日志与撤销。

这段逻辑照搬 BatchRenamer（core/common.py）的成熟做法并做三点扩展：
    1. 名字校验对「文件夹」与「文件」用同一套规则，但文件夹不需要扩展名保护
    2. 冲突检测的「盘上占用」判断现在能识别「本批次会挪走的路径」，
       因此 A→B、B→C 这种链式改名不会被误判成冲突
    3. 落盘执行支持三级策略：直改、临时名中转、回滚；并记录每一步的成败

纯 IO 模块，不依赖任何界面。

—— 已换壳（P3）——
实现迁到 Rust（crates/namecat-core/src/{names,validate,rename,logs}.rs +
namecat-py/src/py_common.rs），公开面保持不变（34 个符号）。
`_root_hash` / `_split_log_name` 是私有函数但被 verify/test_edges.py 直接调用，
显式 import。日志目录本身仍问 `engine.backup_dir()`（环境变量覆盖 + 可写性
探测那套留在根包里，不重复实现）。
"""

from __future__ import annotations

from namecat_py.common import *                  # noqa: F401,F403
from namecat_py.common import (_root_hash,       # noqa: F401  test_edges 直接用
                               _split_log_name)
