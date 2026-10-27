# -*- coding: utf-8 -*-
"""ui.prefs —— 界面偏好的最小实现（白名单，绝不保存密钥）。"""

from __future__ import annotations

import json
import os

from engine import appdata_dir

DEFAULTS = {
    "last_root": "",
    "llm_provider": "",
    "llm_model": "",
    "llm_batch": 0,
}
FORBIDDEN = ("api_key", "apikey", "key", "token", "secret", "password")


def _path() -> str:
    return appdata_dir("gui.json")


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        for key in DEFAULTS:
            if key in raw:
                data[key] = raw[key]
    except (OSError, ValueError):
        pass
    return data


def save(data: dict) -> str:
    clean = {k: data.get(k, v) for k, v in DEFAULTS.items()
             if not any(bad in k.lower() for bad in FORBIDDEN)}
    path = _path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, ensure_ascii=False, indent=2)
        return path
    except OSError:
        return ""
