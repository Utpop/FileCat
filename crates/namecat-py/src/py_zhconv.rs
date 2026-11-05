//! `namecat_py.zhconv` —— 对应 `engine/zhconv.py`（P1 第一个换壳模块）。

use namecat_core::zhconv as core;
use pyo3::prelude::*;
use pyo3::types::PyModule;

/// 按模式逐字转换。mode: `s2t` = 简转繁，`t2s` = 繁转简；其它值原样返回。
#[pyfunction]
#[pyo3(signature = (text, mode = "s2t"))]
fn convert(text: &str, mode: &str) -> String {
    core::convert(text, mode)
}

/// 换壳入口：填好子模块内容并声明 `__all__`。
pub fn attach(py: Python<'_>, parent: &Bound<'_, PyModule>) -> PyResult<()> {
    let m = crate::shim::new_submodule(py, parent, "zhconv")?;
    m.add(
        "__doc__",
        "简繁单字转换（Rust 实现）。数据表取自 OpenCC 的 STCharacters / TSCharacters。",
    )?;
    m.add_function(wrap_pyfunction!(convert, &m)?)?;
    crate::shim::set_all(&m, &["convert"])
}
