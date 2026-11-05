//! `namecat_py.rules` —— 对应 `engine/rules.py`（P1 收口）。
//!
//! 边界设计上的一个硬约束：Python 侧（`adapters/engine_adapter.py`、
//! `engine/cli.py`）会**原地改写** `rule.extras`：
//!
//! ```python
//! rule.extras.setdefault("lang", "zh")     # 适配层
//! r.extras["instruction"] = body           # 命令行
//! ```
//!
//! 所以 `extras` 必须是一个**活的 dict 对象**，不能每次 getter 都返回副本，
//! 否则这些写入会静默丢失。这里让 `PyRule` 直接持有 `Py<PyDict>`，其余字段
//! 走普通 Rust 字段；调用核心前把 dict 快照成 `serde_json::Map`，归一化后再
//! 写回同一个 dict（保持对象身份不变）。

use crate::pyctx::{bool_arg, coerce_i64, text_arg, wrap_ctx};
use crate::pyjson;
use crate::shim;
use namecat_core::rules as rules_core;
use namecat_core::Error;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyList, PyModule, PySet, PyTuple};
use serde_json::Value;

/// Python 侧的 `Rule`（与 dataclass 字段顺序一致）。
#[pyclass(module = "namecat_py.rules", name = "Rule")]
pub struct PyRule {
    #[pyo3(get, set)]
    kind: String,
    #[pyo3(get, set)]
    a: String,
    #[pyo3(get, set)]
    b: String,
    #[pyo3(get, set)]
    c: String,
    #[pyo3(get, set)]
    enabled: bool,
    #[pyo3(get, set)]
    id: String,
    #[pyo3(get, set)]
    selector: String,
    #[pyo3(get, set)]
    charset: String,
    #[pyo3(get, set)]
    occurrence: i64,
    #[pyo3(get, set)]
    skip_ext: bool,
    #[pyo3(get, set)]
    keep_sep: bool,
    extras: Py<PyDict>,
}

impl PyRule {
    fn from_core(py: Python<'_>, rule: rules_core::Rule) -> PyResult<Self> {
        let extras = pyjson::map_to_dict(py, &rule.extras)?.unbind();
        Ok(Self {
            kind: rule.kind,
            a: rule.a,
            b: rule.b,
            c: rule.c,
            enabled: rule.enabled,
            id: rule.id,
            selector: rule.selector,
            charset: rule.charset,
            occurrence: rule.occurrence,
            skip_ext: rule.skip_ext,
            keep_sep: rule.keep_sep,
            extras,
        })
    }

    fn to_core(&self, py: Python<'_>) -> rules_core::Rule {
        rules_core::Rule {
            kind: self.kind.clone(),
            a: self.a.clone(),
            b: self.b.clone(),
            c: self.c.clone(),
            enabled: self.enabled,
            id: self.id.clone(),
            selector: self.selector.clone(),
            charset: self.charset.clone(),
            occurrence: self.occurrence,
            skip_ext: self.skip_ext,
            keep_sep: self.keep_sep,
            extras: pyjson::dict_to_map(Some(self.extras.bind(py).as_any())),
        }
    }

    /// 把核心规则写回本对象（`extras` 原地清空重填，保持 dict 身份）。
    fn assign(&mut self, py: Python<'_>, rule: rules_core::Rule) -> PyResult<()> {
        self.kind = rule.kind;
        self.a = rule.a;
        self.b = rule.b;
        self.c = rule.c;
        self.enabled = rule.enabled;
        self.id = rule.id;
        self.selector = rule.selector;
        self.charset = rule.charset;
        self.occurrence = rule.occurrence;
        self.skip_ext = rule.skip_ext;
        self.keep_sep = rule.keep_sep;
        let dict = self.extras.bind(py);
        dict.clear();
        for (k, v) in &rule.extras {
            dict.set_item(k, pyjson::to_py(py, v)?)?;
        }
        Ok(())
    }
}

#[pymethods]
impl PyRule {
    #[new]
    #[pyo3(signature = (kind = None, a = None, b = None, c = None, enabled = None,
                        id = None, selector = None, charset = None, occurrence = None,
                        skip_ext = None, keep_sep = None, extras = None))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        kind: Option<&Bound<'_, PyAny>>,
        a: Option<&Bound<'_, PyAny>>,
        b: Option<&Bound<'_, PyAny>>,
        c: Option<&Bound<'_, PyAny>>,
        enabled: Option<&Bound<'_, PyAny>>,
        id: Option<&Bound<'_, PyAny>>,
        selector: Option<&Bound<'_, PyAny>>,
        charset: Option<&Bound<'_, PyAny>>,
        occurrence: Option<&Bound<'_, PyAny>>,
        skip_ext: Option<&Bound<'_, PyAny>>,
        keep_sep: Option<&Bound<'_, PyAny>>,
        extras: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Self> {
        let extras: Py<PyDict> = match extras {
            Some(obj) if obj.cast::<PyDict>().is_ok() => obj.clone().cast_into::<PyDict>()?.unbind(),
            // 非字典（含 None）一律空表：手工构造 `Rule(extras=None)` 不能崩，
            // 这是 test_edges 里钉过的行为
            _ => PyDict::new(py).unbind(),
        };
        Ok(Self {
            kind: text_arg(kind),
            a: text_arg(a),
            b: text_arg(b),
            c: text_arg(c),
            enabled: bool_arg(enabled, true),
            id: text_arg(id),
            selector: text_arg(selector),
            charset: text_arg(charset),
            occurrence: coerce_i64(occurrence).unwrap_or(1),
            skip_ext: bool_arg(skip_ext, false),
            keep_sep: bool_arg(keep_sep, false),
            extras,
        })
    }

    #[getter]
    fn get_extras(&self, py: Python<'_>) -> Py<PyDict> {
        self.extras.clone_ref(py)
    }

    #[setter]
    fn set_extras(&mut self, py: Python<'_>, value: Option<&Bound<'_, PyAny>>) -> PyResult<()> {
        self.extras = match value {
            Some(obj) if obj.cast::<PyDict>().is_ok() => obj.clone().cast_into::<PyDict>()?.unbind(),
            _ => PyDict::new(py).unbind(),
        };
        Ok(())
    }

    fn to_dict(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let rule = rules_core::to_dict(&self.to_core(py));
        pyjson::to_py(py, &rule)
    }

    fn describe(&self, py: Python<'_>) -> String {
        rules_core::describe(&self.to_core(py))
    }

    /// `_number_bits()`：发号参数的一串短说明（编号 / 序列化共用）。
    fn _number_bits(&self, py: Python<'_>) -> Vec<String> {
        rules_core::number_bits(&self.to_core(py))
    }

    /// 与 dataclass 的 repr 保持同样的字段顺序与格式（报错信息里会看到它）。
    fn __repr__(&self, py: Python<'_>) -> String {
        let extras = self.extras.bind(py);
        format!(
            "Rule(kind='{}', a='{}', b='{}', c='{}', enabled={}, id='{}', selector='{}', \
             charset='{}', occurrence={}, skip_ext={}, keep_sep={}, extras={:?})",
            self.kind,
            self.a,
            self.b,
            self.c,
            if self.enabled { "True" } else { "False" },
            self.id,
            self.selector,
            self.charset,
            self.occurrence,
            if self.skip_ext { "True" } else { "False" },
            if self.keep_sep { "True" } else { "False" },
            extras
        )
    }
}

fn core_rule(py: Python<'_>, rule: &Bound<'_, PyRule>) -> rules_core::Rule {
    rule.borrow().to_core(py)
}

#[pyfunction]
#[pyo3(signature = (kind, a = None, b = None, c = None, **kw))]
fn make_rule(
    py: Python<'_>,
    kind: Option<&Bound<'_, PyAny>>,
    a: Option<&Bound<'_, PyAny>>,
    b: Option<&Bound<'_, PyAny>>,
    c: Option<&Bound<'_, PyAny>>,
    kw: Option<&Bound<'_, PyDict>>,
) -> PyResult<PyRule> {
    // 等价于 normalize(Rule(kind=kind, a=a, b=b, c=c, **kw))。
    // kw 里不可能再出现 kind/a/b/c（Python 会报重复关键字），只处理其余字段。
    let mut rule = rules_core::Rule {
        kind: text_arg(kind),
        a: text_arg(a),
        b: text_arg(b),
        c: text_arg(c),
        ..rules_core::Rule::default()
    };
    if let Some(kw) = kw {
        for (key, value) in kw.iter() {
            let key: String = key.extract().unwrap_or_default();
            match key.as_str() {
                "enabled" => rule.enabled = value.is_truthy()?,
                "id" => rule.id = value.extract().unwrap_or_default(),
                "selector" => rule.selector = value.extract().unwrap_or_default(),
                "charset" => rule.charset = value.extract().unwrap_or_default(),
                "occurrence" => rule.occurrence = coerce_i64(Some(&value)).unwrap_or(1),
                "skip_ext" => rule.skip_ext = value.is_truthy()?,
                "keep_sep" => rule.keep_sep = value.is_truthy()?,
                "extras" => rule.extras = pyjson::dict_to_map(Some(&value)),
                _ => {}
            }
        }
    }
    PyRule::from_core(py, rules_core::normalize(rule))
}

/// 补齐缺省字段、把别名映射成规范 kind。**原地修改并返回同一个对象**
/// （Python 版返回 self，调用方普遍写 `rule = normalize(rule)`，返回 None 会炸）。
#[pyfunction]
fn normalize(py: Python<'_>, rule: &Bound<'_, PyRule>) -> PyResult<Py<PyAny>> {
    let snapshot = { rule.borrow().to_core(py) };
    let normalized = rules_core::normalize(snapshot);
    rule.borrow_mut().assign(py, normalized)?;
    Ok(rule.clone().unbind().into_any())
}

#[pyfunction]
fn new_id(seed: &str) -> String {
    rules_core::new_id(seed)
}

#[pyfunction]
#[pyo3(signature = (text))]
fn translit(text: &str) -> String {
    rules_core::translit(text)
}

#[pyfunction]
#[pyo3(signature = (text, sep = "_"))]
fn slugify(text: &str, sep: &str) -> String {
    rules_core::slugify(text, sep)
}

#[pyfunction]
#[pyo3(signature = (text, repl = "_"))]
fn clean_filename(text: &str, repl: &str) -> String {
    rules_core::clean_filename(text, repl)
}

#[pyfunction]
#[pyo3(signature = (text, start, count, dest))]
fn move_chars(
    text: &str,
    start: Option<&Bound<'_, PyAny>>,
    count: Option<&Bound<'_, PyAny>>,
    dest: Option<&Bound<'_, PyAny>>,
) -> String {
    match (coerce_i64(start), coerce_i64(count), coerce_i64(dest)) {
        (Some(s), Some(n), Some(d)) => rules_core::move_chars(text, s, n, d),
        _ => text.to_string(),
    }
}

#[pyfunction]
#[pyo3(signature = (text, width, fill = "0"))]
fn pad_numbers(text: &str, width: Option<&Bound<'_, PyAny>>, fill: &str) -> String {
    match coerce_i64(width) {
        Some(w) => rules_core::pad_numbers(text, w, fill),
        None => text.to_string(),
    }
}

#[pyfunction]
#[pyo3(signature = (path, algo))]
fn file_digest(path: &str, algo: &str) -> String {
    rules_core::file_digest(path, algo)
}

#[pyfunction]
#[pyo3(signature = (rule, text, num = 1, width = 1, ctx = None, index = None))]
fn transform(
    py: Python<'_>,
    rule: &Bound<'_, PyRule>,
    text: &str,
    num: Option<i64>,
    width: Option<i64>,
    ctx: Option<&Bound<'_, PyAny>>,
    index: Option<i64>,
) -> String {
    let _ = index;
    let source = wrap_ctx(ctx);
    rules_core::transform(
        &core_rule(py, rule),
        text,
        num.unwrap_or(1),
        width.unwrap_or(1),
        source.as_ref().map(|s| s as &dyn namecat_core::ctx::VarSource),
    )
}

#[pyfunction]
#[pyo3(signature = (rule, text, num = 1, width = 1, ctx = None, index = None))]
fn apply_rule(
    py: Python<'_>,
    rule: &Bound<'_, PyRule>,
    text: &str,
    num: Option<i64>,
    width: Option<i64>,
    ctx: Option<&Bound<'_, PyAny>>,
    index: Option<i64>,
) -> String {
    let _ = index;
    let source = wrap_ctx(ctx);
    rules_core::apply_rule(
        &core_rule(py, rule),
        text,
        num.unwrap_or(1),
        width.unwrap_or(1),
        source.as_ref().map(|s| s as &dyn namecat_core::ctx::VarSource),
    )
}

/// 归一化一条规则并**就地改写** Python 对象，返回归一化后的快照。
///
/// `engine/plan.py` 的 `[R.normalize(r) for r in rules]` 既有返回值也有副作用
/// （别名 kind 会被写回对象），plan 阶段必须复刻这一点。
pub fn normalize_rule(py: Python<'_>, rule: &Bound<'_, PyRule>) -> PyResult<rules_core::Rule> {
    let snapshot = rule.borrow().to_core(py);
    let normalized = rules_core::normalize(snapshot);
    rule.borrow_mut().assign(py, normalized.clone())?;
    Ok(normalized)
}

fn collect_rules(py: Python<'_>, rules: &Bound<'_, PyAny>) -> PyResult<Vec<rules_core::Rule>> {
    let mut out = Vec::new();
    if rules.is_none() {
        return Ok(out);
    }
    for item in rules.try_iter()? {
        let item = item?;
        if let Ok(rule) = item.cast::<PyRule>() {
            out.push(rule.borrow().to_core(py));
        }
    }
    Ok(out)
}

#[pyfunction]
#[pyo3(signature = (rules, text, num = 1, width = 1, ctx = None, index = None, split_ext = true))]
fn apply_chain(
    py: Python<'_>,
    rules: &Bound<'_, PyAny>,
    text: &str,
    num: Option<i64>,
    width: Option<i64>,
    ctx: Option<&Bound<'_, PyAny>>,
    index: Option<i64>,
    split_ext: bool,
) -> PyResult<String> {
    let _ = index;
    let core_rules = collect_rules(py, rules)?;
    let source = wrap_ctx(ctx);
    Ok(rules_core::apply_chain(
        &core_rules,
        text,
        num.unwrap_or(1),
        width.unwrap_or(1),
        source.as_ref().map(|s| s as &dyn namecat_core::ctx::VarSource),
        split_ext,
    ))
}

#[pyfunction]
#[pyo3(signature = (name, rules, num = 1, width = 1, **kw))]
fn apply_rules(
    py: Python<'_>,
    name: &str,
    rules: &Bound<'_, PyAny>,
    num: Option<i64>,
    width: Option<i64>,
    kw: Option<&Bound<'_, PyDict>>,
) -> PyResult<String> {
    let _ = kw;
    let core_rules = collect_rules(py, rules)?;
    Ok(rules_core::apply_chain(
        &core_rules,
        name,
        num.unwrap_or(1),
        width.unwrap_or(1),
        None,
        true,
    ))
}

#[pyfunction]
fn number_rule(py: Python<'_>, rules: &Bound<'_, PyAny>) -> PyResult<Option<Py<PyAny>>> {
    if rules.is_none() {
        return Ok(None);
    }
    for item in rules.try_iter()? {
        let item = item?;
        if let Ok(rule) = item.cast::<PyRule>() {
            let borrowed = rule.borrow();
            if matches!(borrowed.kind.as_str(), "number" | "renumber") && borrowed.enabled {
                return Ok(Some(item.clone().unbind()));
            }
        }
    }
    let _ = py;
    Ok(None)
}

#[pyfunction]
fn number_options<'py>(py: Python<'py>, rule: &Bound<'_, PyRule>) -> PyResult<Bound<'py, PyDict>> {
    let opts = rules_core::number_options(&core_rule(py, rule));
    let dict = PyDict::new(py);
    if let Some(v) = opts.start {
        dict.set_item("start", v)?;
    }
    if let Some(v) = opts.step {
        dict.set_item("step", v)?;
    }
    if let Some(v) = opts.width {
        dict.set_item("width", v)?;
    }
    if let Some(v) = opts.scope {
        dict.set_item("scope", v)?;
    }
    if opts.reverse {
        dict.set_item("reverse", true)?;
    }
    Ok(dict)
}

#[pyfunction]
fn rules_to_json(py: Python<'_>, rules: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let core_rules = collect_rules(py, rules)?;
    let list = PyList::empty(py);
    for value in rules_core::rules_to_json(&core_rules) {
        list.append(pyjson::to_py(py, &value)?)?;
    }
    Ok(list.into_any().unbind())
}

#[pyfunction]
fn rules_from_json(py: Python<'_>, data: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let mut values: Vec<Value> = Vec::new();
    if !data.is_none() {
        for item in data.try_iter()? {
            values.push(pyjson::to_value(&item?));
        }
    }
    let list = PyList::empty(py);
    for rule in rules_core::rules_from_json(&values) {
        list.append(PyRule::from_core(py, rule)?)?;
    }
    Ok(list.into_any().unbind())
}

#[pyfunction]
fn parse_cli_rule(py: Python<'_>, spec: &str) -> PyResult<PyRule> {
    match rules_core::parse_cli_rule(spec) {
        Ok(rule) => PyRule::from_core(py, rule),
        Err(err) => Err(match err {
            Error::InvalidRule(msg) => PyValueError::new_err(msg),
            other => shim::into_pyerr(other),
        }),
    }
}

#[pyfunction]
fn describe_chain(py: Python<'_>, rules: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(rules_core::describe_chain(&collect_rules(py, rules)?))
}

#[pyfunction]
fn describe_rules_table() -> String {
    rules_core::describe_rules_table()
}

/// 模块挂载：常量 + 类 + 函数。
pub fn attach(py: Python<'_>, parent: &Bound<'_, PyModule>) -> PyResult<()> {
    let m = shim::new_submodule(py, parent, "rules")?;
    m.add(
        "__doc__",
        "规则链（Rust 实现）：28 种规则、替换串解析、序列化与命令行写法。",
    )?;

    // ---- 规则类型表：list[(kind, 中文名, 参数提示 dict, 说明)] ----
    let kinds = PyList::empty(py);
    let kind_text = PyDict::new(py);
    let param_hint = PyDict::new(py);
    let notes = PyDict::new(py);
    let ids = PyList::empty(py);
    for kind in rules_core::RULE_KINDS {
        let params = PyDict::new(py);
        for (key, hint) in kind.params {
            params.set_item(*key, *hint)?;
        }
        let params_ref = params.clone();
        kinds.append(PyTuple::new(
            py,
            [
                kind.id.into_pyobject(py)?.into_any(),
                kind.text.into_pyobject(py)?.into_any(),
                params.into_any(),
                kind.note.into_pyobject(py)?.into_any(),
            ],
        )?)?;
        kind_text.set_item(kind.id, kind.text)?;
        param_hint.set_item(kind.id, params_ref)?;
        notes.set_item(kind.id, kind.note)?;
        ids.append(kind.id)?;
    }
    m.add("RULE_KINDS", kinds)?;
    m.add("RULE_KIND_TEXT", kind_text)?;
    m.add("RULE_PARAM_HINT", param_hint)?;
    m.add("RULE_NOTE", notes)?;
    m.add("RULE_KIND_IDS", ids)?;

    let aliases = PyDict::new(py);
    for (from, to) in rules_core::CLI_ALIASES {
        aliases.set_item(*from, *to)?;
    }
    m.add("CLI_ALIASES", aliases)?;

    m.add("INVALID_NAME_CHARS", rules_core::INVALID_NAME_CHARS)?;
    m.add("MAX_NAME_LEN", rules_core::MAX_NAME_LEN)?;
    m.add("MAX_RANDOM", rules_core::MAX_RANDOM)?;

    // RESERVED_NAMES 在 Python 里是 set；类型也要对上（== 比较会露馅）
    let reserved = PySet::empty(py)?;
    for name in ["con", "prn", "aux", "nul"] {
        reserved.add(name)?;
    }
    for i in 1..=9 {
        reserved.add(format!("com{i}"))?;
        reserved.add(format!("lpt{i}"))?;
    }
    m.add("RESERVED_NAMES", reserved)?;

    m.add_class::<PyRule>()?;
    m.add_function(wrap_pyfunction!(make_rule, &m)?)?;
    m.add_function(wrap_pyfunction!(normalize, &m)?)?;
    m.add_function(wrap_pyfunction!(new_id, &m)?)?;
    m.add_function(wrap_pyfunction!(translit, &m)?)?;
    m.add_function(wrap_pyfunction!(slugify, &m)?)?;
    m.add_function(wrap_pyfunction!(clean_filename, &m)?)?;
    m.add_function(wrap_pyfunction!(move_chars, &m)?)?;
    m.add_function(wrap_pyfunction!(pad_numbers, &m)?)?;
    m.add_function(wrap_pyfunction!(file_digest, &m)?)?;
    m.add_function(wrap_pyfunction!(transform, &m)?)?;
    m.add_function(wrap_pyfunction!(apply_rule, &m)?)?;
    m.add_function(wrap_pyfunction!(apply_chain, &m)?)?;
    m.add_function(wrap_pyfunction!(apply_rules, &m)?)?;
    m.add_function(wrap_pyfunction!(number_rule, &m)?)?;
    m.add_function(wrap_pyfunction!(number_options, &m)?)?;
    m.add_function(wrap_pyfunction!(rules_to_json, &m)?)?;
    m.add_function(wrap_pyfunction!(rules_from_json, &m)?)?;
    m.add_function(wrap_pyfunction!(parse_cli_rule, &m)?)?;
    m.add_function(wrap_pyfunction!(describe_chain, &m)?)?;
    m.add_function(wrap_pyfunction!(describe_rules_table, &m)?)?;

    shim::set_all(
        &m,
        &[
            "CLI_ALIASES",
            "INVALID_NAME_CHARS",
            "MAX_NAME_LEN",
            "MAX_RANDOM",
            "RESERVED_NAMES",
            "RULE_KINDS",
            "RULE_KIND_IDS",
            "RULE_KIND_TEXT",
            "RULE_NOTE",
            "RULE_PARAM_HINT",
            "Rule",
            "apply_chain",
            "apply_rule",
            "apply_rules",
            "clean_filename",
            "describe_chain",
            "describe_rules_table",
            "file_digest",
            "make_rule",
            "move_chars",
            "new_id",
            "normalize",
            "number_options",
            "number_rule",
            "pad_numbers",
            "parse_cli_rule",
            "rules_from_json",
            "rules_to_json",
            "slugify",
            "transform",
            "translit",
        ],
    )
}
