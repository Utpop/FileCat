//! `engine/llm.py` 的绑定：LLM 客户端与建议解析。
//!
//! 三条与 Python 版**必须一致**的语义（都有断言/界面依赖）：
//! 1. `LLMClient` 可以被 Python 继承并覆盖 `_chat`（`verify/run_tests.py` 的
//!    `class Boom(L.LLMClient)` 就是这么干的）—— 所以 `plan()` 一律走
//!    `self._chat(...)` 的动态派发，不能直接调 Rust 函数。
//! 2. `EchoClient` 还要能被再继承（`class Spy(L.EchoClient)` 并 `super().plan(...)`），
//!    因此 Rust 侧的继承用 `#[pyclass(extends=…)]`，不是"三个独立类各写一遍"。
//! 3. 没配 Key 是**正常状态**：`make_client()` 返回不可用客户端，`plan()` 给出
//!    `available=false` + 一句原因，绝不抛异常。
//!
//! 网络出口在 [`crate::py_net`]（urllib），本文件不碰 socket。

use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::PyClassInitializer;
use pyo3::types::{PyAny, PyDict, PyList, PyType, PyTuple};
use serde_json::Value;

use namecat_core::http::Transport;
use namecat_core::llm as core_llm;

use crate::py_net::{HttpError, PyNet};
use crate::pyjson;

// --------------------------------------------------------------------------- //
// LLMConfig
// --------------------------------------------------------------------------- //

#[pyclass(module = "namecat_py.llm", name = "LLMConfig", dict)]
pub struct PyLLMConfig {
    #[pyo3(get, set)]
    pub provider: String,
    #[pyo3(get, set)]
    pub base_url: String,
    #[pyo3(get, set)]
    pub api_key: String,
    #[pyo3(get, set)]
    pub model: String,
    #[pyo3(get, set)]
    pub timeout: i64,
    #[pyo3(get, set)]
    pub batch: i64,
    #[pyo3(get, set)]
    pub temperature: f64,
    #[pyo3(get, set)]
    pub max_retries: i64,
    #[pyo3(get, set)]
    pub protocol: String,
    #[pyo3(get, set)]
    pub headers: Py<PyDict>,
    #[pyo3(get, set)]
    pub max_tokens: i64,
}

impl PyLLMConfig {
    pub fn from_core(py: Python<'_>, cfg: &core_llm::Config) -> PyResult<Self> {
        let headers = PyDict::new(py);
        for (key, value) in &cfg.headers {
            headers.set_item(key, value)?;
        }
        Ok(Self {
            provider: cfg.provider.clone(),
            base_url: cfg.base_url.clone(),
            api_key: cfg.api_key.clone(),
            model: cfg.model.clone(),
            timeout: cfg.timeout,
            batch: cfg.batch,
            temperature: cfg.temperature,
            max_retries: cfg.max_retries,
            protocol: cfg.protocol.clone(),
            headers: headers.unbind(),
            max_tokens: cfg.max_tokens,
        })
    }
}

/// 任何"长得像 LLMConfig"的对象 → core 配置（子类可以换掉 `config` 属性）。
pub fn config_from_obj(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<core_llm::Config> {
    let text = |name: &str| -> String {
        obj.getattr(name)
            .ok()
            .and_then(|value| value.extract::<String>().ok())
            .unwrap_or_default()
    };
    let number = |name: &str, default: i64| -> i64 {
        obj.getattr(name)
            .ok()
            .and_then(|value| value.extract::<i64>().ok())
            .unwrap_or(default)
    };
    let mut cfg = core_llm::Config::default();
    cfg.provider = text("provider");
    cfg.base_url = text("base_url");
    cfg.api_key = text("api_key");
    cfg.model = text("model");
    cfg.protocol = text("protocol");
    cfg.timeout = number("timeout", core_llm::DEFAULT_TIMEOUT);
    cfg.batch = number("batch", core_llm::DEFAULT_BATCH);
    cfg.max_retries = number("max_retries", 2);
    cfg.max_tokens = number("max_tokens", 1024);
    cfg.temperature = obj
        .getattr("temperature")
        .ok()
        .and_then(|value| value.extract::<f64>().ok())
        .unwrap_or(0.2);
    if let Ok(headers) = obj.getattr("headers") {
        if let Ok(dict) = headers.cast::<PyDict>() {
            for (key, value) in dict.iter() {
                let key = key.str()?.to_string_lossy().into_owned();
                let value = pyjson::to_value(&value);
                cfg.headers.push((key, core_llm::json_text(&value)));
            }
        }
    }
    let _ = py;
    Ok(cfg)
}

#[pymethods]
impl PyLLMConfig {
    #[new]
    #[pyo3(signature = (provider = "openai", base_url = "https://api.deepseek.com",
        api_key = "", model = "deepseek-chat", timeout = 60, batch = 20,
        temperature = 0.2, max_retries = 2, protocol = "openai_chat",
        headers = None, max_tokens = 1024))]
    fn new(
        py: Python<'_>,
        provider: &str,
        base_url: &str,
        api_key: &str,
        model: &str,
        timeout: i64,
        batch: i64,
        temperature: f64,
        max_retries: i64,
        protocol: &str,
        headers: Option<&Bound<'_, PyAny>>,
        max_tokens: i64,
    ) -> PyResult<Self> {
        let head = PyDict::new(py);
        if let Some(source) = headers {
            if let Ok(dict) = source.cast::<PyDict>() {
                for (key, value) in dict.iter() {
                    head.set_item(key, value)?;
                }
            }
        }
        Ok(Self {
            provider: provider.to_string(),
            base_url: base_url.to_string(),
            api_key: api_key.to_string(),
            model: model.to_string(),
            timeout,
            batch,
            temperature,
            max_retries,
            protocol: protocol.to_string(),
            headers: head.unbind(),
            max_tokens,
        })
    }

    #[classmethod]
    fn from_env(cls: &Bound<'_, PyType>, py: Python<'_>) -> PyResult<Py<PyLLMConfig>> {
        let _ = cls;
        let cfg = core_llm::Config::from_env();
        Py::new(py, PyLLMConfig::from_core(py, &cfg)?)
    }

    /// 绝不把 api_key 序列化出去（可能进日志 / CSV / 预设）。
    fn as_dict(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let out = PyDict::new(py);
        out.set_item("provider", &self.provider)?;
        out.set_item("base_url", &self.base_url)?;
        out.set_item("model", &self.model)?;
        out.set_item("timeout", self.timeout)?;
        out.set_item("batch", self.batch)?;
        out.set_item("has_key", !self.api_key.is_empty())?;
        Ok(out.unbind())
    }

    fn problem(&self) -> String {
        core_llm::Config {
            provider: self.provider.clone(),
            base_url: self.base_url.clone(),
            api_key: self.api_key.clone(),
            model: self.model.clone(),
            ..core_llm::Config::default()
        }
        .problem()
    }

    fn __repr__(&self) -> String {
        format!(
            "LLMConfig(provider={:?}, base_url={:?}, model={:?}, has_key={})",
            self.provider,
            self.base_url,
            self.model,
            !self.api_key.is_empty()
        )
    }
}

// --------------------------------------------------------------------------- //
// SuggestResult
// --------------------------------------------------------------------------- //

#[pyclass(module = "namecat_py.llm", name = "SuggestResult", dict)]
pub struct PySuggestResult {
    #[pyo3(get, set)]
    pub mapping: Py<PyDict>,
    #[pyo3(get, set)]
    pub available: bool,
    #[pyo3(get, set)]
    pub reason: String,
    #[pyo3(get, set)]
    pub used: i64,
    #[pyo3(get, set)]
    pub failed: i64,
    #[pyo3(get, set)]
    pub errors: Py<PyList>,
}

impl PySuggestResult {
    fn empty(py: Python<'_>) -> Self {
        Self {
            mapping: PyDict::new(py).unbind(),
            available: true,
            reason: String::new(),
            used: 0,
            failed: 0,
            errors: PyList::empty(py).unbind(),
        }
    }
}

#[pymethods]
impl PySuggestResult {
    #[new]
    #[pyo3(signature = (mapping = None, available = true, reason = "", used = 0,
        failed = 0, errors = None))]
    fn new(
        py: Python<'_>,
        mapping: Option<&Bound<'_, PyAny>>,
        available: bool,
        reason: &str,
        used: i64,
        failed: i64,
        errors: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Self> {
        let body = PyDict::new(py);
        if let Some(source) = mapping {
            if let Ok(dict) = source.cast::<PyDict>() {
                for (key, value) in dict.iter() {
                    body.set_item(key, value)?;
                }
            }
        }
        let errs = match errors {
            Some(value) if !value.is_none() => value.clone().cast_into::<PyList>()?.unbind(),
            _ => PyList::empty(py).unbind(),
        };
        Ok(Self {
            mapping: body.unbind(),
            available,
            reason: reason.to_string(),
            used,
            failed,
            errors: errs,
        })
    }

    fn summary(&self) -> String {
        if !self.available {
            return format!("LLM 未启用：{}", self.reason);
        }
        let mut msg = format!("LLM 精简：{} 项", self.used);
        if self.failed != 0 {
            msg.push_str(&format!("，{} 项无结果（保持原名）", self.failed));
        }
        msg
    }

    fn __repr__(&self) -> String {
        format!(
            "SuggestResult(available={}, used={}, failed={}, reason={:?})",
            self.available, self.used, self.failed, self.reason
        )
    }
}

// --------------------------------------------------------------------------- //
// LLMClient
// --------------------------------------------------------------------------- //

#[pyclass(module = "namecat_py.llm", name = "LLMClient", subclass, dict)]
pub struct PyLLMClient {
    #[pyo3(get, set)]
    pub config: Py<PyLLMConfig>,
}

impl PyLLMClient {
    pub fn with_core_config(py: Python<'_>, cfg: &core_llm::Config) -> PyResult<Self> {
        Ok(Self {
            config: Py::new(py, PyLLMConfig::from_core(py, cfg)?)?,
        })
    }

    fn config_of(&self, py: Python<'_>) -> PyResult<core_llm::Config> {
        config_from_obj(py, self.config.bind(py).as_any())
    }
}

/// 把 `max_len` 这类"可以是 int 也可以是字符串"的参数按 Python `int()` 收。
fn int_arg(py: Python<'_>, value: Option<&Bound<'_, PyAny>>, default: i64) -> i64 {
    let Some(value) = value else { return default };
    if value.is_none() {
        return default;
    }
    if let Ok(number) = value.extract::<i64>() {
        return number;
    }
    match py.import("builtins").and_then(|builtins| builtins.getattr("int")) {
        Ok(int) => int
            .call1((value,))
            .ok()
            .and_then(|number| number.extract::<i64>().ok())
            .unwrap_or(default),
        Err(_) => default,
    }
}

/// 名字列表：`[n for n in (names or []) if n]`。
fn name_list(source: Option<&Bound<'_, PyAny>>) -> PyResult<Vec<String>> {
    let mut out = Vec::new();
    let Some(source) = source else { return Ok(out) };
    if source.is_none() {
        return Ok(out);
    }
    for item in source.try_iter()? {
        let item = item?;
        if item.is_truthy()? {
            out.push(item.str()?.to_string_lossy().into_owned());
        }
    }
    Ok(out)
}

#[pymethods]
impl PyLLMClient {
    #[new]
    #[pyo3(signature = (config = None))]
    fn new(py: Python<'_>, config: Option<Py<PyLLMConfig>>) -> PyResult<Self> {
        let config = match config {
            Some(cfg) => cfg,
            None => Py::new(py, PyLLMConfig::from_core(py, &core_llm::Config::from_env())?)?,
        };
        Ok(Self { config })
    }

    fn available(&self, py: Python<'_>) -> PyResult<bool> {
        Ok(self.config_of(py)?.problem().is_empty())
    }

    fn problem(&self, py: Python<'_>) -> PyResult<String> {
        Ok(self.config_of(py)?.problem())
    }

    /// 子类实现：一次补全。基类直接喊 NotImplementedError（与 Python 版一致）。
    fn _chat(slf: &Bound<'_, Self>, _system: &str, _user: &str) -> PyResult<String> {
        let _ = slf;
        Err(pyo3::exceptions::PyNotImplementedError::new_err(
            "LLMClient._chat 需要子类实现",
        ))
    }

    /// 优先 JSON 模式；不支持时退回普通补全（`parse_suggestions` 两种都认）。
    fn _chat_json(slf: &Bound<'_, Self>, system: &str, user: &str) -> PyResult<String> {
        slf.call_method1("_chat", (system, user))?
            .extract::<String>()
    }

    #[staticmethod]
    fn parse_suggestions(py: Python<'_>, raw: &str, count: usize) -> PyResult<Py<PyAny>> {
        match core_llm::parse_suggestions(raw, count) {
            None => Ok(py.None()),
            Some(pairs) => {
                let dict = PyDict::new(py);
                for (idx, name) in pairs {
                    dict.set_item(idx, name)?;
                }
                Ok(dict.into_any().unbind())
            }
        }
    }

    /// 把一批名字交给模型，返回「原名->新名」映射。绝不抛异常。
    #[pyo3(signature = (names, instruction = "", lang = None, max_len = None,
        style = None, progress = None))]
    fn plan(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        names: Option<&Bound<'_, PyAny>>,
        instruction: &str,
        lang: Option<&str>,
        max_len: Option<&Bound<'_, PyAny>>,
        style: Option<&str>,
        progress: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PySuggestResult>> {
        let lang = lang.unwrap_or(core_llm::DEFAULT_LANG);
        let style = style.unwrap_or(core_llm::DEFAULT_STYLE);
        let max_len = int_arg(py, max_len, core_llm::DEFAULT_MAX_LEN);
        let names = name_list(names)?;
        let result = Py::new(py, PySuggestResult::empty(py))?;

        let problem: String = slf.call_method0("problem")?.extract()?;
        if !problem.is_empty() {
            {
                let mut res = result.borrow_mut(py);
                res.available = false;
                res.reason = problem;
            }
            return Ok(result);
        }
        if names.is_empty() {
            return Ok(result);
        }

        let mut seen: Vec<String> = Vec::new();
        for name in &names {
            if !seen.iter().any(|item| item == name) {
                seen.push(name.clone());
            }
        }

        let cfg_obj = slf.getattr("config")?;
        let batch = cfg_obj
            .getattr("batch")
            .ok()
            .and_then(|value| value.extract::<i64>().ok())
            .filter(|value| *value != 0)
            .unwrap_or(core_llm::DEFAULT_BATCH)
            .max(1) as usize;
        let retries = cfg_obj
            .getattr("max_retries")
            .ok()
            .and_then(|value| value.extract::<i64>().ok())
            .filter(|value| *value != 0)
            .unwrap_or(1)
            .max(1);

        let mut start = 0usize;
        while start < seen.len() {
            let end = (start + batch).min(seen.len());
            let chunk: Vec<String> = seen[start..end].to_vec();
            let got = plan_one(
                slf, py, &chunk, instruction, lang, max_len, style, retries, &result,
            )?;
            {
                let mapping = result.borrow(py).mapping.clone_ref(py);
                let dict = mapping.bind(py);
                let mut res = result.borrow_mut(py);
                for (src, dst) in got {
                    let clean = core_llm::sanitize_name(&dst, max_len, style);
                    if !clean.is_empty() && clean != src {
                        dict.set_item(&src, &clean)?;
                        res.used += 1;
                    }
                }
            }
            if let Some(callback) = progress {
                if !callback.is_none() {
                    callback.call1((end.min(seen.len()), seen.len(), "LLM 精简"))?;
                }
            }
            start = end;
        }
        Ok(result)
    }

    /// 单个名字的新建议（空串表示没意见）。调试与手工操作用。
    #[pyo3(signature = (name, instruction = "", lang = None, max_len = None, style = None))]
    fn suggest(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        name: &str,
        instruction: &str,
        lang: Option<&str>,
        max_len: Option<&Bound<'_, PyAny>>,
        style: Option<&str>,
    ) -> PyResult<String> {
        let single = name.into_pyobject(py)?.into_any();
        let result = PyLLMClient::plan(
            slf,
            py,
            Some(&single),
            instruction,
            lang,
            max_len,
            style,
            None,
        )?;
        let mapping = result.borrow(py).mapping.clone_ref(py);
        let got = mapping.bind(py).get_item(name)?;
        Ok(got
            .map(|value| value.extract::<String>().unwrap_or_default())
            .unwrap_or_default())
    }
}

/// 一批（一个 chunk）的请求 + 解析；失败只记账，不抛。
#[allow(clippy::too_many_arguments)]
fn plan_one(
    slf: &Bound<'_, PyLLMClient>,
    py: Python<'_>,
    chunk: &[String],
    instruction: &str,
    lang: &str,
    max_len: i64,
    style: &str,
    retries: i64,
    result: &Py<PySuggestResult>,
) -> PyResult<Vec<(String, String)>> {
    let user = core_llm::build_prompt(chunk, instruction, lang, max_len, style, false);
    for attempt in 0..retries {
        let strict = attempt > 0;
        // 注意：这里**不能**用 `?` —— 网络/鉴权/超时一律降级，异常要进 errors 列表
        // （Python 版是 `except Exception`；`?` 会把整批炸掉）。
        let raw: PyResult<String> = if strict {
            let strict_prompt =
                core_llm::build_prompt(chunk, instruction, lang, max_len, style, true);
            match slf.call_method1("_chat_json", (core_llm::SYSTEM_PROMPT, strict_prompt)) {
                Ok(value) => value.extract(),
                Err(err) => Err(err),
            }
        } else {
            match slf.call_method1("_chat", (core_llm::SYSTEM_PROMPT, user.clone())) {
                Ok(value) => value.extract(),
                Err(err) => Err(err),
            }
        };
        match raw {
            Ok(text) => {
                if let Some(pairs) = core_llm::parse_suggestions(&text, chunk.len()) {
                    let mut pairs = pairs;
                    pairs.sort_by_key(|(idx, _)| *idx);
                    let mut out = Vec::new();
                    for (idx, value) in pairs {
                        if idx >= 1 && idx as usize <= chunk.len() {
                            out.push((chunk[idx as usize - 1].clone(), value));
                        }
                    }
                    return Ok(out);
                }
            }
            Err(err) => {
                let name = err
                    .get_type(py)
                    .name()
                    .map(|name| name.to_string())
                    .unwrap_or_else(|_| "Exception".to_string());
                let text = err.value(py).str()?.to_string_lossy().into_owned();
                let mut res = result.borrow_mut(py);
                res.errors
                    .bind(py)
                    .append(format!("{name}: {text}"))?;
                res.failed += chunk.len() as i64;
                return Ok(Vec::new());
            }
        }
    }
    {
        let mut res = result.borrow_mut(py);
        res.errors
            .bind(py)
            .append(format!("模型回答无法解析（{} 条）", chunk.len()))?;
        res.failed += chunk.len() as i64;
    }
    Ok(Vec::new())
}

// --------------------------------------------------------------------------- //
// HttpChatClient
// --------------------------------------------------------------------------- //

/// 一次请求失败后记进连接状态（界面左下角气泡显示的就是它）。
///
/// 必须在**请求成功之后**才记 ok：旧实现写在 `finally` 里，于是网络一失败
/// 状态也被刷成「已连接 · 平均 N ms」，气泡等于在说谎。
fn note_status(py: Python<'_>, ok: bool, message: &str, started: std::time::Instant, model: &str) {
    let Ok(provider) = py.import("namecat_py.provider") else {
        return;
    };
    let Ok(status) = provider.getattr("STATUS") else {
        return;
    };
    let elapsed = started.elapsed().as_millis() as i64;
    let _ = status.call_method1("report", (ok, message, elapsed));
    if !model.is_empty() {
        let _ = status.call_method1("set_model", (model,));
    }
}

#[pyclass(module = "namecat_py.llm", name = "HttpChatClient", extends = PyLLMClient, subclass)]
pub struct PyHttpChatClient;

fn client_config(slf: &Bound<'_, PyHttpChatClient>) -> Py<PyLLMConfig> {
    let cfg = &slf.as_super().borrow().config;
    cfg.clone_ref(slf.py())
}

#[pymethods]
impl PyHttpChatClient {
    #[new]
    #[pyo3(signature = (config = None))]
    fn new(py: Python<'_>, config: Option<Py<PyLLMConfig>>) -> PyResult<PyClassInitializer<Self>> {
        let base = PyLLMClient::new(py, config)?;
        Ok(PyClassInitializer::from(base).add_subclass(Self))
    }

    fn _endpoint(slf: &Bound<'_, Self>, py: Python<'_>) -> PyResult<String> {
        let cfg = client_config(slf);
        let obj = cfg.bind(py);
        let borrowed = obj.borrow();
        let core = core_llm::Config {
            base_url: borrowed.base_url.clone(),
            protocol: borrowed.protocol.clone(),
            ..core_llm::Config::default()
        };
        drop(borrowed);
        Ok(core_llm::chat_endpoint(&core))
    }

    fn _headers(slf: &Bound<'_, Self>, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let mut cfg = config_from_obj(py, client_config(slf).bind(py).as_any())?;
        cfg.headers.retain(|(key, _)| !key.trim().is_empty());
        let head = PyDict::new(py);
        for (key, value) in core_llm::headers_for(&cfg, true) {
            head.set_item(key, value)?;
        }
        Ok(head.unbind())
    }

    #[pyo3(signature = (system, user, json_mode = false))]
    fn _payload(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        system: &str,
        user: &str,
        json_mode: bool,
    ) -> PyResult<Py<PyAny>> {
        let cfg = config_from_obj(py, client_config(slf).bind(py).as_any())?;
        let value = core_llm::payload(&cfg, system, user, json_mode);
        pyjson::to_py(py, &value)
    }

    fn _post(slf: &Bound<'_, Self>, py: Python<'_>, payload: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let cfg = config_from_obj(py, client_config(slf).bind(py).as_any())?;
        let value = pyjson::to_value(payload);
        let body = serde_json::to_vec(&value)
            .map_err(|err| PyValueError::new_err(format!("请求体序列化失败：{err}")))?;
        let url = core_llm::chat_endpoint(&cfg);
        let headers = core_llm::headers_for(&cfg, true);
        let started = std::time::Instant::now();
        let net = PyNet { py };
        let response = net.request(
            "POST",
            &url,
            Some(&body),
            &headers,
            cfg.timeout.max(0) as u64,
        );
        if !response.ok() {
            note_status(
                py,
                false,
                &format!("请求失败：{}", response.error),
                started,
                "",
            );
            return Err(HttpError::new_err(response.error));
        }
        note_status(py, true, "", started, &cfg.model);
        let data: Value = serde_json::from_str(&response.body).map_err(|err| {
            PyValueError::new_err(format!("回复不是合法 JSON：{err}"))
        })?;
        pyjson::to_py(py, &data)
    }

    #[staticmethod]
    fn _extract(data: &Bound<'_, PyAny>, protocol: &str) -> PyResult<String> {
        let value = pyjson::to_value(data);
        core_llm::extract(&value, protocol).map_err(|err| PyValueError::new_err(err.to_string()))
    }

    #[pyo3(signature = (system, user, json_mode = false))]
    fn _chat(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        system: &str,
        user: &str,
        json_mode: bool,
    ) -> PyResult<String> {
        let payload = PyHttpChatClient::_payload(slf, py, system, user, json_mode)?;
        let data = PyHttpChatClient::_post(slf, py, payload.bind(py))?;
        let protocol = {
            let cfg = client_config(slf);
            let obj = cfg.bind(py);
            let borrowed = obj.borrow();
            borrowed.protocol.clone()
        };
        PyHttpChatClient::_extract(data.bind(py), &protocol)
    }

    fn _chat_json(slf: &Bound<'_, Self>, py: Python<'_>, system: &str, user: &str) -> PyResult<String> {
        match PyHttpChatClient::_chat(slf, py, system, user, true) {
            Ok(text) => Ok(text),
            Err(err) if err.is_instance_of::<HttpError>(py) => {
                PyHttpChatClient::_chat(slf, py, system, user, false)
            }
            Err(err) => Err(err),
        }
    }
}

// --------------------------------------------------------------------------- //
// OllamaClient
// --------------------------------------------------------------------------- //

#[pyclass(module = "namecat_py.llm", name = "OllamaClient", extends = PyLLMClient, subclass)]
pub struct PyOllamaClient;

#[pymethods]
impl PyOllamaClient {
    #[new]
    #[pyo3(signature = (config = None))]
    fn new(py: Python<'_>, config: Option<Py<PyLLMConfig>>) -> PyResult<PyClassInitializer<Self>> {
        let base = PyLLMClient::new(py, config)?;
        // 照抄：provider=ollama 但地址还挂着 openai.com 时，换成本机地址
        let cfg_obj = base.config.clone_ref(py);
        let obj = cfg_obj.bind(py);
        {
            let mut cfg = obj.borrow_mut();
            let base_url = cfg.base_url.clone();
            if base_url.is_empty() || base_url.contains("ollama") {
                // 不动
            } else if cfg.provider == "ollama" && base_url.contains("openai.com") {
                cfg.base_url = "http://127.0.0.1:11434".to_string();
            }
        }
        Ok(PyClassInitializer::from(base).add_subclass(Self))
    }

    fn available(slf: &Bound<'_, Self>, py: Python<'_>) -> PyResult<bool> {
        let cfg = client_config_ollama(slf).bind(py).borrow();
        Ok(!cfg.base_url.is_empty() && !cfg.model.is_empty())
    }

    fn problem(slf: &Bound<'_, Self>, py: Python<'_>) -> PyResult<String> {
        if PyOllamaClient::available(slf, py)? {
            Ok(String::new())
        } else {
            Ok("Ollama 需要 base_url 与 model".to_string())
        }
    }

    fn _chat(slf: &Bound<'_, Self>, py: Python<'_>, system: &str, user: &str) -> PyResult<String> {
        let cfg = client_config_ollama(slf).bind(py).borrow();
        let mut base = cfg.base_url.clone();
        if base.is_empty() {
            base = "http://127.0.0.1:11434".to_string();
        }
        let base = base.trim_end_matches('/').to_string();
        let url = if base.ends_with("/api/chat") {
            base
        } else {
            format!("{base}/api/chat")
        };
        let mut payload = serde_json::Map::new();
        payload.insert("model".into(), Value::String(cfg.model.clone()));
        payload.insert("stream".into(), Value::Bool(false));
        let mut options = serde_json::Map::new();
        options.insert(
            "temperature".into(),
            serde_json::Number::from_f64(cfg.temperature)
                .map(Value::Number)
                .unwrap_or(Value::Null),
        );
        payload.insert("options".into(), Value::Object(options));
        payload.insert(
            "messages".into(),
            Value::Array(vec![
                message_json("system", system),
                message_json("user", user),
            ]),
        );
        let timeout = cfg.timeout.max(0) as u64;
        drop(cfg);

        let body = serde_json::to_vec(&Value::Object(payload))
            .map_err(|err| PyValueError::new_err(format!("请求体序列化失败：{err}")))?;
        let urllib = py.import("urllib.request")?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("data", pyo3::types::PyBytes::new(py, &body))?;
        kwargs.set_item("method", "POST")?;
        let head = PyDict::new(py);
        head.set_item("Content-Type", "application/json")?;
        kwargs.set_item("headers", head)?;
        let req = urllib.getattr("Request")?.call((url,), Some(&kwargs))?;
        let net = PyNet { py };
        let resp = net.open_url(&req, timeout)?;
        let raw = resp.bind(py).call_method0("read")?;
        let bytes = raw
            .cast::<pyo3::types::PyBytes>()
            .map(|bytes| bytes.as_bytes().to_vec())
            .unwrap_or_default();
        let _ = resp.bind(py).call_method0("close");
        let data: Value = serde_json::from_slice(&bytes)
            .map_err(|err| PyValueError::new_err(format!("回复不是合法 JSON：{err}")))?;
        let content = data
            .get("message")
            .and_then(|message| message.get("content"))
            .map(core_llm::json_text)
            .unwrap_or_default();
        Ok(content)
    }
}

fn client_config_ollama(slf: &Bound<'_, PyOllamaClient>) -> Py<PyLLMConfig> {
    slf.as_super().borrow().config.clone_ref(slf.py())
}

fn message_json(role: &str, content: &str) -> Value {
    let mut map = serde_json::Map::new();
    map.insert("role".into(), Value::String(role.to_string()));
    map.insert("content".into(), Value::String(content.to_string()));
    Value::Object(map)
}

// --------------------------------------------------------------------------- //
// EchoClient
// --------------------------------------------------------------------------- //

#[pyclass(module = "namecat_py.llm", name = "EchoClient", extends = PyLLMClient, subclass)]
pub struct PyEchoClient;

#[pymethods]
impl PyEchoClient {
    #[new]
    #[pyo3(signature = (config = None))]
    fn new(py: Python<'_>, config: Option<Py<PyLLMConfig>>) -> PyResult<PyClassInitializer<Self>> {
        let base = PyLLMClient::new(py, config)?;
        Ok(PyClassInitializer::from(base).add_subclass(Self))
    }

    fn available(&self) -> bool {
        true
    }

    fn problem(&self) -> String {
        String::new()
    }

    /// 机械精简。`keep_ext = false` 时名字**不含**扩展名（规则链里由 skip_ext 保护）。
    #[classmethod]
    #[pyo3(signature = (name, keep_ext = false))]
    fn simplify(
        cls: &Bound<'_, PyType>,
        name: &str,
        keep_ext: bool,
    ) -> PyResult<String> {
        let _ = cls;
        Ok(core_llm::echo_simplify(name, keep_ext))
    }

    #[pyo3(signature = (names, instruction = "", lang = None, max_len = None,
        style = None, progress = None))]
    fn plan(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        names: Option<&Bound<'_, PyAny>>,
        instruction: &str,
        lang: Option<&str>,
        max_len: Option<&Bound<'_, PyAny>>,
        style: Option<&str>,
        progress: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PySuggestResult>> {
        let _ = (slf, instruction, lang, progress);
        let max_len = int_arg(py, max_len, core_llm::DEFAULT_MAX_LEN);
        let style = style.unwrap_or(core_llm::DEFAULT_STYLE);
        let names = name_list(names)?;
        let result = Py::new(py, PySuggestResult::empty(py))?;
        let mapping = result.borrow(py).mapping.clone_ref(py);
        {
            let dict = mapping.bind(py);
            let mut res = result.borrow_mut(py);
            // 照抄 `dict.fromkeys(...)`：同名只处理一次（used 不能重复计数）
            let mut seen: Vec<String> = Vec::new();
            for name in names {
                if seen.iter().any(|item| item == &name) {
                    continue;
                }
                seen.push(name.clone());
                let out = core_llm::echo_simplify(&name, false);
                let out = core_llm::sanitize_name(&out, max_len, style);
                if !out.is_empty() && out != name {
                    dict.set_item(&name, &out)?;
                    res.used += 1;
                }
            }
        }
        Ok(result)
    }
}

// --------------------------------------------------------------------------- //
// _Unavailable
// --------------------------------------------------------------------------- //

#[pyclass(module = "namecat_py.llm", name = "_Unavailable", extends = PyLLMClient, subclass)]
pub struct PyUnavailable {
    #[pyo3(get)]
    pub _reason: String,
}

#[pymethods]
impl PyUnavailable {
    #[new]
    fn new(py: Python<'_>, reason: &str) -> PyResult<PyClassInitializer<Self>> {
        let base = PyLLMClient::with_core_config(py, &core_llm::Config::default())?;
        Ok(
            PyClassInitializer::from(base).add_subclass(Self {
                _reason: reason.to_string(),
            }),
        )
    }

    fn available(&self) -> bool {
        false
    }

    fn problem(&self) -> String {
        self._reason.clone()
    }
}

// --------------------------------------------------------------------------- //
// 模块函数
// --------------------------------------------------------------------------- //

fn appdata_dir(py: Python<'_>, parts: &[&str]) -> PyResult<String> {
    let engine = py.import("engine")?;
    let args = PyTuple::new(py, parts)?;
    engine.getattr("appdata_dir")?.call1(args)?.extract()
}

/// 要求文件位置：显式路径 > 环境变量 > `%APPDATA%\NameCat\llm_instruction.txt`。
#[pyfunction]
#[pyo3(signature = (custom = ""))]
fn instruction_path(py: Python<'_>, custom: &str) -> PyResult<String> {
    let default = appdata_dir(py, &[core_llm::INSTRUCTION_NAME])?;
    Ok(core_llm::instruction_path(custom, &default))
}

/// 读使用者写的要求；文件缺失或为空时回落 DEFAULT_INSTRUCTION（绝不报错）。
#[pyfunction]
#[pyo3(signature = (custom = ""))]
fn read_instruction(py: Python<'_>, custom: &str) -> PyResult<String> {
    let path = instruction_path(py, custom)?;
    Ok(core_llm::read_instruction(&path))
}

/// 把要求写进文件（自动建目录），返回文件路径。
#[pyfunction]
#[pyo3(signature = (text, custom = "", with_header = true))]
fn write_instruction(
    py: Python<'_>,
    text: &str,
    custom: &str,
    with_header: bool,
) -> PyResult<String> {
    let path = instruction_path(py, custom)?;
    core_llm::write_instruction(&path, text, with_header)
        .map_err(|err| PyOSError::new_err(err.to_string()))
}

/// 确保要求文件存在：缺失时用默认要求建一份。
#[pyfunction]
#[pyo3(signature = (custom = ""))]
fn ensure_instruction_file(py: Python<'_>, custom: &str) -> PyResult<String> {
    let path = instruction_path(py, custom)?;
    core_llm::ensure_instruction_file(&path).map_err(|err| PyOSError::new_err(err.to_string()))
}

/// 拼出一次请求的用户消息（调试/自检用）。
#[pyfunction]
#[pyo3(signature = (names, instruction = "", lang = "zh", max_len = 24,
    style = "natural", strict = false))]
fn build_prompt(
    names: Vec<String>,
    instruction: &str,
    lang: &str,
    max_len: i64,
    style: &str,
    strict: bool,
) -> String {
    core_llm::build_prompt(&names, instruction, lang, max_len, style, strict)
}

/// 把模型输出收拾成合法文件名片段（纯函数，界面也会直接调）。
#[pyfunction]
#[pyo3(signature = (text, max_len = None, style = "natural"))]
fn sanitize_name(py: Python<'_>, text: &str, max_len: Option<&Bound<'_, PyAny>>, style: &str) -> String {
    let max_len = int_arg(py, max_len, core_llm::DEFAULT_MAX_LEN);
    core_llm::sanitize_name(text, max_len, style)
}

/// 按配置造客户端。没配 key 时返回一个"不可用"客户端而不是抛异常。
#[pyfunction]
#[pyo3(signature = (config = None, kind = ""))]
fn make_client(
    py: Python<'_>,
    config: Option<Py<PyLLMConfig>>,
    kind: &str,
) -> PyResult<Py<PyAny>> {
    let config = match config {
        Some(cfg) => cfg,
        None => Py::new(py, PyLLMConfig::from_core(py, &core_llm::Config::from_env())?)?,
    };
    let cfg_obj = config.bind(py);
    let cfg_core = config_from_obj(py, cfg_obj.as_any())?;
    let kind = if kind.trim().is_empty() {
        cfg_core.provider.clone()
    } else {
        kind.to_string()
    }
    .trim()
    .to_lowercase();
    let kind = if kind.is_empty() {
        "openai".to_string()
    } else {
        kind
    };

    if matches!(kind.as_str(), "echo" | "offline" | "mock") {
        let cls = py.import("namecat_py.llm")?.getattr("EchoClient")?;
        return Ok(cls.call1((config,))?.unbind());
    }
    if matches!(kind.as_str(), "ollama" | "local") {
        let cls = py.import("namecat_py.llm")?.getattr("OllamaClient")?;
        let client = cls.call1((config,))?;
        let available: bool = client.call_method0("available")?.extract()?;
        if available {
            return Ok(client.unbind());
        }
        let reason: String = client.call_method0("problem")?.extract()?;
        let cls = py.import("namecat_py.llm")?.getattr("_Unavailable")?;
        return Ok(cls.call1((reason,))?.unbind());
    }
    let cls = py.import("namecat_py.llm")?.getattr("HttpChatClient")?;
    let client = cls.call1((config,))?;
    let available: bool = client.call_method0("available")?.extract()?;
    if available {
        return Ok(client.unbind());
    }
    let reason: String = client.call_method0("problem")?.extract()?;
    let cls = py.import("namecat_py.llm")?.getattr("_Unavailable")?;
    Ok(cls.call1((reason,))?.unbind())
}

pub fn attach(py: Python<'_>, parent: &Bound<'_, pyo3::types::PyModule>) -> PyResult<()> {
    let m = crate::shim::new_submodule(py, parent, "llm")?;
    m.add(
        "__doc__",
        "大模型改名建议层（Rust 实现）：客户端、提示词、回复解析、离线替身。",
    )?;

    m.add_class::<PyLLMConfig>()?;
    m.add_class::<PyLLMClient>()?;
    m.add_class::<PyHttpChatClient>()?;
    m.add_class::<PyOllamaClient>()?;
    m.add_class::<PyEchoClient>()?;
    m.add_class::<PySuggestResult>()?;
    // 内部类：Python 侧不 star-import，但 make_client 会用它，所以必须挂上
    m.add_class::<PyUnavailable>()?;

    m.add("DEFAULT_INSTRUCTION", core_llm::DEFAULT_INSTRUCTION)?;
    m.add("DEFAULT_LANG", core_llm::DEFAULT_LANG)?;
    m.add("DEFAULT_MAX_LEN", core_llm::DEFAULT_MAX_LEN)?;
    m.add("DEFAULT_STYLE", core_llm::DEFAULT_STYLE)?;
    m.add("INSTRUCTION_ENV", core_llm::INSTRUCTION_ENV)?;
    m.add("LEGACY_INSTRUCTION_ENV", core_llm::LEGACY_INSTRUCTION_ENV)?;
    m.add("INSTRUCTION_NAME", core_llm::INSTRUCTION_NAME)?;
    m.add("INSTRUCTION_HEADER", core_llm::INSTRUCTION_HEADER)?;
    m.add("SYSTEM_PROMPT", core_llm::SYSTEM_PROMPT)?;

    let styles = PyDict::new(py);
    for (key, text) in core_llm::STYLES {
        styles.set_item(*key, *text)?;
    }
    m.add("STYLES", styles)?;

    m.add_function(wrap_pyfunction!(instruction_path, &m)?)?;
    m.add_function(wrap_pyfunction!(read_instruction, &m)?)?;
    m.add_function(wrap_pyfunction!(write_instruction, &m)?)?;
    m.add_function(wrap_pyfunction!(ensure_instruction_file, &m)?)?;
    m.add_function(wrap_pyfunction!(build_prompt, &m)?)?;
    m.add_function(wrap_pyfunction!(sanitize_name, &m)?)?;
    m.add_function(wrap_pyfunction!(make_client, &m)?)?;

    crate::shim::set_all(
        &m,
        &[
            "DEFAULT_INSTRUCTION",
            "DEFAULT_LANG",
            "DEFAULT_MAX_LEN",
            "DEFAULT_STYLE",
            "EchoClient",
            "HttpChatClient",
            "LLMClient",
            "LLMConfig",
            "OllamaClient",
            "STYLES",
            "SuggestResult",
            "build_prompt",
            "make_client",
            "sanitize_name",
        ],
    )
}
