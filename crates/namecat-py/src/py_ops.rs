//! `engine/ops.py` 的绑定：预览 / 落盘 / 撤销 / 导出。
//!
//! 界面层与命令行都只调这里，保证两条入口行为完全一致。
//!
//! 这一层是**编排**：真正的算法在 `namecat-core`，这里只负责把 Python 侧的
//! 参数理顺、按顺序调用、把结果装回 Python 对象。所以它刻意保留了几处
//! 通过 `engine.plan` / `engine.common` 的模块级调用——比在这里重写一遍
//! 参数默认值更不容易走样。

use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyList};

use namecat_core::ops as ops_core;

use crate::py_common::{execute_impl, undo_impl};
use crate::py_context::PyItem;

/// `ops.Result`：一次动作的结果。
#[pyclass(module = "namecat_py.ops", name = "Result", dict)]
pub struct PyOpsResult {
    #[pyo3(get, set)]
    pub plan: Py<PyAny>,
    #[pyo3(get, set)]
    pub steps: Py<PyList>,
    #[pyo3(get, set)]
    pub log_path: String,
    #[pyo3(get, set)]
    pub message: String,
    /// 「这一步整体上算不算成功」。撤销没有 step 列表，只有 per-entry 的错误
    /// 清单，所以成败必须由调用方显式给出——否则 steps 为空时 ok 恒为 True，
    /// 撤销明明一项都没还原却被当成成功（`main.py undo` 因此错误地返回 0）。
    #[pyo3(get, set)]
    pub success: bool,
}

#[pymethods]
impl PyOpsResult {
    #[new]
    #[pyo3(signature = (plan = None, steps = None, log_path = "", message = "", success = true))]
    fn new(
        py: Python<'_>,
        plan: Option<&Bound<'_, PyAny>>,
        steps: Option<&Bound<'_, PyAny>>,
        log_path: &str,
        message: &str,
        success: bool,
    ) -> PyResult<Self> {
        let steps = match steps {
            Some(v) if !v.is_none() => v.clone().cast_into::<PyList>()?.unbind(),
            _ => PyList::empty(py).unbind(),
        };
        Ok(Self {
            plan: plan.map(|p| p.clone().unbind()).unwrap_or_else(|| py.None()),
            steps,
            log_path: log_path.to_string(),
            message: message.to_string(),
            success,
        })
    }

    #[getter]
    fn ok(&self, py: Python<'_>) -> PyResult<bool> {
        if !self.success {
            return Ok(false);
        }
        for step in self.steps.bind(py).iter() {
            let ok = step.getattr("ok")?.is_truthy()?;
            if !ok {
                return Ok(false);
            }
        }
        Ok(true)
    }

    #[getter]
    fn failed(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        let out = PyList::empty(py);
        for step in self.steps.bind(py).iter() {
            if !step.getattr("ok")?.is_truthy()? {
                out.append(step)?;
            }
        }
        Ok(out.unbind())
    }

    fn __repr__(&self) -> String {
        format!(
            "Result(message={:?}, log_path={:?}, success={})",
            self.message, self.log_path, self.success
        )
    }
}

fn plan_options(py: Python<'_>) -> PyResult<Py<PyAny>> {
    let plan_mod = py.import("namecat_py.plan")?;
    Ok(plan_mod.getattr("PlanOptions")?.call0()?.unbind())
}

fn call_build(
    py: Python<'_>,
    items: &Bound<'_, PyAny>,
    rules: Option<&Bound<'_, PyAny>>,
    opts: Option<&Bound<'_, PyAny>>,
    progress: Option<&Bound<'_, PyAny>>,
    client: Option<&Bound<'_, PyAny>>,
) -> PyResult<Py<PyAny>> {
    let plan_mod = py.import("namecat_py.plan")?;
    let build = plan_mod.getattr("build")?;
    let none = || py.None();
    let plan = build.call1((
        items,
        rules.map(|r| r.clone().unbind()).unwrap_or_else(none),
        opts.map(|o| o.clone().unbind()).unwrap_or_else(none),
        progress.map(|p| p.clone().unbind()).unwrap_or_else(none),
        client.map(|c| c.clone().unbind()).unwrap_or_else(none),
    ))?;
    Ok(plan.unbind())
}

/// 只算不改（默认行为）。
#[pyfunction]
#[pyo3(signature = (items, rules = None, opts = None, progress = None, client = None))]
fn preview(
    py: Python<'_>,
    items: &Bound<'_, PyAny>,
    rules: Option<&Bound<'_, PyAny>>,
    opts: Option<&Bound<'_, PyAny>>,
    progress: Option<&Bound<'_, PyAny>>,
    client: Option<&Bound<'_, PyAny>>,
) -> PyResult<Py<PyAny>> {
    call_build(py, items, rules, opts, progress, client)
}

fn item_dirname(obj: &Bound<'_, PyAny>) -> String {
    if let Ok(native) = obj.cast::<PyItem>() {
        return native.borrow().dirname_value();
    }
    obj.getattr("dirname")
        .and_then(|v| v.str().map(|s| s.to_string_lossy().into_owned()))
        .unwrap_or_default()
}

/// 落盘并写撤销日志。`dry_run=True` 时只预览。
///
/// `plan` 可以直接传入已经算好的计划——命令行先用 scan_and_plan 预览、
/// 再调本函数落盘，传进来就不必重算。重算会带来两个真实缺陷：LLM 规则被
/// 请求两次（多花钱、可能给出不同名字），randomize 规则两次结果不同
/// （预览显示的名字和真正落盘的名字不一致）。
#[pyfunction]
#[pyo3(signature = (items, rules = None, opts = None, root = "", kind = "mixed",
                    progress = None, client = None, plan = None))]
#[allow(clippy::too_many_arguments)]
fn apply(
    py: Python<'_>,
    items: &Bound<'_, PyAny>,
    rules: Option<&Bound<'_, PyAny>>,
    opts: Option<&Bound<'_, PyAny>>,
    root: &str,
    kind: &str,
    progress: Option<&Bound<'_, PyAny>>,
    client: Option<&Bound<'_, PyAny>>,
    plan: Option<&Bound<'_, PyAny>>,
) -> PyResult<Py<PyOpsResult>> {
    let opts_owned: Py<PyAny> = match opts {
        Some(o) if !o.is_none() => o.clone().unbind(),
        _ => plan_options(py)?,
    };
    let opts_obj = opts_owned.bind(py);

    let plan_owned: Py<PyAny> = match plan {
        Some(p) if !p.is_none() => p.clone().unbind(),
        _ => call_build(py, items, rules, Some(&opts_obj), progress, client)?,
    };
    let plan_obj = plan_owned.bind(py);

    let dry_run = opts_obj
        .getattr("dry_run")
        .and_then(|v| v.is_truthy())
        .unwrap_or(false);
    if dry_run {
        let summary = plan_obj.getattr("summary")?.call0()?.str()?.to_string_lossy().into_owned();
        return build_result(
            py,
            &plan_obj,
            PyList::empty(py).unbind(),
            String::new(),
            format!("预览：{summary}"),
            true,
        );
    }

    let todo = plan_obj.getattr("changing")?.call0()?;
    if todo.len()? == 0 {
        return build_result(
            py,
            &plan_obj,
            PyList::empty(py).unbind(),
            String::new(),
            "没有需要改名的项".to_string(),
            true,
        );
    }

    let steps = execute_impl(py, &todo, progress)?;
    let steps_bound = steps.bind(py);
    let mut ok_n = 0usize;
    let mut dirs: Vec<String> = Vec::new();
    let mut pairs: Vec<(String, String)> = Vec::new();
    for step in steps_bound.iter() {
        if step.getattr("ok")?.is_truthy()? {
            ok_n += 1;
            dirs.push(item_dirname(&step.getattr("item")?));
            pairs.push((
                step.getattr("old")?.extract()?,
                step.getattr("new")?.extract()?,
            ));
        }
    }
    let total = steps_bound.len();
    let bad = total - ok_n;

    let mut log_path = String::new();
    if ok_n > 0 {
        let base = if root.is_empty() {
            ops_core::common_base(&dirs)
        } else {
            root.to_string()
        };

        // 执行阶段的真实成败：「执行记录」页显示的就是这个，不是计划统计
        let executed = PyDict::new(py);
        executed.set_item("done", ok_n)?;
        executed.set_item("failed", bad)?;
        let errors = PyList::empty(py);
        for step in steps_bound.iter() {
            if step.getattr("ok")?.is_truthy()? {
                continue;
            }
            if errors.len() >= 20 {
                break;
            }
            let entry = PyDict::new(py);
            entry.set_item("old", step.getattr("old")?)?;
            entry.set_item("new", step.getattr("new")?)?;
            entry.set_item("error", step.getattr("error")?)?;
            errors.append(entry)?;
        }
        executed.set_item("errors", errors)?;

        let report = plan_obj.getattr("report")?;
        let summary = if report.is_none() {
            PyDict::new(py).into_any()
        } else {
            report.call_method0("as_dict")?
        };
        let options = opts_obj.call_method0("as_dict")?;
        let rules_json = match rules {
            Some(r) if !r.is_none() => {
                let rules_mod = py.import("namecat_py.rules")?;
                rules_mod.getattr("rules_to_json")?.call1((r,))?
            }
            _ => PyList::empty(py).into_any(),
        };

        let common = py.import("namecat_py.common")?;
        let kw = PyDict::new(py);
        kw.set_item("summary", summary)?;
        kw.set_item("options", options)?;
        kw.set_item("rules", rules_json)?;
        kw.set_item("executed", executed)?;
        let saved = common.call_method("save_log", (base, pairs, kind), Some(&kw))?;
        log_path = saved.extract()?;
    }

    let mut msg = format!("已重命名 {ok_n} 项");
    if bad > 0 {
        msg.push_str(&format!("，失败 {bad} 项（已回滚）"));
    }
    if ok_n > 0 && log_path.is_empty() {
        // 日志写不进去时绝不能只报"成功"：这批改名将无法撤销，
        // 而执行前的确认框明确承诺了"会写撤销日志"。
        msg.push_str("；但撤销日志没能写入（日志目录不可写），本次改名无法撤销");
    }
    build_result(py, &plan_obj, steps, log_path, msg, true)
}

fn build_result(
    py: Python<'_>,
    plan: &Bound<'_, PyAny>,
    steps: Py<PyList>,
    log_path: String,
    message: String,
    success: bool,
) -> PyResult<Py<PyOpsResult>> {
    Py::new(
        py,
        PyOpsResult {
            plan: plan.clone().unbind(),
            steps,
            log_path,
            message,
            success,
        },
    )
}

/// 撤销一份日志。
#[pyfunction]
#[pyo3(signature = (log_path, progress = None))]
fn undo(
    py: Python<'_>,
    log_path: &str,
    progress: Option<&Bound<'_, PyAny>>,
) -> PyResult<Py<PyOpsResult>> {
    let (ok, errors) = undo_impl(log_path, progress)?;
    let mut msg = if ok {
        "撤销完成".to_string()
    } else {
        format!("撤销有 {} 处未完成", errors.len())
    };
    if !errors.is_empty() {
        msg.push('；');
        msg.push_str(&errors.join("；"));
    }
    // success 必须显式给：撤销没有 step 列表，ok 不能靠 steps 推
    Py::new(
        py,
        PyOpsResult {
            plan: py.None(),
            steps: PyList::empty(py).unbind(),
            log_path: log_path.to_string(),
            message: msg,
            success: ok,
        },
    )
}

/// 导出预览结果。列与 BatchRenamer 的导出保持兼容并做了叠加。
#[pyfunction]
#[pyo3(signature = (plan, path))]
fn export_csv(py: Python<'_>, plan: &Bound<'_, PyAny>, path: &str) -> PyResult<String> {
    let kind_text = py.import("namecat_py.scan")?.getattr("KIND_TEXT")?;
    let status_text = py.import("namecat_py.common")?.getattr("STATUS_TEXT")?;

    let mut rows: Vec<Vec<String>> = vec![vec![
        "序号".to_string(),
        "类型".to_string(),
        "所在文件夹".to_string(),
        "原名称".to_string(),
        "新名称".to_string(),
        "状态".to_string(),
        "说明".to_string(),
        "大小(字节)".to_string(),
    ]];

    for (i, it) in plan.getattr("items")?.try_iter()?.enumerate() {
        let it = it?;
        let is_dir = it.getattr("is_dir").and_then(|v| v.is_truthy()).unwrap_or(false);
        let kind = it
            .getattr("kind")
            .map(|v| v.str().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default())
            .unwrap_or_default();
        let status = it
            .getattr("status")
            .map(|v| v.str().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default())
            .unwrap_or_default();
        let mut note = it
            .getattr("note")
            .map(|v| v.str().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default())
            .unwrap_or_default();
        if note == "None" {
            note.clear();
        }
        let new_name = it
            .getattr("new_name")
            .map(|v| {
                if v.is_none() {
                    String::new()
                } else {
                    v.str().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default()
                }
            })
            .unwrap_or_default();

        // 0.29 的 `get_item` 在键缺失时抛 KeyError，先问 contains 再用
        let kind_label: String = if kind_text.contains(&kind)? {
            kind_text
                .get_item(&kind)?
                .str()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_default()
        } else {
            String::new()
        };
        let status_label: String = if status_text.contains(&status)? {
            status_text
                .get_item(&status)?
                .str()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_default()
        } else if status.is_empty() {
            String::new()
        } else {
            status.clone()
        };

        let size = if is_dir {
            String::new()
        } else {
            let st = it.getattr("st").unwrap_or_else(|_| py.None().into_bound(py));
            match st.getattr("st_size") {
                Ok(v) if !v.is_none() => v.str().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default(),
                _ => String::new(),
            }
        };

        rows.push(vec![
            (i + 1).to_string(),
            kind_label,
            it.getattr("path")
                .and_then(|v| v.str().map(|s| s.to_string_lossy().into_owned()))
                .map(|p| namecat_core::pathops::dirname(&p))
                .unwrap_or_default(),
            it.getattr("name")
                .and_then(|v| v.str().map(|s| s.to_string_lossy().into_owned()))
                .unwrap_or_default(),
            new_name,
            status_label,
            note,
            size,
        ]);
    }

    let text = ops_core::csv_text(&rows);
    std::fs::write(path, text.as_bytes())
        .map_err(|e| pyo3::exceptions::PyOSError::new_err(e.to_string()))?;
    Ok(path.to_string())
}

/// 一次调用跑完 扫描 -> 排序编号 -> 算名 -> 校验。
#[pyfunction]
#[pyo3(signature = (root, rules = None, scan_opts = None, plan_opts = None,
                    progress = None, client = None))]
fn scan_and_plan(
    py: Python<'_>,
    root: &str,
    rules: Option<&Bound<'_, PyAny>>,
    scan_opts: Option<&Bound<'_, PyAny>>,
    plan_opts: Option<&Bound<'_, PyAny>>,
    progress: Option<&Bound<'_, PyAny>>,
    client: Option<&Bound<'_, PyAny>>,
) -> PyResult<Py<PyAny>> {
    let scan_mod = py.import("namecat_py.scan")?;
    let opts: Bound<'_, PyAny> = match scan_opts {
        Some(o) if !o.is_none() => o.clone(),
        _ => {
            let kw = PyDict::new(py);
            kw.set_item("root", root)?;
            scan_mod.getattr("ScanOptions")?.call((), Some(&kw))?
        }
    };
    let res = scan_mod
        .getattr("scan")?
        .call1((opts, progress.map(|p| p.clone().unbind()).unwrap_or_else(|| py.None())))?;
    let items = res.getattr("items")?;
    call_build(py, &items, rules, plan_opts, progress, client)
}

/// `_common_base(dirs)` —— 多个目录的公共父目录（跨盘时退回第一项）。
///
/// 名字带下划线是故意的：它不是给界面用的，而是 `verify/run_tests.py` 里
/// 直接断言的私有辅助（对应原 Python 实现的同名函数）。留着同名同义，
/// "跨盘退回首项"这条规则才继续有人盯着。
#[pyfunction]
fn _common_base(dirs: Vec<String>) -> String {
    ops_core::common_base(&dirs)
}

pub fn attach(py: Python<'_>, parent: &Bound<'_, PyModule>) -> PyResult<()> {
    let m = crate::shim::new_submodule(py, parent, "ops")?;
    m.add(
        "__doc__",
        "高层动作：预览 / 落盘 / 撤销 / 导出（Rust 实现）。",
    )?;
    m.add("APP_NAME", namecat_core::consts::APP_NAME)?;
    m.add("APP_VER", namecat_core::APP_VER)?;
    m.add("ST_OK", namecat_core::consts::ST_OK)?;

    let status_text = PyDict::new(py);
    for (key, text) in namecat_core::consts::STATUS_TEXT {
        status_text.set_item(*key, *text)?;
    }
    m.add("STATUS_TEXT", status_text)?;

    let kind_text = PyDict::new(py);
    for (key, text) in namecat_core::scan::kind_text() {
        kind_text.set_item(key, text)?;
    }
    m.add("KIND_DIR", namecat_core::scan::KIND_DIR)?;
    m.add("KIND_TEXT", kind_text)?;

    m.add_class::<PyOpsResult>()?;
    m.add_function(wrap_pyfunction!(preview, &m)?)?;
    m.add_function(wrap_pyfunction!(apply, &m)?)?;
    m.add_function(wrap_pyfunction!(undo, &m)?)?;
    m.add_function(wrap_pyfunction!(export_csv, &m)?)?;
    m.add_function(wrap_pyfunction!(scan_and_plan, &m)?)?;
    m.add_function(wrap_pyfunction!(_common_base, &m)?)?;

    crate::shim::set_all(
        &m,
        &[
            "APP_NAME",
            "APP_VER",
            "KIND_DIR",
            "KIND_TEXT",
            "Result",
            "STATUS_TEXT",
            "ST_OK",
            "apply",
            "export_csv",
            "preview",
            "scan_and_plan",
            "undo",
        ],
    )
}
