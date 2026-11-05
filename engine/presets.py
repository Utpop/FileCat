# -*- coding: utf-8 -*-
"""
engine.presets —— 规则预设（ReNamer 的 Presets 目录 + 可分享的单文件预设）。

存成 JSON，放在 `%APPDATA%\\NameCat\\presets\\` 下；也支持任意路径导入导出。
本模块只碰配置文件，不动用户数据。

—— 已换壳（P4）——
实现迁到 Rust（crates/namecat-core/src/presets.rs + namecat-py/src/py_presets.rs），
公开面保持不变（10 个符号）。磁盘格式与 Python 版逐字节一致
（`ensure_ascii=False, indent=2`）；脏 JSON 一律兜底：
`plan: 3` / `scan: "x"` 当空配置，规则能救的照常读回。
"""

from __future__ import annotations

from namecat_py.presets import *  # noqa: F401,F403
