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
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# 默认要求（可被使用者整段改写）
# --------------------------------------------------------------------------- #

DEFAULT_INSTRUCTION = (
    "把文件名里没有意义的片段去掉，只留下能让人一眼认出的语义信息。\n"
    "应当去掉：设备/相机/软件前缀（DSC、IMG、PXL、Screenshot、微信图片）、"
    "纯数字编号与时间戳（20230815、0432、_1）、下载站与广告后缀"
    "（_无水印、_高清、www.xxx.com、[更多资源]）、重复与临时词"
    "（副本、拷贝、copy、新建、未命名、final、final2）、"
    "无意义分隔符堆叠与括号空壳。\n"
    "应当保留：主体内容、地点、人物、项目、主题等真正有辨识度的信息。\n"
    "不要杜撰原文里没有的信息；信息本就不多时，简短即可。"
)

DEFAULT_LANG = "zh"
DEFAULT_MAX_LEN = 24

# 使用者可编辑的要求文件（不存在时按 DEFAULT_INSTRUCTION 自动建一份）
INSTRUCTION_ENV = "NAMECAT_LLM_INSTRUCTION_FILE"
# 改名前的变量名：仍然要认，否则老用户的配置会"突然失效"
LEGACY_INSTRUCTION_ENV = "NAMEKIT_LLM_INSTRUCTION_FILE"
INSTRUCTION_NAME = "llm_instruction.txt"

INSTRUCTION_HEADER = """\
# NameCat —— 交给大模型的文件名整理要求
# 这个文件就是提示词里「整理要求」那一段，可以随便改、随便扩写。
# 以单个 # 开头的行是注释，不会发给模型；空行会保留。
# 改完保存即可，下次运行自动生效（--llm-instruction 可临时换一个文件）。
"""

# 命名风格：影响提示词里的一句话约束，界面可做成下拉框
STYLES = {
    "short": "尽量短，只留最关键的 2~4 个词",
    "natural": "像正常标题一样可读，词与词之间可保留空格",
    "kebab": "用短横线连接，全小写，适合做 URL",
    "snake": "用下划线连接，全小写",
    "camel": "驼峰式，无分隔符",
}
DEFAULT_STYLE = "natural"

SYSTEM_PROMPT = (
    "你是一个文件名整理助手。你会收到一批文件名（不含扩展名），"
    "对每一个，只输出精简后的名字。"
)

# --------------------------------------------------------------------------- #
# 可编辑的「给 LLM 的要求」
# --------------------------------------------------------------------------- #

def instruction_path(custom: str = "") -> str:
    """要求文件位置：显式路径 > 环境变量 > `%APPDATA%\\NameCat\\llm_instruction.txt`。

    不放在工作目录里：换个目录跑同一套要求，行为应当一致。
    环境变量先认 `NAMECAT_LLM_INSTRUCTION_FILE`，再认改名前的
    `NAMEKIT_LLM_INSTRUCTION_FILE`。
    """
    if custom:
        return os.path.abspath(os.path.expanduser(custom))
    env = os.environ.get(INSTRUCTION_ENV) or os.environ.get(LEGACY_INSTRUCTION_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    from . import appdata_dir
    return appdata_dir(INSTRUCTION_NAME)


def _strip_comment_lines(text: str) -> str:
    """去掉整行注释（`#` 开头）与首尾空行；正文原样保留（含内部空行与缩进）。"""
    keep = []
    for line in str(text or "").splitlines():
        if line.lstrip().startswith("#"):
            continue
        keep.append(line)
    while keep and not keep[0].strip():
        keep.pop(0)
    while keep and not keep[-1].strip():
        keep.pop()
    return "\n".join(keep)


def read_instruction(custom: str = "") -> str:
    """读使用者写的要求；文件缺失或为空时回落 DEFAULT_INSTRUCTION（绝不报错）。"""
    path = instruction_path(custom)
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            body = _strip_comment_lines(fh.read())
    except OSError:
        return DEFAULT_INSTRUCTION
    return body or DEFAULT_INSTRUCTION


def write_instruction(text: str, custom: str = "", with_header: bool = True) -> str:
    """把要求写进文件（自动建目录），返回文件路径。"""
    path = instruction_path(custom)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    body = (INSTRUCTION_HEADER + "\n" + str(text or "").strip() + "\n") \
        if with_header else str(text or "")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    return path


def ensure_instruction_file(custom: str = "") -> str:
    """确保要求文件存在：缺失时用默认要求建一份，使用者随后直接改即可。"""
    path = instruction_path(custom)
    if not os.path.exists(path):
        try:
            write_instruction(DEFAULT_INSTRUCTION, custom)
        except OSError:
            return path
    return path


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT = 60
# 一次请求带多少条名字（太少浪费往返，太多模型容易漏项）
DEFAULT_BATCH = 20


@dataclass
class LLMConfig:
    """接入配置。全部来自环境变量或显式传参，绝不写死在代码里。"""
    provider: str = "openai"          # openai（/chat/completions 兼容） / ollama
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    timeout: int = DEFAULT_TIMEOUT
    batch: int = DEFAULT_BATCH
    temperature: float = 0.2
    max_retries: int = 2
    # 协议：openai_chat（/chat/completions）/ openai_responses（/responses）
    #       / anthropic_messages（/messages）
    protocol: str = "openai_chat"
    # 额外请求头（界面「自定义请求头」）：同名以这里为准
    headers: Dict[str, str] = field(default_factory=dict)
    max_tokens: int = 1024

    @classmethod
    def from_env(cls, **override) -> "LLMConfig":
        """按 NAMECAT_LLM_* → NAMEKIT_LLM_* → OPENAI_* → DEEPSEEK_* 的顺序读环境变量。

        改名前的 `NAMEKIT_LLM_*` 仍然可用，所以老用户不用改环境就能继续跑。
        """
        env = os.environ

        def pick(*names, default=""):
            for n in names:
                v = env.get(n)
                if v:
                    return v
            return default

        cfg = cls(
            provider=pick("NAMECAT_LLM_PROVIDER", "NAMEKIT_LLM_PROVIDER", "NAMEXIT_LLM_PROVIDER",
                          "LLM_PROVIDER", default="openai"),
            base_url=pick("NAMECAT_LLM_BASE_URL", "NAMEKIT_LLM_BASE_URL", "NAMEXIT_LLM_BASE_URL",
                          "OPENAI_BASE_URL", "DEEPSEEK_BASE_URL", default=DEFAULT_BASE_URL),
            api_key=pick("NAMECAT_LLM_API_KEY", "NAMEKIT_LLM_API_KEY", "NAMEXIT_LLM_API_KEY",
                         "OPENAI_API_KEY", "DEEPSEEK_API_KEY", default=""),
            model=pick("NAMECAT_LLM_MODEL", "NAMEKIT_LLM_MODEL", "NAMEXIT_LLM_MODEL",
                       "OPENAI_MODEL", "DEEPSEEK_MODEL", default=DEFAULT_MODEL),
        )
        proto = pick("NAMECAT_LLM_PROTOCOL", "NAMEKIT_LLM_PROTOCOL", default="")
        if proto:
            cfg.protocol = proto
        raw_headers = pick("NAMECAT_LLM_HEADERS", "NAMEKIT_LLM_HEADERS", default="")
        if raw_headers:
            try:
                loaded = json.loads(raw_headers)
            except (ValueError, TypeError):
                loaded = None
            if isinstance(loaded, dict):
                cfg.headers = {str(k): str(v) for k, v in loaded.items() if str(k).strip()}
        for k, v in override.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg

    def as_dict(self) -> dict:
        # 绝不把 api_key 序列化出去（可能进日志 / CSV / 预设）
        return {"provider": self.provider, "base_url": self.base_url,
                "model": self.model, "timeout": self.timeout,
                "batch": self.batch, "has_key": bool(self.api_key)}

    def problem(self) -> str:
        """返回一句"为什么不可用"，空串表示可用。"""
        if not self.api_key and self.provider != "ollama":
            return ("未配置 API Key：设置环境变量 NAMECAT_LLM_API_KEY"
                    "（旧名 NAMEKIT_LLM_API_KEY 仍然可用；或 OPENAI_API_KEY / "
                    "DEEPSEEK_API_KEY），或把 provider 设为 ollama 走本地模型")
        if not self.base_url:
            return "未配置 base_url"
        if not self.model:
            return "未配置 model"
        return ""


# --------------------------------------------------------------------------- #
# 名字净化（模型输出不可信，必须先过一遍）
# --------------------------------------------------------------------------- #

_INVALID = '\\/:*?"<>|'
_RESERVED = {"con", "prn", "aux", "nul",
             *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}
_WS = re.compile(r"\s+")
_BAD_SEP = re.compile(r"[_\-.\s]{2,}")


def sanitize_name(text: str, max_len: int = DEFAULT_MAX_LEN,
                  style: str = DEFAULT_STYLE) -> str:
    """把模型输出收拾成合法文件名片段。返回空串表示"不能用"。"""
    if not text:
        return ""
    out = str(text).strip().strip("\"'`“”‘’")
    out = out.splitlines()[0].strip() if out else ""
    out = "".join("_" if (c in _INVALID or ord(c) < 32) else c for c in out)
    out = _WS.sub(" ", out).strip()
    style = (style or DEFAULT_STYLE).lower()
    if style in ("kebab", "snake"):
        sep = "-" if style == "kebab" else "_"
        out = re.sub(r"[\s_\-]+", sep, out).strip(sep)
    elif style == "camel":
        parts = re.split(r"[\s_\-]+", out)
        out = "".join(p[:1].upper() + p[1:] for p in parts if p)
    else:
        out = _BAD_SEP.sub(" ", out).strip()
    out = out.strip(" .")
    try:
        n = int(max_len or 0)
    except (TypeError, ValueError):
        n = DEFAULT_MAX_LEN
    if n > 0 and len(out) > n:
        out = out[:n].rstrip(" .-_")
    if out.split(".")[0].strip().lower() in _RESERVED:
        out = "_" + out
    return out


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #

def build_prompt(names: Sequence[str], instruction: str = "",
                 lang: str = DEFAULT_LANG, max_len: int = DEFAULT_MAX_LEN,
                 style: str = DEFAULT_STYLE, strict: bool = False) -> str:
    """拼出一次请求的用户消息。要求原文逐字插入，不做改写。"""
    req = (instruction or DEFAULT_INSTRUCTION).strip()
    lines = [
        "## 整理要求（使用者自定义，优先遵守）",
        req,
        "",
        "## 输出约定",
        "· 语言：%s" % (lang or DEFAULT_LANG),
        "· 每个新名字不超过 %s 个字符" % (max_len or DEFAULT_MAX_LEN),
        "· 命名风格：%s" % STYLES.get(style, STYLES[DEFAULT_STYLE]),
        "· 不要输出扩展名、不要输出路径、不要加引号",
        "· 保持顺序一一对应，不要合并或删除条目",
    ]
    if strict:
        lines += [
            "",
            "## 输出格式（严格）",
            '只输出一个 JSON 数组，长度等于输入条目数。每项形如 '
            '{"i": 序号, "n": 新名字}。不要输出任何解释文字。',
        ]
    lines += ["", "## 输入文件名（共 %d 条）" % len(names)]
    for i, n in enumerate(names, 1):
        lines.append("%d. %s" % (i, n))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #

@dataclass
class SuggestResult:
    """一次批量规划的结果。"""
    mapping: Dict[str, str] = field(default_factory=dict)   # 原名 -> 新名
    available: bool = True
    reason: str = ""
    used: int = 0
    failed: int = 0
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.available:
            return "LLM 未启用：%s" % self.reason
        msg = "LLM 精简：%d 项" % self.used
        if self.failed:
            msg += "，%d 项无结果（保持原名）" % self.failed
        return msg


class LLMClient:
    """统一入口。子类只需实现 available() 与 _chat()。"""

    name = "base"

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig.from_env()

    # ---- 能力 ----
    def available(self) -> bool:
        return not self.config.problem()

    def problem(self) -> str:
        return self.config.problem()

    # ---- 单次补全 ----
    def _chat(self, system: str, user: str) -> str:
        raise NotImplementedError

    def _chat_json(self, system: str, user: str) -> str:
        """优先 JSON 模式；不支持时退回普通补全（load_suggestions 两种都认）。"""
        return self._chat(system, user)

    # ---- 结果解析 ----
    @staticmethod
    def parse_suggestions(raw: str, count: int) -> Optional[Dict[int, str]]:
        """从模型回复里抠出 {序号: 新名}。解析不了返回 None（→ 触发严格重试）。"""
        if not raw:
            return None
        text = raw.strip()
        # 去掉 ```json ``` 包裹
        fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
        if fence:
            text = fence.group(1)
        # 1) 标准 JSON 数组
        for cand in (text, _slice_json(text)):
            if not cand:
                continue
            try:
                data = json.loads(cand)
            except (ValueError, TypeError):
                continue
            out = _coerce_pairs(data, count)
            if out:
                return out
        # 2) 逐行 "1. 名字" / "1|名字" / "名字"
        out: Dict[int, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(("##", "#")):
                continue
            m = re.match(r"^(\d+)\s*[.、,，:：|)\]]\s*(.+)$", line)
            if m:
                out[int(m.group(1))] = m.group(2).strip()
            elif len(out) < count and line:
                out[len(out) + 1] = line
        return out or None

    # ---- 批量规划（规则链用这个） ----
    def plan(self, names: Sequence[str], instruction: str = "",
             lang: str = DEFAULT_LANG, max_len: int = DEFAULT_MAX_LEN,
             style: str = DEFAULT_STYLE,
             progress: Optional[Callable] = None) -> SuggestResult:
        """把一批名字交给模型，返回「原名->新名」映射。绝不抛异常。"""
        names = [n for n in (names or []) if n]
        res = SuggestResult()
        prob = self.problem()
        if prob:
            res.available, res.reason = False, prob
            return res
        if not names:
            return res

        seen = list(dict.fromkeys(names))    # 去重且保持顺序：同名只问一次
        batch = max(1, int(self.config.batch or DEFAULT_BATCH))
        for start in range(0, len(seen), batch):
            chunk = seen[start:start + batch]
            got = self._plan_one(chunk, instruction, lang, max_len, style, res)
            for src, dst in got.items():
                clean = sanitize_name(dst, max_len, style)
                if clean and clean != src:
                    res.mapping[src] = clean
                    res.used += 1
            if progress:
                progress(min(start + batch, len(seen)), len(seen), "LLM 精简")
        return res

    def _plan_one(self, chunk, instruction, lang, max_len, style, res) -> Dict[str, str]:
        user = build_prompt(chunk, instruction, lang, max_len, style)
        for attempt in range(max(1, int(self.config.max_retries or 1))):
            strict = attempt > 0
            try:
                if strict:
                    raw = self._chat_json(SYSTEM_PROMPT, build_prompt(
                        chunk, instruction, lang, max_len, style, strict=True))
                else:
                    raw = self._chat(SYSTEM_PROMPT, user)
            except Exception as exc:              # 网络/鉴权/超时一律降级
                res.errors.append("%s: %s" % (type(exc).__name__, exc))
                res.failed += len(chunk)
                return {}
            pairs = self.parse_suggestions(raw, len(chunk))
            if pairs:
                return {chunk[i - 1]: v for i, v in sorted(pairs.items())
                        if 1 <= i <= len(chunk)}
        res.errors.append("模型回答无法解析（%d 条）" % len(chunk))
        res.failed += len(chunk)
        return {}

    # ---- 单个调用 ----
    def suggest(self, name: str, instruction: str = "",
                lang: str = DEFAULT_LANG, max_len: int = DEFAULT_MAX_LEN,
                style: str = DEFAULT_STYLE) -> str:
        """单个名字的新建议（空串表示没意见）。调试与手工操作用。"""
        r = self.plan([name], instruction, lang, max_len, style)
        return r.mapping.get(name, "")


class HttpChatClient(LLMClient):
    """OpenAI 兼容接口。

    支持三种协议（界面「协议」下拉框里选）：
        · openai_chat       POST {base}/chat/completions
        · openai_responses  POST {base}/responses
        · anthropic_messages POST {base}/messages（鉴权头换成 x-api-key）

    任何一种协议，回复的最后都归一成"一段文本"，上层解析逻辑不用改。
    """

    name = "openai"

    def _endpoint(self) -> str:
        base = (self.config.base_url or "").rstrip("/")
        if self.config.protocol == "anthropic_messages":
            if base.endswith("/messages"):
                return base
            if re.search(r"/v\d+$", base):
                return base + "/messages"
            return base + "/v1/messages"
        if self.config.protocol == "openai_responses":
            if base.endswith("/responses"):
                return base
            if re.search(r"/v\d+$", base):
                return base + "/responses"
            return base + "/v1/responses"
        if base.endswith("/chat/completions"):
            return base
        if re.search(r"/v\d+$", base):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    def _headers(self) -> Dict[str, str]:
        head = {"Content-Type": "application/json",
                "Accept": "application/json"}
        if self.config.protocol == "anthropic_messages":
            if self.config.api_key:
                head["x-api-key"] = self.config.api_key
                head["anthropic-version"] = "2023-06-01"
        else:
            head["Authorization"] = "Bearer %s" % self.config.api_key
        for key, value in (self.config.headers or {}).items():
            if str(key).strip():
                head[str(key).strip()] = str(value)
        return head

    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._endpoint(), data=body, method="POST",
            headers=self._headers())
        import time as _time
        started = _time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as fh:
                data = json.loads(fh.read().decode("utf-8", "replace"))
        finally:
            # 真实请求的耗时记进状态里（界面左下角的气泡显示的就是它）
            try:
                from . import provider as _prv
                _prv.STATUS.report(True, "", int((_time.time() - started) * 1000))
                _prv.STATUS.set_model(self.config.model)
            except Exception:
                pass
        return data

    # ---- 三种协议的请求体与回复解析 ----
    def _payload(self, system: str, user: str, json_mode: bool = False) -> dict:
        proto = self.config.protocol
        if proto == "anthropic_messages":
            payload = {"model": self.config.model,
                       "max_tokens": self.config.max_tokens,
                       "messages": [{"role": "user", "content": user}]}
            if system:
                payload["system"] = system
            if json_mode:
                payload["messages"][0]["content"] += \
                    "\n只输出 JSON，不要任何解释文字。"
            return payload
        if proto == "openai_responses":
            payload = {"model": self.config.model,
                       "input": user,
                       "max_output_tokens": self.config.max_tokens}
            if system:
                payload["instructions"] = system
            if json_mode:
                payload["text"] = {"format": {"type": "json_object"}}
            return payload
        payload = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    @staticmethod
    def _extract(data: dict, protocol: str) -> str:
        if protocol == "anthropic_messages":
            try:
                return "".join(str(part.get("text") or "")
                               for part in (data.get("content") or []))
            except (AttributeError, TypeError):
                raise ValueError("回复格式异常：%s" % json.dumps(data, ensure_ascii=False)[:200])
        if protocol == "openai_responses":
            if isinstance(data.get("output_text"), str):
                return data["output_text"]
            chunks = []
            for item in (data.get("output") or []):
                for part in (item.get("content") or []):
                    if isinstance(part, dict) and part.get("text"):
                        chunks.append(str(part["text"]))
            if chunks:
                return "".join(chunks)
            raise ValueError("回复格式异常：%s" % json.dumps(data, ensure_ascii=False)[:200])
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise ValueError("回复格式异常：%s" % json.dumps(data, ensure_ascii=False)[:200])

    def _chat(self, system: str, user: str, json_mode: bool = False) -> str:
        data = self._post(self._payload(system, user, json_mode))
        return self._extract(data, self.config.protocol)

    def _chat_json(self, system: str, user: str) -> str:
        try:
            return self._chat(system, user, json_mode=True)
        except urllib.error.HTTPError:
            return self._chat(system, user)


class OllamaClient(LLMClient):
    """本地 Ollama：不需要 API Key，离线可用，适合隐私敏感场景。"""

    name = "ollama"

    def __init__(self, config: Optional[LLMConfig] = None):
        super().__init__(config)
        if "ollama" in (self.config.base_url or "") or not self.config.base_url:
            pass
        elif self.config.provider == "ollama" and "openai.com" in self.config.base_url:
            self.config.base_url = "http://127.0.0.1:11434"

    def available(self) -> bool:
        return bool(self.config.base_url and self.config.model)

    def problem(self) -> str:
        return "" if self.available() else "Ollama 需要 base_url 与 model"

    def _chat(self, system: str, user: str) -> str:
        base = (self.config.base_url or "http://127.0.0.1:11434").rstrip("/")
        if not base.endswith("/api/chat"):
            base = base + "/api/chat"
        payload = {"model": self.config.model, "stream": False,
                   "options": {"temperature": self.config.temperature},
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}]}
        req = urllib.request.Request(
            base, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.config.timeout) as fh:
            data = json.loads(fh.read().decode("utf-8", "replace"))
        return (data.get("message") or {}).get("content", "")


class EchoClient(LLMClient):
    """离线替身：按内置规则做"精简"，用于验证套件与没网时的行为对比。

    它**不联网**，只做可预测的机械清理：
        · 去掉相机/软件前缀（DSC_ IMG_ PXL_ Screenshot 微信图片 等）
        · 去掉纯数字段与长数字戳
        · 去掉下载站/广告后缀与"副本/拷贝/copy/新建/final"等垃圾词
        · 合并多余分隔符
    这样验证套件可以端到端跑通整条「LLM 规则 → 计划 → 落盘」链路。
    """

    name = "echo"

    JUNK_PREFIX = re.compile(
        r"^(?:(?:dsc|dscn|img|image|pxl|vid|video|photo|pic|screenshot|screen|"
        r"snipaste|微信图片|微信截图|新建|未命名|untitled|copy|scan|output|"
        r"屏幕截图|截屏)"
        r"(?:[\s_\-.]*\d+)*[\s_\-.]*)+", re.I)
    JUNK_WORD = re.compile(
        r"(?<![A-Za-z])(?:screenshot|snipaste|screen shot|copy)(?![A-Za-z])", re.I)
    JUNK_SUFFIX = re.compile(
        r"(?:[\s_\-.]*(?:副本|拷贝|copy|复件|backup|bak|final\d*|修改版?|"
        r"已编辑|edited|无水印|高清|超清|原版|更多资源|下载))+$", re.I)
    SITE = re.compile(r"(?:www\.[a-z0-9.\-]+|https?://\S+|\[[^\]]*\]|【[^】]*】)",
                      re.I)
    LONGNUM = re.compile(r"(?<![a-z0-9])\d{4,}(?![a-z0-9])", re.I)
    TIMESTAMP = re.compile(
        r"\d{4}[\-_年]\d{1,2}[\-_月]\d{1,2}日?"
        r"(?:[\s_\-T]*(?:at\s*)?\d{1,2}[.\-:：时]\d{2}(?:[.\-:：分]\d{2}秒?)?)?"
        r"|\d{8}", re.I)
    TIME_ONLY = re.compile(
        r"(?:^|(?<=[\s_\-.]))(?:at\s*)?\d{1,2}[.\-:：时]\d{2}"
        r"(?:[.\-:：分]\d{2}秒?)?(?=$|[\s_\-.])", re.I)
    EXT = re.compile(r"^\.[A-Za-z0-9]{1,8}$")

    def available(self) -> bool:
        return True

    def problem(self) -> str:
        return ""

    def plan(self, names, instruction="", lang=DEFAULT_LANG,
             max_len=DEFAULT_MAX_LEN, style=DEFAULT_STYLE, progress=None):
        res = SuggestResult(available=True)
        for name in dict.fromkeys(n for n in (names or []) if n):
            out = self.simplify(name)
            out = sanitize_name(out, max_len, style)
            if out and out != name:
                res.mapping[name] = out
                res.used += 1
        return res

    @classmethod
    def simplify(cls, name: str, keep_ext: bool = False) -> str:
        """机械精简。keep_ext=False 时名字**不含**扩展名（规则链里由 skip_ext 保护）。"""
        t = str(name or "")
        if not keep_ext:
            stem, ext = os.path.splitext(t)
            if ext and cls.EXT.match(ext):
                t = stem
        # 前缀与后缀都是"垃圾词 _ 垃圾词"连成的串，交替剥到不动为止
        for _ in range(6):
            before = t
            t = cls.JUNK_PREFIX.sub("", t)
            t = cls.JUNK_SUFFIX.sub("", t)
            t = t.strip(" _-.")
            if t == before:
                break
        for i in range(4):                    # 日期与时间可能互相遮蔽
            before = t
            t = cls.SITE.sub(" ", t)
            t = cls.TIMESTAMP.sub(" ", t)
            t = cls.TIME_ONLY.sub(" ", t)
            t = cls.LONGNUM.sub(" ", t)
            t = re.sub(r"(?:^|(?<=[\s_]))(?:at|on|of)(?=$|[\s_])", " ", t, flags=re.I)
            t = t.strip(" _-.")
            if t == before:
                break
        t = cls.JUNK_WORD.sub(" ", t)         # Screenshot/截图 这类纯垃圾词最后剥
        t = re.sub(r"\(\s*\d+\s*\)", " ", t)          # (1) (2)
        t = re.sub(r"[_\-. ]{2,}", " ", t)
        t = re.sub(r"\s+", " ", t).strip(" _-.")
        if not t:                             # 精简后什么都不剩就不改名
            return ""
        return t


class _Unavailable(LLMClient):
    name = "unavailable"

    def __init__(self, reason: str):
        super().__init__(LLMConfig())
        self._reason = reason

    def available(self) -> bool:
        return False

    def problem(self) -> str:
        return self._reason


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #

def make_client(config: Optional[LLMConfig] = None, kind: str = "") -> LLMClient:
    """按配置造客户端。没配 key 时返回一个"不可用"客户端而不是抛异常。"""
    cfg = config or LLMConfig.from_env()
    kind = (kind or cfg.provider or "openai").strip().lower()
    if kind in ("echo", "offline", "mock"):
        return EchoClient(cfg)
    if kind in ("ollama", "local"):
        c = OllamaClient(cfg)
        return c if c.available() else _Unavailable(c.problem())
    client = HttpChatClient(cfg)
    if not client.available():
        return _Unavailable(client.problem())
    return client


def _slice_json(text: str) -> str:
    """从夹杂解释文字的回复里切出第一个完整 JSON 数组/对象。"""
    for opener, closer in (("[", "]"), ("{", "}")):
        i, j = text.find(opener), text.rfind(closer)
        if 0 <= i < j:
            return text[i:j + 1]
    return ""


def _coerce_pairs(data, count: int) -> Dict[int, str]:
    """把各种合理形状（[{i,n}] / [[i,name]] / {name: new} / [name, ...]）统一成 {序号: 名字}。"""
    out: Dict[int, str] = {}
    if isinstance(data, dict):
        # {"1": "名字"} 或 {"items": [...]}
        if "items" in data and isinstance(data["items"], list):
            return _coerce_pairs(data["items"], count)
        for k, v in data.items():
            try:
                idx = int(k)
            except (TypeError, ValueError):
                continue
            if isinstance(v, str):
                out[idx] = v
        return out if out else {}
    if not isinstance(data, list):
        return {}
    for pos, item in enumerate(data, 1):
        if isinstance(item, str):
            out[pos] = item
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                out[int(item[0])] = str(item[1])
            except (TypeError, ValueError):
                out[pos] = str(item[1])
        elif isinstance(item, dict):
            idx = None
            for key in ("i", "index", "id", "no", "idx"):
                if key in item:
                    try:
                        idx = int(item[key])
                        break
                    except (TypeError, ValueError):
                        pass
            val = None
            for key in ("n", "name", "new", "new_name", "result", "output", "value"):
                if key in item and isinstance(item[key], str):
                    val = item[key]
                    break
            if val is not None:
                out[idx if idx else pos] = val
    return out


__all__ = ["DEFAULT_INSTRUCTION", "DEFAULT_LANG", "DEFAULT_MAX_LEN", "STYLES",
           "DEFAULT_STYLE", "LLMConfig", "LLMClient", "HttpChatClient",
           "OllamaClient", "EchoClient", "SuggestResult", "make_client",
           "build_prompt", "sanitize_name"]
