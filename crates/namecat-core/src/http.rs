//! HTTP 的**最小抽象**：核心只决定"发什么、怎么解释回什么"，不发字节。
//!
//! 为什么不把传输也搬到 Rust（与设计稿 §3 的一处有意偏离，理由写在这）：
//! 现实现用 `urllib`，它把三件很难在 Rust 侧重现到"一模一样"的事情包了下去：
//! ① 系统代理（Windows 走注册表 IE 设置 + 环境变量两套口径）；
//! ② 回环地址绕过代理（`opener_for` 的两套 opener，见 `engine.provider`）；
//! ③ HTTPS 证书与 SNI。换成 reqwest/ureq 会把这些语义悄悄换一套，
//! 而 `verify/test_edges.py` 的 H5 明确断言"127.0.0.1 不能被代理 502 掉"。
//!
//! 所以边界划在这里：**决策逻辑在 core，字节出口在绑定层**（`py_net` 仍然调
//! urllib）。代价是网络调用要跨一次 Python 边界（毫秒级，远小于一次往返）。

/// 一次 HTTP 结果。`error` 为空串表示成功（与 `engine.provider._request` 同口径）。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Response {
    pub status: u16,
    pub body: String,
    pub error: String,
}

impl Response {
    pub fn ok(&self) -> bool {
        self.error.is_empty()
    }
}

/// 字节出口。实现在绑定层（urllib），core 侧只认这个接口。
pub trait Transport {
    /// 发一个请求。**绝不 panic、绝不抛异常**：失败一律折进 `Response::error`。
    fn request(
        &self,
        method: &str,
        url: &str,
        payload: Option<&[u8]>,
        headers: &[(String, String)],
        timeout: u64,
    ) -> Response;
}

/// 把状态码翻译成人话，并顺手带上服务端给的错误信息。
///
/// 逐字对齐 `engine.provider._http_reason`：先剥服务端的 `error.message` /
/// `error` / `message`，取不到就把正文前 180 个字符（换行压成空格）当详情。
pub fn http_reason(code: u16, body: &str) -> String {
    let detail = error_detail(body);
    let base = match code {
        400 => "请求被拒绝（参数或权限不对）",
        401 => "API Key 无效或已过期",
        403 => "没有权限访问该模型",
        404 => "地址不对（404）：检查基础 URL 与模型名",
        408 => "服务端超时",
        413 => "请求内容过大",
        422 => "参数不被接受",
        429 => "触发限流或额度用尽",
        500 => "服务端内部错误",
        502 => "网关错误，服务端暂时不可用",
        503 => "服务暂时不可用",
        _ => "请求失败",
    };
    if detail.is_empty() {
        format!("{base}（HTTP {code}）")
    } else {
        format!("{base}（HTTP {code}）：{detail}")
    }
}

/// 从错误正文里抠出"人话详情"。
fn error_detail(body: &str) -> String {
    let mut detail = String::new();
    if !body.is_empty() {
        if let Ok(data) = serde_json::from_str::<serde_json::Value>(body) {
            if let Some(obj) = data.as_object() {
                match obj.get("error") {
                    Some(serde_json::Value::Object(err)) => {
                        if let Some(msg) = err.get("message") {
                            detail = json_scalar_text(msg);
                        }
                    }
                    Some(serde_json::Value::String(text)) => detail = text.clone(),
                    _ => {}
                }
                if detail.is_empty() {
                    if let Some(msg) = obj.get("message") {
                        detail = json_scalar_text(msg);
                    }
                }
            }
        }
        if detail.is_empty() {
            // Python: `body[:180].replace("\n", " ")` —— 按**字符**截，只压换行
            detail = body.chars().take(180).collect::<String>().replace('\n', " ");
        }
    }
    detail
}

/// `str(value)` 的近似：字符串原样，其它按 JSON 标量转。
fn json_scalar_text(value: &serde_json::Value) -> String {
    match value {
        serde_json::Value::String(text) => text.clone(),
        serde_json::Value::Null => "None".to_string(),
        serde_json::Value::Bool(flag) => {
            if *flag {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        other => other.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reason_prefers_error_message() {
        let body = r#"{"error": {"message": "Incorrect API key provided"}}"#;
        assert_eq!(
            http_reason(401, body),
            "API Key 无效或已过期（HTTP 401）：Incorrect API key provided"
        );
    }

    #[test]
    fn reason_accepts_string_error_and_top_message() {
        assert!(http_reason(400, r#"{"error":"model not found"}"#).ends_with("：model not found"));
        assert!(http_reason(422, r#"{"message":"bad param"}"#).ends_with("：bad param"));
    }

    #[test]
    fn reason_falls_back_to_body_prefix() {
        let text = "x".repeat(300);
        let got = http_reason(500, &text);
        assert_eq!(got, format!("服务端内部错误（HTTP 500）：{}", "x".repeat(180)));
    }

    #[test]
    fn reason_without_body_has_no_colon() {
        assert_eq!(http_reason(404, ""), "地址不对（404）：检查基础 URL 与模型名（HTTP 404）");
        assert_eq!(http_reason(599, ""), "请求失败（HTTP 599）");
    }
}
