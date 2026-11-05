# -*- coding: utf-8 -*-
"""
engine.llm —— 大模型改名建议层（"智能精简文件名"）。

定位
    规则引擎能可靠地搬运**已知**文本，但判断不出「哪些片段是没有意义的」：
    `DSC_04321_IMG_20230815_拷贝(2).jpg` 里真正该留的往往是 「海边日落」这类
    语义内容，而 编号 / 日期戳 / 相机前缀 / 下载站后缀 / 校验串 都该丢。
    这类判断交给大模型，把它作为一种规则（kind="llm"）挂进整条规则链。

★ 留给使用者的自由度：要求可编辑
    要求不是写死的提示词，而是**可以随便改的用户要求**，三处可改：
        · Rule.extras["instruction"]   —— 主体要求（默认 DEFAULT_INSTRUCTION）
        · Rule.extras["lang"]          —— 输出语言（zh / en / ja…）
        · Rule.extras["max_len"]       —— 名字最大长度
    引擎只负责把它们拼成提示词；界面层应当把这三项做成可编辑输入框，
    命令行用 `--rule "llm=<要求>=<语言>=<最大长度>"` 或 `--llm-instruction 文件`。

两级入口（重要）
    plan(ctxs, ...)  批量规划：**一次请求算一批**，结果缓存在 ctx.extra["llm"]。
                     规则变换（纯函数、无 IO）只读缓存，绝不在变换里发请求。
    rename(ctx)      单个调用：debug 与手工兜底用。

离线性
    没配置 API Key 时不会报错也不会悄悄乱改：make_client() 返回一个
    Unavailable 客户端，plan() 给出 available=False 与一句原因，规则保持原样。
    这样「没网 / 没配 key」等同于「这条规则没启用」，整批其它规则照常工作。

安全
    模型输出一律经过 sanitize_name() 净化：去非法字符、去首尾空格与点、
    防 Windows 保留名、截断长度；空结果或与原名相同都视为"不改"。

—— 已换壳（P4）——
实现迁到 Rust（crates/namecat-core/src/{http,llm}.rs +
namecat-py/src/{py_llm,py_net}.rs），公开面保持不变（14 个符号）。
两处要点：
    · HTTP 字节出口仍在宿主 Python（urllib）：系统代理、回环绕代理、证书
      这三件事换实现就等于换行为，而 test_edges 的 H5 断言了 opener 身份。
    · LLMClient 用 `#[pyclass(extends=…, subclass)]` 保留"Python 继承 + 覆盖
      _chat / plan"的用法（run_tests 的 Boom / Spy 就是靠这个）。
下面显式 import 的符号不在 __all__ 里，但界面/命令行与自检直接用了它们。
"""

from __future__ import annotations

from namecat_py.llm import *                     # noqa: F401,F403
from namecat_py.llm import (                      # noqa: F401
    INSTRUCTION_ENV,
    INSTRUCTION_HEADER,
    INSTRUCTION_NAME,
    LEGACY_INSTRUCTION_ENV,
    SYSTEM_PROMPT,
    ensure_instruction_file,
    instruction_path,
    read_instruction,
    write_instruction,
)
