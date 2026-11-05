//! `engine/secrets.py` 的绑定：本地密钥的存取（DPAPI）。
//!
//! 密钥文件路径由 `engine.appdata_dir()` 决定（那里还负责旧目录迁移），
//! 加解密与磁盘格式在 [`namecat_core::secrets`]。
//!
//! 这里**不打印、不返回明文给日志**：只回 bool / str，出错一律 False / 空串。

use pyo3::prelude::*;

use namecat_core::secrets as core;

use crate::shim::appdata_dir;

#[pyfunction]
pub fn secret_path(py: Python<'_>) -> PyResult<String> {
    appdata_dir(py, &[core::SECRET_FILE])
}

/// 保存密钥。空串 = 保持原样不动。返回是否真的写盘了。
#[pyfunction]
fn save_secret(py: Python<'_>, text: &str) -> PyResult<bool> {
    Ok(core::save_secret(&secret_path(py)?, text))
}

/// 读回密钥；读不到（没有文件 / 解不开）一律返回空串，绝不抛异常。
#[pyfunction]
fn load_secret(py: Python<'_>) -> PyResult<String> {
    Ok(core::load_secret(&secret_path(py)?))
}

/// 删掉已保存的密钥（用户主动点「清除密钥」时才用）。
#[pyfunction]
fn clear_secret(py: Python<'_>) -> PyResult<bool> {
    Ok(core::clear_secret(&secret_path(py)?))
}

#[pyfunction]
fn has_secret(py: Python<'_>) -> PyResult<bool> {
    Ok(core::has_secret(&secret_path(py)?))
}

pub fn attach(py: Python<'_>, parent: &Bound<'_, pyo3::types::PyModule>) -> PyResult<()> {
    let m = crate::shim::new_submodule(py, parent, "secrets")?;
    m.add(
        "__doc__",
        "本地密钥的存取（Rust + Windows DPAPI）：不进日志、不进预设、不回显。",
    )?;
    m.add("SECRET_FILE", core::SECRET_FILE)?;
    m.add_function(wrap_pyfunction!(secret_path, &m)?)?;
    m.add_function(wrap_pyfunction!(save_secret, &m)?)?;
    m.add_function(wrap_pyfunction!(load_secret, &m)?)?;
    m.add_function(wrap_pyfunction!(clear_secret, &m)?)?;
    m.add_function(wrap_pyfunction!(has_secret, &m)?)?;

    crate::shim::set_all(
        &m,
        &[
            "SECRET_FILE",
            "clear_secret",
            "has_secret",
            "load_secret",
            "save_secret",
            "secret_path",
        ],
    )
}
