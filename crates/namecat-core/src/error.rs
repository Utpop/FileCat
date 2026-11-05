//! 统一错误类型。
//!
//! 说明（与设计稿的一处**有意偏离**，原因写在这里免得下一位 Agent 又改回去）：
//! 设计稿 §4.3 说"跨边界统一映射为 `EngineError`"。但 `engine/*.py` 现在对
//! 用户侧错误（未知规则、非法文件名、LLM 回复格式异常）**一律 raise
//! `ValueError`**，`verify/test_edges.py` 里也有 `except ValueError` 的断言。
//! 为了让"行为 100% 对齐"这条硬约束优先，绑定层把 [`Error::InvalidRule`] /
//! [`Error::InvalidName`] / [`Error::Conflict`] / [`Error::Llm`] 映射成
//! `ValueError`；只有 panic（catch_unwind 兜住的）才转成自定义 `EngineError`。

/// 引擎统一错误。文案直接沿用 Python 版的中文提示，便于界面原样展示。
#[derive(Debug, thiserror::Error)]
pub enum Error {
    /// 规则种类未知 / 规则参数非法（Python 版：`raise ValueError("未知规则：…")`）。
    #[error("{0}")]
    InvalidRule(String),

    /// 文件名为空、含非法字符、超长等（Python 版：`ValueError`）。
    #[error("{0}")]
    InvalidName(String),

    /// 目标名冲突（同一批次内重名、或磁盘上已存在）。
    #[error("{0}")]
    Conflict(String),

    /// LLM 交互失败/回复格式异常（Python 版：`ValueError("回复格式异常：…")`）。
    #[error("{0}")]
    Llm(String),

    /// 磁盘操作失败。`#[from]` 让 `?` 直接把 io::Error 抬上来。
    #[error("IO 错误：{0}")]
    Io(#[from] std::io::Error),

    /// JSON 解析/序列化失败（能兜底的场景应在调用处兜底，不该走到这里）。
    #[error("数据格式错误：{0}")]
    Json(String),

    /// 兜底：不该发生的内部不一致（panic 兜住后也归到这里）。
    #[error("{0}")]
    Internal(String),
}

pub type Result<T> = std::result::Result<T, Error>;

impl Error {
    /// 归类，供绑定层选择 Python 异常类型（见模块头注释）。
    pub fn kind(&self) -> ErrorKind {
        match self {
            Error::InvalidRule(_) | Error::InvalidName(_) | Error::Conflict(_) | Error::Llm(_) => {
                ErrorKind::Value
            }
            Error::Io(_) => ErrorKind::Os,
            Error::Json(_) | Error::Internal(_) => ErrorKind::Internal,
        }
    }
}

/// 错误大类 → 绑定层的异常类型。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    /// → `ValueError`（与现实现的报错类型一致）
    Value,
    /// → `OSError`
    Os,
    /// → `EngineError`（自定义异常，正常路径不该出现）
    Internal,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn io_error_keeps_message_prefix() {
        let err = Error::from(std::io::Error::new(std::io::ErrorKind::NotFound, "nope"));
        assert!(err.to_string().starts_with("IO 错误："));
        assert_eq!(err.kind(), ErrorKind::Os);
    }

    #[test]
    fn rule_errors_are_value_errors() {
        assert_eq!(Error::InvalidRule("未知规则：x".into()).kind(), ErrorKind::Value);
        assert_eq!(Error::Internal("boom".into()).kind(), ErrorKind::Internal);
    }
}
