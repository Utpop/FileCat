# -*- coding: utf-8 -*-
"""
engine.template —— 模板渲染与两种旧写法兼容。

变量语法统一成 `{变量}`，同时认出两边的旧写法：
    ReNamer:          $Counter / $Date(yyyy-mm-dd) / $MD5 / $FilePath / <file.name> / &name& / #
    BatchRenamer:     #Name# / %name% / #name# / {n} / {date} / {parent}

另外补齐 ReNamer 的细节能力：
    {n:03}            补零宽度
    @@1000000@@       伪随机数字
    \\x41 / \\x{1F600}   转义

—— 已换壳（P1）——
实现迁到 Rust（crates/namecat-core/src/template.rs），公开面保持不变：
cut_index / parse_range / select / splice / split_half / canon_key /
expand_escape / render / apply_num_format / expand_refs 与四个常量。
变量来源通过 namecat_core::ctx::VarSource 抽象，Python 侧由 pyctx::PyVarSource
适配 NameContext，所以本模块不再直接依赖 context。

`_num_text` 是私有函数，但 engine/rules.py 的 `_digits` 还在用，显式 import。
"""

from __future__ import annotations

from namecat_py.template import *          # noqa: F401,F403
from namecat_py.template import _num_text  # noqa: F401  rules._digits 在用
