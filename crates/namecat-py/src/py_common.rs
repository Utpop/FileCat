//! `engine/common.py` 的绑定：名字校验、两遍冲突检测、落盘改名、日志与撤销。

use std::collections::HashSet;

use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyList, PySet, PyTuple};
use serde_json::{Map, Value};

use namecat_core::names as names_core;
use namecat_core::rename as rename_core;
use namecat_core::validate as validate_core;
use namecat_core::{consts, logs as logs_core};

use crate::py_context::PyItem;
use crate::pyjson;

// --------------------------------------------------------------------------- //
// ValidateReport / RenameStep
// --------------------------------------------------------------------------- //

/// 校验结果汇总。属性面与 Python 版一致（`ok/same/conflict/error/skip`）。
#[pyclass(module = "namecat_py.common", name = "ValidateReport")]
#[derive(Default)]
pub struct PyValidateReport {
    #[pyo3(get, set)]
    pub ok: u32,
    #[pyo3(get, set)]
    pub same: u32,
    #[pyo3(get, set)]
    pub conflict: u32,
    #[pyo3(get, set)]
    pub error: u32,
    #[pyo3(get, set)]
    pub skip: u32,
}

impl PyValidateReport {
    fn from_core(rep: &validate_core::ValidateReport) -> Self {
        Self {
            ok: rep.ok,
            same: rep.same,
            conflict: rep.conflict,
            error: rep.error,
            skip: rep.skip,
        }
    }
}

#[pymethods]
impl PyValidateReport {
    #[new]
    fn new() -> Self {
        Self::default()
    }

    /// 按一个条目的当前状态计数（界面偶发用得上，保留）。
    fn count(&mut self, item: &Bound<'_, PyAny>) -> PyResult<()> {
        let st = match item.getattr("status") {
            Ok(v) => v.str()?.to_string_lossy().into_owned(),
            Err(_) => consts::ST_OK.to_string(),
        };
        self.bump(&st);
        Ok(())
    }

    fn __str__(&self) -> String {
        self.to_core().to_string()
    }

    fn __repr__(&self) -> String {
        format!(
            "ValidateReport(ok={}, same={}, conflict={}, error={}, skip={})",
            self.ok, self.same, self.conflict, self.error, self.skip
        )
    }

    fn as_dict(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let d = PyDict::new(py);
        d.set_item("ok", self.ok)?;
        d.set_item("same", self.same)?;
        d.set_item("conflict", self.conflict)?;
        d.set_item("error", self.error)?;
        d.set_item("skip", self.skip)?;
        Ok(d.unbind())
    }
}

impl PyValidateReport {
    fn bump(&mut self, status: &str) {
        match status {
            consts::ST_OK => self.ok += 1,
            consts::ST_SAME => self.same += 1,
            consts::ST_CONFLICT => self.conflict += 1,
            consts::ST_ERROR => self.error += 1,
            _ => self.skip += 1,
        }
    }

    fn to_core(&self) -> validate_core::ValidateReport {
        validate_core::ValidateReport {
            ok: self.ok,
            same: self.same,
            conflict: self.conflict,
            error: self.error,
            skip: self.skip,
        }
    }
}

/// 一步的执行结果。
#[pyclass(module = "namecat_py.common", name = "RenameStep", dict)]
pub struct PyRenameStep {
    #[pyo3(get, set)]
    pub item: Py<PyAny>,
    #[pyo3(get, set)]
    pub old: String,
    #[pyo3(get, set)]
    pub new: String,
    #[pyo3(get, set)]
    pub ok: bool,
    #[pyo3(get, set)]
    pub error: String,
}

#[pymethods]
impl PyRenameStep {
    #[new]
    #[pyo3(signature = (item = None, old = "", new = "", ok = false, error = ""))]
    fn new(
        py: Python<'_>,
        item: Option<&Bound<'_, PyAny>>,
        old: &str,
        new: &str,
        ok: bool,
        error: &str,
    ) -> Self {
        Self {
            item: item.map(|o| o.clone().unbind()).unwrap_or_else(|| py.None()),
            old: old.to_string(),
            new: new.to_string(),
            ok,
            error: error.to_string(),
        }
    }

    fn __repr__(&self) -> String {
        format!(
            "RenameStep(old={:?}, new={:?}, ok={}, error={:?})",
            self.old, self.new, self.ok, self.error
        )
    }
}

// --------------------------------------------------------------------------- //
// 条目适配：PyItem 走快路径，其它对象走 getattr（鸭子类型）
// --------------------------------------------------------------------------- //

fn attr_str(obj: &Bound<'_, PyAny>, name: &str) -> String {
    match obj.getattr(name) {
        Ok(v) => {
            if v.is_none() {
                String::new()
            } else {
                v.str()
                    .map(|s| s.to_string_lossy().into_owned())
                    .unwrap_or_default()
            }
        }
        Err(_) => String::new(),
    }
}

fn attr_bool(obj: &Bound<'_, PyAny>, name: &str, default: bool) -> bool {
    match obj.getattr(name) {
        Ok(v) => v.is_truthy().unwrap_or(default),
        Err(_) => default,
    }
}

fn set_attr_str(obj: &Bound<'_, PyAny>, name: &str, value: &str) -> PyResult<()> {
    obj.setattr(name, value)
}

/// 把一个 Python 条目包成 `ValidateItem`。`native` 命中时省掉属性查找
/// （2000 项 × 十来个字段，这一层就是 P2 里 `finalize` 变慢的原因）。
struct ItemAdapter<'py> {
    obj: Bound<'py, PyAny>,
}

impl<'py> ItemAdapter<'py> {
    fn new(obj: &Bound<'py, PyAny>) -> Self {
        Self { obj: obj.clone() }
    }

    fn native(&self) -> Option<PyRef<'py, PyItem>> {
        self.obj.cast::<PyItem>().ok().map(|b| b.borrow())
    }

    fn native_mut(&self) -> Option<PyRefMut<'py, PyItem>> {
        self.obj.cast::<PyItem>().ok().map(|b| b.borrow_mut())
    }
}

impl validate_core::ValidateItem for ItemAdapter<'_> {
    fn name(&self) -> String {
        match self.native() {
            Some(n) => n.name.clone(),
            None => attr_str(&self.obj, "name"),
        }
    }

    fn new_name(&self) -> String {
        match self.native() {
            Some(n) => n.new_name.clone(),
            None => attr_str(&self.obj, "new_name"),
        }
    }

    fn set_new_name(&self, value: String) {
        match self.native_mut() {
            Some(mut n) => n.new_name = value,
            None => {
                let _ = set_attr_str(&self.obj, "new_name", &value);
            }
        }
    }

    fn path(&self) -> String {
        match self.native() {
            Some(n) => n.path.clone(),
            None => attr_str(&self.obj, "path"),
        }
    }

    fn dirname(&self) -> String {
        match self.native() {
            Some(n) => n.dirname_value(),
            None => attr_str(&self.obj, "dirname"),
        }
    }

    fn is_dir(&self) -> bool {
        match self.native() {
            Some(n) => n.is_dir,
            None => attr_bool(&self.obj, "is_dir", false),
        }
    }

    fn status(&self) -> String {
        match self.native() {
            Some(n) => n.status.clone(),
            None => match self.obj.getattr("status") {
                Ok(_) => attr_str(&self.obj, "status"),
                Err(_) => consts::ST_OK.to_string(),
            },
        }
    }

    fn set_status(&self, value: String) {
        match self.native_mut() {
            Some(mut n) => n.status = value,
            None => {
                let _ = set_attr_str(&self.obj, "status", &value);
            }
        }
    }

    fn note(&self) -> String {
        match self.native() {
            Some(n) => n.note.clone(),
            None => attr_str(&self.obj, "note"),
        }
    }

    fn set_note(&self, value: String) {
        match self.native_mut() {
            Some(mut n) => n.note = value,
            None => {
                let _ = set_attr_str(&self.obj, "note", &value);
            }
        }
    }
}

/// 进度回调。Python 侧的签名是 `(当前, 总数, 文案)`；回调自己出错就跳过——
/// 中途抛异常会让改名停在临时名上，比少刷新一次进度糟糕得多。
fn call_progress(progress: Option<&Bound<'_, PyAny>>, cur: usize, total: usize, msg: &str) {
    if let Some(p) = progress {
        let _ = p.call1((cur, total, msg));
    }
}

// --------------------------------------------------------------------------- //
// 名字校验
// --------------------------------------------------------------------------- //

#[pyfunction]
#[pyo3(signature = (name, is_dir = false))]
fn name_problem(name: Option<&Bound<'_, PyAny>>, is_dir: bool) -> PyResult<String> {
    // `name=None` 在 Python 版里单独有分支；传别的非字符串对象会直接炸在
    // `name.strip()` 上（AttributeError），这里保持同样的失败方式，别偷偷 str()
    let value = match name {
        None => return Ok(names_core::name_problem(None, is_dir)),
        Some(v) => v,
    };
    if value.is_none() {
        return Ok(names_core::name_problem(None, is_dir));
    }
    let text: String = match value.extract::<String>() {
        Ok(s) => s,
        Err(_) => {
            let type_name: String = value
                .get_type()
                .name()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_else(|_| "object".to_string());
            return Err(pyo3::exceptions::PyAttributeError::new_err(format!(
                "'{type_name}' object has no attribute 'strip'"
            )));
        }
    };
    Ok(names_core::name_problem(Some(&text), is_dir))
}

#[pyfunction]
#[pyo3(signature = (new, old, is_dir = false))]
fn nameless_problem(new: &str, old: &str, is_dir: bool) -> String {
    names_core::nameless_problem(new, old, is_dir)
}

#[pyfunction]
#[pyo3(signature = (rel))]
fn safe_rel(rel: &str) -> String {
    names_core::safe_rel(rel)
}

// --------------------------------------------------------------------------- //
// 两遍校验
// --------------------------------------------------------------------------- //

#[pyfunction]
#[pyo3(signature = (items, dir_keys = None, progress = None))]
pub fn validate_items(
    py: Python<'_>,
    items: &Bound<'_, PyAny>,
    dir_keys: Option<&Bound<'_, PyAny>>,
    progress: Option<&Bound<'_, PyAny>>,
) -> PyResult<Py<PyValidateReport>> {
    let list: Vec<Bound<'_, PyAny>> = items.try_iter()?.collect::<PyResult<Vec<_>>>()?;
    let adapters: Vec<ItemAdapter<'_>> = list.iter().map(ItemAdapter::new).collect();
    let keys: Option<HashSet<String>> = match dir_keys {
        Some(obj) if !obj.is_none() => {
            let mut set = HashSet::new();
            for value in obj.try_iter()? {
                let value = value?;
                set.insert(value.str()?.to_string_lossy().into_owned());
            }
            Some(set)
        }
        _ => None,
    };
    let mut cb = |cur: usize, total: usize, msg: &str| call_progress(progress, cur, total, msg);
    let rep = validate_core::validate_items(
        &adapters,
        keys.as_ref(),
        &validate_core::RealFs,
        &mut cb,
    );
    Py::new(py, PyValidateReport::from_core(&rep))
}

// --------------------------------------------------------------------------- //
// 落盘
// --------------------------------------------------------------------------- //

#[pyfunction]
#[pyo3(signature = (src, dst, attempts = 3))]
fn rename_with_retry(src: &str, dst: &str, attempts: usize) -> PyResult<()> {
    rename_core::rename_with_retry(src, dst, attempts.max(1)).map_err(|e| {
        let msg = rename_core::describe_io_error(&e);
        PyOSError::new_err(msg)
    })
}

/// `execute` 的实际实现，`ops.apply` 也要用（别让两条入口各写一份）。
pub fn execute_impl(
    py: Python<'_>,
    items: &Bound<'_, PyAny>,
    progress: Option<&Bound<'_, PyAny>>,
) -> PyResult<Py<PyList>> {
    let list: Vec<Bound<'_, PyAny>> = items.try_iter()?.collect::<PyResult<Vec<_>>>()?;

    // 只挑"状态 ok + 有新名字 + 真的变了"的项
    let mut picked: Vec<usize> = Vec::new();
    for (i, obj) in list.iter().enumerate() {
        let status = {
            let native = obj.cast::<PyItem>().ok().map(|b| b.borrow());
            match native {
                Some(n) => n.status.clone(),
                None => attr_str(obj, "status"),
            }
        };
        if status != consts::ST_OK {
            continue;
        }
        let (name, new_name, is_dir) = {
            let native = obj.cast::<PyItem>().ok().map(|b| b.borrow());
            match native {
                Some(n) => (n.name.clone(), n.new_name.clone(), n.is_dir),
                None => (
                    attr_str(obj, "name"),
                    attr_str(obj, "new_name"),
                    attr_bool(obj, "is_dir", false),
                ),
            }
        };
        if new_name.is_empty() || new_name == name {
            continue;
        }
        let _ = is_dir;
        picked.push(i);
    }

    // 先备好拥有所有权的字符串，再借给 ExecItem（借用不能跨语句活太久）
    let owned: Vec<(usize, String, String, String, bool)> = picked
        .iter()
        .map(|&i| {
            let obj = &list[i];
            let native = obj.cast::<PyItem>().ok().map(|b| b.borrow());
            match native {
                Some(n) => (
                    i,
                    n.path.clone(),
                    n.dirname_value(),
                    n.new_name.clone(),
                    n.is_dir,
                ),
                None => (
                    i,
                    attr_str(obj, "path"),
                    attr_str(obj, "dirname"),
                    attr_str(obj, "new_name"),
                    attr_bool(obj, "is_dir", false),
                ),
            }
        })
        .collect();

    let exec_items: Vec<rename_core::ExecItem<'_>> = owned
        .iter()
        .map(|(idx, path, dirname, new_name, is_dir)| rename_core::ExecItem {
            index: *idx,
            path,
            dirname,
            new_name,
            is_dir: *is_dir,
        })
        .collect();

    let mut cb = |cur: usize, total: usize, msg: &str| call_progress(progress, cur, total, msg);
    let steps = rename_core::execute(&exec_items, &mut cb);

    let out = PyList::empty(py);
    for step in steps {
        let obj = Py::new(
            py,
            PyRenameStep {
                item: list[step.index].clone().unbind(),
                old: step.old,
                new: step.new,
                ok: step.ok,
                error: step.error,
            },
        )?;
        out.append(obj)?;
    }
    Ok(out.unbind())
}

#[pyfunction]
#[pyo3(signature = (items, progress = None))]
fn execute(
    py: Python<'_>,
    items: &Bound<'_, PyAny>,
    progress: Option<&Bound<'_, PyAny>>,
) -> PyResult<Py<PyList>> {
    execute_impl(py, items, progress)
}

#[pyfunction]
#[pyo3(signature = (root, max_depth = 8))]
fn find_temp_leftovers(root: &str, max_depth: usize) -> Vec<String> {
    rename_core::find_temp_leftovers(
        root,
        max_depth,
        &[consts::LOG_DIR_NAME, consts::BACKUP_DIR_NAME],
    )
}

// --------------------------------------------------------------------------- //
// 日志与撤销
// --------------------------------------------------------------------------- //

/// 撤销日志的存放目录。真正的决策在 `engine.backup_dir()`（环境变量覆盖 +
/// 程序目录可写性探测），这里只负责问它一句——那套逻辑留在根包里。
fn resolve_log_dir(py: Python<'_>) -> PyResult<String> {
    let engine = py.import("engine")?;
    let dir = engine.getattr("backup_dir")?.call0()?;
    dir.extract::<String>()
}

#[pyfunction]
#[pyo3(signature = (root = ""))]
fn log_dir(py: Python<'_>, root: &str) -> PyResult<String> {
    let _ = root; // 统一目录，与目标目录无关
    resolve_log_dir(py)
}

#[pyfunction]
#[pyo3(signature = (root))]
fn legacy_log_dir(root: &str) -> String {
    logs_core::legacy_log_dir(root)
}

fn pairs_of(pairs: &Bound<'_, PyAny>) -> PyResult<Vec<(String, String)>> {
    let mut out = Vec::new();
    for entry in pairs.try_iter()? {
        let entry = entry?;
        let (old, new): (String, String) = entry.extract()?;
        out.push((old, new));
    }
    Ok(out)
}

/// `x or {}`：None / 空容器一律当成空对象。
fn value_or_empty_object(obj: Option<&Bound<'_, PyAny>>) -> Value {
    match obj {
        Some(v) if !v.is_none() => {
            let value = pyjson::to_value(v);
            match value {
                Value::Null => Value::Object(Map::new()),
                other => other,
            }
        }
        _ => Value::Object(Map::new()),
    }
}

#[pyfunction]
#[pyo3(signature = (root, pairs, kind = "mixed", summary = None, options = None,
                    rules = None, note = "", executed = None))]
#[allow(clippy::too_many_arguments)]
fn save_log(
    py: Python<'_>,
    root: &str,
    pairs: &Bound<'_, PyAny>,
    kind: &str,
    summary: Option<&Bound<'_, PyAny>>,
    options: Option<&Bound<'_, PyAny>>,
    rules: Option<&Bound<'_, PyAny>>,
    note: &str,
    executed: Option<&Bound<'_, PyAny>>,
) -> PyResult<String> {
    let directory = resolve_log_dir(py)?;
    let meta = logs_core::LogMeta {
        kind: kind.to_string(),
        summary: value_or_empty_object(summary),
        executed: value_or_empty_object(executed),
        options: value_or_empty_object(options),
        rules: match rules {
            Some(v) if !v.is_none() => pyjson::to_value(v),
            _ => Value::Array(Vec::new()),
        },
        note: note.to_string(),
    };
    let pairs = pairs_of(pairs)?;
    Ok(logs_core::save_log(&directory, root, &pairs, &meta))
}

/// `_root_hash(root)`：目录指纹。`verify/test_edges.py` 直接调它来核对
/// 日志文件名与目录的对应关系，所以必须导出（不在 `__all__` 里，壳显式 import）。
#[pyfunction]
#[pyo3(signature = (root))]
fn _root_hash(root: &str) -> String {
    logs_core::root_hash(root)
}

/// `_split_log_name(name)`：拆 (指纹, 排序键)，认不出来给 ("", "")。
#[pyfunction]
#[pyo3(signature = (name))]
fn _split_log_name(name: &str) -> (String, String) {
    logs_core::split_log_name(name)
}

#[pyfunction]
#[pyo3(signature = (root = ""))]
fn list_logs(py: Python<'_>, root: &str) -> PyResult<Vec<String>> {
    let directory = resolve_log_dir(py)?;
    Ok(logs_core::list_logs(&directory, root))
}

#[pyfunction]
#[pyo3(signature = (path))]
fn load_log(py: Python<'_>, path: &str) -> PyResult<Py<PyAny>> {
    let _ = py;
    match logs_core::load_log(path) {
        Ok(value) => Python::attach(|py| pyjson::to_py(py, &value)),
        Err(msg) => {
            // 读不出来分两种：文件层面（OSError）与格式层面（ValueError）——
            // 调用方普遍写 `except (OSError, ValueError)`，两类都得给
            if std::path::Path::new(path).exists() {
                Err(PyValueError::new_err(msg))
            } else {
                Err(PyOSError::new_err(msg))
            }
        }
    }
}

#[pyfunction]
#[pyo3(signature = (data))]
fn log_pairs(py: Python<'_>, data: &Bound<'_, PyAny>) -> PyResult<Py<PyList>> {
    let value = pyjson::to_value(data);
    let out = PyList::empty(py);
    for (old, new) in logs_core::log_pairs(&value) {
        out.append(PyTuple::new(py, [old, new])?)?;
    }
    Ok(out.unbind())
}

/// `undo` 的实现，`ops.undo` 复用。
pub fn undo_impl(
    log_path: &str,
    progress: Option<&Bound<'_, PyAny>>,
) -> PyResult<(bool, Vec<String>)> {
    let mut cb = |cur: usize, total: usize, msg: &str| call_progress(progress, cur, total, msg);
    match logs_core::undo(log_path, &mut cb) {
        Ok(pair) => Ok(pair),
        Err(msg) => {
            if std::path::Path::new(log_path).exists() {
                Err(PyValueError::new_err(msg))
            } else {
                Err(PyOSError::new_err(msg))
            }
        }
    }
}

#[pyfunction]
#[pyo3(signature = (log_path, progress = None))]
fn undo(
    py: Python<'_>,
    log_path: &str,
    progress: Option<&Bound<'_, PyAny>>,
) -> PyResult<(bool, Vec<String>)> {
    let _ = py;
    undo_impl(log_path, progress)
}

// --------------------------------------------------------------------------- //
// 模块挂载
// --------------------------------------------------------------------------- //

pub fn attach(py: Python<'_>, parent: &Bound<'_, PyModule>) -> PyResult<()> {
    let m = crate::shim::new_submodule(py, parent, "common")?;
    m.add(
        "__doc__",
        "名字校验、两遍冲突检测、落盘改名、撤销日志（Rust 实现）。",
    )?;

    m.add("APP_NAME", consts::APP_NAME)?;
    m.add("APP_VER", namecat_core::APP_VER)?;
    m.add("LOG_DIR_NAME", consts::LOG_DIR_NAME)?;
    m.add("BACKUP_DIR_NAME", consts::BACKUP_DIR_NAME)?;
    m.add("LOG_SCHEMA", consts::LOG_SCHEMA)?;
    m.add("MAX_LOGS", consts::MAX_LOGS)?;
    m.add("MAX_LOGS_TOTAL", consts::MAX_LOGS_TOTAL)?;
    m.add("TEMP_PREFIX", consts::TEMP_PREFIX)?;
    m.add("INVALID_NAME_CHARS", namecat_core::rules::INVALID_NAME_CHARS)?;
    m.add("ILLEGAL_CHARS", namecat_core::rules::INVALID_NAME_CHARS)?;
    m.add("MAX_NAME_LEN", namecat_core::rules::MAX_NAME_LEN)?;

    let reserved = PySet::empty(py)?;
    for name in ["con", "prn", "aux", "nul"] {
        reserved.add(name)?;
    }
    for i in 1..=9 {
        reserved.add(format!("com{i}"))?;
        reserved.add(format!("lpt{i}"))?;
    }
    m.add("RESERVED_NAMES", reserved)?;

    let status_text = PyDict::new(py);
    for (key, text) in consts::STATUS_TEXT {
        status_text.set_item(*key, *text)?;
    }
    m.add("STATUS_TEXT", status_text)?;
    for (key, value) in [
        ("ST_OK", consts::ST_OK),
        ("ST_SAME", consts::ST_SAME),
        ("ST_CONFLICT", consts::ST_CONFLICT),
        ("ST_ERROR", consts::ST_ERROR),
        ("ST_SKIP", consts::ST_SKIP),
    ] {
        m.add(key, value)?;
    }

    m.add_class::<PyValidateReport>()?;
    m.add_class::<PyRenameStep>()?;
    m.add_function(wrap_pyfunction!(name_problem, &m)?)?;
    m.add_function(wrap_pyfunction!(nameless_problem, &m)?)?;
    m.add_function(wrap_pyfunction!(safe_rel, &m)?)?;
    m.add_function(wrap_pyfunction!(validate_items, &m)?)?;
    m.add_function(wrap_pyfunction!(rename_with_retry, &m)?)?;
    m.add_function(wrap_pyfunction!(execute, &m)?)?;
    m.add_function(wrap_pyfunction!(find_temp_leftovers, &m)?)?;
    m.add_function(wrap_pyfunction!(_root_hash, &m)?)?;
    m.add_function(wrap_pyfunction!(_split_log_name, &m)?)?;
    m.add_function(wrap_pyfunction!(log_dir, &m)?)?;
    m.add_function(wrap_pyfunction!(legacy_log_dir, &m)?)?;
    m.add_function(wrap_pyfunction!(save_log, &m)?)?;
    m.add_function(wrap_pyfunction!(list_logs, &m)?)?;
    m.add_function(wrap_pyfunction!(load_log, &m)?)?;
    m.add_function(wrap_pyfunction!(log_pairs, &m)?)?;
    m.add_function(wrap_pyfunction!(undo, &m)?)?;

    crate::shim::set_all(
        &m,
        &[
            "APP_NAME",
            "APP_VER",
            "BACKUP_DIR_NAME",
            "ILLEGAL_CHARS",
            "INVALID_NAME_CHARS",
            "LOG_DIR_NAME",
            "LOG_SCHEMA",
            "MAX_LOGS",
            "MAX_LOGS_TOTAL",
            "MAX_NAME_LEN",
            "RESERVED_NAMES",
            "RenameStep",
            "STATUS_TEXT",
            "ST_CONFLICT",
            "ST_ERROR",
            "ST_OK",
            "ST_SAME",
            "ST_SKIP",
            "TEMP_PREFIX",
            "ValidateReport",
            "execute",
            "find_temp_leftovers",
            "legacy_log_dir",
            "list_logs",
            "load_log",
            "log_dir",
            "log_pairs",
            "name_problem",
            "nameless_problem",
            "rename_with_retry",
            "safe_rel",
            "save_log",
            "undo",
            "validate_items",
        ],
    )
}
