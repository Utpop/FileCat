//! `namecat_py.extract` —— FileCat 文件批量提取引擎的 PyO3 绑定。
//!
//! 与原有 12 个重命名子模块完全隔离：
//! - 数据全部用 `serde_json::Value` / Python dict 互转，不复用 `PyItem` 等类型；
//! - 长耗时操作用 `py.detach(...)` 释放 GIL，进度回调内部 `Python::attach` 重新取 GIL；
//! - 停止标志按 task_id 存在 Rust 全局表，Python 侧不持有跨线程对象；
//! - 程序根目录由 Python 侧判定（源码 = 项目根，EXE = exe 所在目录）后传入，
//!   Rust 侧只负责拼固定的 `Extract/Rollback_Log`。

use namecat_extract_core as core;
use pyo3::exceptions::{PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyList};
use serde_json::Value;
use std::path::PathBuf;
use std::sync::Arc;

use crate::pyjson;
use crate::shim;

const SYMBOLS: &[&str] = &[
    "SCHEMA", "INTERNAL_DIR_NAME", "ROLLBACK_DIR_NAME", "BACKUP_SUBDIR",
    "MODES", "POLICIES", "STATUSES",
    "program_root_hint", "rollback_dir", "parse_list",
    "build_plan", "execute", "stop_task", "undo_latest", "undo_path",
    "list_logs", "export_csv", "plan_stats", "read_log",
];

pub fn attach(py: Python<'_>, parent: &Bound<'_, PyModule>) -> PyResult<()> {
    let m = shim::new_submodule(py, parent, "extract")?;
    m.add("__doc__", "FileCat 文件批量提取引擎（独立 Rust core，namecat_py 子模块）")?;
    m.add("SCHEMA", core::model::SCHEMA)?;
    m.add("INTERNAL_DIR_NAME", core::model::INTERNAL_DIR_NAME)?;
    m.add("ROLLBACK_DIR_NAME", core::model::ROLLBACK_DIR_NAME)?;
    m.add("BACKUP_SUBDIR", core::model::BACKUP_SUBDIR)?;
    m.add("MODES", vec!["flatten", "one_level"])?;
    m.add("POLICIES", vec!["rename", "skip", "overwrite"])?;
    m.add("STATUSES", vec![
        "pending", "ready", "renamed", "conflict", "skip", "error", "done", "stopped", "dry_run",
    ])?;
    m.add_function(wrap_pyfunction!(program_root_hint, &m)?)?;
    m.add_function(wrap_pyfunction!(rollback_dir, &m)?)?;
    m.add_function(wrap_pyfunction!(parse_list, &m)?)?;
    m.add_function(wrap_pyfunction!(build_plan, &m)?)?;
    m.add_function(wrap_pyfunction!(execute, &m)?)?;
    m.add_function(wrap_pyfunction!(stop_task, &m)?)?;
    m.add_function(wrap_pyfunction!(undo_latest, &m)?)?;
    m.add_function(wrap_pyfunction!(undo_path, &m)?)?;
    m.add_function(wrap_pyfunction!(list_logs, &m)?)?;
    m.add_function(wrap_pyfunction!(export_csv, &m)?)?;
    m.add_function(wrap_pyfunction!(plan_stats, &m)?)?;
    m.add_function(wrap_pyfunction!(read_log, &m)?)?;
    shim::set_all(&m, SYMBOLS)
}

// --------------------------------------------------------------------------- //
// 错误映射
// --------------------------------------------------------------------------- //

fn to_pyerr(err: core::model::Error) -> PyErr {
    use core::model::Error as E;
    match err {
        E::Invalid(msg) => PyValueError::new_err(msg.to_string()),
        E::Path(msg) | E::Log(msg) | E::Json(msg) => PyValueError::new_err(msg),
        E::Io(io) => PyOSError::new_err(io.to_string()),
    }
}

fn wrap<T>(result: core::model::Result<T>) -> PyResult<T> {
    result.map_err(to_pyerr)
}

// --------------------------------------------------------------------------- //
// Python → Rust 数据转换
// --------------------------------------------------------------------------- //

fn to_value(obj: &Bound<'_, PyAny>) -> Value {
    pyjson::to_value(obj)
}

fn to_py(py: Python<'_>, value: &Value) -> PyResult<Py<PyAny>> {
    pyjson::to_py(py, value)
}

fn json_to_py<T: serde::Serialize>(py: Python<'_>, value: &T) -> PyResult<Py<PyAny>> {
    let v = serde_json::to_value(value)
        .map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
    to_py(py, &v)
}

fn value_to_type<T: serde::de::DeserializeOwned>(value: Value) -> PyResult<T> {
    serde_json::from_value(value).map_err(|e| PyValueError::new_err(format!("参数格式错误：{e}")))
}

fn opts_from_py(obj: Option<&Bound<'_, PyAny>>) -> PyResult<core::model::ExtractOptions> {
    let Some(o) = obj else {
        return Err(PyValueError::new_err("缺少 options 字典"));
    };
    if o.is_none() {
        return Err(PyValueError::new_err("缺少 options 字典"));
    }
    value_to_type(to_value(o))
}

fn plan_from_py(obj: Option<&Bound<'_, PyAny>>) -> PyResult<core::model::ExtractPlan> {
    let Some(o) = obj else {
        return Err(PyValueError::new_err("缺少 plan 字典"));
    };
    value_to_type(to_value(o))
}

// --------------------------------------------------------------------------- //
// 进度回调（跨 GIL 边界）
// --------------------------------------------------------------------------- //

struct PyProgress {
    callback: Py<PyAny>,
}

impl core::model::Progress for PyProgress {
    fn on_progress(&self, current: usize, total: usize, message: &str) {
        // detach 期间没有 GIL：重新 attach 后调用 Python 回调。
        Python::attach(|py| {
            if let Ok(obj) = self.callback.bind(py).call1((current, total, message)) {
                // 回调返回值不用；异常不让它穿过 Rust 边界（记录即可）
                let _ = obj;
            }
        });
    }
}

struct NoProgress;
impl core::model::Progress for NoProgress {
    fn on_progress(&self, _c: usize, _t: usize, _m: &str) {}
}

fn progress_obj(callback: Option<Py<PyAny>>) -> Box<dyn core::model::Progress> {
    match callback {
        Some(cb) => Box::new(PyProgress { callback: cb }),
        None => Box::new(NoProgress),
    }
}

// --------------------------------------------------------------------------- //
// 模块函数
// --------------------------------------------------------------------------- //

/// 程序根目录识别提示：Python 侧据此判定后传给 options.program_root。
/// 规则：EXE 运行（sys.frozen）→ sys.executable 所在目录；源码运行 → 项目根。
/// 绝不使用 cwd 或 sys._MEIPASS。
#[pyfunction]
fn program_root_hint(py: Python<'_>) -> PyResult<String> {
    let sys = py.import("sys")?;
    let frozen = sys.getattr("frozen").map(|v| v.is_truthy().unwrap_or(false)).unwrap_or(false);
    let executable: String = sys.getattr("executable")?.extract()?;
    let exe_dir = PathBuf::from(&executable)
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."));
    if frozen {
        return Ok(exe_dir.to_string_lossy().into_owned());
    }
    // 源码运行：namecat_py 是扩展模块，拿它的 __file__ 不可靠；
    // 用 engine 包所在目录的上一级（项目程序根）。
    let engine = py.import("engine")?;
    let file: String = engine.getattr("__file__")?.extract()?;
    let root = PathBuf::from(file)
        .parent()                       // .../engine
        .and_then(|p| p.parent())       // 项目根
        .map(|p| p.to_path_buf())
        .unwrap_or(exe_dir);
    Ok(root.to_string_lossy().into_owned())
}

/// 固定撤销日志目录（供界面显示 / 打开）。
#[pyfunction]
fn rollback_dir(program_root: &str) -> String {
    core::rollback::rollback_dir(program_root)
        .to_string_lossy()
        .into_owned()
}

/// 解析逗号分隔的排除列表（界面与命令行共用引擎的解析逻辑）。
#[pyfunction]
#[pyo3(signature = (text, dotted = false))]
fn parse_list(text: &str, dotted: bool) -> Vec<String> {
    core::model::ExtractOptions::parse_list(text, dotted)
}

/// 扫描 + 生成计划（只读文件系统，不写任何内容）。返回 dict。
#[pyfunction]
#[pyo3(signature = (options, progress = None))]
fn build_plan(py: Python<'_>, options: Option<&Bound<'_, PyAny>>,
              progress: Option<Py<PyAny>>) -> PyResult<Py<PyAny>> {
    let opts = opts_from_py(options)?;
    let cb: Option<Py<PyAny>> = progress;
    let result = shim::guard(|| {
        py.detach(|| {
            let p = progress_obj(cb);
            p.on_progress(0, 0, "正在扫描目录…");
            let plan = core::plan::build_plan(&opts).map_err(to_pyerr)?;
            p.on_progress(plan.items.len(), plan.items.len(), "扫描完成");
            PyResult::Ok(plan)
        })
    })?;
    json_to_py(py, &result)
}

/// 执行 / 模拟运行。返回 dict（含 items / 统计 / 日志路径）。
#[pyfunction]
#[pyo3(signature = (options, plan, dry_run = false, task_id = "", progress = None))]
fn execute(py: Python<'_>, options: Option<&Bound<'_, PyAny>>, plan: Option<&Bound<'_, PyAny>>,
           dry_run: bool, task_id: &str, progress: Option<Py<PyAny>>) -> PyResult<Py<PyAny>> {
    let opts = opts_from_py(options)?;
    let plan_val = plan_from_py(plan)?;
    let cb: Option<Py<PyAny>> = progress;
    let tid = task_id.to_string();
    let result = shim::guard(|| {
        py.detach(|| {
            let stop = if tid.is_empty() {
                Arc::new(core::model::StopFlag::new())
            } else {
                core::execute::register_stop(&tid)
            };
            let p = progress_obj(cb);
            let out = core::execute::execute(&opts, &plan_val, dry_run, &stop, p.as_ref())
                .map_err(to_pyerr);
            if !tid.is_empty() {
                core::execute::clear_stop(&tid);
            }
            PyResult::Ok(out?)
        })
    })?;
    json_to_py(py, &result)
}

/// 请求停止任务（只置标志，当前文件操作完整结束）。
#[pyfunction]
fn stop_task(task_id: &str) -> bool {
    core::execute::request_stop(task_id)
}

/// 撤销最新一份有效记录。返回 dict(counts / errors / complete)。
#[pyfunction]
#[pyo3(signature = (program_root, progress = None))]
fn undo_latest(py: Python<'_>, program_root: &str,
               progress: Option<Py<PyAny>>) -> PyResult<Py<PyAny>> {
    let root = program_root.to_string();
    let cb: Option<Py<PyAny>> = progress;
    let result = shim::guard(|| {
        py.detach(|| {
            let p = progress_obj(cb);
            core::rollback::undo_latest(&root, p.as_ref()).map_err(to_pyerr)
        })
    })?;
    json_to_py(py, &result)
}

/// 按日志路径撤销指定一次操作。
#[pyfunction]
#[pyo3(signature = (log_path, progress = None))]
fn undo_path(py: Python<'_>, log_path: &str,
             progress: Option<Py<PyAny>>) -> PyResult<Py<PyAny>> {
    let path = PathBuf::from(log_path);
    let cb: Option<Py<PyAny>> = progress;
    let result = shim::guard(|| {
        py.detach(|| {
            let p = progress_obj(cb);
            core::rollback::undo_path(&path, p.as_ref()).map_err(to_pyerr)
        })
    })?;
    json_to_py(py, &result)
}

/// 列出撤销日志摘要（最新在前）。坏日志跳过。
#[pyfunction]
fn list_logs(py: Python<'_>, program_root: &str) -> PyResult<Py<PyAny>> {
    let rows = core::rollback::list_logs(program_root);
    let list = PyList::empty(py);
    for (path, log) in rows {
        let d = PyDict::new(py);
        d.set_item("path", path.to_string_lossy().as_ref())?;
        d.set_item("name", path.file_name().unwrap_or_default().to_string_lossy().as_ref())?;
        d.set_item("operation_id", log.operation_id)?;
        d.set_item("time", log.time)?;
        d.set_item("root", log.root.to_string_lossy().as_ref())?;
        d.set_item("mode", log.mode.as_str())?;
        d.set_item("result", log.result)?;
        d.set_item("state", match log.state {
            core::model::RollbackState::Active => "active",
            core::model::RollbackState::Partial => "partial",
            core::model::RollbackState::Undone => "undone",
        })?;
        d.set_item("count", log.entries.len())?;
        d.set_item("cleaned_dirs", log.cleaned_dirs.len())?;
        list.append(d)?;
    }
    Ok(list.into_any().unbind())
}

/// 读取单份日志全文（界面「日志详情」用）。坏日志抛 ValueError。
#[pyfunction]
fn read_log(py: Python<'_>, log_path: &str) -> PyResult<Py<PyAny>> {
    let text = std::fs::read_to_string(log_path)
        .map_err(|e| PyOSError::new_err(e.to_string()))?;
    let log: core::model::RollbackLog = serde_json::from_str(&text)
        .map_err(|e| PyValueError::new_err(format!("日志无法解析：{e}")))?;
    json_to_py(py, &log)
}

/// 导出 CSV（UTF-8 BOM）。`items` 是计划项或执行结果项列表。
#[pyfunction]
fn export_csv(path: &str, items: Option<&Bound<'_, PyAny>>) -> PyResult<()> {
    let list: Vec<core::model::PlanItem> = match items {
        Some(o) => value_to_type(to_value(o))?,
        None => Vec::new(),
    };
    wrap(core::csv::export_to_path(&list, &PathBuf::from(path)))
}

/// 统计一份计划（界面摘要条用，避免界面自己数数）。
#[pyfunction]
fn plan_stats(py: Python<'_>, plan: Option<&Bound<'_, PyAny>>) -> PyResult<Py<PyAny>> {
    let p = plan_from_py(plan)?;
    json_to_py(py, &p.stats())
}
