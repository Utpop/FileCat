//! 供应商配置（OpenAI 兼容接口为主）+ 连通性测试（对应 `engine/provider.py`）。
//!
//! 定位：这一层只负责"怎么连"（地址、密钥、请求头、协议），"连上之后叫什么
//! 名字"在 [`crate::llm`]。
//!
//! 三件事
//! 1. 预设：常见供应商一键填好地址与模型列表 URL；「自定义供应商」全手填。
//! 2. 落盘：普通配置存 `llm_provider.json`（可分享），**密钥单独存**
//!    （[`crate::secrets`]）——两者永远不混在一起（[`Config::as_dict`] 里没有
//!    `api_key` 这个键，这是 F5 断言钉住的）。
//! 3. 连通性测试：真发一个请求（优先拉模型列表，失败再退回最小对话）。

use crate::http::Transport;
use crate::llm;
use crate::secrets;
use serde_json::{Map, Value};

pub const CONFIG_NAME: &str = "llm_provider.json";

// --------------------------------------------------------------------------- //
// 协议
// --------------------------------------------------------------------------- //

pub use crate::llm::{PROTOCOL_ANTHROPIC, PROTOCOL_CHAT, PROTOCOL_RESPONSES};

pub const PROTOCOLS: &[(&str, &str)] = &[
    (PROTOCOL_CHAT, "OpenAI Chat Completions"),
    (PROTOCOL_RESPONSES, "OpenAI Responses"),
    (PROTOCOL_ANTHROPIC, "Anthropic Messages"),
];

pub fn protocol_text(protocol: &str) -> Option<&'static str> {
    PROTOCOLS
        .iter()
        .find(|(key, _)| *key == protocol)
        .map(|(_, text)| *text)
}

pub const DEFAULT_PROTOCOL: &str = PROTOCOL_CHAT;

// --------------------------------------------------------------------------- //
// 供应商预设
// --------------------------------------------------------------------------- //

pub struct ProviderPreset {
    pub key: &'static str,
    pub label: &'static str,
    pub base_url: &'static str,
    pub models_url: &'static str,
    pub protocol: &'static str,
    /// openai（HTTP 兼容）/ ollama / echo（离线替身）
    pub kind: &'static str,
    pub note: &'static str,
}

pub const PROVIDER_PRESETS: &[ProviderPreset] = &[
    ProviderPreset {
        key: "custom",
        label: "自定义供应商",
        base_url: "",
        models_url: "",
        protocol: PROTOCOL_CHAT,
        kind: "openai",
        note: "地址、密钥、模型全部自己填",
    },
    ProviderPreset {
        key: "openai",
        label: "OpenAI",
        base_url: "https://api.openai.com/v1",
        models_url: "https://api.openai.com/v1/models",
        protocol: PROTOCOL_CHAT,
        kind: "openai",
        note: "官方接口",
    },
    ProviderPreset {
        key: "deepseek",
        label: "DeepSeek",
        base_url: "https://api.deepseek.com",
        models_url: "https://api.deepseek.com/models",
        protocol: PROTOCOL_CHAT,
        kind: "openai",
        note: "OpenAI 兼容，无需 /v1 前缀",
    },
    ProviderPreset {
        key: "xiaomi_mimo",
        label: "Xiaomi MiMo",
        base_url: "https://api.xiaomimimo.com/v1",
        models_url: "https://api.xiaomimimo.com/v1/models",
        protocol: PROTOCOL_CHAT,
        kind: "openai",
        note: "小米开放平台，OpenAI 兼容",
    },
    ProviderPreset {
        key: "opencode_go",
        label: "OpenCode Go",
        base_url: "https://opencode.ai/zen/go/v1",
        models_url: "https://opencode.ai/zen/go/v1/models",
        protocol: PROTOCOL_CHAT,
        kind: "openai",
        note: "订阅制，OpenAI 兼容",
    },
    ProviderPreset {
        key: "ollama",
        label: "本地 Ollama",
        base_url: "http://127.0.0.1:11434",
        models_url: "http://127.0.0.1:11434/v1/models",
        protocol: PROTOCOL_CHAT,
        kind: "ollama",
        note: "本地模型，不需要密钥",
    },
    ProviderPreset {
        key: "echo",
        label: "离线替身",
        base_url: "",
        models_url: "",
        protocol: PROTOCOL_CHAT,
        kind: "echo",
        note: "不联网的机械精简，用来验证流程",
    },
];

pub fn preset_by_key(key: &str) -> Option<&'static ProviderPreset> {
    PROVIDER_PRESETS.iter().find(|p| p.key == key)
}

pub const DEFAULT_PRESET: &str = "custom";

// --------------------------------------------------------------------------- //
// 默认值
// --------------------------------------------------------------------------- //

pub const DEFAULT_NAME: &str = "OpenAI Compatible";
pub const DEFAULT_TIMEOUT: i64 = 60;
pub const DEFAULT_BATCH: i64 = 20;
pub const DEFAULT_TEMPERATURE: f64 = 0.2;
pub const DEFAULT_MAX_TOKENS: i64 = 1024;

// --------------------------------------------------------------------------- //
// 配置
// --------------------------------------------------------------------------- //

/// 供应商配置。`api_key` 只活在内存里，[`Config::as_dict`] 永远不带它。
#[derive(Debug, Clone, PartialEq)]
pub struct Config {
    pub name: String,
    pub protocol: String,
    pub preset: String,
    pub kind: String,
    pub base_url: String,
    pub models_url: String,
    pub model: String,
    pub models: Vec<String>,
    pub headers: Vec<(String, String)>,
    pub timeout: i64,
    pub batch: i64,
    pub temperature: f64,
    pub max_tokens: i64,
    pub api_key: String,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            name: DEFAULT_NAME.to_string(),
            protocol: DEFAULT_PROTOCOL.to_string(),
            preset: DEFAULT_PRESET.to_string(),
            kind: "openai".to_string(),
            base_url: String::new(),
            models_url: String::new(),
            model: String::new(),
            models: Vec::new(),
            headers: Vec::new(),
            timeout: DEFAULT_TIMEOUT,
            batch: DEFAULT_BATCH,
            temperature: DEFAULT_TEMPERATURE,
            max_tokens: DEFAULT_MAX_TOKENS,
            api_key: String::new(),
        }
    }
}

impl Config {
    /// 只序列化非敏感字段；api_key 永远不出现。
    pub fn as_dict(&self) -> Value {
        let mut map = Map::new();
        map.insert("name".into(), Value::String(self.name.clone()));
        map.insert("protocol".into(), Value::String(self.protocol.clone()));
        map.insert("preset".into(), Value::String(self.preset.clone()));
        map.insert("kind".into(), Value::String(self.kind.clone()));
        map.insert("base_url".into(), Value::String(self.base_url.clone()));
        map.insert("models_url".into(), Value::String(self.models_url.clone()));
        map.insert("model".into(), Value::String(self.model.clone()));
        map.insert(
            "models".into(),
            Value::Array(self.models.iter().cloned().map(Value::String).collect()),
        );
        let mut headers = Map::new();
        for (key, value) in &self.headers {
            headers.insert(key.clone(), Value::String(value.clone()));
        }
        map.insert("headers".into(), Value::Object(headers));
        map.insert("timeout".into(), Value::from(self.timeout));
        map.insert("batch".into(), Value::from(self.batch));
        map.insert(
            "temperature".into(),
            serde_json::Number::from_f64(self.temperature)
                .map(Value::Number)
                .unwrap_or(Value::Null),
        );
        map.insert("max_tokens".into(), Value::from(self.max_tokens));
        Value::Object(map)
    }

    /// 脏 JSON 兜底：认不出的字段一律保留默认值，绝不抛异常。
    pub fn from_dict(data: &Value) -> Self {
        let mut cfg = Self::default();
        let Some(map) = data.as_object() else {
            return cfg;
        };
        for key in [
            "name",
            "protocol",
            "preset",
            "kind",
            "base_url",
            "models_url",
            "model",
        ] {
            if let Some(Value::String(value)) = map.get(key) {
                cfg.set_text(key, value.clone());
            }
        }
        if let Some(Value::Array(items)) = map.get("models") {
            cfg.models = items
                .iter()
                .map(llm::json_text)
                .filter(|text| !text.trim().is_empty())
                .collect();
        }
        if let Some(Value::Object(headers)) = map.get("headers") {
            cfg.headers = headers
                .iter()
                .filter(|(key, _)| !key.trim().is_empty())
                .map(|(key, value)| (key.clone(), llm::json_text(value)))
                .collect();
        }
        for (key, field) in [("timeout", 0u8), ("batch", 1u8), ("max_tokens", 2u8)] {
            let Some(value) = map.get(key) else { continue };
            if value.is_null() {
                continue;
            }
            let Some(parsed) = py_int(value) else { continue };
            match field {
                0 => cfg.timeout = parsed,
                1 => cfg.batch = parsed,
                _ => cfg.max_tokens = parsed,
            }
        }
        if let Some(value) = map.get("temperature") {
            if !value.is_null() {
                if let Some(v) = py_float(value) {
                    cfg.temperature = v;
                }
            }
        }
        if protocol_text(&cfg.protocol).is_none() {
            cfg.protocol = DEFAULT_PROTOCOL.to_string();
        }
        cfg
    }

    fn set_text(&mut self, key: &str, value: String) {
        match key {
            "name" => self.name = value,
            "protocol" => self.protocol = value,
            "preset" => self.preset = value,
            "kind" => self.kind = value,
            "base_url" => self.base_url = value,
            "models_url" => self.models_url = value,
            "model" => self.model = value,
            _ => {}
        }
    }

    /// 返回一句"为什么用不了"，空串表示可以。
    pub fn problem(&self) -> String {
        if self.kind == "echo" {
            return String::new(); // 离线替身不需要任何东西
        }
        if self.base_url.is_empty() {
            return "还没填基础 URL".to_string();
        }
        if self.kind == "ollama" {
            return String::new(); // 本地模型不需要密钥
        }
        if self.api_key.is_empty() {
            return "还没填 API Key".to_string();
        }
        if self.model.is_empty() {
            return "还没选模型".to_string();
        }
        String::new()
    }

    /// 转成引擎的接入配置（密钥原样带过去，只在内存里）。
    pub fn to_llm_config(&self) -> llm::Config {
        llm::Config {
            provider: if self.kind.is_empty() {
                "openai".to_string()
            } else {
                self.kind.clone()
            },
            base_url: self.base_url.clone(),
            api_key: self.api_key.clone(),
            model: self.model.clone(),
            timeout: self.timeout,
            batch: self.batch,
            temperature: self.temperature,
            protocol: self.protocol.clone(),
            headers: self.headers.clone(),
            max_tokens: self.max_tokens,
            ..llm::Config::default()
        }
    }
}

/// Python `int(value)` 的近似：数值截断、字符串去空白后解析、布尔当 0/1。
fn py_int(value: &Value) -> Option<i64> {
    match value {
        Value::Number(num) => num.as_i64().or_else(|| num.as_f64().map(|f| f.trunc() as i64)),
        Value::String(text) => text.trim().replace('_', "").parse::<i64>().ok(),
        Value::Bool(flag) => Some(i64::from(*flag)),
        _ => None,
    }
}

fn py_float(value: &Value) -> Option<f64> {
    match value {
        Value::Number(num) => num.as_f64(),
        Value::String(text) => text.trim().parse::<f64>().ok(),
        Value::Bool(flag) => Some(if *flag { 1.0 } else { 0.0 }),
        _ => None,
    }
}

// --------------------------------------------------------------------------- //
// 读 / 写
// --------------------------------------------------------------------------- //

/// 读配置；文件缺失或损坏时返回带默认值的配置，绝不抛异常。
///
/// 密钥由 [`crate::secrets`] 单独读；`secret_path` 传空串表示"这次不读密钥"。
pub fn load_config(config_path: &str, secret_path: &str) -> Config {
    let data = std::fs::read(config_path)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .unwrap_or(Value::Null);
    let mut cfg = Config::from_dict(&data);
    if !secret_path.is_empty() {
        cfg.api_key = secrets::load_secret(secret_path);
    }
    cfg
}

/// 写配置。
///
/// `api_key = None` → 不动已保存的密钥（界面"留空即沿用"走这条）；
/// `Some("")` → 清除密钥；`Some(key)` → 覆盖。返回配置文件路径（写失败返回空串）。
pub fn save_config(
    cfg: &Config,
    config_path: &str,
    api_key: Option<&str>,
    secret_path: &str,
) -> String {
    if let Some(parent) = std::path::Path::new(config_path).parent() {
        if !parent.as_os_str().is_empty() && std::fs::create_dir_all(parent).is_err() {
            return String::new();
        }
    }
    // 注意：Python 版是 `newline="\n"` 且**不**补尾部换行，这里照抄
    let body = serde_json::to_string_pretty(&cfg.as_dict()).unwrap_or_default();
    if std::fs::write(config_path, body.as_bytes()).is_err() {
        return String::new();
    }
    match api_key {
        Some("") => {
            secrets::clear_secret(secret_path);
        }
        Some(key) => {
            secrets::save_secret(secret_path, key);
        }
        None => {}
    }
    config_path.to_string()
}

/// 套用供应商预设：填好地址与模型列表 URL；密钥不动。
pub fn apply_preset(cfg: &mut Config, preset_key: &str) {
    let Some(preset) = preset_by_key(preset_key) else {
        return;
    };
    cfg.preset = preset.key.to_string();
    if preset.key == DEFAULT_PRESET {
        return;
    }
    cfg.base_url = preset.base_url.to_string();
    cfg.models_url = preset.models_url.to_string();
    cfg.protocol = preset.protocol.to_string();
    cfg.kind = preset.kind.to_string();
    if cfg.name.is_empty() || cfg.name == DEFAULT_NAME {
        cfg.name = preset.label.to_string();
    }
}

fn pick(names: &[&str]) -> String {
    for name in names {
        if let Ok(value) = std::env::var(name) {
            if !value.is_empty() {
                return value;
            }
        }
    }
    String::new()
}

/// 从环境变量拼一份配置（界面上从没保存过时用它兜底）。
///
/// 只认**真正设过**的环境变量，不搬引擎里的内置默认值 —— 否则刚装好的界面上
/// 会凭空冒出"DeepSeek 地址"，而用户其实什么都没配。
pub fn env_config() -> Config {
    let mut cfg = Config::default();
    cfg.base_url = pick(&[
        "NAMECAT_LLM_BASE_URL",
        "NAMEKIT_LLM_BASE_URL",
        "OPENAI_BASE_URL",
        "DEEPSEEK_BASE_URL",
    ]);
    cfg.api_key = pick(&[
        "NAMECAT_LLM_API_KEY",
        "NAMEKIT_LLM_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
    ]);
    cfg.model = pick(&[
        "NAMECAT_LLM_MODEL",
        "NAMEKIT_LLM_MODEL",
        "OPENAI_MODEL",
        "DEEPSEEK_MODEL",
    ]);
    let protocol = pick(&["NAMECAT_LLM_PROTOCOL", "NAMEKIT_LLM_PROTOCOL"]);
    cfg.protocol = if protocol.is_empty() {
        llm::Config::from_env().protocol
    } else {
        protocol
    };
    let raw_headers = pick(&["NAMECAT_LLM_HEADERS", "NAMEKIT_LLM_HEADERS"]);
    if !raw_headers.is_empty() {
        if let Ok(Value::Object(map)) = serde_json::from_str::<Value>(&raw_headers) {
            cfg.headers = llm::header_pairs(&map);
        }
    }
    let provider = pick(&[
        "NAMECAT_LLM_PROVIDER",
        "NAMEKIT_LLM_PROVIDER",
        "LLM_PROVIDER",
    ]);
    if matches!(provider.as_str(), "openai" | "ollama" | "echo") {
        cfg.kind = provider;
    } else if !cfg.base_url.is_empty() && cfg.base_url.contains("11434") {
        cfg.kind = "ollama".to_string();
    }
    cfg
}

/// 界面上要显示的那一份：保存过就以保存的为准（连空值也是用户的本意），
/// 从没保存过则用环境变量兜底。密钥同理：本地没存过就用环境变量里的。
pub fn effective_provider_config(config_path: &str, secret_path: &str) -> Config {
    let mut cfg = load_config(config_path, secret_path);
    if !std::path::Path::new(config_path).is_file() {
        return env_config();
    }
    if cfg.api_key.is_empty() {
        cfg.api_key = env_config().api_key;
    }
    cfg
}

/// 最终生效的接入配置：界面保存过的 > 环境变量 > 内置默认值。
pub fn effective_config(config_path: &str, secret_path: &str) -> llm::Config {
    let mut cfg = llm::Config::from_env();
    if !std::path::Path::new(config_path).is_file() {
        return cfg;
    }
    let pc = load_config(config_path, secret_path);
    cfg.base_url = pc.base_url;
    cfg.api_key = pc.api_key;
    cfg.model = pc.model;
    cfg.protocol = pc.protocol;
    cfg.headers = pc.headers;
    cfg.timeout = pc.timeout;
    cfg.batch = pc.batch;
    cfg.temperature = pc.temperature;
    cfg.max_tokens = pc.max_tokens;
    cfg.provider = if pc.kind.is_empty() {
        "openai".to_string()
    } else {
        pc.kind
    };
    cfg
}

// --------------------------------------------------------------------------- //
// 模型列表
// --------------------------------------------------------------------------- //

/// 从模型列表响应里抠出模型名（兼容几种常见形状）。
pub fn parse_models(body: &str) -> Vec<String> {
    let Ok(data) = serde_json::from_str::<Value>(body) else {
        return Vec::new();
    };
    let items: Vec<Value> = match &data {
        Value::Object(map) => {
            let mut found = Vec::new();
            for key in ["data", "models", "result", "items"] {
                if let Some(Value::Array(values)) = map.get(key) {
                    found = values.clone();
                    break;
                }
            }
            found
        }
        Value::Array(values) => values.clone(),
        _ => Vec::new(),
    };
    let mut names: Vec<String> = Vec::new();
    for item in &items {
        let name = match item {
            Value::String(text) => text.clone(),
            Value::Object(obj) => {
                let mut picked = String::new();
                for key in ["id", "name", "model", "model_id"] {
                    match obj.get(key) {
                        Some(Value::String(text)) if !text.is_empty() => {
                            picked = text.clone();
                            break;
                        }
                        Some(other) if llm::truthy_json(other) => {
                            picked = llm::json_text(other);
                            break;
                        }
                        _ => {}
                    }
                }
                picked
            }
            _ => String::new(),
        };
        let name = name.trim().to_string();
        if !name.is_empty() && !names.contains(&name) {
            names.push(name);
        }
    }
    names
}

/// 模型列表地址：填了就用填的；没填按基础 URL 推一个。
pub fn models_url_for(cfg: &Config) -> String {
    let url = cfg.models_url.trim();
    if !url.is_empty() {
        return url.to_string();
    }
    let base = cfg.base_url.trim().trim_end_matches('/');
    if base.is_empty() {
        return String::new();
    }
    if llm::has_version_tail(base) {
        return format!("{base}/models");
    }
    format!("{base}/v1/models")
}

/// 拉取模型列表。返回 (模型名列表, 错误原因)；成功时错误原因为空串。
pub fn list_models(cfg: &Config, net: &dyn Transport) -> (Vec<String>, String) {
    if cfg.kind == "echo" {
        return (Vec::new(), "离线替身不联网，没有模型列表".to_string());
    }
    let url = models_url_for(cfg);
    if url.is_empty() {
        return (Vec::new(), "还没填基础 URL".to_string());
    }
    let response = net.request(
        "GET",
        &url,
        None,
        &provider_headers(cfg),
        cfg.timeout.max(0) as u64,
    );
    if !response.ok() {
        return (Vec::new(), response.error);
    }
    let names = parse_models(&response.body);
    if names.is_empty() {
        return (
            Vec::new(),
            format!("模型列表为空或格式不认（{url}）"),
        );
    }
    (names, String::new())
}

/// 供应商侧请求头（与 `engine.provider._headers` 同口径：没填 Key 就不发鉴权头）。
pub fn provider_headers(cfg: &Config) -> Vec<(String, String)> {
    let engine = cfg.to_llm_config();
    llm::headers_for(&engine, !cfg.api_key.is_empty())
}

// --------------------------------------------------------------------------- //
// 连通性测试
// --------------------------------------------------------------------------- //

/// 构造一个最小的探测请求（三种协议各自不同）。
pub fn probe_payload(cfg: &Config) -> Value {
    let mut map = Map::new();
    if cfg.kind == "ollama" {
        map.insert("model".into(), Value::String(cfg.model.clone()));
        map.insert("stream".into(), Value::Bool(false));
        map.insert(
            "messages".into(),
            Value::Array(vec![probe_message("ping")]),
        );
        return Value::Object(map);
    }
    map.insert("model".into(), Value::String(cfg.model.clone()));
    if cfg.protocol == PROTOCOL_ANTHROPIC {
        map.insert("max_tokens".into(), Value::from(16));
        map.insert(
            "messages".into(),
            Value::Array(vec![probe_message("ping")]),
        );
        return Value::Object(map);
    }
    if cfg.protocol == PROTOCOL_RESPONSES {
        map.insert("max_tokens".into(), Value::from(16));
        map.insert("input".into(), Value::String("ping".into()));
        return Value::Object(map);
    }
    map.insert("max_tokens".into(), Value::from(16));
    map.insert(
        "messages".into(),
        Value::Array(vec![probe_message("ping")]),
    );
    Value::Object(map)
}

fn probe_message(content: &str) -> Value {
    let mut map = Map::new();
    map.insert("role".into(), Value::String("user".into()));
    map.insert("content".into(), Value::String(content.to_string()));
    Value::Object(map)
}

/// 只看"服务端认了这个请求"，不要求模型真回答了什么。
pub fn probe_ok(cfg: &Config, body: &str) -> bool {
    let Ok(data) = serde_json::from_str::<Value>(body) else {
        return !body.trim().is_empty();
    };
    let Some(map) = data.as_object() else {
        return false;
    };
    if let Some(error) = map.get("error") {
        if llm::truthy_json(error) {
            return false;
        }
    }
    if cfg.kind == "ollama" {
        return map
            .get("message")
            .map(llm::truthy_json)
            .unwrap_or(false)
            || map.get("response").map(llm::truthy_json).unwrap_or(false);
    }
    if cfg.protocol == PROTOCOL_ANTHROPIC {
        return matches!(map.get("content"), Some(Value::Array(_)));
    }
    if cfg.protocol == PROTOCOL_RESPONSES {
        return map.get("output").map(llm::truthy_json).unwrap_or(false)
            || map.get("output_text").map(llm::truthy_json).unwrap_or(false);
    }
    matches!(map.get("choices"), Some(Value::Array(_)))
}

/// 对话端点（探测用；ollama 走 `/api/chat`）。
pub fn chat_endpoint(cfg: &Config) -> String {
    let base = cfg.base_url.trim().trim_end_matches('/').to_string();
    if cfg.kind == "ollama" {
        if base.ends_with("/api/chat") {
            return base;
        }
        return format!("{base}/api/chat");
    }
    let mut engine = cfg.to_llm_config();
    engine.base_url = base;
    llm::chat_endpoint(&engine)
}

/// `detail` 的两种形态（Python 版这里一会儿是字符串、一会儿是列表）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Detail {
    Text(String),
    List(Vec<String>),
}

impl Detail {
    pub fn text(&self) -> String {
        match self {
            Detail::Text(text) => text.clone(),
            Detail::List(items) => py_repr_list(items),
        }
    }
}

/// 一次连通性测试的结果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TestResult {
    pub ok: bool,
    pub message: String,
    pub latency_ms: i64,
    pub detail: Detail,
    /// 是否要把模型名记进连接状态（只有"真的连上了"的两个分支为 true）
    pub set_model: bool,
    /// 记进状态气泡的消息（与 `message` 相同，单独列出来是为了对齐三个分支）
    pub report_message: String,
}

/// 真发一次请求，测这个供应商能不能用。
///
/// 顺序：先拉模型列表（便宜、顺带验证地址与密钥），失败再退回最小对话请求
/// （有些服务不开放模型列表，但对话是通的）。
pub fn test_connection(cfg: &Config, net: &dyn Transport) -> TestResult {
    let mut result = TestResult {
        ok: false,
        message: String::new(),
        latency_ms: 0,
        detail: Detail::Text(String::new()),
        set_model: false,
        report_message: String::new(),
    };
    if cfg.kind == "echo" {
        result.ok = true;
        result.message = "离线替身不联网，永远可用".to_string();
        result.report_message = result.message.clone();
        return result;
    }
    let problem = cfg.problem();
    if !problem.is_empty() {
        result.message = problem.clone();
        result.report_message = problem;
        return result;
    }

    let started = std::time::Instant::now();
    let (names, err) = list_models(cfg, net);
    let elapsed = started.elapsed().as_millis() as i64;
    if err.is_empty() {
        result.ok = true;
        result.latency_ms = elapsed;
        result.message = format!("连接成功 · 拉到 {} 个模型", names.len());
        result.detail = Detail::List(names[..names.len().min(8)].to_vec());
        result.set_model = true;
        result.report_message = result.message.clone();
        return result;
    }

    // 模型列表不通，再试最小对话（部分服务不开放列表接口）
    let url = chat_endpoint(cfg);
    let payload = serde_json::to_vec(&probe_payload(cfg)).unwrap_or_default();
    let started = std::time::Instant::now();
    let response = net.request(
        "POST",
        &url,
        Some(&payload),
        &provider_headers(cfg),
        cfg.timeout.max(0) as u64,
    );
    let elapsed = started.elapsed().as_millis() as i64;
    if response.ok() && probe_ok(cfg, &response.body) {
        result.ok = true;
        result.latency_ms = elapsed;
        result.message = "连接成功 · 对话接口可用（模型列表不可用）".to_string();
        result.detail = Detail::Text(format!("模型列表：{err}"));
        result.set_model = true;
        result.report_message = result.message.clone();
        return result;
    }
    result.latency_ms = elapsed;
    result.message = if response.ok() {
        err.clone()
    } else {
        response.error.clone()
    };
    result.detail = Detail::Text(format!("模型列表：{err}"));
    result.set_model = true;
    result.report_message = result.message.clone();
    result
}

/// Python `repr(list)` 的近似（`['a', 'b']`），只用于连通性测试的 `detail`。
fn py_repr_list(items: &[String]) -> String {
    let body: Vec<String> = items
        .iter()
        .map(|text| {
            let quoted = text.replace('\\', "\\\\").replace('\'', "\\'");
            format!("'{quoted}'")
        })
        .collect();
    format!("[{}]", body.join(", "))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::http::Response;

    struct Fixed(Response);

    impl Transport for Fixed {
        fn request(
            &self,
            _m: &str,
            _u: &str,
            _p: Option<&[u8]>,
            _h: &[(String, String)],
            _t: u64,
        ) -> Response {
            self.0.clone()
        }
    }

    #[test]
    fn presets_are_complete() {
        let keys: Vec<&str> = PROVIDER_PRESETS.iter().map(|p| p.key).collect();
        for want in ["custom", "openai", "deepseek", "xiaomi_mimo", "opencode_go"] {
            assert!(keys.contains(&want), "缺预设 {want}");
        }
        assert_eq!(keys[0], "custom");
        for preset in PROVIDER_PRESETS {
            if !matches!(preset.key, "custom" | "echo" | "ollama") {
                assert!(
                    preset.base_url.starts_with("https://"),
                    "{} 不是 https",
                    preset.key
                );
            }
        }
        assert!(preset_by_key("ollama").unwrap().base_url.starts_with("http://127.0.0.1"));
        assert!(preset_by_key("nope").is_none());
    }

    #[test]
    fn apply_preset_fills_urls_but_keeps_custom_address() {
        let mut cfg = Config::default();
        apply_preset(&mut cfg, "deepseek");
        assert_eq!(cfg.base_url, "https://api.deepseek.com");
        assert_eq!(cfg.models_url, "https://api.deepseek.com/models");
        assert_eq!(cfg.name, "DeepSeek");
        assert_eq!(cfg.preset, "deepseek");

        let mut custom = Config::default();
        custom.base_url = "https://my.example.com".into();
        apply_preset(&mut custom, "custom");
        assert_eq!(custom.base_url, "https://my.example.com");
        assert_eq!(custom.preset, "custom");

        // 未知预设不动配置
        let mut other = Config::default();
        other.base_url = "https://keep.example.com".into();
        apply_preset(&mut other, "nope");
        assert_eq!(other.base_url, "https://keep.example.com");

        // 已经改过名字的，套预设不覆盖
        let mut named = Config::default();
        named.name = "我的供应商".into();
        apply_preset(&mut named, "openai");
        assert_eq!(named.name, "我的供应商");
    }

    #[test]
    fn models_url_is_derived() {
        let mut cfg = Config::default();
        cfg.base_url = "https://api.openai.com/v1".into();
        assert_eq!(models_url_for(&cfg), "https://api.openai.com/v1/models");
        cfg.base_url = "https://api.deepseek.com".into();
        assert_eq!(models_url_for(&cfg), "https://api.deepseek.com/v1/models");
        cfg.models_url = "https://x.example.com/list".into();
        assert_eq!(models_url_for(&cfg), "https://x.example.com/list");
        cfg.models_url = String::new();
        cfg.base_url = String::new();
        assert_eq!(models_url_for(&cfg), "");
    }

    #[test]
    fn config_never_serializes_key() {
        let mut cfg = Config::default();
        cfg.api_key = "sk-should-not-appear".into();
        let text = serde_json::to_string(&cfg.as_dict()).unwrap();
        assert!(!text.contains("sk-should-not-appear"));
        assert!(!cfg.as_dict().as_object().unwrap().contains_key("api_key"));
    }

    #[test]
    fn config_from_dict_tolerates_garbage() {
        let cfg = Config::from_dict(&serde_json::json!({
            "name": 3, "protocol": "bogus", "models": ["m1", " ", 7],
            "headers": {"X-Test": 1, "": "skip"}, "timeout": "abc",
            "batch": "8", "temperature": "0.5", "max_tokens": 12.7
        }));
        assert_eq!(cfg.name, DEFAULT_NAME, "非字符串不覆盖");
        assert_eq!(cfg.protocol, DEFAULT_PROTOCOL, "未知协议回默认");
        assert_eq!(cfg.models, vec!["m1".to_string(), "7".to_string()]);
        assert_eq!(cfg.headers, vec![("X-Test".to_string(), "1".to_string())]);
        assert_eq!(cfg.timeout, DEFAULT_TIMEOUT, "转不了的保持默认");
        assert_eq!(cfg.batch, 8);
        assert_eq!(cfg.temperature, 0.5);
        assert_eq!(cfg.max_tokens, 12);
        // 非字典直接当空
        assert_eq!(Config::from_dict(&serde_json::json!(3)), Config::default());
    }

    #[test]
    fn problem_messages_follow_kind() {
        let mut cfg = Config::default();
        assert_eq!(cfg.problem(), "还没填基础 URL");
        cfg.base_url = "http://x".into();
        assert_eq!(cfg.problem(), "还没填 API Key");
        cfg.api_key = "k".into();
        assert_eq!(cfg.problem(), "还没选模型");
        cfg.model = "m".into();
        assert_eq!(cfg.problem(), "");
        cfg.kind = "ollama".into();
        assert_eq!(cfg.problem(), "");
        cfg.kind = "echo".into();
        assert_eq!(cfg.problem(), "");
    }

    #[test]
    fn parse_models_accepts_shapes() {
        assert_eq!(
            parse_models(r#"{"data":[{"id":"a"},{"name":"b"},"c",{"model":"a"}]}"#),
            vec!["a".to_string(), "b".to_string(), "c".to_string()]
        );
        assert_eq!(parse_models(r#"["x","y"]"#), vec!["x", "y"]);
        assert_eq!(parse_models("not json"), Vec::<String>::new());
        assert_eq!(parse_models(r#"{"data":[]}"#), Vec::<String>::new());
    }

    #[test]
    fn probe_payload_per_protocol() {
        let mut cfg = Config::default();
        cfg.model = "m".into();
        assert_eq!(
            probe_payload(&cfg)["messages"][0]["content"],
            Value::String("ping".into())
        );
        cfg.protocol = PROTOCOL_RESPONSES.into();
        assert_eq!(probe_payload(&cfg)["input"], Value::String("ping".into()));
        cfg.protocol = PROTOCOL_ANTHROPIC.into();
        assert_eq!(probe_payload(&cfg)["max_tokens"], Value::from(16));
        cfg.kind = "ollama".into();
        assert_eq!(probe_payload(&cfg)["stream"], Value::Bool(false));
    }

    #[test]
    fn probe_ok_reads_each_protocol() {
        let mut cfg = Config::default();
        assert!(probe_ok(&cfg, r#"{"choices":[]}"#));
        assert!(!probe_ok(&cfg, r#"{"error":"bad"}"#));
        assert!(!probe_ok(&cfg, r#"{"error":""}"#));
        assert!(probe_ok(&cfg, "not json but text"), "非 JSON 有内容就算通");
        assert!(!probe_ok(&cfg, "   "), "空白文本不算通");
        cfg.protocol = PROTOCOL_ANTHROPIC.into();
        assert!(probe_ok(&cfg, r#"{"content":[]}"#));
        assert!(!probe_ok(&cfg, r#"{"content":"x"}"#));
        cfg.protocol = PROTOCOL_RESPONSES.into();
        assert!(probe_ok(&cfg, r#"{"output_text":"pong"}"#));
        cfg.kind = "ollama".into();
        assert!(probe_ok(&cfg, r#"{"message":{"content":"pong"}}"#));
    }

    #[test]
    fn test_connection_reports_without_network() {
        // echo 分支不联网
        let mut cfg = Config::default();
        cfg.kind = "echo".into();
        let net = Fixed(Response::default());
        let result = test_connection(&cfg, &net);
        assert!(result.ok);
        assert!(!result.set_model, "echo 不该改模型名");

        // 还没填地址：直接给出原因，不打网络
        let cfg = Config::default();
        let result = test_connection(&cfg, &net);
        assert!(!result.ok);
        assert_eq!(result.message, "还没填基础 URL");

        // 连接失败：把传输层的错误原样带出来
        let mut cfg = Config::default();
        cfg.base_url = "http://127.0.0.1:1/v1".into();
        cfg.api_key = "k".into();
        cfg.model = "m".into();
        let net = Fixed(Response {
            status: 0,
            body: String::new(),
            error: "连不上服务器：Connection refused".into(),
        });
        let result = test_connection(&cfg, &net);
        assert!(!result.ok);
        assert!(result.message.contains("连不上服务器"));
        assert!(result.detail.text().starts_with("模型列表："));
    }

    #[test]
    fn test_connection_success_path() {
        let mut cfg = Config::default();
        cfg.base_url = "https://api.example.com/v1".into();
        cfg.api_key = "k".into();
        cfg.model = "m".into();
        let net = Fixed(Response {
            status: 200,
            body: r#"{"data":[{"id":"m1"},{"id":"m2"}]}"#.into(),
            error: String::new(),
        });
        let result = test_connection(&cfg, &net);
        assert!(result.ok);
        assert_eq!(result.message, "连接成功 · 拉到 2 个模型");
        assert!(result.set_model);
    }
}
