//! Python 对象 → core trait 的适配桥。
//!
//! `engine/context.py` 的 `NameContext` / `Item` 继续留在 Python（P2 才迁），
//! 但 Rust 的 template / rules 需要从它取变量。这里把"取变量"这一件事
//! 适配成 [`VarSource`]，核心因此完全不认识 Python。
//!
//! 异常语义照抄 Python 版：`ctx.var()` 抛异常时当空串（现实现到处是
//! `try: ... except Exception: val = ""`），不把异常放出去。

use std::sync::{Mutex, MutexGuard};

use crate::py_context::PyNameContext;
use namecat_core::context::NumArg;
use namecat_core::ctx::VarSource;
use namecat_core::template::MatchGroups;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict};

/// 忽略中毒的加锁（见 `py_context::lock` 的说明）。
pub fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// 上下文来源：自家 `NameContext` 走快路径，其它（`Item`、鸭子类型对象）
/// 走"调它的 `var()` 方法"这条通用路径。
pub enum CtxSource<'py> {
    Native(PyRef<'py, PyNameContext>),
    Duck(Bound<'py, PyAny>),
}

pub struct PyVarSource<'py> {
    obj: Bound<'py, PyAny>,
}

impl<'py> PyVarSource<'py> {
    pub fn new(obj: &Bound<'py, PyAny>) -> Self {
        Self { obj: obj.clone() }
    }

    fn attr_text(&self, name: &str) -> Option<String> {
        let value = self.obj.getattr(name).ok()?;
        if value.is_none() {
            return None;
        }
        // str(value)：Python 的 str() 与本函数只对文本有意义，非文本一律 None
        value.str().ok().map(|s| s.to_string_lossy().into_owned())
    }

    fn attr_int(&self, name: &str) -> Option<i64> {
        let value = self.obj.getattr(name).ok()?;
        value.extract::<i64>().ok()
    }
}

impl VarSource for PyVarSource<'_> {
    fn var(&self, key: &str) -> Option<String> {
        match self.obj.call_method1("var", (key,)) {
            Ok(v) => {
                if v.is_none() {
                    None
                } else {
                    v.str().ok().map(|s| s.to_string_lossy().into_owned())
                }
            }
            Err(_) => None,        // Python 版的 try/except：拿不到就当没有
        }
    }

    fn seed_key(&self) -> String {
        // Python: `getattr(ctx, "path", "") or getattr(ctx, "name", "")`
        let path = self.attr_text("path").unwrap_or_default();
        if !path.is_empty() {
            return path;
        }
        self.attr_text("name").unwrap_or_default()
    }

    fn path(&self) -> Option<String> {
        self.attr_text("path")
    }

    fn is_dir(&self) -> Option<bool> {
        self.obj.getattr("is_dir").ok().and_then(|v| v.extract::<bool>().ok())
    }

    fn num(&self) -> Option<i64> {
        self.attr_int("num")
    }

    fn width(&self) -> Option<i64> {
        self.attr_int("width")
    }

    fn extra(&self, key: &str) -> Option<String> {
        let dict = self.obj.getattr("extra").ok()?.cast_into::<PyDict>().ok()?;
        let value = dict.get_item(key).ok()??;
        if value.is_none() {
            return None;
        }
        value.str().ok().map(|s| s.to_string_lossy().into_owned())
    }

    fn extra_dict_get(&self, dict_key: &str, key: &str) -> Option<String> {
        let extra = self.obj.getattr("extra").ok()?.cast_into::<PyDict>().ok()?;
        let inner = extra.get_item(dict_key).ok()??;
        let inner = inner.cast_into::<PyDict>().ok()?;
        let value = inner.get_item(key).ok()??;
        if value.is_none() {
            return None;
        }
        value.str().ok().map(|s| s.to_string_lossy().into_owned())
    }
}

impl VarSource for CtxSource<'_> {
    fn var(&self, key: &str) -> Option<String> {
        match self {
            CtxSource::Native(c) => lock(&c.inner).var(key),
            CtxSource::Duck(o) => PyVarSource::new(o).var(key),
        }
    }

    fn seed_key(&self) -> String {
        match self {
            CtxSource::Native(c) => {
                let inner = lock(&c.inner);
                if !inner.path.is_empty() {
                    return inner.path.clone();
                }
                inner.name.clone()
            }
            CtxSource::Duck(o) => PyVarSource::new(o).seed_key(),
        }
    }

    fn path(&self) -> Option<String> {
        match self {
            CtxSource::Native(c) => Some(lock(&c.inner).path.clone()),
            CtxSource::Duck(o) => PyVarSource::new(o).path(),
        }
    }

    fn is_dir(&self) -> Option<bool> {
        match self {
            CtxSource::Native(c) => Some(lock(&c.inner).is_dir),
            CtxSource::Duck(o) => PyVarSource::new(o).is_dir(),
        }
    }

    fn num(&self) -> Option<i64> {
        match self {
            CtxSource::Native(c) => Some(match &lock(&c.inner).num {
                Some(NumArg::Int(n)) => *n,
                _ => 1,
            }),
            CtxSource::Duck(o) => PyVarSource::new(o).num(),
        }
    }

    fn width(&self) -> Option<i64> {
        match self {
            CtxSource::Native(c) => Some(lock(&c.inner).width),
            CtxSource::Duck(o) => PyVarSource::new(o).width(),
        }
    }

    fn extra(&self, key: &str) -> Option<String> {
        match self {
            CtxSource::Native(c) => {
                let dict = lock(&c.extra);
                Python::attach(|py| {
                    let v = dict.bind(py).get_item(key).ok()??;
                    if v.is_none() {
                        return None;
                    }
                    v.str().ok().map(|s| s.to_string_lossy().into_owned())
                })
            }
            CtxSource::Duck(o) => PyVarSource::new(o).extra(key),
        }
    }

    fn extra_dict_get(&self, dict_key: &str, key: &str) -> Option<String> {
        match self {
            CtxSource::Native(c) => {
                let dict = lock(&c.extra);
                Python::attach(|py| {
                    let d = dict.bind(py);
                    let inner = d.get_item(dict_key).ok()??;
                    let inner = inner.cast_into::<PyDict>().ok()?;
                    let v = inner.get_item(key).ok()??;
                    if v.is_none() {
                        return None;
                    }
                    v.str().ok().map(|s| s.to_string_lossy().into_owned())
                })
            }
            CtxSource::Duck(o) => PyVarSource::new(o).extra_dict_get(dict_key, key),
        }
    }
}

/// 把 `Option<&Bound<PyAny>>` 包成 `Option<CtxSource>` 的便捷入口。
pub fn wrap_ctx<'py>(ctx: Option<&Bound<'py, PyAny>>) -> Option<CtxSource<'py>> {
    ctx.map(|c| match c.extract::<PyRef<PyNameContext>>() {
        Ok(native) => CtxSource::Native(native),
        Err(_) => CtxSource::Duck(c.clone()),
    })
}

/// `re.Match` → [`MatchGroups`]（取不到组就返回 None，对齐 Python 的
/// `except (IndexError, re.error): val = None`）。
pub struct PyMatch<'py> {
    inner: Bound<'py, PyAny>,
}

impl<'py> PyMatch<'py> {
    pub fn new(obj: &Bound<'py, PyAny>) -> Self {
        Self { inner: obj.clone() }
    }
}

impl MatchGroups for PyMatch<'_> {
    fn group(&self, index: usize) -> Option<String> {
        let value = self.inner.call_method1("group", (index,)).ok()?;
        if value.is_none() {
            return None;
        }
        value.str().ok().map(|s| s.to_string_lossy().into_owned())
    }
}

/// Python 的 `str(x)` 语义：None → 空串，字符串原样，其余转 str()。
///
/// 为什么要这么宽：`Rule(selector=3)` 这类脏构造在 `test_edges.py` 里明确要求
/// **不抛异常**（normalize 会把它回落成 "text"）。边界上若能收就收，
/// 校验留给 normalize —— 与 Python dataclass 的行为一致。
pub fn text_arg(value: Option<&Bound<'_, PyAny>>) -> String {
    let Some(value) = value else { return String::new() };
    if value.is_none() {
        return String::new();
    }
    match value.extract::<String>() {
        Ok(s) => s,
        Err(_) => value
            .str()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_default(),
    }
}

/// Python 的 `bool(x)` 语义。
pub fn bool_arg(value: Option<&Bound<'_, PyAny>>, default: bool) -> bool {
    match value {
        Some(value) => value.is_truthy().unwrap_or(default),
        None => default,
    }
}

/// Python 的 `int(x)` 语义：容忍数字字符串与浮点，失败返回 None。
pub fn coerce_i64(value: Option<&Bound<'_, PyAny>>) -> Option<i64> {
    let value = value?;
    if value.is_none() {
        return None;
    }
    if let Ok(n) = value.extract::<i64>() {
        return Some(n);
    }
    if let Ok(f) = value.extract::<f64>() {
        return Some(f as i64);
    }
    if let Ok(s) = value.extract::<String>() {
        if let Ok(n) = s.trim().parse::<i64>() {
            return Some(n);
        }
    }
    None
}
