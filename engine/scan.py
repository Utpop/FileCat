# -*- coding: utf-8 -*-
"""
engine.scan —— 扫描、过滤、排序、成链。

合并三处几乎重复的扫描逻辑：
    ReNamer 的「文件 + 文件夹 + 递归 + 只读/隐藏/系统过滤 + 自然序」
    BatchRenamer.folders 的「子文件夹 + only/exclude 文本过滤 + 按名/时间/大小排序」
    BatchRenamer.media 的「图片/视频扩展名 + 文件头校验 + 多种排序 + 每个子目录重置」

统一成一条 Options：想只看图/视频就设 kinds，想只看文件夹就设 kinds=("dir",)，
想混合处理就都开。排序键从 3 档扩展到 8 档（吸收 media.SORT_MODES）。

本模块只做 IO 扫描，不做改名计算（见 engine.plan）。

—— 已换壳（P2）——
实现迁到 Rust（crates/namecat-core/src/scan.rs + namecat-py/src/py_scan.rs），
公开面保持不变（25 个符号）。**扫描跳过日志目录必须用小写比对表**
`LOG_DIR_SKIP`，这个坑在 Rust 侧同样绕不开（见 scan.rs 注释）。
"""

from __future__ import annotations

from namecat_py.scan import *  # noqa: F401,F403
