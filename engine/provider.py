# -*- coding: utf-8 -*-
"""
engine.provider —— 供应商配置（OpenAI 兼容接口为主）+ 连通性测试。

定位
    界面上「LLM 设置」这一页管的其实是**供应商配置**：
        供应商名称 / 协议 / 供应商预设 / 基础 URL / API Key
        供应商模型（手动添加 / 拉取模型列表）
        高级（模型列表 URL / 自定义请求头 / 生成参数）
    这一层只负责"怎么连"（地址、密钥、请求头、协议），
    "连上之后叫什么名字"由 engine.llm 的要求文件与规则链负责。

三件事
    1. 预设：常见供应商一键填好地址与模型列表 URL（OpenAI / DeepSeek /
       小米 MiMo / OpenCode Go），选「自定义供应商」则全手填。
    2. 落盘：普通配置存 %APPDATA%\\NameCat\\llm_provider.json（可分享），
       **密钥单独存** %APPDATA%\\NameCat\\llm_credential.dat（DPAPI 加密，
       见 engine.secrets）——两者永远不混在一起。
    3. 连通性测试：真发一个请求（优先拉模型列表，失败再退回最小对话），
       返回耗时与失败原因，界面直接把原因显示出来。

原则
    · 任何函数都不打印密钥；错误信息里也不带密钥。
    · 断网 / 401 / 404 都要给出**人话原因**，不是抛栈。

—— 已换壳（P4）——
实现迁到 Rust（crates/namecat-core/src/{http,provider}.rs +
namecat-py/src/{py_provider,py_net}.rs），公开面保持不变（22 个符号）。
`STATUS`（左下角气泡的数据源）、`opener_for` 与两个 opener 常量不在
`__all__` 里，但界面与 `verify/test_edges.py` 直接用，故显式 import。
"""

from __future__ import annotations

from namecat_py.provider import *                # noqa: F401,F403
from namecat_py.provider import (                # noqa: F401
    _DEFAULT_OPENER,
    _LOOPBACK_HOSTS,
    _LOOPBACK_OPENER,
    _request,
    STATUS,
    ProviderStatus,
    open_url,
    opener_for,
)
from . import secrets                            # noqa: F401  沿用 Python 版的口径
