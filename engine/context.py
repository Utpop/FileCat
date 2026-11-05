# -*- coding: utf-8 -*-
"""
engine.context —— 命名上下文（供模板变量使用）。

合并两边之后暴露的变量是两边变量表的并集：
    ReNamer 侧：#、$Counter、$Date、Date、$MD5、$SHA1、$FilePath、$FilePath1 等
    BatchRenamer 侧：{n} {name} {ext} {index} {total} {size} {mtime} {year} …

变量命名一律小写、下划线分隔；$Counter 与 # 等价，都是「本组编号」。
template.py 负责把两种写法都映射到这里。

—— 已换壳（P2）——
实现迁到 Rust（crates/namecat-core/src/context.rs + namecat-py/src/py_context.rs），
公开面保持不变：Item / NameContext / split_ext / fmt_date / human_size。

两个类都是 PyO3 pyclass，属性面与原来的 dataclass 一致（含 Item 的 `__dict__`，
适配层会往里挂 `_excluded` 之类的临时字段）。
"""

from __future__ import annotations

from namecat_py.context import *  # noqa: F401,F403
