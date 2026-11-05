//! `engine/plan.py` 的绑定：`PlanOptions` / `Plan` 与各阶段函数。
//!
//! 分工：**算**在 core（`namecat_core::plan`），**写回对象**在这里。
//! 唯一还会回调 Python 的地方是两处：
//! · LLM 规则要批量问模型（`engine.llm_resolve`，P4 才迁）；
//! · 落盘前的两遍校验仍在 `engine.common.validate_items`（P3 才迁）。
//! 这两种回调都只发生在"整批一次"的粒度上，不在逐项循环里。

use namecat_core::plan as core;
use namecat_core::rules as rules_core;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyList, PySet, PyTuple};

use crate::py_context::{lock, PyItem, PyNameContext};
use crate::pyctx::{text_arg, wrap_ctx};
use crate::py_rules::PyRule;

// --------------------------------------------------------------------------- //
// PlanOptions
// --------------------------------------------------------------------------- //

#[pyclass(module = "namecat_py.plan", name = "PlanOptions", skip_from_py_object)]
#[derive(Clone)]
pub struct PyPlanOptions {
    #[pyo3(get, set)]
    pub start: i64,
    #[pyo3(get, set)]
    pub step: i64,
    #[pyo3(get, set)]
    pub width: i64,
    #[pyo3(get, set)]
    pub scope: String,
    #[pyo3(get, set)]
    pub reverse: bool,
    #[pyo3(get, set)]
    pub split_ext: bool,
    #[pyo3(get, set)]
    pub rename_dirs: bool,
    #[pyo3(get, set)]
    pub dry_run: bool,
}

impl PyPlanOptions {
    pub fn to_core(&self) -> core::PlanOptions {
        core::PlanOptions {
            start: self.start,
            step: self.step,
            width: self.width,
            scope: self.scope.clone(),
            reverse: self.reverse,
            split_ext: self.split_ext,
            rename_dirs: self.rename_dirs,
            dry_run: self.dry_run,
        }
    }

    pub fn from_core(o: &core::PlanOptions) -> Self {
        Self {
            start: o.start,
            step: o.step,
            width: o.width,
            scope: o.scope.clone(),
            reverse: o.reverse,
            split_ext: o.split_ext,
            rename_dirs: o.rename_dirs,
            dry_run: o.dry_run,
        }
    }
}

#[pymethods]
impl PyPlanOptions {
    #[new]
    #[pyo3(signature = (start = None, step = None, width = None, scope = None,
                        reverse = None, split_ext = None, rename_dirs = None,
                        dry_run = None))]
    fn new(
        start: Option<i64>,
        step: Option<i64>,
        width: Option<i64>,
        scope: Option<&Bound<'_, PyAny>>,
        reverse: Option<bool>,
        split_ext: Option<bool>,
        rename_dirs: Option<bool>,
        dry_run: Option<bool>,
    ) -> Self {
        let d = core::PlanOptions::default();
        let scope = {
            let s = text_arg(scope);
            if s.is_empty() { d.scope.clone() } else { s }
        };
        Self {
            start: start.unwrap_or(d.start),
            step: step.unwrap_or(d.step),
            width: width.unwrap_or(d.width),
            scope,
            reverse: reverse.unwrap_or(d.reverse),
            split_ext: split_ext.unwrap_or(d.split_ext),
            rename_dirs: rename_dirs.unwrap_or(d.rename_dirs),
            dry_run: dry_run.unwrap_or(d.dry_run),
        }
    }

    /// 与 Python 版的 `as_dict()` 一致：数值就是 int、开关就是 bool
    /// （撤销日志里存的是这个 dict，类型错了会让旧日志对不上）。
    fn as_dict(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let d = PyDict::new(py);
        d.set_item("start", self.start)?;
        d.set_item("step", self.step)?;
        d.set_item("width", self.width)?;
        d.set_item("scope", &self.scope)?;
        d.set_item("reverse", self.reverse)?;
        d.set_item("split_ext", self.split_ext)?;
        d.set_item("rename_dirs", self.rename_dirs)?;
        Ok(d.unbind())
    }

    fn __repr__(&self) -> String {
        format!(
            "PlanOptions(start={}, step={}, width={}, scope={:?}, reverse={}, rename_dirs={})",
            self.start, self.step, self.width, self.scope, self.reverse, self.rename_dirs
        )
    }
}

// --------------------------------------------------------------------------- //
// Plan
// --------------------------------------------------------------------------- //

#[pyclass(module = "namecat_py.plan", name = "Plan")]
pub struct PyPlan {
    #[pyo3(get, set)]
    pub items: Py<PyList>,
    #[pyo3(get, set)]
    pub report: Py<PyAny>,
    #[pyo3(get, set)]
    pub warnings: Py<PyList>,
}

#[pymethods]
impl PyPlan {
    #[new]
    #[pyo3(signature = (items = None, report = None, warnings = None))]
    fn new(
        py: Python<'_>,
        items: Option<&Bound<'_, PyAny>>,
        report: Option<&Bound<'_, PyAny>>,
        warnings: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Self> {
        Ok(Self {
            items: to_list(py, items)?,
            report: match report {
                Some(v) if !v.is_none() => v.clone().unbind(),
                _ => py.None(),
            },
            warnings: to_list(py, warnings)?,
        })
    }

    /// 真正会改名的 (旧路径, 新路径)。
    fn pairs(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        let out = PyList::empty(py);
        for item in self.items.bind(py).iter() {
            let it = item.cast::<PyItem>()?;
            let (changed, status) = (it.borrow().is_changed(), it.borrow().status.clone());
            if changed && status == "ok" {
                out.append(PyTuple::new(py, [item.getattr("path")?, item.getattr("new_path")?])?)?;
            }
        }
        Ok(out.unbind())
    }

    fn changing(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        let out = PyList::empty(py);
        for item in self.items.bind(py).iter() {
            let it = item.cast::<PyItem>()?;
            let ok = {
                let r = it.borrow();
                r.is_changed() && r.status == "ok"
            };
            if ok {
                out.append(item)?;
            }
        }
        Ok(out.unbind())
    }

    fn by_status(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let out = PyDict::new(py);
        for item in self.items.bind(py).iter() {
            let it = item.cast::<PyItem>()?;
            let status = it.borrow().status.clone();
            let bucket = match out.get_item(&status)? {
                Some(v) => v.cast_into::<PyList>()?,
                None => {
                    let fresh = PyList::empty(py);
                    out.set_item(&status, &fresh)?;
                    fresh
                }
            };
            bucket.append(item)?;
        }
        Ok(out.unbind())
    }

    fn summary(&self, py: Python<'_>) -> String {
        if !self.report.bind(py).is_none() {
            return self.report.bind(py).str().map(|s| s.to_string()).unwrap_or_default();
        }
        format!("{} 项", self.items.bind(py).len())
    }

    fn __len__(&self, py: Python<'_>) -> usize {
        self.items.bind(py).len()
    }
}

// --------------------------------------------------------------------------- //
// 小工具
// --------------------------------------------------------------------------- //

/// 把任意可迭代对象收成 Python list（调用方可能传 list / tuple / 生成器）。
fn to_list(py: Python<'_>, value: Option<&Bound<'_, PyAny>>) -> PyResult<Py<PyList>> {
    let Some(v) = value else {
        return Ok(PyList::empty(py).unbind());
    };
    if v.is_none() {
        return Ok(PyList::empty(py).unbind());
    }
    if let Ok(l) = v.cast::<PyList>() {
        return Ok(l.clone().unbind());
    }
    let out = PyList::empty(py);
    for item in v.try_iter()? {
        out.append(item?)?;
    }
    Ok(out.unbind())
}

fn item_at<'py>(list: &Bound<'py, PyList>, i: usize) -> PyResult<Bound<'py, PyItem>> {
    let obj = list.get_item(i)?;
    Ok(obj.cast_into::<PyItem>()?)
}

/// 收集规则：既做归一化（原地，与 `[R.normalize(r) ...]` 的副作用一致），
/// 又保留 Python 对象引用（LLM 阶段要用 `rule.id` / `rule.selector`）。
fn collect_chain<'py>(
    py: Python<'py>,
    rules: Option<&Bound<'py, PyAny>>,
) -> PyResult<(Vec<rules_core::Rule>, Vec<Bound<'py, PyRule>>)> {
    let mut chain = Vec::new();
    let mut objs: Vec<Bound<'py, PyRule>> = Vec::new();
    let Some(rs) = rules else {
        return Ok((chain, objs));
    };
    if rs.is_none() {
        return Ok((chain, objs));
    }
    for item in rs.try_iter()? {
        let item = item?;
        if let Ok(rule) = item.cast::<PyRule>() {
            chain.push(crate::py_rules::normalize_rule(py, rule)?);
            objs.push(rule.clone());
        }
    }
    Ok((chain, objs))
}

/// 发号：算在 core，写回在 item。
fn apply_numbers(py: Python<'_>, list: &Bound<'_, PyList>, opts: &core::PlanOptions) -> PyResult<()> {
    let n = list.len();
    let mut inputs: Vec<core::NumberInput> = Vec::with_capacity(n);
    for i in 0..n {
        let it = item_at(list, i)?;
        let r = it.borrow();
        inputs.push(core::NumberInput {
            path: r.path.clone(),
            kind: r.kind.clone(),
            is_dir: r.is_dir,
        });
    }
    let _ = py;
    let res = core::assign_numbers(&inputs, opts);
    for (i, got) in res.iter().enumerate() {
        let Some(g) = got else { continue };
        let it = item_at(list, i)?;
        let mut r = it.borrow_mut();
        r.index = g.index;
        r.total = g.total;
        r.num = g.num;
        r.width = g.width;
    }
    Ok(())
}

// --------------------------------------------------------------------------- //
// 函数
// --------------------------------------------------------------------------- //

#[pyfunction]
#[pyo3(signature = (items, opts))]
fn assign_numbers(
    py: Python<'_>,
    items: &Bound<'_, PyAny>,
    opts: &Bound<'_, PyPlanOptions>,
) -> PyResult<()> {
    let list = to_list(py, Some(items))?;
    let o = opts.borrow().to_core();
    apply_numbers(py, list.bind(py), &o)
}

#[pyfunction]
#[pyo3(signature = (opts, chain))]
fn with_number_rule(
    opts: &Bound<'_, PyPlanOptions>,
    chain: &Bound<'_, PyAny>,
) -> PyResult<PyPlanOptions> {
    let (rules, _) = collect_chain(opts.py(), Some(chain))?;
    let o = core::with_number_rule(&opts.borrow().to_core(), &rules);
    Ok(PyPlanOptions::from_core(&o))
}

/// 对每个条目跑规则链，得到 `new_name`（不含落盘与校验）。
#[pyfunction]
#[pyo3(signature = (items, rules = None, opts = None, client = None))]
fn compute_names(
    py: Python<'_>,
    items: &Bound<'_, PyAny>,
    rules: Option<&Bound<'_, PyAny>>,
    opts: Option<&Bound<'_, PyPlanOptions>>,
    client: Option<&Bound<'_, PyAny>>,
) -> PyResult<Py<PyPlan>> {
    let list = to_list(py, Some(items))?;
    let list = list.bind(py);
    let n = list.len();

    let (chain, rule_objs) = collect_chain(py, rules)?;
    let mut o = match opts {
        Some(v) => v.borrow().to_core(),
        None => core::PlanOptions::default(),
    };
    o = core::with_number_rule(&o, &chain);

    let mut notes: Vec<String> = Vec::new();
    if o.step == 0 {
        // 步长 0 没有意义：不回落就会让整批拿到同一个号码（全员"同名冲突"）
        o.step = 1;
        notes.push("步长 0 已按 1 处理".to_string());
    }

    apply_numbers(py, list, &o)?;

    // 每条规则都基于"当前编号"的上下文
    let mut ctxs: Vec<Py<PyNameContext>> = Vec::with_capacity(n);
    for i in 0..n {
        let it = item_at(list, i)?;
        ctxs.push(it.borrow().ctx_obj(py)?);
    }

    if chain.iter().any(|r| r.kind == "llm") {
        notes = resolve_llm(py, list, &ctxs, &rule_objs, &o, client, notes)?;
    }

    for i in 0..n {
        let it = item_at(list, i)?;
        let (name, num, width, keep_as_is) = {
            let r = it.borrow();
            (r.name.clone(), r.num, r.width, r.is_dir && !o.rename_dirs)
        };
        let new_name = if chain.is_empty() || keep_as_is {
            name
        } else {
            let bound_ctx = ctxs[i].bind(py);
            let source = wrap_ctx(Some(bound_ctx.as_any()));
            rules_core::apply_chain(
                &chain,
                &name,
                num,
                width,
                source.as_ref().map(|s| s as &dyn namecat_core::ctx::VarSource),
                o.split_ext,
            )
        };
        let mut r = it.borrow_mut();
        r.new_name = new_name;
    }

    // 落盘前先挑出"目标名字重复"
    let mut pairs: Vec<(String, String)> = Vec::with_capacity(n);
    for i in 0..n {
        let it = item_at(list, i)?;
        let r = it.borrow();
        if r.new_name.is_empty() || r.new_name == r.name {
            continue;
        }
        pairs.push((r.dirname_value(), r.new_name.clone()));
    }
    let mut warnings = core::precheck(&pairs);
    warnings.extend(notes);

    let warn_list = PyList::empty(py);
    for w in warnings {
        warn_list.append(w)?;
    }
    Py::new(
        py,
        PyPlan {
            items: to_list(py, Some(list.as_any()))?,
            report: py.None(),
            warnings: warn_list.unbind(),
        },
    )
}

/// LLM 规则：交给 Python 侧的桥（`engine.llm_resolve`），返回补充提示。
fn resolve_llm(
    py: Python<'_>,
    list: &Bound<'_, PyList>,
    ctxs: &[Py<PyNameContext>],
    rules: &[Bound<'_, PyRule>],
    opts: &core::PlanOptions,
    client: Option<&Bound<'_, PyAny>>,
    notes: Vec<String>,
) -> PyResult<Vec<String>> {
    let module = py.import("engine.llm_resolve")?;
    let ctx_list = PyList::empty(py);
    for c in ctxs {
        ctx_list.append(c.bind(py))?;
    }
    let rule_list = PyList::empty(py);
    for r in rules {
        rule_list.append(r)?;
    }
    let opts_obj = Py::new(py, PyPlanOptions::from_core(opts))?;
    let note_list = PyList::empty(py);
    for n in &notes {
        note_list.append(n)?;
    }
    let got = module.call_method1(
        "resolve",
        (
            list,
            ctx_list,
            rule_list,
            opts_obj.bind(py),
            client.map(|c| c.clone()).unwrap_or_else(|| py.None().into_bound(py)),
            note_list,
        ),
    )?;
    let mut out: Vec<String> = Vec::new();
    if let Ok(iter) = got.try_iter() {
        for item in iter.flatten() {
            out.push(text_arg(Some(&item)));
        }
    }
    Ok(out)
}

/// 父子路径修正 + 两遍校验。
#[pyfunction]
#[pyo3(signature = (plan, opts = None))]
fn finalize(
    py: Python<'_>,
    plan: &Bound<'_, PyPlan>,
    opts: Option<&Bound<'_, PyPlanOptions>>,
) -> PyResult<Py<PyPlan>> {
    let _ = opts; // Python 版也没用它（留着是为了签名兼容）
    let items = plan.borrow().items.clone_ref(py);
    let list = items.bind(py);
    let n = list.len();

    let mut inputs: Vec<core::PathInput> = Vec::with_capacity(n);
    for i in 0..n {
        let it = item_at(list, i)?;
        let r = it.borrow();
        inputs.push(core::PathInput {
            path: r.path.clone(),
            name: r.name.clone(),
            is_dir: r.is_dir,
            new_name: r.new_name.clone(),
            dirname: r.dirname_value(),
        });
    }

    let (fixes, dir_keys) = core::finalize_paths(&inputs);
    for (i, fix) in fixes.iter().enumerate() {
        if let Some(dir) = &fix.new_dir {
            let it = item_at(list, i)?;
            *lock(&it.borrow().new_dir) = dir.clone();
        }
    }

    let keys = PySet::empty(py)?;
    for k in &dir_keys {
        keys.add(k)?;
    }

    // 直接调 Rust 的校验（不再绕回 `engine.common.validate_items`）：
    // 少一层 Python 调用，P2 里 `finalize` 那 +59% 的耗时就是它带来的。
    let report = crate::py_common::validate_items(py, list.as_any(), Some(keys.as_any()), None)?;
    {
        let mut p = plan.borrow_mut();
        p.report = report.into_any();
    }
    Ok(plan.clone().unbind())
}

/// 一步到位：扫描结果 -> 可执行计划。
#[pyfunction]
#[pyo3(signature = (items, rules = None, opts = None, progress = None, client = None))]
fn build(
    py: Python<'_>,
    items: &Bound<'_, PyAny>,
    rules: Option<&Bound<'_, PyAny>>,
    opts: Option<&Bound<'_, PyPlanOptions>>,
    progress: Option<&Bound<'_, PyAny>>,
    client: Option<&Bound<'_, PyAny>>,
) -> PyResult<Py<PyPlan>> {
    let plan = compute_names(py, items, rules, opts, client)?;
    let total = plan.borrow(py).items.bind(py).len();
    if let Some(p) = progress {
        let _ = p.call1((0, total, "校验目标名字"));
    }
    finalize(py, plan.bind(py), opts)
}

#[pyfunction]
#[pyo3(signature = (plan, limit = 60))]
fn describe_plan(py: Python<'_>, plan: &Bound<'_, PyPlan>, limit: i64) -> PyResult<String> {
    let items = plan.borrow().items.clone_ref(py);
    let list = items.bind(py);
    let mut rows: Vec<core::DescribeRow> = Vec::with_capacity(list.len());
    for i in 0..list.len() {
        let it = item_at(list, i)?;
        let r = it.borrow();
        rows.push(core::DescribeRow {
            status: r.status.clone(),
            name: r.name.clone(),
            new_name: r.new_name.clone(),
            note: r.note.clone(),
            dirname: r.dirname_value(),
            changed: r.is_changed(),
        });
    }
    Ok(core::describe_plan(&rows, limit))
}

/// 所有条目的公共父目录（`test_edges` 会直接调这个私有函数）。
#[pyfunction]
#[pyo3(signature = (items))]
fn _common_root(py: Python<'_>, items: &Bound<'_, PyAny>) -> PyResult<String> {
    let list = to_list(py, Some(items))?;
    let list = list.bind(py);
    let mut inputs: Vec<core::PathInput> = Vec::with_capacity(list.len());
    for i in 0..list.len() {
        let it = item_at(list, i)?;
        let r = it.borrow();
        inputs.push(core::PathInput {
            path: r.path.clone(),
            name: r.name.clone(),
            is_dir: r.is_dir,
            new_name: r.new_name.clone(),
            dirname: r.dirname_value(),
        });
    }
    Ok(core::common_root(&inputs))
}

// --------------------------------------------------------------------------- //
// 模块挂载
// --------------------------------------------------------------------------- //

pub fn attach(py: Python<'_>, parent: &Bound<'_, PyModule>) -> PyResult<()> {
    let m = crate::shim::new_submodule(py, parent, "plan")?;
    m.add(
        "__doc__",
        "改名计划（Rust 实现）：发号、规则链、父子路径修正、命令行预览。",
    )?;
    m.add("SCOPE_GLOBAL", core::SCOPE_GLOBAL)?;
    m.add("SCOPE_FOLDER", core::SCOPE_FOLDER)?;
    m.add("SCOPE_KIND", core::SCOPE_KIND)?;
    m.add("SCOPE_FOLDER_KIND", core::SCOPE_FOLDER_KIND)?;
    m.add("SCOPES", PyList::new(py, core::scopes())?)?;
    let text = PyDict::new(py);
    for (k, v) in core::scope_text() {
        text.set_item(k, v)?;
    }
    m.add("SCOPE_TEXT", text)?;
    // plan 也把这些类型常量再导出一遍（与 Python 版 `from .scan import ...` 一致）
    m.add("KIND_FILE", namecat_core::scan::KIND_FILE)?;
    m.add("KIND_DIR", namecat_core::scan::KIND_DIR)?;
    m.add("KIND_IMAGE", namecat_core::scan::KIND_IMAGE)?;
    m.add("KIND_VIDEO", namecat_core::scan::KIND_VIDEO)?;
    let kind_text = PyDict::new(py);
    for (k, v) in namecat_core::scan::kind_text() {
        kind_text.set_item(k, v)?;
    }
    m.add("KIND_TEXT", kind_text)?;

    m.add_class::<PyPlanOptions>()?;
    m.add_class::<PyPlan>()?;
    m.add_function(wrap_pyfunction!(assign_numbers, &m)?)?;
    m.add_function(wrap_pyfunction!(with_number_rule, &m)?)?;
    m.add_function(wrap_pyfunction!(compute_names, &m)?)?;
    m.add_function(wrap_pyfunction!(finalize, &m)?)?;
    m.add_function(wrap_pyfunction!(build, &m)?)?;
    m.add_function(wrap_pyfunction!(describe_plan, &m)?)?;
    m.add_function(wrap_pyfunction!(_common_root, &m)?)?;
    crate::shim::attach_field_names(
        &m,
        "PlanOptions",
        &["start", "step", "width", "scope", "reverse", "split_ext",
          "rename_dirs", "dry_run"],
    )?;
    crate::shim::set_all(
        &m,
        &[
            "KIND_DIR",
            "KIND_FILE",
            "KIND_IMAGE",
            "KIND_TEXT",
            "KIND_VIDEO",
            "Plan",
            "PlanOptions",
            "SCOPES",
            "SCOPE_FOLDER",
            "SCOPE_FOLDER_KIND",
            "SCOPE_GLOBAL",
            "SCOPE_KIND",
            "SCOPE_TEXT",
            "assign_numbers",
            "build",
            "compute_names",
            "describe_plan",
            "finalize",
            "with_number_rule",
        ],
    )
}
