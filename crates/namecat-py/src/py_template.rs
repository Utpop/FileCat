//! `namecat_py.template` —— 对应 `engine/template.py`（P1 第二个换壳模块）。
//!
//! 公开面与 Python 版逐字对齐：`cut_index / parse_range / select / splice /
//! split_half / canon_key / expand_escape / render / apply_num_format /
//! expand_refs` 与 `MAX_WIDTH / SELECTOR_KINDS / SELECTOR_TEXT / REF_KEYS`。
//!
//! 参数默认值也必须一致（`width=1` 而不是 `None`）：`render("{n}", ctx)` 省略
//! width 时用的是 1，不是 ctx.width —— 这是现有行为，界面/命令行都依赖它。

use crate::pyctx::{wrap_ctx, PyMatch};
use crate::shim;
use namecat_core::template as core;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyModule, PyTuple};

#[pyfunction]
#[pyo3(signature = (text, charset, occurrence = 1))]
fn cut_index(text: &str, charset: &str, occurrence: i64) -> i64 {
    core::cut_index(text, charset, occurrence)
}

#[pyfunction]
#[pyo3(signature = (expr, length))]
fn parse_range(expr: &str, length: i64) -> (i64, i64) {
    core::parse_range(expr, length)
}

#[pyfunction]
#[pyo3(signature = (text, mode, charset = "", occurrence = 1))]
fn select(text: &str, mode: &str, charset: &str, occurrence: i64) -> String {
    core::select(text, mode, charset, occurrence)
}

#[pyfunction]
#[pyo3(signature = (text, mode, charset, occurrence, new))]
fn splice(text: &str, mode: &str, charset: &str, occurrence: i64, new: &str) -> String {
    core::splice(text, mode, charset, occurrence, new)
}

#[pyfunction]
fn split_half(text: &str) -> String {
    core::split_half(text)
}

#[pyfunction]
fn canon_key(name: &str) -> String {
    core::canon_key(name)
}

#[pyfunction]
fn expand_escape(text: &str) -> String {
    core::expand_escape(text)
}

/// `{n:03}` 的补零格式；不匹配的格式原样返回。
#[pyfunction]
#[pyo3(signature = (value, spec))]
fn apply_num_format(value: &Bound<'_, PyAny>, spec: &str) -> String {
    // Python 的 str(value)：整数、字符串都走同一条路
    let text = value.str().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
    core::apply_num_format(&text, spec)
}

/// `_num_text(num, width)`：`engine/rules.py` 的 `_digits` 还在用这个私有函数，
/// 所以必须导出（不在 `__all__` 里，壳会显式 import 它）。
/// 语义：`int(num)` 失败当 1，宽度夹到 0..64，然后 zfill。
#[pyfunction]
#[pyo3(signature = (num, width))]
fn _num_text(num: Option<&Bound<'_, PyAny>>, width: Option<&Bound<'_, PyAny>>) -> String {
    core::num_text(crate::pyctx::coerce_i64(num).unwrap_or(1), crate::pyctx::coerce_i64(width).unwrap_or(0))
}

#[pyfunction]
#[pyo3(signature = (text, ctx = None, num = None, width = 1))]
fn render(
    text: Option<&str>,
    ctx: Option<&Bound<'_, PyAny>>,
    num: Option<i64>,
    width: Option<i64>,
) -> String {
    let text = text.unwrap_or("");
    let source = wrap_ctx(ctx);
    core::render(
        text,
        source.as_ref().map(|s| s as &dyn namecat_core::ctx::VarSource),
        num,
        width,
    )
}

#[pyfunction]
#[pyo3(signature = (repl, match_ = None, ctx = None, num = None, width = 1, left = "", right = ""))]
fn expand_refs(
    repl: &str,
    match_: Option<&Bound<'_, PyAny>>,
    ctx: Option<&Bound<'_, PyAny>>,
    num: Option<i64>,
    width: Option<i64>,
    left: &str,
    right: &str,
) -> PyResult<String> {
    let groups = match_.map(PyMatch::new);
    let source = wrap_ctx(ctx);
    let num = num.unwrap_or(1);
    let width = width.unwrap_or(0);
    Ok(shim::guard(|| {
        Ok(core::expand_refs(
            repl,
            groups.as_ref().map(|g| g as &dyn namecat_core::template::MatchGroups),
            source.as_ref().map(|s| s as &dyn namecat_core::ctx::VarSource),
            num,
            width,
            left,
            right,
        ))
    })?)
}

pub fn attach(py: Python<'_>, parent: &Bound<'_, PyModule>) -> PyResult<()> {
    let m = shim::new_submodule(py, parent, "template")?;
    m.add(
        "__doc__",
        "模板渲染与选择器（Rust 实现）。变量语法兼容 {x} / #x# / %x% / &x& / <x> / $Var(args)。",
    )?;
    m.add("MAX_WIDTH", core::MAX_WIDTH)?;
    m.add("SELECTOR_KINDS", core::SELECTOR_KINDS.to_vec())?;

    let sel = PyDict::new(py);
    for (k, v) in core::SELECTOR_TEXT {
        sel.set_item(*k, *v)?;
    }
    m.add("SELECTOR_TEXT", sel)?;
    // 类型要对齐：Python 侧是 tuple，列表会在 == 比较时露馅
    m.add("REF_KEYS", PyTuple::new(py, core::REF_KEYS)?)?;

    m.add_function(wrap_pyfunction!(cut_index, &m)?)?;
    m.add_function(wrap_pyfunction!(parse_range, &m)?)?;
    m.add_function(wrap_pyfunction!(select, &m)?)?;
    m.add_function(wrap_pyfunction!(splice, &m)?)?;
    m.add_function(wrap_pyfunction!(split_half, &m)?)?;
    m.add_function(wrap_pyfunction!(canon_key, &m)?)?;
    m.add_function(wrap_pyfunction!(expand_escape, &m)?)?;
    m.add_function(wrap_pyfunction!(apply_num_format, &m)?)?;
    m.add_function(wrap_pyfunction!(render, &m)?)?;
    m.add_function(wrap_pyfunction!(expand_refs, &m)?)?;
    m.add_function(wrap_pyfunction!(_num_text, &m)?)?;

    shim::set_all(
        &m,
        &[
            "MAX_WIDTH",
            "SELECTOR_KINDS",
            "SELECTOR_TEXT",
            "REF_KEYS",
            "cut_index",
            "parse_range",
            "select",
            "splice",
            "split_half",
            "canon_key",
            "expand_escape",
            "apply_num_format",
            "render",
            "expand_refs",
        ],
    )
}
