//! 绑定层公共设施：子模块挂载、错误映射、panic 兜底。
//!
//! 这一层**不实现任何算法**，只负责：
//! 1. 把 Rust 的类型/返回值搬成 Python 对象；
//! 2. 把 [`namecat_core::Error`] 映射成 Python 异常（类型要对齐现实现，见下）；
//! 3. 把 panic 拦在边界内，绝不让它穿到 Python 解释器。

// 这一层是给"迁移后"的 py_<module>.rs 用的公共设施；P0 阶段还没有调用方，
// 故整体屏蔽 dead_code（而不是让每条告警都挂着，掩盖真正的问题）。
#![allow(dead_code)]

use namecat_core::{Error, ErrorKind};
use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyModule;
use std::panic::{catch_unwind, AssertUnwindSafe};

pyo3::create_exception!(
    namecat_py,
    EngineError,
    pyo3::exceptions::PyException,
    "引擎内部错误（正常路径不该出现；多为 Rust 侧 panic 被兜住）"
);

/// `Error` → Python 异常。
///
/// 注意：`ValueError` 不是随手选的——`engine/*.py` 现在对用户侧错误一律
/// `raise ValueError`，`verify/test_edges.py` 也按 `ValueError` 断言。
pub fn into_pyerr(err: Error) -> PyErr {
    match err.kind() {
        ErrorKind::Value => PyValueError::new_err(err.to_string()),
        ErrorKind::Os => PyOSError::new_err(err.to_string()),
        ErrorKind::Internal => EngineError::new_err(err.to_string()),
    }
}

/// 把 `namecat_core::Result<T>` 转成 `PyResult<T>`。
pub fn wrap<T>(result: namecat_core::Result<T>) -> PyResult<T> {
    result.map_err(into_pyerr)
}

/// 包一层 `catch_unwind`：任何 panic 都变成 `EngineError`，不越过边界。
///
/// 绑定层的每个 `#[pyfunction]` 都用它包住核心调用。
pub fn guard<T>(f: impl FnOnce() -> PyResult<T>) -> PyResult<T> {
    match catch_unwind(AssertUnwindSafe(f)) {
        Ok(result) => result,
        Err(payload) => {
            let detail = panic_message(payload.as_ref());
            Err(EngineError::new_err(format!("引擎内部错误：{detail}")))
        }
    }
}

fn panic_message(payload: &(dyn std::any::Any + Send)) -> String {
    if let Some(text) = payload.downcast_ref::<&str>() {
        (*text).to_string()
    } else if let Some(text) = payload.downcast_ref::<String>() {
        text.clone()
    } else {
        "未知 panic".to_string()
    }
}

/// 建一个子模块、挂到父模块上，并注册进 `sys.modules`。
///
/// 第三步是关键：PyO3 的 `add_submodule` 只让 `namecat_py.rules` 能用属性访问
/// 到，**不注册 sys.modules 就不能 `import namecat_py.rules`**，而 strangler
/// 壳要写的正是 `from namecat_py.rules import *`。
pub fn new_submodule<'py>(
    py: Python<'py>,
    parent: &Bound<'py, PyModule>,
    name: &str,
) -> PyResult<Bound<'py, PyModule>> {
    let module = PyModule::new(py, name)?;
    parent.add_submodule(&module)?;
    let full_name = format!("{}.{}", parent.name()?, name);
    py.import("sys")?
        .getattr("modules")?
        .set_item(full_name, &module)?;
    Ok(module)
}

/// 声明 `__all__`，并顺手校验每个名字真的挂在模块上。
///
/// 为什么自己维护：PyO3 给模块预设的 `__all__` 只含 `__doc__`，不反映我们用
/// `module.add()` 挂进去的东西。而 strangler 壳写的是
/// `from namecat_py.rules import *` —— `__all__` 不对就等于什么都没导入。
/// 这里显式声明 + 逐个 `hasattr` 校验，漏名字在自检阶段就炸，不会等到界面上
/// 才 AttributeError。
pub fn set_all(module: &Bound<'_, PyModule>, names: &[&str]) -> PyResult<()> {
    for name in names {
        if !module.hasattr(*name)? {
            return Err(EngineError::new_err(format!(
                "{} 声明了 {name} 但模块里没有这个符号",
                module.name()?
            )));
        }
    }
    module.add("__all__", names.to_vec())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// cargo test 不会自动起解释器（`extension-module` 关掉了 auto-initialize），
    /// 手动初始化一次。正式加载时解释器本来就在跑，不需要这一步。
    fn init() {
        Python::initialize();
    }

    #[test]
    fn value_errors_map_to_value_error() {
        init();
        let err = into_pyerr(Error::InvalidRule("未知规则：x".into()));
        Python::attach(|py| {
            assert!(err.value(py).is_instance_of::<pyo3::exceptions::PyValueError>());
        });
    }

    #[test]
    fn guard_turns_panic_into_engine_error() {
        init();
        Python::attach(|_py| {
            let result: PyResult<()> = guard(|| panic!("boom"));
            let err = result.unwrap_err();
            assert!(err.to_string().contains("boom"));
        });
    }

    #[test]
    fn set_all_rejects_unknown_name() {
        init();
        Python::attach(|py| {
            let module = PyModule::new(py, "demo").unwrap();
            module.add("present", 1).unwrap();
            assert!(set_all(&module, &["present"]).is_ok());
            assert!(set_all(&module, &["missing"]).is_err());
        });
    }
}

/// `engine.appdata_dir(*parts)` —— 用户数据目录。
///
/// 刻意**不**在 Rust 里重写一份：`engine/__init__.py` 里那段还负责旧目录
/// （NameKit → NameCat）的一次性迁移，两份实现迟早会走岔。
pub fn appdata_dir<'py>(py: Python<'py>, parts: &[&str]) -> PyResult<String> {
    let engine = py.import("engine")?;
    let args = pyo3::types::PyTuple::new(py, parts)?;
    engine.getattr("appdata_dir")?.call1(args)?.extract()
}

/// 给 pyclass 挂一个 `__dataclass_fields__`，让"按字段名过滤"的老代码继续能用。
///
/// `engine/presets.py` 里写的是 `if k in ScanOptions.__dataclass_fields__`：
/// 换了实现之后这两个类不再是 dataclass，但那个判断只用到"键在不在"，
/// 所以给一个同名 dict 就够（值是 None，不做 `dataclasses.fields()` 那套）。
pub fn attach_field_names(
    module: &Bound<'_, PyModule>,
    class: &str,
    names: &[&str],
) -> PyResult<()> {
    let cls = module.getattr(class)?;
    let fields = pyo3::types::PyDict::new(module.py());
    for name in names {
        fields.set_item(*name, module.py().None())?;
    }
    cls.setattr("__dataclass_fields__", fields)
}
