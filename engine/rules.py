# -*- coding: utf-8 -*-
"""
engine.rules —— 规则链。

合并原则
    · 吸收 ReNamer 的规则清单：种类多、可排序启停、可保存成预设
    · 吸收 BatchRenamer 的 13 种规则与其中文文案、`名称=参数` 命令行写法
    · 两边重复的（keep/remove after/before、replace、prefix、suffix、trim、
      template）合并成一条，用 selector 表达「只作用于某一段」

每条规则的结构：
    id          稳定标识（预设与撤销日志引用它）
    kind        规则类型（见 RULE_KINDS）
    a / b / c   三段参数
    enabled     是否启用
    selector    作用范围：text/after/before/from/until/range
    charset     selector 的分隔符或区间表达式
    occurrence  第几次出现（1 首次，-1 最后）
    skip_ext    是否排除扩展名（仅文件有意义）
    keep_sep    切分时是否保留分隔符（对照 BatchRenamer 的同名开关）

    —— 已换壳（P1）——
    实现迁到 Rust：
      crates/namecat-core/src/rules.rs   规则表与全部变换
      crates/namecat-py/src/py_rules.rs  边界（Rule 类 / 函数 / 常量）
    Rule 的 `extras` 在 Rust 侧直接持有 Python dict 对象，所以
    `rule.extras.setdefault(...)`、`rule.extras[k] = v` 这类原地改写在
    适配层与命令行里照常生效（换壳不改调用方）。
"""

from __future__ import annotations

from namecat_py.rules import *  # noqa: F401,F403
