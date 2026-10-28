# -*- coding: utf-8 -*-
"""
NameCat.engine —— 合并版重命名引擎（纯逻辑层，无界面依赖）。

设计目标：一个规则引擎同时覆盖三类对象
    · 文件（可带扩展名，扩展名可排除在规则链之外）
    · 文件夹
    · 两者的混合批次，且能正确处理父子同时改名

源自：
    · ReNamer Pro 的规则清单语义（序列化 / 冲突检测 / 循环处理 / 撤销）
    · BatchRenamer 的扫描-计算-校验-执行-撤销流水线

本包刻意不 import tkinter，也不 import 任何第三方库，可自由用于单元验证。
"""

from __future__ import annotations

import os
import shutil
import sys

APP_NAME = "NameCat"
APP_VER = "1.5.2"
LOG_DIR_NAME = "_rename_logs"
# 撤销日志统一存放的文件夹名（放在**程序目录**下，与被改名的目标目录无关）
BACKUP_DIR_NAME = "Name_backup"
# 撤销日志目录的环境变量覆盖（验证脚本用它把日志写进沙箱，不污染程序目录）
LOG_DIR_ENV = "NAMECAT_LOG_DIR"
MAX_LOGS = 20
# 目标目录很多时的总量兜底（每个目录各留 MAX_LOGS 份之外再压一道）
MAX_LOGS_TOTAL = 200
TEMP_PREFIX = "__nk_tmp_"

# 用户数据目录：`%APPDATA%\<DATA_DIR_NAME>`。
# 旧版本叫 NameKit，改名后仍要把用户已经存在的配置读出来 —— 首次访问时
# 把旧目录里"目标不存在"的文件复制过来（只复制，不删除旧数据）。
DATA_DIR_NAME = "NameCat"
LEGACY_DATA_DIRS = ("NameKit",)


def appdata_base() -> str:
    """`%APPDATA%`；没有该变量时退回到用户主目录。"""
    return os.environ.get("APPDATA") or os.path.expanduser("~")


def appdata_dir(*parts: str) -> str:
    """本程序的数据目录（或其子路径），并顺手完成一次旧目录迁移。"""
    migrate_legacy_appdata()
    return os.path.join(appdata_base(), DATA_DIR_NAME, *parts)


def program_dir() -> str:
    """程序所在目录（engine 包的上一级，即 main.py 所在的那一层）。

    打包成 exe 后这个目录可能没有写权限 —— 调用方要能容错。
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_BACKUP_DIR = ""


def backup_dir() -> str:
    """撤销日志的统一存放目录：`<程序目录>\\Name_backup`。

    · 环境变量 `NAMECAT_LOG_DIR` 优先（验证脚本靠它把日志写进沙箱）；
    · 程序目录不可写时退回 `%APPDATA%\\NameCat\\Name_backup`（不能因为
      日志没地方写就让整个改名失败）。
    · 结果缓存起来：探测一次写权限要建目录 + 试写 + 删探针，不该每次都做。
    """
    global _BACKUP_DIR
    override = os.environ.get(LOG_DIR_ENV)
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if _BACKUP_DIR:
        return _BACKUP_DIR
    candidate = os.path.join(program_dir(), BACKUP_DIR_NAME)
    if _writable(candidate):
        _BACKUP_DIR = candidate
    else:
        _BACKUP_DIR = appdata_dir(BACKUP_DIR_NAME)
    return _BACKUP_DIR


def _writable(path: str) -> bool:
    """目录能不能建、能不能写。建不出来就换别处。"""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return False
    probe = os.path.join(path, ".write_probe")
    try:
        with open(probe, "w") as fh:
            fh.write("1")
        os.remove(probe)
        return True
    except OSError:
        return False


def migrate_legacy_appdata() -> None:
    """把旧目录里的用户文件补齐到新目录（幂等，失败不抛异常）。

    只做三件安全的事：建新目录、复制缺失文件、跳过已存在文件。
    旧目录保持原样 —— 用户可能还要回退到旧版本。
    """
    base = appdata_base()
    target = os.path.join(base, DATA_DIR_NAME)
    if not os.path.isdir(target):
        for legacy_name in LEGACY_DATA_DIRS:
            legacy = os.path.join(base, legacy_name)
            if not os.path.isdir(legacy):
                continue
            try:
                shutil.copytree(legacy, target)
            except FileExistsError:
                pass
            except OSError:
                pass
            break
    # 新目录已存在时，仍补齐旧目录里新增/缺失的文件（例如新加了指令文件）
    for legacy_name in LEGACY_DATA_DIRS:
        legacy = os.path.join(base, legacy_name)
        if not os.path.isdir(legacy):
            continue
        for root, _dirs, files in os.walk(legacy):
            rel = os.path.relpath(root, legacy)
            dest_dir = target if rel == "." else os.path.join(target, rel)
            for name in files:
                dest = os.path.join(dest_dir, name)
                if os.path.exists(dest):
                    continue
                try:
                    os.makedirs(dest_dir, exist_ok=True)
                    shutil.copy2(os.path.join(root, name), dest)
                except OSError:
                    pass

# 状态码：沿用 BatchRenamer 的 5 种，文案直接复用，界面与日志不必再翻译
ST_OK = "ok"
ST_SAME = "same"
ST_CONFLICT = "conflict"
ST_ERROR = "error"
ST_SKIP = "skip"

STATUS_TEXT = {
    ST_OK: "待重命名",
    ST_SAME: "名称未变",
    ST_CONFLICT: "冲突",
    ST_ERROR: "无效",
    ST_SKIP: "已跳过",
}

LOG_SCHEMA = 4
UNDO_SCHEMA = 3          # 撤销日志格式版本：3 = BatchRenamer 原格式（可读旧日志）

PY_VER = sys.version_info[:2]

__all__ = ["APP_NAME", "APP_VER", "LOG_DIR_NAME", "BACKUP_DIR_NAME", "LOG_DIR_ENV",
           "MAX_LOGS", "MAX_LOGS_TOTAL", "TEMP_PREFIX",
           "DATA_DIR_NAME", "LEGACY_DATA_DIRS", "appdata_base", "appdata_dir",
           "migrate_legacy_appdata", "program_dir", "backup_dir",
           "ST_OK", "ST_SAME", "ST_CONFLICT", "ST_ERROR", "ST_SKIP",
           "STATUS_TEXT", "LOG_SCHEMA", "UNDO_SCHEMA", "PY_VER"]
