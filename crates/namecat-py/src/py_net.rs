//! 字节出口：把 [`namecat_core::http::Transport`] 实现在 Python 的 urllib 上。
//!
//! 为什么是 urllib 而不是 reqwest（这是 P4 唯一一处对设计稿的偏离，见
//! `crates/namecat-core/src/http.rs` 的模块头）：系统代理、回环绕代理、证书与
//! 自动重定向这些语义都由宿主运行时提供，换实现就等于换行为，而
//! `verify/test_edges.py` 的 H5 是拿对象身份断言的
//! （`PRV.opener_for(url) is PRV._LOOPBACK_OPENER`）。
//!
//! 两个 opener 只在**模块挂载时**建一次（与 Python 版一致）：建 opener 会读
//! 环境变量（代理设置），每次请求都建一遍既慢又可能读到已被改动的环境。

use namecat_core::http::{Response, Transport};
use pyo3::create_exception;
use pyo3::exceptions::{PyOSError, PyTimeoutError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBytes, PyDict};
use std::sync::OnceLock;

create_exception!(
    namecat_py,
    HttpError,
    PyOSError,
    "HTTP 状态码非 2xx（正文与原因已翻成人话；`_chat_json` 靠它决定要不要退回普通补全）"
);

static DEFAULT_OPENER: OnceLock<Py<PyAny>> = OnceLock::new();
static LOOPBACK_OPENER: OnceLock<Py<PyAny>> = OnceLock::new();

/// 建两个 opener（默认走系统代理 / 回环强制直连）。模块挂载时调一次。
pub fn init_openers(py: Python<'_>) -> PyResult<()> {
    if DEFAULT_OPENER.get().is_some() {
        return Ok(());
    }
    let urllib = py.import("urllib.request")?;
    let default = urllib.call_method0("build_opener")?.unbind();
    let empty = PyDict::new(py);
    let handler = urllib.getattr("ProxyHandler")?.call1((empty,))?;
    let loopback = urllib.call_method1("build_opener", (handler,))?.unbind();
    let _ = DEFAULT_OPENER.set(default);
    let _ = LOOPBACK_OPENER.set(loopback);
    Ok(())
}

pub fn default_opener(py: Python<'_>) -> Py<PyAny> {
    DEFAULT_OPENER
        .get()
        .map(|obj| obj.clone_ref(py))
        .unwrap_or_else(|| py.None())
}

pub fn loopback_opener(py: Python<'_>) -> Py<PyAny> {
    LOOPBACK_OPENER
        .get()
        .map(|obj| obj.clone_ref(py))
        .unwrap_or_else(|| py.None())
}

/// 回环地址一律**绕过系统代理**。
///
/// 本机装了代理（Clash / 企业代理之类）时，`HTTP(S)_PROXY` 会让发往 127.0.0.1
/// 的请求也交给代理，于是本地 Ollama 的失败原因变成代理的"网关错误 502 ·
/// upstream connect failed"，既连不上也看不懂。
pub fn is_loopback(url: &str) -> bool {
    let host = url_host(url);
    matches!(host.as_str(), "127.0.0.1" | "localhost" | "::1" | "[::1]")
        || host.ends_with(".localhost")
}

/// `urllib.parse.urlsplit(url).hostname` 的近似（小写、去端口、去 userinfo）。
pub fn url_host(url: &str) -> String {
    let rest = match url.split_once("://") {
        Some((_, rest)) => rest,
        None => url,
    };
    let authority = rest
        .split(['/', '?', '#'])
        .next()
        .unwrap_or("")
        .rsplit('@')
        .next()
        .unwrap_or("");
    let host = if let Some(stripped) = authority.strip_prefix('[') {
        // IPv6：`[::1]:8080` → `[::1]`
        match stripped.split_once(']') {
            Some((inner, _)) => format!("[{inner}]"),
            None => authority.to_string(),
        }
    } else {
        match authority.rsplit_once(':') {
            Some((host, port)) if port.chars().all(|c| c.is_ascii_digit()) => host.to_string(),
            _ => authority.to_string(),
        }
    };
    host.to_lowercase()
}

pub fn opener_for(py: Python<'_>, url: &str) -> Py<PyAny> {
    if is_loopback(url) {
        loopback_opener(py)
    } else {
        default_opener(py)
    }
}

/// 请求失败的分类结果（与 `engine.provider._request` 的四种 except 一一对应）。
fn classify(py: Python<'_>, err: &PyErr, timeout: u64) -> Response {
    let error_mod = match py.import("urllib.error") {
        Ok(module) => module,
        Err(_) => return Response::default(),
    };
    let http_error = error_mod.getattr("HTTPError").ok();
    let url_error = error_mod.getattr("URLError").ok();
    let value = err.value(py);

    if let Some(cls) = http_error {
        if err.is_instance(py, &cls) {
            let code: u16 = value
                .getattr("code")
                .ok()
                .and_then(|code| code.extract::<u16>().ok())
                .unwrap_or(0);
            let body = value
                .call_method0("read")
                .ok()
                .map(|raw| any_to_text(&raw))
                .unwrap_or_default();
            let reason = namecat_core::http::http_reason(code, &body);
            return Response {
                status: code,
                body,
                error: reason,
            };
        }
    }
    if let Some(cls) = url_error {
        if err.is_instance(py, &cls) {
            let reason = value
                .getattr("reason")
                .map(|reason| py_text(&reason))
                .unwrap_or_else(|_| py_text(value));
            return Response {
                status: 0,
                body: String::new(),
                error: format!("连不上服务器：{reason}"),
            };
        }
    }
    if err.is_instance_of::<PyTimeoutError>(py) {
        return Response {
            status: 0,
            body: String::new(),
            error: format!("超时（{timeout}s）"),
        };
    }
    let text = py_text(value);
    if err.is_instance_of::<PyValueError>(py) {
        // URL 本身有问题（unknown url type 之类）：原样带出去，别伪装成网络错误
        return Response {
            status: 0,
            body: String::new(),
            error: text,
        };
    }
    Response {
        status: 0,
        body: String::new(),
        error: format!("网络错误：{text}"),
    }
}

/// `str(obj)`。
fn py_text(obj: &Bound<'_, PyAny>) -> String {
    obj.str()
        .map(|text| text.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// `bytes` / `str` → `String`（非 UTF-8 按替换字符处理，与 `.decode("utf-8","replace")` 同口径）。
fn any_to_text(obj: &Bound<'_, PyAny>) -> String {
    if let Ok(raw) = obj.cast::<PyBytes>() {
        return String::from_utf8_lossy(raw.as_bytes()).into_owned();
    }
    if let Ok(text) = obj.extract::<String>() {
        return text;
    }
    py_text(obj)
}

/// 绑定层的传输实现：一次请求跨一次 Python 边界。
pub struct PyNet<'py> {
    pub py: Python<'py>,
}

impl PyNet<'_> {
    /// 发请求并返回响应对象（失败会抛 Python 原异常，供 ollama 那条路径用）。
    pub fn open_url(&self, req: &Bound<'_, PyAny>, timeout: u64) -> PyResult<Py<PyAny>> {
        let py = self.py;
        let opener = opener_for(py, &req.getattr("full_url")?.extract::<String>()?);
        let kwargs = PyDict::new(py);
        kwargs.set_item("timeout", timeout)?;
        let resp = opener.bind(py).call_method("open", (req,), Some(&kwargs))?;
        Ok(resp.unbind())
    }

    fn try_request(
        &self,
        method: &str,
        url: &str,
        payload: Option<&[u8]>,
        headers: &[(String, String)],
        timeout: u64,
    ) -> PyResult<(u16, String)> {
        let py = self.py;
        let urllib = py.import("urllib.request")?;
        let kwargs = PyDict::new(py);
        if let Some(bytes) = payload {
            kwargs.set_item("data", PyBytes::new(py, bytes))?;
        }
        kwargs.set_item("method", method)?;
        let head = PyDict::new(py);
        for (key, value) in headers {
            head.set_item(key, value)?;
        }
        kwargs.set_item("headers", head)?;
        let req = urllib
            .getattr("Request")?
            .call((url,), Some(&kwargs))?;

        let opener = opener_for(py, url);
        let open_kwargs = PyDict::new(py);
        open_kwargs.set_item("timeout", timeout)?;
        let resp = opener.bind(py).call_method("open", (req,), Some(&open_kwargs))?;
        let status = resp
            .getattr("status")
            .ok()
            .and_then(|value| value.extract::<u16>().ok())
            .unwrap_or(200);
        let body = resp
            .call_method0("read")
            .map(|raw| any_to_text(&raw))
            .unwrap_or_default();
        let _ = resp.call_method0("close");
        Ok((status, body))
    }
}

impl Transport for PyNet<'_> {
    fn request(
        &self,
        method: &str,
        url: &str,
        payload: Option<&[u8]>,
        headers: &[(String, String)],
        timeout: u64,
    ) -> Response {
        match self.try_request(method, url, payload, headers, timeout) {
            Ok((status, body)) => Response {
                status,
                body,
                error: String::new(),
            },
            Err(err) => classify(self.py, &err, timeout),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn loopback_detection_matches_python() {
        assert!(is_loopback("http://127.0.0.1:11434/v1/models"));
        assert!(is_loopback("http://localhost:8080/"));
        assert!(is_loopback("http://[::1]:8080/"));
        assert!(is_loopback("http://api.localhost/v1"));
        assert!(!is_loopback("https://api.deepseek.com"));
        assert!(!is_loopback("http://127.0.0.1.example.com/"));
        assert!(!is_loopback(""));
    }

    #[test]
    fn host_parsing_handles_ports_and_userinfo() {
        assert_eq!(url_host("https://user:pw@api.example.com:443/v1"), "api.example.com");
        assert_eq!(url_host("http://[::1]:11434/"), "[::1]");
        assert_eq!(url_host("https://API.Example.COM"), "api.example.com");
        assert_eq!(url_host("127.0.0.1:1"), "127.0.0.1");
    }
}
