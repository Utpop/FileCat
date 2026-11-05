# -*- coding: utf-8 -*-
"""
engine.zhconv —— 中文简繁单字映射（规则 zhconv 用）。

数据取自 OpenCC 的 STCharacters / TSCharacters 字典（Apache-2.0 许可，
https://github.com/BYVoid/OpenCC），构建时取「最常用第一个」：一个简体字
对应多个繁体写法时只保留字典列出的第一个。文件名重命名只需要逐字转换，
够用；多字词级别的地区习惯转换（如 软件→軟體）不在本规则的目标里。

—— 已换壳（P1）——
实现迁到 Rust（crates/namecat-core/src/zhconv.rs），映射表由
tools/gen_zhconv.py 生成成 zhconv_data.rs（FROM/TO 两行逐字对齐，
同一原字多次出现时后来者胜，对齐 str.maketrans 的覆盖顺序）。
公开面只有 `convert`，调用方（engine/rules.py）零改动。
改表流程：git 回退本文件到 1.6.3 的 Python 版 → 改表 → 重跑
`python tools/gen_zhconv.py` → 再换回壳。
"""

from __future__ import annotations

from namecat_py.zhconv import *  # noqa: F401,F403
