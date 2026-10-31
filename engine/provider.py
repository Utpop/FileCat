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
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import appdata_dir
from . import secrets

CONFIG_NAME = "llm_provider.json"

# --------------------------------------------------------------------------- #
# 协议
# --------------------------------------------------------------------------- #

PROTOCOL_CHAT = "openai_chat"
PROTOCOL_RESPONSES = "openai_responses"
PROTOCOL_ANTHROPIC = "anthropic_messages"

PROTOCOLS: List[Tuple[str, str]] = [
    (PROTOCOL_CHAT, "OpenAI Chat Completions"),
    (PROTOCOL_RESPONSES, "OpenAI Responses"),
    (PROTOCOL_ANTHROPIC, "Anthropic Messages"),
]
PROTOCOL_TEXT = dict(PROTOCOLS)
DEFAULT_PROTOCOL = PROTOCOL_CHAT


# --------------------------------------------------------------------------- #
# 供应商预设
# --------------------------------------------------------------------------- #

@dataclass
class ProviderPreset:
    key: str
    label: str
    base_url: str = ""
    models_url: str = ""
    protocol: str = PROTOCOL_CHAT
    kind: str = "openai"          # openai（HTTP 兼容）/ ollama / echo（离线替身）
    note: str = ""


PROVIDER_PRESETS: List[ProviderPreset] = [
    ProviderPreset("custom", "自定义供应商", "", "", PROTOCOL_CHAT, "openai",
                   "地址、密钥、模型全部自己填"),
    ProviderPreset("openai", "OpenAI", "https://api.openai.com/v1",
                   "https://api.openai.com/v1/models", PROTOCOL_CHAT, "openai",
                   "官方接口"),
    ProviderPreset("deepseek", "DeepSeek", "https://api.deepseek.com",
                   "https://api.deepseek.com/models", PROTOCOL_CHAT, "openai",
                   "OpenAI 兼容，无需 /v1 前缀"),
    ProviderPreset("xiaomi_mimo", "Xiaomi MiMo", "https://api.xiaomimimo.com/v1",
                   "https://api.xiaomimimo.com/v1/models", PROTOCOL_CHAT, "openai",
                   "小米开放平台，OpenAI 兼容"),
    ProviderPreset("opencode_go", "OpenCode Go", "https://opencode.ai/zen/go/v1",
                   "https://opencode.ai/zen/go/v1/models", PROTOCOL_CHAT, "openai",
                   "订阅制，OpenAI 兼容"),
    ProviderPreset("ollama", "本地 Ollama", "http://127.0.0.1:11434",
                   "http://127.0.0.1:11434/v1/models", PROTOCOL_CHAT, "ollama",
                   "本地模型，不需要密钥"),
    ProviderPreset("echo", "离线替身", "", "", PROTOCOL_CHAT, "echo",
                   "不联网的机械精简，用来验证流程"),
]
PRESET_BY_KEY = {p.key: p for p in PROVIDER_PRESETS}
DEFAULT_PRESET = "custom"

# --------------------------------------------------------------------------- #
# 默认值
# --------------------------------------------------------------------------- #

DEFAULT_NAME = "OpenAI Compatible"
DEFAULT_BASE_URL = ""          # 空 = 没填（界面里显示为空，等用户自己填）
DEFAULT_MODEL = ""             # 空 = 没选
DEFAULT_TIMEOUT = 60
DEFAULT_BATCH = 20
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 1024


def config_path() -> str:
    return appdata_dir(CONFIG_NAME)


# --------------------------------------------------------------------------- #
# 连接状态（界面左下角那个气泡显示的就是它）
# --------------------------------------------------------------------------- #

class ProviderStatus:
    """记录供应商的连接状态与实测延迟。

    界面左下角的气泡要显示「连上了吗 / 用的哪个模型 / 平均多快」，
    这三样都得有真实来源：
        · 连上了吗 → 测试连接的结果，或真实请求的成败；
        · 哪个模型 → 当前生效配置里的模型名；
        · 多快     → 真实请求的耗时（不是猜的）。

    状态存在进程内存里（不落盘）：它是"此刻"的连接状况，
    下次开程序重新测一次更有意义。
    """

    # 平均延迟取最近这么多次成功请求的均值
    MAX_SAMPLES = 20

    def __init__(self):
        self._lock = threading.Lock()
        self.state = "unset"        # unset / unknown / ok / error
        self.message = ""
        self.latency_ms = 0
        self.model = ""
        self.checked_at = 0.0
        self._samples: List[int] = []

    def set_model(self, model: str):
        with self._lock:
            self.model = (model or "").strip()

    def report(self, ok: bool, message: str = "", latency_ms: int = 0):
        """记一次观测结果。ok=True 且给了耗时就进平均值的样本池。"""
        with self._lock:
            self.state = "ok" if ok else "error"
            self.message = message or ""
            self.latency_ms = int(latency_ms or 0)
            self.checked_at = time.time()
            if ok and self.latency_ms > 0:
                self._samples.append(self.latency_ms)
                if len(self._samples) > self.MAX_SAMPLES:
                    del self._samples[:-self.MAX_SAMPLES]

    def clear(self):
        with self._lock:
            self.state = "unset"
            self.message = ""
            self.latency_ms = 0
            self.checked_at = 0.0
            self._samples = []

    def mark_unknown(self, model: str = ""):
        """配置变了但还没测 —— 状态回到"未知"，别拿旧的测试结果说事。"""
        with self._lock:
            if model:
                self.model = model.strip()
            if self.state in ("ok", "error"):
                self.state = "unknown"
                self.message = ""
                self.latency_ms = 0
                self.checked_at = 0.0
                self._samples = []

    def average_ms(self) -> int:
        """最近若干次成功请求的平均耗时（毫秒）；没有样本返回 0。"""
        with self._lock:
            if not self._samples:
                return 0
            return int(sum(self._samples) / len(self._samples))

    def snapshot(self) -> dict:
        with self._lock:
            avg = int(sum(self._samples) / len(self._samples)) if self._samples else 0
            return {"state": self.state, "message": self.message,
                    "latency_ms": self.latency_ms, "average_ms": avg,
                    "model": self.model, "checked_at": self.checked_at}


# 全局单例：引擎与界面共用一份
STATUS = ProviderStatus()


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

@dataclass
class ProviderConfig:
    """供应商配置。api_key 不参与序列化（由 engine.secrets 单独保管）。"""

    name: str = DEFAULT_NAME
    protocol: str = DEFAULT_PROTOCOL
    preset: str = DEFAULT_PRESET
    kind: str = "openai"           # 客户端类型：openai / ollama / echo
    base_url: str = DEFAULT_BASE_URL
    models_url: str = ""
    model: str = DEFAULT_MODEL
    models: List[str] = field(default_factory=list)
    headers: Dict[str, str] = field(default_factory=dict)
    timeout: int = DEFAULT_TIMEOUT
    batch: int = DEFAULT_BATCH
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS
    api_key: str = ""          # 只在内存里，绝不写进 json

    # ---- 序列化 ----
    def as_dict(self) -> dict:
        """只序列化非敏感字段；api_key 永远不出现。"""
        return {
            "name": self.name,
            "protocol": self.protocol,
            "preset": self.preset,
            "kind": self.kind,
            "base_url": self.base_url,
            "models_url": self.models_url,
            "model": self.model,
            "models": list(self.models),
            "headers": dict(self.headers),
            "timeout": int(self.timeout),
            "batch": int(self.batch),
            "temperature": float(self.temperature),
            "max_tokens": int(self.max_tokens),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProviderConfig":
        data = data if isinstance(data, dict) else {}
        cfg = cls()
        for key in ("name", "protocol", "preset", "kind", "base_url", "models_url", "model"):
            val = data.get(key)
            if isinstance(val, str):
                setattr(cfg, key, val)
        models = data.get("models")
        if isinstance(models, list):
            cfg.models = [str(m) for m in models if str(m).strip()]
        headers = data.get("headers")
        if isinstance(headers, dict):
            cfg.headers = {str(k): str(v) for k, v in headers.items()
                           if str(k).strip()}
        for key, cast in (("timeout", int), ("batch", int), ("max_tokens", int)):
            try:
                val = data.get(key)
                if val is not None:
                    setattr(cfg, key, cast(val))
            except (TypeError, ValueError):
                pass
        try:
            val = data.get("temperature")
            if val is not None:
                cfg.temperature = float(val)
        except (TypeError, ValueError):
            pass
        if cfg.protocol not in PROTOCOL_TEXT:
            cfg.protocol = DEFAULT_PROTOCOL
        return cfg

    # ---- 展示 ----
    def has_key(self) -> bool:
        return bool(self.api_key)

    def protocol_text(self) -> str:
        return PROTOCOL_TEXT.get(self.protocol, self.protocol)

    def problem(self) -> str:
        """返回一句"为什么用不了"，空串表示可以。"""
        if self.kind == "echo":
            return ""                    # 离线替身不需要任何东西
        if not self.base_url:
            return "还没填基础 URL"
        if self.kind == "ollama":
            return ""                    # 本地模型不需要密钥
        if not self.api_key:
            return "还没填 API Key"
        if not self.model:
            return "还没选模型"
        return ""


# --------------------------------------------------------------------------- #
# 读 / 写
# --------------------------------------------------------------------------- #

def load_config() -> ProviderConfig:
    """读配置；文件缺失或损坏时返回带默认值的配置，绝不抛异常。"""
    path = config_path()
    data = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}
    cfg = ProviderConfig.from_dict(data)
    cfg.api_key = secrets.load_secret()
    return cfg


def save_config(cfg: ProviderConfig, api_key: Optional[str] = None) -> str:
    """写配置。

    api_key 传 None = 不动已保存的密钥；传空串 = 清除密钥；传非空 = 覆盖。
    返回配置文件路径。
    """
    path = config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(cfg.as_dict(), fh, ensure_ascii=False, indent=2)
    except OSError:
        return ""
    if api_key is not None:
        if api_key:
            secrets.save_secret(api_key)
        else:
            secrets.clear_secret()
    return path


def apply_preset(cfg: ProviderConfig, preset_key: str) -> ProviderConfig:
    """套用供应商预设：填好地址与模型列表 URL；密钥不动。"""
    preset = PRESET_BY_KEY.get(preset_key or "")
    if preset is None:
        return cfg
    cfg.preset = preset.key
    if preset.key != DEFAULT_PRESET:
        cfg.base_url = preset.base_url
        cfg.models_url = preset.models_url
        cfg.protocol = preset.protocol
        cfg.kind = preset.kind
        if not cfg.name or cfg.name == DEFAULT_NAME:
            cfg.name = preset.label
    return cfg


def env_config() -> ProviderConfig:
    """从环境变量拼一份配置（界面上从没保存过时用它兜底）。

    只认**真正设过**的环境变量，不搬引擎里的内置默认值 —— 否则刚装好的
    界面上会凭空冒出"DeepSeek 地址"，而用户其实什么都没配。
    """
    from . import llm as LLM            # 延迟导入，避免两个模块互相牵扯

    env = LLM.LLMConfig.from_env()
    cfg = ProviderConfig()
    raw = os.environ

    def pick(*names):
        for name in names:
            val = raw.get(name)
            if val:
                return val
        return ""

    cfg.base_url = pick("NAMECAT_LLM_BASE_URL", "NAMEKIT_LLM_BASE_URL",
                        "OPENAI_BASE_URL", "DEEPSEEK_BASE_URL")
    cfg.api_key = pick("NAMECAT_LLM_API_KEY", "NAMEKIT_LLM_API_KEY",
                       "OPENAI_API_KEY", "DEEPSEEK_API_KEY")
    cfg.model = pick("NAMECAT_LLM_MODEL", "NAMEKIT_LLM_MODEL",
                     "OPENAI_MODEL", "DEEPSEEK_MODEL")
    cfg.protocol = pick("NAMECAT_LLM_PROTOCOL", "NAMEKIT_LLM_PROTOCOL") \
        or env.protocol
    raw_headers = pick("NAMECAT_LLM_HEADERS", "NAMEKIT_LLM_HEADERS")
    if raw_headers:
        try:
            loaded = json.loads(raw_headers)
        except (ValueError, TypeError):
            loaded = None
        if isinstance(loaded, dict):
            cfg.headers = {str(k): str(v) for k, v in loaded.items() if str(k).strip()}
    provider = pick("NAMECAT_LLM_PROVIDER", "NAMEKIT_LLM_PROVIDER", "LLM_PROVIDER")
    if provider in ("openai", "ollama", "echo"):
        cfg.kind = provider
    elif cfg.base_url and "11434" in cfg.base_url:
        cfg.kind = "ollama"
    return cfg


def effective_provider_config() -> ProviderConfig:
    """界面上要显示的那一份。

    保存过配置 → 以保存的为准（连空值也是用户的本意）；
    从没保存过 → 用环境变量兜底，老用户看到的还是熟悉的那套。
    密钥同理：本地没存过就用环境变量里的。
    """
    cfg = load_config()
    if not os.path.isfile(config_path()):
        return env_config()
    if not cfg.api_key:
        env = env_config()
        cfg.api_key = env.api_key
    return cfg


def effective_config() -> "LLMConfig":
    """最终生效的接入配置。

    优先级：界面上保存过的供应商配置 > 环境变量 > 内置默认值。
    界面上保存过一次之后，就完全以那份配置为准；一次都没保存过则走环境变量，
    老用户的行为一字不变。
    """
    from . import llm as LLM            # 延迟导入，避免两个模块互相牵扯

    cfg = LLM.LLMConfig.from_env()
    if not os.path.isfile(config_path()):
        return cfg                      # 从没保存过 → 纯环境变量
    pc = load_config()
    cfg.base_url = pc.base_url
    cfg.api_key = pc.api_key
    cfg.model = pc.model
    cfg.protocol = pc.protocol
    cfg.headers = dict(pc.headers or {})
    cfg.timeout = int(pc.timeout)
    cfg.batch = int(pc.batch)
    cfg.temperature = float(pc.temperature)
    cfg.max_tokens = int(pc.max_tokens)
    cfg.provider = pc.kind or "openai"
    return cfg


# --------------------------------------------------------------------------- #
# HTTP 底座
# --------------------------------------------------------------------------- #

# 两个 opener 只在导入时建一次：建 opener 会读环境变量（代理设置），
# 每次请求都建一遍既慢又可能读到已被改动的环境。
_DEFAULT_OPENER = urllib.request.build_opener()
_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")


def opener_for(url: str):
    """按目标地址挑 opener：回环地址一律**绕过系统代理**。

    本机装了代理（Clash / 企业代理之类）时，`HTTP(S)_PROXY` 会让发往
    127.0.0.1 的请求也交给代理，于是本地 Ollama 的失败原因变成代理的
    "网关错误 502 · upstream connect failed"，既连不上也看不懂。
    engine.llm 的直接请求也走这里，保证两处口径一致。
    """
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    if host in _LOOPBACK_HOSTS or host.endswith(".localhost"):
        return _LOOPBACK_OPENER
    return _DEFAULT_OPENER


def open_url(req, timeout: int):
    """发请求（回环不代理）。engine.llm 用它，避免两套逻辑各写一遍。"""
    return opener_for(req.full_url).open(req, timeout=timeout)


def _headers(cfg: ProviderConfig, with_auth: bool = True) -> Dict[str, str]:
    head = {"Content-Type": "application/json",
            "Accept": "application/json"}
    if with_auth and cfg.api_key:
        if cfg.protocol == PROTOCOL_ANTHROPIC:
            head["x-api-key"] = cfg.api_key
            head["anthropic-version"] = "2023-06-01"
        else:
            head["Authorization"] = "Bearer %s" % cfg.api_key
    for key, value in (cfg.headers or {}).items():
        if str(key).strip():
            head[str(key).strip()] = str(value)
    return head


def _request(method: str, url: str, payload: Optional[dict],
             headers: Dict[str, str], timeout: int) -> Tuple[int, str, str]:
    """发一个请求。返回 (状态码, 正文, 错误原因)；错误原因为空串表示成功。"""
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with opener_for(url).open(req, timeout=timeout) as fh:
            body = fh.read().decode("utf-8", "replace")
            return fh.status, body, ""
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return exc.code, body, _http_reason(exc.code, body)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return 0, "", "连不上服务器：%s" % (reason,)
    except TimeoutError:
        return 0, "", "超时（%ds）" % int(timeout)
    except OSError as exc:
        return 0, "", "网络错误：%s" % (exc,)


def _http_reason(code: int, body: str) -> str:
    """把状态码翻译成人话，并顺手带上服务端给的错误信息。"""
    detail = ""
    if body:
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                detail = str(err.get("message") or "")
            elif isinstance(err, str):
                detail = err
            if not detail:
                detail = str(data.get("message") or "")
        if not detail:
            detail = body[:180].replace("\n", " ")
    base = {
        400: "请求被拒绝（参数或权限不对）",
        401: "API Key 无效或已过期",
        403: "没有权限访问该模型",
        404: "地址不对（404）：检查基础 URL 与模型名",
        408: "服务端超时",
        413: "请求内容过大",
        422: "参数不被接受",
        429: "触发限流或额度用尽",
        500: "服务端内部错误",
        502: "网关错误，服务端暂时不可用",
        503: "服务暂时不可用",
    }.get(code, "请求失败")
    return "%s（HTTP %d）%s" % (base, code, ("：" + detail) if detail else "")


# --------------------------------------------------------------------------- #
# 模型列表
# --------------------------------------------------------------------------- #

def _parse_models(body: str) -> List[str]:
    """从模型列表响应里抠出模型名（兼容几种常见形状）。"""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return []
    items = []
    if isinstance(data, dict):
        for key in ("data", "models", "result", "items"):
            val = data.get(key)
            if isinstance(val, list):
                items = val
                break
    elif isinstance(data, list):
        items = data
    names: List[str] = []
    for item in items:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = str(item.get("id") or item.get("name") or
                        item.get("model") or item.get("model_id") or "")
        else:
            name = ""
        name = name.strip()
        if name and name not in names:
            names.append(name)
    return names


def models_url_for(cfg: ProviderConfig) -> str:
    """模型列表地址：填了就用填的；没填按基础 URL 推一个。"""
    url = (cfg.models_url or "").strip()
    if url:
        return url
    base = (cfg.base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if re.search(r"/v\d+$", base):
        return base + "/models"
    return base + "/v1/models"


def list_models(cfg: ProviderConfig) -> Tuple[List[str], str]:
    """拉取模型列表。返回 (模型名列表, 错误原因)；成功时错误原因为空串。"""
    if cfg.kind == "echo":
        return [], "离线替身不联网，没有模型列表"
    url = models_url_for(cfg)
    if not url:
        return [], "还没填基础 URL"
    code, body, err = _request("GET", url, None, _headers(cfg), cfg.timeout)
    if err:
        return [], err
    names = _parse_models(body)
    if not names:
        return [], "模型列表为空或格式不认（%s）" % url
    return names, ""


# --------------------------------------------------------------------------- #
# 连通性测试
# --------------------------------------------------------------------------- #

def _chat_endpoint(cfg: ProviderConfig) -> str:
    base = (cfg.base_url or "").strip().rstrip("/")
    if cfg.kind == "ollama":
        if base.endswith("/api/chat"):
            return base
        return base + "/api/chat"
    if cfg.protocol == PROTOCOL_ANTHROPIC:
        tail = "/messages"
    elif cfg.protocol == PROTOCOL_RESPONSES:
        tail = "/responses"
    else:
        tail = "/chat/completions"
    if base.endswith(tail):
        return base
    if re.search(r"/v\d+$", base):
        return base + tail
    return base + "/v1" + tail


def _probe_payload(cfg: ProviderConfig) -> dict:
    """构造一个最小的探测请求（三种协议各自不同）。"""
    if cfg.kind == "ollama":
        return {"model": cfg.model, "stream": False,
                "messages": [{"role": "user", "content": "ping"}]}
    if cfg.protocol == PROTOCOL_ANTHROPIC:
        return {"model": cfg.model, "max_tokens": 16,
                "messages": [{"role": "user", "content": "ping"}]}
    if cfg.protocol == PROTOCOL_RESPONSES:
        return {"model": cfg.model, "max_tokens": 16,
                "input": "ping"}
    return {"model": cfg.model, "max_tokens": 16,
            "messages": [{"role": "user", "content": "ping"}]}


def _probe_ok(cfg: ProviderConfig, body: str) -> bool:
    """只看"服务端认了这个请求"，不要求模型真回答了什么。"""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return bool(body.strip())
    if not isinstance(data, dict):
        return False
    if "error" in data and data["error"]:
        return False
    if cfg.kind == "ollama":
        return bool(data.get("message") or data.get("response"))
    if cfg.protocol == PROTOCOL_ANTHROPIC:
        return isinstance(data.get("content"), list)
    if cfg.protocol == PROTOCOL_RESPONSES:
        return bool(data.get("output") or data.get("output_text"))
    return isinstance(data.get("choices"), list)


def test_connection(cfg: ProviderConfig) -> dict:
    """真发一次请求，测这个供应商能不能用。

    顺序：先拉模型列表（便宜、顺带验证地址与密钥），失败再退回最小对话请求
    （有些服务不开放模型列表，但对话是通的）。
    返回 {"ok": bool, "message": str, "latency_ms": int, "detail": str}。
    """
    result = {"ok": False, "message": "", "latency_ms": 0, "detail": ""}
    if cfg.kind == "echo":
        result["ok"] = True
        result["message"] = "离线替身不联网，永远可用"
        STATUS.report(True, result["message"], 0)
        return result
    problem = cfg.problem()
    if problem:
        result["message"] = problem
        STATUS.report(False, problem, 0)
        return result

    start = time.time()
    names, err = list_models(cfg)
    elapsed = int((time.time() - start) * 1000)
    if not err:
        result["ok"] = True
        result["latency_ms"] = elapsed
        result["message"] = "连接成功 · 拉到 %d 个模型" % len(names)
        result["detail"] = names[:8]
        STATUS.set_model(cfg.model)
        STATUS.report(True, result["message"], elapsed)
        return result

    # 模型列表不通，再试最小对话（部分服务不开放列表接口）
    url = _chat_endpoint(cfg)
    start = time.time()
    code, body, err2 = _request("POST", url, _probe_payload(cfg),
                                _headers(cfg), cfg.timeout)
    elapsed = int((time.time() - start) * 1000)
    if not err2 and _probe_ok(cfg, body):
        result["ok"] = True
        result["latency_ms"] = elapsed
        result["message"] = "连接成功 · 对话接口可用（模型列表不可用）"
        result["detail"] = "模型列表：%s" % err
        STATUS.set_model(cfg.model)
        STATUS.report(True, result["message"], elapsed)
        return result
    result["latency_ms"] = elapsed
    result["message"] = err2 or err
    result["detail"] = "模型列表：%s" % err
    STATUS.set_model(cfg.model)
    STATUS.report(False, result["message"], elapsed)
    return result


__all__ = ["CONFIG_NAME", "PROTOCOL_CHAT", "PROTOCOL_RESPONSES", "PROTOCOL_ANTHROPIC",
           "PROTOCOLS", "PROTOCOL_TEXT", "DEFAULT_PROTOCOL", "ProviderPreset",
           "PROVIDER_PRESETS", "PRESET_BY_KEY", "DEFAULT_PRESET", "ProviderConfig",
           "config_path", "load_config", "save_config", "apply_preset", "list_models",
           "models_url_for", "test_connection", "env_config", "effective_provider_config",
           "effective_config"]
