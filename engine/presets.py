# -*- coding: utf-8 -*-
"""
engine.presets —— 规则预设（ReNamer 的 Presets 目录 + 可分享的单文件预设）。

存成 JSON，放在 `%APPDATA%\\NameCat\\presets\\` 下；也支持任意路径导入导出。
本模块只碰配置文件，不动用户数据。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import APP_NAME, APP_VER, appdata_dir
from . import rules as R
from .plan import PlanOptions
from .scan import ScanOptions


def preset_dir() -> str:
    return appdata_dir("presets")


def _safe_name(name: str) -> str:
    bad = '\\/:*?"<>|'
    out = "".join("_" if c in bad else c for c in (name or "").strip())
    return out or "未命名预设"


@dataclass
class Preset:
    name: str = "未命名预设"
    rules: List = None
    scan: Dict = None
    plan: Dict = None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "app": APP_NAME, "version": APP_VER,
            "name": self.name, "note": self.note,
            "rules": R.rules_to_json(self.rules or []),
            "scan": dict(self.scan or {}),
            "plan": dict(self.plan or {}),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Preset":
        return cls(name=d.get("name") or "未命名预设",
                   rules=R.rules_from_json(d.get("rules") or []),
                   scan=dict(d.get("scan") or {}),
                   plan=dict(d.get("plan") or {}),
                   note=d.get("note") or "")

    def scan_options(self, **over) -> ScanOptions:
        data = dict(self.scan or {})
        data.update(over)
        return ScanOptions(**{k: v for k, v in data.items()
                              if k in ScanOptions.__dataclass_fields__})

    def plan_options(self, **over) -> PlanOptions:
        data = dict(self.plan or {})
        data.update(over)
        return PlanOptions(**{k: v for k, v in data.items()
                             if k in PlanOptions.__dataclass_fields__})


def save(preset: Preset, path: Optional[str] = None) -> str:
    target = path or os.path.join(preset_dir(), _safe_name(preset.name) + ".json")
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(preset.to_dict(), fh, ensure_ascii=False, indent=2)
    return target


def load(path: str) -> Preset:
    with open(path, "r", encoding="utf-8") as fh:
        return Preset.from_dict(json.load(fh))


def list_presets() -> List[str]:
    d = preset_dir()
    if not os.path.isdir(d):
        return []
    return sorted(os.path.join(d, n) for n in os.listdir(d) if n.endswith(".json"))


def delete(name_or_path: str) -> bool:
    path = name_or_path
    if not path.lower().endswith(".json"):
        path = os.path.join(preset_dir(), _safe_name(name_or_path) + ".json")
    try:
        os.remove(path)
        return True
    except OSError:
        return False


# 内置的常用预设（首次使用即可参考，不写盘）
BUILTIN = {
    "照片序号（四位）": Preset(
        name="照片序号（四位）",
        rules=[R.make_rule("template", "{n}")],
        scan={"kinds": ("image", "video")},
        plan={"scope": "folder", "start": 1, "width": 4},
        note="按文件夹各自从 0001 起编号，扩展名不变"),
    "去掉末尾圆括号序号": Preset(
        name="去掉末尾圆括号序号",
        rules=[R.make_rule("regex", r"\s*\(\d+\)$", "")],
        note="把「副本 (2)」这类尾部编号去掉"),
    "首尾去空格并规范空格": Preset(
        name="首尾去空格并规范空格",
        rules=[R.make_rule("trim"), R.make_rule("collapse", " ")],
        note=""),
    "改为小写并去非法字符": Preset(
        name="改为小写并去非法字符",
        rules=[R.make_rule("case", "lower"), R.make_rule("filename_clean")],
        note=""),
}


def builtin_list() -> List[str]:
    return list(BUILTIN)
