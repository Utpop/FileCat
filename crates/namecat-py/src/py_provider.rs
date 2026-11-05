//! `engine/provider.py` 的绑定：供应商配置 + 连通性测试。
//!
//! 密钥永远不在这里落盘：配置对象里有 `api_key` 字段（只在内存里），
//! 但 [`PyProviderConfig::as_dict`] 里没有这个键；真正的存储走
//! [`crate::py_secrets`]（DPAPI）。
//!
//! `STATUS` 是进程内单例（不落盘）：它是"此刻"的连接状况，下次开程序重新测
//! 一次更有意义。界面左下角的气泡显示的就是它。

use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyList};

use namecat_core::provider as core_prv;

use crate::py_llm::PyLLMConfig;
use crate::py_net::{self, PyNet};
use crate::pyjson;
use crate::shim::appdata_dir;

/// 全局单例：引擎与界面共用一份（与 Python 版的 `STATUS` 等价）。
static STATUS: std::sync::OnceLock<Py<PyAny>> = std::sync::OnceLock::new();

fn status_obj(py: Python<'_>) -> Py<PyAny> {
    match STATUS.get() {
        Some(obj) => obj.clone_ref(py),
        None => py.None(),
    }
}

// --------------------------------------------------------------------------- //
// ProviderPreset
// --------------------------------------------------------------------------- //

#[pyclass(module = "namecat_py.provider", name = "ProviderPreset", frozen)]
pub struct PyProviderPreset {
    #[pyo3(get)]
    pub key: String,
    #[pyo3(get)]
    pub label: String,
    #[pyo3(get)]
    pub base_url: String,
    #[pyo3(get)]
    pub models_url: String,
    #[pyo3(get)]
    pub protocol: String,
    #[pyo3(get)]
    pub kind: String,
    #[pyo3(get)]
    pub note: String,
}

impl PyProviderPreset {
    fn from_core(preset: &core_prv::ProviderPreset) -> Self {
        Self {
            key: preset.key.to_string(),
            label: preset.label.to_string(),
            base_url: preset.base_url.to_string(),
            models_url: preset.models_url.to_string(),
            protocol: preset.protocol.to_string(),
            kind: preset.kind.to_string(),
            note: preset.note.to_string(),
        }
    }
}

#[pymethods]
impl PyProviderPreset {
    fn __repr__(&self) -> String {
        format!("ProviderPreset(key={:?}, label={:?})", self.key, self.label)
    }
}

// --------------------------------------------------------------------------- //
// ProviderConfig
// --------------------------------------------------------------------------- //

#[pyclass(module = "namecat_py.provider", name = "ProviderConfig", dict)]
pub struct PyProviderConfig {
    #[pyo3(get, set)]
    pub name: String,
    #[pyo3(get, set)]
    pub protocol: String,
    #[pyo3(get, set)]
    pub preset: String,
    #[pyo3(get, set)]
    pub kind: String,
    #[pyo3(get, set)]
    pub base_url: String,
    #[pyo3(get, set)]
    pub models_url: String,
    #[pyo3(get, set)]
    pub model: String,
    #[pyo3(get, set)]
    pub models: Vec<String>,
    #[pyo3(get, set)]
    pub headers: Py<PyDict>,
    #[pyo3(get, set)]
    pub timeout: i64,
    #[pyo3(get, set)]
    pub batch: i64,
    #[pyo3(get, set)]
    pub temperature: f64,
    #[pyo3(get, set)]
    pub max_tokens: i64,
    /// 只在内存里，绝不写进 json
    #[pyo3(get, set)]
    pub api_key: String,
}

impl PyProviderConfig {
    pub fn from_core(py: Python<'_>, cfg: &core_prv::Config) -> PyResult<Self> {
        let headers = PyDict::new(py);
        for (key, value) in &cfg.headers {
            headers.set_item(key, value)?;
        }
        Ok(Self {
            name: cfg.name.clone(),
            protocol: cfg.protocol.clone(),
            preset: cfg.preset.clone(),
            kind: cfg.kind.clone(),
            base_url: cfg.base_url.clone(),
            models_url: cfg.models_url.clone(),
            model: cfg.model.clone(),
            models: cfg.models.clone(),
            headers: headers.unbind(),
            timeout: cfg.timeout,
            batch: cfg.batch,
            temperature: cfg.temperature,
            max_tokens: cfg.max_tokens,
            api_key: cfg.api_key.clone(),
        })
    }

    pub fn to_core(&self, py: Python<'_>) -> core_prv::Config {
        let mut headers = Vec::new();
        if let Ok(dict) = self.headers.bind(py).cast::<PyDict>() {
            for (key, value) in dict.iter() {
                let key = key.str().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
                let value = value.str().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
                headers.push((key, value));
            }
        }
        core_prv::Config {
            name: self.name.clone(),
            protocol: self.protocol.clone(),
            preset: self.preset.clone(),
            kind: self.kind.clone(),
            base_url: self.base_url.clone(),
            models_url: self.models_url.clone(),
            model: self.model.clone(),
            models: self.models.clone(),
            headers,
            timeout: self.timeout,
            batch: self.batch,
            temperature: self.temperature,
            max_tokens: self.max_tokens,
            api_key: self.api_key.clone(),
        }
    }
}

/// 任何一个"长得像 ProviderConfig"的对象 → core 配置。
fn provider_from_obj(py: Python<'_>, obj: &Bound<'_, PyAny>) -> core_prv::Config {
    let mut cfg = core_prv::Config::default();
    let text = |name: &str| -> Option<String> {
        obj.getattr(name).ok().and_then(|v| v.extract::<String>().ok())
    };
    if let Some(value) = text("name") {
        cfg.name = value;
    }
    if let Some(value) = text("protocol") {
        cfg.protocol = value;
    }
    if let Some(value) = text("preset") {
        cfg.preset = value;
    }
    if let Some(value) = text("kind") {
        cfg.kind = value;
    }
    if let Some(value) = text("base_url") {
        cfg.base_url = value;
    }
    if let Some(value) = text("models_url") {
        cfg.models_url = value;
    }
    if let Some(value) = text("model") {
        cfg.model = value;
    }
    if let Some(value) = text("api_key") {
        cfg.api_key = value;
    }
    if let Ok(models) = obj.getattr("models") {
        if let Ok(items) = models.try_iter() {
            for item in items.flatten() {
                if let Ok(text) = item.extract::<String>() {
                    cfg.models.push(text);
                }
            }
        }
    }
    if let Ok(headers) = obj.getattr("headers") {
        if let Ok(dict) = headers.cast::<PyDict>() {
            for (key, value) in dict.iter() {
                let key = key.str().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
                let value = value.str().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
                cfg.headers.push((key, value));
            }
        }
    }
    if let Some(number) = obj
        .getattr("timeout")
        .ok()
        .and_then(|value| value.extract::<i64>().ok())
    {
        cfg.timeout = number;
    }
    if let Some(number) = obj
        .getattr("batch")
        .ok()
        .and_then(|value| value.extract::<i64>().ok())
    {
        cfg.batch = number;
    }
    if let Some(number) = obj
        .getattr("max_tokens")
        .ok()
        .and_then(|value| value.extract::<i64>().ok())
    {
        cfg.max_tokens = number;
    }
    if let Ok(value) = obj.getattr("temperature") {
        if let Ok(number) = value.extract::<f64>() {
            cfg.temperature = number;
        }
    }
    let _ = py;
    cfg
}

#[pymethods]
impl PyProviderConfig {
    #[new]
    #[pyo3(signature = (name = "OpenAI Compatible", protocol = "openai_chat",
        preset = "custom", kind = "openai", base_url = "", models_url = "",
        model = "", models = None, headers = None, timeout = 60, batch = 20,
        temperature = 0.2, max_tokens = 1024, api_key = ""))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        name: &str,
        protocol: &str,
        preset: &str,
        kind: &str,
        base_url: &str,
        models_url: &str,
        model: &str,
        models: Option<&Bound<'_, PyAny>>,
        headers: Option<&Bound<'_, PyAny>>,
        timeout: i64,
        batch: i64,
        temperature: f64,
        max_tokens: i64,
        api_key: &str,
    ) -> PyResult<Self> {
        let mut model_list: Vec<String> = Vec::new();
        if let Some(source) = models {
            if let Ok(items) = source.try_iter() {
                for item in items.flatten() {
                    if let Ok(text) = item.extract::<String>() {
                        model_list.push(text);
                    }
                }
            }
        }
        let head = PyDict::new(py);
        if let Some(source) = headers {
            if let Ok(dict) = source.cast::<PyDict>() {
                for (key, value) in dict.iter() {
                    head.set_item(key, value)?;
                }
            }
        }
        Ok(Self {
            name: name.to_string(),
            protocol: protocol.to_string(),
            preset: preset.to_string(),
            kind: kind.to_string(),
            base_url: base_url.to_string(),
            models_url: models_url.to_string(),
            model: model.to_string(),
            models: model_list,
            headers: head.unbind(),
            timeout,
            batch,
            temperature,
            max_tokens,
            api_key: api_key.to_string(),
        })
    }

    /// 只序列化非敏感字段；api_key 永远不出现。
    fn as_dict(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let value = self.to_core(py).as_dict();
        pyjson::to_py(py, &value)
    }

    #[classmethod]
    fn from_dict(
        cls: &Bound<'_, pyo3::types::PyType>,
        py: Python<'_>,
        data: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyProviderConfig>> {
        let _ = cls;
        let value = pyjson::to_value(data);
        let cfg = core_prv::Config::from_dict(&value);
        Py::new(py, PyProviderConfig::from_core(py, &cfg)?)
    }

    fn has_key(&self) -> bool {
        !self.api_key.is_empty()
    }

    fn protocol_text(&self) -> String {
        core_prv::protocol_text(&self.protocol).unwrap_or(&self.protocol).to_string()
    }

    /// 返回一句"为什么用不了"，空串表示可以。
    fn problem(&self, py: Python<'_>) -> String {
        self.to_core(py).problem()
    }

    fn __repr__(&self) -> String {
        format!(
            "ProviderConfig(name={:?}, preset={:?}, base_url={:?}, model={:?}, has_key={})",
            self.name,
            self.preset,
            self.base_url,
            self.model,
            !self.api_key.is_empty()
        )
    }
}

// --------------------------------------------------------------------------- //
// ProviderStatus
// --------------------------------------------------------------------------- //

#[pyclass(module = "namecat_py.provider", name = "ProviderStatus", dict)]
pub struct PyProviderStatus {
    #[pyo3(get, set)]
    pub state: String,
    #[pyo3(get, set)]
    pub message: String,
    #[pyo3(get, set)]
    pub latency_ms: i64,
    /// 属性名仍是 `model`，但**不能用** `#[pyo3(get, set)]`：那会生成
    /// `set_model`，和下面的同名方法撞车（PyO3 直接报重复定义）。
    pub model_name: String,
    #[pyo3(get, set)]
    pub checked_at: f64,
    /// 最近若干次成功请求的耗时（毫秒）
    samples: Vec<i64>,
}

fn now_ts() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

#[pymethods]
impl PyProviderStatus {
    #[new]
    fn new() -> Self {
        Self {
            state: "unset".to_string(),
            message: String::new(),
            latency_ms: 0,
            model_name: String::new(),
            checked_at: 0.0,
            samples: Vec::new(),
        }
    }

    #[classattr]
    const MAX_SAMPLES: usize = 20;

    #[getter(model)]
    fn get_model(&self) -> String {
        self.model_name.clone()
    }

    #[setter(model)]
    fn put_model(&mut self, model: String) {
        self.model_name = model.trim().to_string();
    }

    fn set_model(&mut self, model: String) {
        self.model_name = model.trim().to_string();
    }

    /// 记一次观测结果。ok=true 且给了耗时就进平均值的样本池。
    fn report(&mut self, ok: bool, message: &str, latency_ms: i64) {
        self.state = if ok { "ok" } else { "error" }.to_string();
        self.message = message.to_string();
        self.latency_ms = latency_ms.max(0);
        self.checked_at = now_ts();
        if ok && self.latency_ms > 0 {
            self.samples.push(self.latency_ms);
            let max = Self::MAX_SAMPLES;
            if self.samples.len() > max {
                let cut = self.samples.len() - max;
                self.samples.drain(0..cut);
            }
        }
    }

    fn clear(&mut self) {
        self.state = "unset".to_string();
        self.message.clear();
        self.latency_ms = 0;
        self.checked_at = 0.0;
        self.samples.clear();
    }

    /// 配置变了但还没测 —— 状态回到"未知"，别拿旧的测试结果说事。
    fn mark_unknown(&mut self, model: Option<String>) {
        if let Some(model) = model {
            if !model.is_empty() {
                self.model_name = model.trim().to_string();
            }
        }
        if self.state == "ok" || self.state == "error" {
            self.state = "unknown".to_string();
            self.message.clear();
            self.latency_ms = 0;
            self.checked_at = 0.0;
            self.samples.clear();
        }
    }

    fn average_ms(&self) -> i64 {
        if self.samples.is_empty() {
            return 0;
        }
        let total: i64 = self.samples.iter().sum();
        total / self.samples.len() as i64
    }

    fn snapshot(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let out = PyDict::new(py);
        out.set_item("state", &self.state)?;
        out.set_item("message", &self.message)?;
        out.set_item("latency_ms", self.latency_ms)?;
        out.set_item("average_ms", self.average_ms())?;
        out.set_item("model", &self.model_name)?;
        out.set_item("checked_at", self.checked_at)?;
        Ok(out.unbind())
    }

    fn __repr__(&self) -> String {
        format!(
            "ProviderStatus(state={:?}, model={:?}, latency_ms={}, average_ms={})",
            self.state,
            self.model_name,
            self.latency_ms,
            self.average_ms()
        )
    }
}

// --------------------------------------------------------------------------- //
// 模块函数
// --------------------------------------------------------------------------- //

#[pyfunction]
pub fn config_path(py: Python<'_>) -> PyResult<String> {
    appdata_dir(py, &[core_prv::CONFIG_NAME])
}

pub fn secret_path(py: Python<'_>) -> PyResult<String> {
    appdata_dir(py, &[namecat_core::secrets::SECRET_FILE])
}

#[pyfunction]
fn load_config(py: Python<'_>) -> PyResult<Py<PyProviderConfig>> {
    let cfg = core_prv::load_config(&config_path(py)?, &secret_path(py)?);
    Py::new(py, PyProviderConfig::from_core(py, &cfg)?)
}

/// 写配置。
///
/// `api_key = None` → 不动已保存的密钥（界面"留空即沿用"走这条）；
/// `""` → 清除密钥；非空 → 覆盖。返回配置文件路径。
#[pyfunction]
#[pyo3(signature = (cfg, api_key = None))]
fn save_config(py: Python<'_>, cfg: &Bound<'_, PyAny>, api_key: Option<&str>) -> PyResult<String> {
    let core = provider_from_obj(py, cfg);
    Ok(core_prv::save_config(
        &core,
        &config_path(py)?,
        api_key,
        &secret_path(py)?,
    ))
}

/// 套用供应商预设：填好地址与模型列表 URL；密钥不动。
#[pyfunction]
fn apply_preset(
    py: Python<'_>,
    cfg: &Bound<'_, PyProviderConfig>,
    preset_key: &str,
) -> PyResult<Py<PyAny>> {
    let mut core = provider_from_obj(py, cfg.as_any());
    core_prv::apply_preset(&mut core, preset_key);
    {
        let mut borrowed = cfg.borrow_mut();
        borrowed.name = core.name;
        borrowed.protocol = core.protocol;
        borrowed.preset = core.preset;
        borrowed.kind = core.kind;
        borrowed.base_url = core.base_url;
        borrowed.models_url = core.models_url;
    }
    Ok(cfg.clone().into_any().unbind())
}

#[pyfunction]
fn models_url_for(py: Python<'_>, cfg: &Bound<'_, PyAny>) -> String {
    core_prv::models_url_for(&provider_from_obj(py, cfg))
}

/// 拉取模型列表。返回 (模型名列表, 错误原因)；成功时错误原因为空串。
#[pyfunction]
fn list_models(py: Python<'_>, cfg: &Bound<'_, PyAny>) -> PyResult<(Vec<String>, String)> {
    let core = provider_from_obj(py, cfg);
    let net = PyNet { py };
    Ok(core_prv::list_models(&core, &net))
}

/// 真发一次请求，测这个供应商能不能用。
#[pyfunction]
fn test_connection(py: Python<'_>, cfg: &Bound<'_, PyAny>) -> PyResult<Py<PyDict>> {
    let core = provider_from_obj(py, cfg);
    let net = PyNet { py };
    let result = core_prv::test_connection(&core, &net);
    let status = status_obj(py);
    if !status.bind(py).is_none() {
        let _ = status.bind(py).call_method1("report", (
            result.ok,
            result.report_message.clone(),
            result.latency_ms,
        ));
        if result.set_model {
            let _ = status.bind(py).call_method1("set_model", (core.model.clone(),));
        }
    }
    let out = PyDict::new(py);
    out.set_item("ok", result.ok)?;
    out.set_item("message", result.message)?;
    out.set_item("latency_ms", result.latency_ms)?;
    match &result.detail {
        namecat_core::provider::Detail::Text(text) => out.set_item("detail", text)?,
        namecat_core::provider::Detail::List(items) => out.set_item("detail", items)?,
    }
    Ok(out.unbind())
}

/// 环境变量兜底的那一份配置（界面上从没保存过时用）。
#[pyfunction]
fn env_config(py: Python<'_>) -> PyResult<Py<PyProviderConfig>> {
    Py::new(py, PyProviderConfig::from_core(py, &core_prv::env_config())?)
}

/// 界面上要显示的那一份（保存过的 > 环境变量）。
#[pyfunction]
fn effective_provider_config(py: Python<'_>) -> PyResult<Py<PyProviderConfig>> {
    let cfg = core_prv::effective_provider_config(&config_path(py)?, &secret_path(py)?);
    Py::new(py, PyProviderConfig::from_core(py, &cfg)?)
}

/// 最终生效的接入配置（给 engine.llm 用）。
#[pyfunction]
fn effective_config(py: Python<'_>) -> PyResult<Py<PyLLMConfig>> {
    let cfg = core_prv::effective_config(&config_path(py)?, &secret_path(py)?);
    Py::new(py, PyLLMConfig::from_core(py, &cfg)?)
}

/// 按目标地址挑 opener：回环地址一律**绕过系统代理**。
#[pyfunction]
fn opener_for(py: Python<'_>, url: &str) -> Py<PyAny> {
    py_net::opener_for(py, url)
}

/// 发请求（回环不代理）。`engine.llm` 用它，避免两套逻辑各写一遍。
#[pyfunction]
#[pyo3(signature = (req, timeout = 60))]
fn open_url(py: Python<'_>, req: &Bound<'_, PyAny>, timeout: u64) -> PyResult<Py<PyAny>> {
    let net = PyNet { py };
    net.open_url(req, timeout)
}

/// 一次 HTTP 请求（不抛异常，错误折进第三项）。
#[pyfunction]
#[pyo3(signature = (method, url, payload = None, headers = None, timeout = 60))]
fn _request(
    py: Python<'_>,
    method: &str,
    url: &str,
    payload: Option<&Bound<'_, PyAny>>,
    headers: Option<&Bound<'_, PyAny>>,
    timeout: u64,
) -> PyResult<(u16, String, String)> {
    let body = payload.map(|value| {
        if let Ok(text) = value.extract::<String>() {
            text.into_bytes()
        } else {
            pyjson::to_value(value).to_string().into_bytes()
        }
    });
    let mut head: Vec<(String, String)> = Vec::new();
    if let Some(source) = headers {
        if let Ok(dict) = source.cast::<PyDict>() {
            for (key, value) in dict.iter() {
                head.push((
                    key.str()?.to_string_lossy().into_owned(),
                    value.str()?.to_string_lossy().into_owned(),
                ));
            }
        }
    }
    let net = PyNet { py };
    let response = <PyNet<'_> as namecat_core::http::Transport>::request(
        &net,
        method,
        url,
        body.as_deref(),
        &head,
        timeout,
    );
    Ok((response.status, response.body, response.error))
}

pub fn attach(py: Python<'_>, parent: &Bound<'_, pyo3::types::PyModule>) -> PyResult<()> {
    let m = crate::shim::new_submodule(py, parent, "provider")?;
    m.add(
        "__doc__",
        "供应商配置与连通性检测（Rust 实现）。",
    )?;

    // 两个 opener 在建模块时就建好（Python 版的 `_DEFAULT_OPENER` 是模块级常量，
    // test_edges 的 H5 拿对象身份断言 `opener_for(url) is _LOOPBACK_OPENER`）
    py_net::init_openers(py)?;
    let default_opener = py_net::default_opener(py);
    let loopback_opener = py_net::loopback_opener(py);
    m.add("_DEFAULT_OPENER", default_opener.bind(py))?;
    m.add("_LOOPBACK_OPENER", loopback_opener.bind(py))?;
    m.add("_LOOPBACK_HOSTS", ("127.0.0.1", "localhost", "::1", "[::1]"))?;

    let status = Py::new(py, PyProviderStatus::new())?;
    let _ = STATUS.set(status.clone_ref(py).into_any());
    m.add("STATUS", status)?;

    m.add_class::<PyProviderPreset>()?;
    m.add_class::<PyProviderConfig>()?;
    m.add_class::<PyProviderStatus>()?;

    m.add("CONFIG_NAME", core_prv::CONFIG_NAME)?;
    m.add("PROTOCOL_CHAT", core_prv::PROTOCOL_CHAT)?;
    m.add("PROTOCOL_RESPONSES", core_prv::PROTOCOL_RESPONSES)?;
    m.add("PROTOCOL_ANTHROPIC", core_prv::PROTOCOL_ANTHROPIC)?;
    m.add("DEFAULT_PROTOCOL", core_prv::DEFAULT_PROTOCOL)?;
    m.add("DEFAULT_PRESET", core_prv::DEFAULT_PRESET)?;

    let protocols = PyList::empty(py);
    for (key, text) in core_prv::PROTOCOLS {
        protocols.append((*key, *text))?;
    }
    m.add("PROTOCOLS", protocols)?;

    let protocol_text = PyDict::new(py);
    for (key, text) in core_prv::PROTOCOLS {
        protocol_text.set_item(*key, *text)?;
    }
    m.add("PROTOCOL_TEXT", protocol_text)?;

    let presets = PyList::empty(py);
    let by_key = PyDict::new(py);
    for preset in core_prv::PROVIDER_PRESETS {
        let obj = Py::new(py, PyProviderPreset::from_core(preset))?;
        presets.append(obj.clone_ref(py))?;
        by_key.set_item(preset.key, obj)?;
    }
    m.add("PROVIDER_PRESETS", presets)?;
    m.add("PRESET_BY_KEY", by_key)?;

    // `provider.secrets` 在 Python 版里是"模块对象"，界面据此判 `has_secret()`
    let secrets_mod = py.import("namecat_py.secrets")?;
    m.add("secrets", secrets_mod)?;

    m.add_function(wrap_pyfunction!(config_path, &m)?)?;
    m.add_function(wrap_pyfunction!(load_config, &m)?)?;
    m.add_function(wrap_pyfunction!(save_config, &m)?)?;
    m.add_function(wrap_pyfunction!(apply_preset, &m)?)?;
    m.add_function(wrap_pyfunction!(models_url_for, &m)?)?;
    m.add_function(wrap_pyfunction!(list_models, &m)?)?;
    m.add_function(wrap_pyfunction!(test_connection, &m)?)?;
    m.add_function(wrap_pyfunction!(env_config, &m)?)?;
    m.add_function(wrap_pyfunction!(effective_provider_config, &m)?)?;
    m.add_function(wrap_pyfunction!(effective_config, &m)?)?;
    m.add_function(wrap_pyfunction!(opener_for, &m)?)?;
    m.add_function(wrap_pyfunction!(open_url, &m)?)?;
    m.add_function(wrap_pyfunction!(_request, &m)?)?;

    crate::shim::set_all(
        &m,
        &[
            "CONFIG_NAME",
            "DEFAULT_PRESET",
            "DEFAULT_PROTOCOL",
            "PRESET_BY_KEY",
            "PROTOCOLS",
            "PROTOCOL_ANTHROPIC",
            "PROTOCOL_CHAT",
            "PROTOCOL_RESPONSES",
            "PROTOCOL_TEXT",
            "PROVIDER_PRESETS",
            "ProviderConfig",
            "ProviderPreset",
            "apply_preset",
            "config_path",
            "effective_config",
            "effective_provider_config",
            "env_config",
            "list_models",
            "load_config",
            "models_url_for",
            "save_config",
            "test_connection",
        ],
    )
}
