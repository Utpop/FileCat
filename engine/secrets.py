# -*- coding: utf-8 -*-
"""
engine.secrets —— 本地密钥的存取（只给界面用，不进日志、不进预设）。

为什么单独一个模块
    供应商配置（地址、模型、请求头）是可以随便备份的普通设置，
    但**密钥**不是。两者必须分开存：配置文件可以分享，密钥文件不能。

怎么存
    Windows：用 DPAPI（CryptProtectData）加密，只有当前 Windows 账号能解开，
             别的账号 / 别的机器拿到文件也读不出明文。
    其它平台：退化成 Base64，只是防君子不防小人，注释里写清楚。

约定
    · save_secret("") 是**空操作**（不写盘、也不清空）—— 界面「留空沿用已保存的
      密钥」靠的就是它；要清空必须显式调 clear_secret()。
      （provider.save_config 传 "" 表示"清除"，两个层级的口径不同，调用方别记混。）
    · 任何函数都不打印、不返回明文给日志；出错一律返回 False / 空串。

—— 已换壳（P4）——
实现迁到 Rust（crates/namecat-core/src/secrets.rs + namecat-py/src/py_secrets.rs），
公开面保持不变（6 个符号）。磁盘格式 `DPAPI1:` + 密文 / `BASE64:` + 明文，
与旧版本写出的文件**互相**能认（同一套 CryptProtectData，描述串仍是 "NameCat"）。
"""

from __future__ import annotations

from namecat_py.secrets import *  # noqa: F401,F403
