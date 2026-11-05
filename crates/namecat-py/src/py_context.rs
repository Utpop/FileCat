//! `engine/context.py` 的绑定：`NameContext` 与 `Item` 两个 pyclass。
//!
//! 这两个类的**属性面**是契约的一部分（适配层会 `item.__dict__["_excluded"]`、
//! 会直接给 `item.new_name` 赋值），所以：
//! · `Item` 必须是 `dict` 类（可以随时挂临时字段）；
//! · `path/name/is_dir/index/total/num/width/st/kind/new_name/status/note`
//!   全部可读可写；
//! · 内部状态放 `Mutex`（不是 `RefCell`）——`#[pyclass]` 要求 `Sync`，
//!   而 `Item` 会**跨线程**流转：后台线程扫描出清单，界面线程再读它。
//!   用 `RefCell` 会编译不过，用 `unsendable` 会在换线程时 panic。

use std::sync::{Mutex, MutexGuard};

use namecat_core::context::{NameCtx, NumArg};
use namecat_core::pathops;
use namecat_core::scan::Meta;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyList, PyTuple};

use crate::pyctx::{bool_arg, coerce_i64, text_arg};

/// 忽略中毒的加锁（持锁期间 panic 只会发生在编码错误上，
/// 让它把整批改名搞成"永远拿不到锁"是更糟的结果）。
pub fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// 用 [`Meta`] 拼一个 `os.stat_result`（`Item.st` 的对外形态）。
fn stat_result(py: Python<'_>, meta: &Meta) -> PyResult<Py<PyAny>> {
    let os = py.import("os")?;
    let cls = os.getattr("stat_result")?;
    let mut items: Vec<Py<PyAny>> = Vec::with_capacity(10);
    for v in [
        meta.mode as i64,
        meta.ino as i64,
        meta.dev as i64,
        meta.nlink as i64,
        meta.uid as i64,
        meta.gid as i64,
        meta.size,
    ] {
        items.push(v.into_pyobject(py)?.into_any().unbind());
    }
    for v in [meta.atime, meta.mtime, meta.ctime] {
        items.push(v.into_pyobject(py)?.into_any().unbind());
    }
    let seq = PyTuple::new(py, items)?;
    Ok(cls.call1((seq,))?.unbind())
}

/// 从一个鸭子类型的 stat 对象里抠出我们需要的四个数。
fn meta_from_object(obj: &Bound<'_, PyAny>) -> Meta {
    let size = obj.getattr("st_size").ok().and_then(|v| v.extract::<i64>().ok()).unwrap_or(0);
    let mtime = obj.getattr("st_mtime").ok().and_then(|v| v.extract::<f64>().ok()).unwrap_or(0.0);
    let ctime = obj.getattr("st_ctime").ok().and_then(|v| v.extract::<f64>().ok()).unwrap_or(0.0);
    let atime = obj.getattr("st_atime").ok().and_then(|v| v.extract::<f64>().ok()).unwrap_or(0.0);
    let mode = obj
        .getattr("st_mode")
        .ok()
        .and_then(|v| v.extract::<u32>().ok())
        .unwrap_or(0o100644);
    Meta {
        mode,
        size,
        mtime,
        ctime,
        atime,
        ..Meta::default()
    }
}

// --------------------------------------------------------------------------- //
// NameContext
// --------------------------------------------------------------------------- //

#[pyclass(module = "namecat_py.context", name = "NameContext")]
pub struct PyNameContext {
    pub inner: Mutex<NameCtx>,
    /// 外部传进来的 stat 对象（原样返回给 `.stat`）
    st_obj: Mutex<Option<Py<PyAny>>>,
    /// 活的 dict（适配层/LLM 会原地改写它）
    pub extra: Mutex<Py<PyDict>>,
}

impl PyNameContext {
    #[allow(clippy::too_many_arguments)]
    pub fn build(
        py: Python<'_>,
        path: &str,
        name: &str,
        is_dir: bool,
        index: i64,
        total: i64,
        num: Option<NumArg>,
        width: i64,
        meta: Option<Meta>,
        st_obj: Option<Py<PyAny>>,
    ) -> Self {
        let ctx = NameCtx::new(path, name, is_dir, index, total, num, width).with_meta(meta);
        Self {
            inner: Mutex::new(ctx),
            st_obj: Mutex::new(st_obj),
            extra: Mutex::new(PyDict::new(py).unbind()),
        }
    }
}

#[pymethods]
impl PyNameContext {
    #[new]
    #[pyo3(signature = (path, name = None, is_dir = None, index = 1, total = 1,
                        num = None, width = None, st = None, extra = None))]
    fn new(
        py: Python<'_>,
        path: &str,
        name: Option<&Bound<'_, PyAny>>,
        is_dir: Option<&Bound<'_, PyAny>>,
        index: i64,
        total: i64,
        num: Option<&Bound<'_, PyAny>>,
        width: Option<&Bound<'_, PyAny>>,
        st: Option<&Bound<'_, PyAny>>,
        extra: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Self> {
        let name = if name.is_none() || name.map(|v| v.is_none()).unwrap_or(false) {
            pathops::basename(path)
        } else {
            text_arg(name)
        };
        let is_dir = if is_dir.is_none() || is_dir.map(|v| v.is_none()).unwrap_or(false) {
            std::path::Path::new(path).is_dir()
        } else {
            bool_arg(is_dir, false)
        };
        // `num is None` → 用 index；传了别的类型就按 int() 语义收，收不了记 Bad
        let num = match num {
            None => Some(NumArg::Int(index)),
            Some(v) if v.is_none() => Some(NumArg::Int(index)),
            Some(v) => Some(match coerce_i64(Some(v)) {
                Some(n) => NumArg::Int(n),
                None => NumArg::Bad,
            }),
        };
        let width = coerce_i64(width).unwrap_or(1);

        let (meta, st_obj) = match st {
            Some(v) if !v.is_none() => (Some(meta_from_object(v)), Some(v.clone().unbind())),
            _ => (None, None),
        };
        let obj = Self::build(py, path, &name, is_dir, index, total, num, width, meta, st_obj);
        if let Some(ex) = extra {
            if !ex.is_none() {
                *lock(&obj.extra) = dict_of(py, ex)?;
            }
        }
        Ok(obj)
    }

    // ---- 可写字段 ----
    #[getter]
    fn path(&self) -> String {
        lock(&self.inner).path.clone()
    }
    #[setter]
    fn set_path(&self, v: String) {
        lock(&self.inner).path = v;
    }
    #[getter]
    fn name(&self) -> String {
        lock(&self.inner).name.clone()
    }
    #[setter]
    fn set_name(&self, v: String) {
        lock(&self.inner).name = v;
    }
    #[getter]
    fn is_dir(&self) -> bool {
        lock(&self.inner).is_dir
    }
    #[setter]
    fn set_is_dir(&self, v: bool) {
        lock(&self.inner).is_dir = v;
    }
    #[getter]
    fn index(&self) -> i64 {
        lock(&self.inner).index
    }
    #[setter]
    fn set_index(&self, v: i64) {
        lock(&self.inner).index = v;
    }
    #[getter]
    fn total(&self) -> i64 {
        lock(&self.inner).total
    }
    #[setter]
    fn set_total(&self, v: i64) {
        lock(&self.inner).total = v;
    }
    #[getter]
    fn num(&self) -> i64 {
        match &lock(&self.inner).num {
            Some(NumArg::Int(n)) => *n,
            _ => 0,
        }
    }
    #[setter]
    fn set_num(&self, v: Option<&Bound<'_, PyAny>>) {
        let mut inner = lock(&self.inner);
        let idx = inner.index;
        inner.num = match v {
            None => Some(NumArg::Int(idx)),
            Some(v) if v.is_none() => Some(NumArg::Int(idx)),
            Some(v) => match coerce_i64(Some(v)) {
                Some(n) => Some(NumArg::Int(n)),
                None => Some(NumArg::Bad),
            },
        };
    }
    #[getter]
    fn width(&self) -> i64 {
        lock(&self.inner).width
    }
    #[setter]
    fn set_width(&self, v: i64) {
        lock(&self.inner).width = v;
    }

    // ---- 只读属性 ----
    #[getter]
    fn stat(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let cached = { lock(&self.st_obj).as_ref().map(|o| o.clone_ref(py)) };
        if let Some(obj) = cached {
            return Ok(obj);
        }
        let meta = lock(&self.inner).stat();
        match meta {
            Some(m) => {
                let obj = stat_result(py, &m)?;
                *lock(&self.st_obj) = Some(obj.clone_ref(py));
                Ok(obj)
            }
            None => Ok(py.None()),
        }
    }

    #[getter]
    fn split(&self) -> (String, String) {
        lock(&self.inner).split()
    }

    #[getter]
    fn ext(&self) -> String {
        lock(&self.inner).ext()
    }

    #[getter]
    fn e(&self) -> String {
        lock(&self.inner).e()
    }

    #[getter]
    fn stem(&self) -> String {
        lock(&self.inner).stem()
    }

    #[getter]
    fn dirname(&self) -> String {
        lock(&self.inner).dirname()
    }

    #[getter]
    fn kind(&self) -> String {
        lock(&self.inner).kind()
    }

    #[getter]
    fn extra(&self, py: Python<'_>) -> Py<PyDict> {
        lock(&self.extra).clone_ref(py)
    }

    /// `dict(value or {})`：传进来的是字典也要复制一份，之后改原对象不影响这里。
    #[setter]
    fn set_extra(&self, py: Python<'_>, value: Option<&Bound<'_, PyAny>>) -> PyResult<()> {
        let next = match value {
            Some(v) if !v.is_none() => dict_of(py, v)?,
            _ => PyDict::new(py).unbind(),
        };
        *lock(&self.extra) = next;
        Ok(())
    }

    #[getter]
    fn new_path_name(&self) -> String {
        lock(&self.inner).name.clone()
    }

    // ---- 方法 ----
    fn counter(&self) -> String {
        lock(&self.inner).counter()
    }

    fn variables(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let pairs: Vec<(String, String)> = lock(&self.inner).variables().to_vec();
        let d = PyDict::new(py);
        for (k, v) in pairs {
            d.set_item(k, v)?;
        }
        Ok(d.unbind())
    }

    fn var(&self, key: &Bound<'_, PyAny>) -> String {
        let key = text_arg(Some(key));
        // Python 版在 parentdir/filepath 上会抛 AttributeError，被上层吞成空串；
        // 这里直接给空串（没有任何调用方区分这两者）。
        lock(&self.inner).var(&key).unwrap_or_default()
    }

    fn digest(&self, algo: &str) -> String {
        lock(&self.inner).digest(algo)
    }

    fn __repr__(&self) -> String {
        let inner = lock(&self.inner);
        format!("NameContext(path={:?}, name={:?})", inner.path, inner.name)
    }
}

/// `dict(value)`。
fn dict_of(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<Py<PyDict>> {
    let cls = py.get_type::<PyDict>();
    Ok(cls.call1((value,))?.cast_into::<PyDict>()?.unbind())
}

// --------------------------------------------------------------------------- //
// Item
// --------------------------------------------------------------------------- //

#[pyclass(module = "namecat_py.context", name = "Item", dict)]
pub struct PyItem {
    pub path: String,
    pub name: String,
    pub is_dir: bool,
    pub index: i64,
    pub total: i64,
    pub num: i64,
    pub width: i64,
    pub kind: String,
    pub new_name: String,
    pub status: String,
    pub note: String,
    meta: Mutex<Option<Meta>>,
    st_obj: Mutex<Option<Py<PyAny>>>,
    ctx: Mutex<Option<Py<PyNameContext>>>,
    dirname_cache: Mutex<Option<String>>,
    /// `plan.finalize` 写进来的"新所在目录"（父目录改名时子项跟着走）
    pub new_dir: Mutex<String>,
}

impl PyItem {
    pub fn build(path: &str, name: &str, is_dir: bool) -> Self {
        Self {
            path: path.to_string(),
            name: name.to_string(),
            is_dir,
            index: 1,
            total: 1,
            num: 1,
            width: 1,
            kind: String::new(),
            new_name: String::new(),
            status: "ok".to_string(),
            note: String::new(),
            meta: Mutex::new(None),
            st_obj: Mutex::new(None),
            ctx: Mutex::new(None),
            dirname_cache: Mutex::new(None),
            new_dir: Mutex::new(String::new()),
        }
    }

    pub fn set_meta(&self, meta: Option<Meta>) {
        *lock(&self.meta) = meta;
    }

    pub fn meta(&self) -> Option<Meta> {
        lock(&self.meta).clone()
    }

    /// `bool(self.new_name) and self.new_name != self.name`
    pub fn is_changed(&self) -> bool {
        !self.new_name.is_empty() && self.new_name != self.name
    }

    pub fn dirname_value(&self) -> String {
        if let Some(d) = lock(&self.dirname_cache).as_ref() {
            return d.clone();
        }
        let d = pathops::dirname(&self.path);
        *lock(&self.dirname_cache) = Some(d.clone());
        d
    }

    /// 取内部上下文对象，顺手把编号相关的字段同步过去（Python 版 `_ctx_obj` 的语义）。
    pub fn ctx_obj(&self, py: Python<'_>) -> PyResult<Py<PyNameContext>> {
        let cached = { lock(&self.ctx).as_ref().map(|o| o.clone_ref(py)) };
        if let Some(obj) = cached {
            {
                let bound = obj.borrow(py);
                let mut inner = lock(&bound.inner);
                inner.index = self.index;
                inner.total = self.total;
                inner.num = Some(NumArg::Int(self.num));
                inner.width = self.width;
            }
            return Ok(obj);
        }
        let meta = self.meta();
        let obj = Py::new(
            py,
            PyNameContext::build(
                py,
                &self.path,
                &self.name,
                self.is_dir,
                self.index,
                self.total,
                Some(NumArg::Int(self.num)),
                self.width,
                meta,
                None,
            ),
        )?;
        *lock(&self.ctx) = Some(obj.clone_ref(py));
        Ok(obj)
    }
}

#[pymethods]
impl PyItem {
    #[new]
    #[pyo3(signature = (path, name = None, is_dir = None, index = 1, total = 1,
                        num = None, width = None, st = None, kind = None))]
    fn new(
        path: &str,
        name: Option<&Bound<'_, PyAny>>,
        is_dir: Option<&Bound<'_, PyAny>>,
        index: i64,
        total: i64,
        num: Option<&Bound<'_, PyAny>>,
        width: Option<&Bound<'_, PyAny>>,
        st: Option<&Bound<'_, PyAny>>,
        kind: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Self> {
        let name = if name.is_none() || name.map(|v| v.is_none()).unwrap_or(false) {
            pathops::basename(path)
        } else {
            text_arg(name)
        };
        let is_dir = if is_dir.is_none() || is_dir.map(|v| v.is_none()).unwrap_or(false) {
            std::path::Path::new(path).is_dir()
        } else {
            bool_arg(is_dir, false)
        };
        let num = match num {
            None => index,
            Some(v) if v.is_none() => index,
            Some(v) => coerce_i64(Some(v)).unwrap_or(index),
        };
        let mut item = Self::build(path, &name, is_dir);
        item.index = index;
        item.total = total;
        item.num = num;
        item.width = coerce_i64(width).unwrap_or(1);
        if let Some(k) = kind {
            if !k.is_none() {
                item.kind = text_arg(Some(k));
            }
        }
        if let Some(s) = st {
            if !s.is_none() {
                item.set_meta(Some(meta_from_object(s)));
                *lock(&item.st_obj) = Some(s.clone().unbind());
            }
        }
        Ok(item)
    }

    #[getter]
    fn path(&self) -> String {
        self.path.clone()
    }
    #[setter]
    fn set_path(&mut self, v: String) {
        self.path = v;
        *lock(&self.dirname_cache) = None;
    }
    #[getter]
    fn name(&self) -> String {
        self.name.clone()
    }
    #[setter]
    fn set_name(&mut self, v: String) {
        self.name = v;
    }
    #[getter]
    fn is_dir(&self) -> bool {
        self.is_dir
    }
    #[setter]
    fn set_is_dir(&mut self, v: bool) {
        self.is_dir = v;
    }
    #[getter]
    fn index(&self) -> i64 {
        self.index
    }
    #[setter]
    fn set_index(&mut self, v: i64) {
        self.index = v;
    }
    #[getter]
    fn total(&self) -> i64 {
        self.total
    }
    #[setter]
    fn set_total(&mut self, v: i64) {
        self.total = v;
    }
    #[getter]
    fn num(&self) -> i64 {
        self.num
    }
    #[setter]
    fn set_num(&mut self, v: Option<&Bound<'_, PyAny>>) {
        let idx = self.index;
        self.num = coerce_i64(v).unwrap_or(idx);
    }
    #[getter]
    fn width(&self) -> i64 {
        self.width
    }
    #[setter]
    fn set_width(&mut self, v: i64) {
        self.width = v;
    }
    #[getter]
    fn kind(&self) -> String {
        self.kind.clone()
    }
    #[setter]
    fn set_kind(&mut self, v: String) {
        self.kind = v;
    }
    #[getter]
    fn new_name(&self) -> String {
        self.new_name.clone()
    }
    #[setter]
    fn set_new_name(&mut self, v: Option<&Bound<'_, PyAny>>) {
        self.new_name = text_arg(v);
    }
    #[getter]
    fn status(&self) -> String {
        self.status.clone()
    }
    #[setter]
    fn set_status(&mut self, v: String) {
        self.status = v;
    }
    #[getter]
    fn note(&self) -> String {
        self.note.clone()
    }
    #[setter]
    fn set_note(&mut self, v: Option<&Bound<'_, PyAny>>) {
        self.note = text_arg(v);
    }

    /// `Item.st` 就是"当初给进来的那个 stat 对象"，**不做惰性 stat**：
    /// Python 版的 `self.st = st` 是一句普通赋值，手工 `Item(path)` 出来的
    /// 对象 `st` 就是 None（`export_csv` 靠这个判空）。扫描出来的条目在
    /// 扫描阶段就带着元数据，这里按需拼一个 `os.stat_result` 给它。
    #[getter]
    fn st(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let cached = { lock(&self.st_obj).as_ref().map(|o| o.clone_ref(py)) };
        if let Some(obj) = cached {
            return Ok(obj);
        }
        let meta = lock(&self.meta).clone();
        match meta {
            Some(m) => {
                let obj = stat_result(py, &m)?;
                *lock(&self.st_obj) = Some(obj.clone_ref(py));
                Ok(obj)
            }
            None => Ok(py.None()),
        }
    }

    #[setter]
    fn set_st(&self, value: Option<&Bound<'_, PyAny>>) -> PyResult<()> {
        match value {
            Some(v) if !v.is_none() => {
                let meta = meta_from_object(v);
                *lock(&self.meta) = Some(meta);
                *lock(&self.st_obj) = Some(v.clone().unbind());
            }
            _ => {
                *lock(&self.meta) = None;
                *lock(&self.st_obj) = None;
            }
        }
        Ok(())
    }

    #[getter]
    fn ext(&self, py: Python<'_>) -> PyResult<String> {
        let obj = self.ctx_obj(py)?;
        Ok(lock(&obj.borrow(py).inner).ext())
    }

    #[getter]
    fn stem(&self, py: Python<'_>) -> PyResult<String> {
        let obj = self.ctx_obj(py)?;
        Ok(lock(&obj.borrow(py).inner).stem())
    }

    #[getter]
    fn dirname(&self) -> String {
        self.dirname_value()
    }

    /// 改名后的完整路径（父目录若也改名，这里跟着走）。
    #[getter]
    fn new_path(&self) -> String {
        if self.new_name.is_empty() {
            return self.path.clone();
        }
        let nd = lock(&self.new_dir).clone();
        let base = if nd.is_empty() { self.dirname_value() } else { nd };
        pathops::join(&[&base, &self.new_name])
    }

    #[getter]
    fn changed(&self) -> bool {
        self.is_changed()
    }

    #[getter]
    fn _new_dir(&self) -> String {
        lock(&self.new_dir).clone()
    }

    #[setter(_new_dir)]
    fn set_new_dir(&self, v: String) {
        *lock(&self.new_dir) = v;
    }

    fn variables(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let obj = self.ctx_obj(py)?;
        let pairs = {
            let bound = obj.borrow(py);
            lock(&bound.inner).variables().to_vec()
        };
        let d = PyDict::new(py);
        for (k, v) in pairs {
            d.set_item(k, v)?;
        }
        Ok(d.unbind())
    }

    fn var(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<String> {
        let obj = self.ctx_obj(py)?;
        let key = text_arg(Some(key));
        let bound = obj.borrow(py);
        Ok(lock(&bound.inner).var(&key).unwrap_or_default())
    }

    fn digest(&self, py: Python<'_>, algo: &str) -> PyResult<String> {
        let obj = self.ctx_obj(py)?;
        let bound = obj.borrow(py);
        Ok(lock(&bound.inner).digest(algo))
    }

    fn _ctx_obj(&self, py: Python<'_>) -> PyResult<Py<PyNameContext>> {
        self.ctx_obj(py)
    }

    fn __repr__(&self) -> String {
        format!(
            "Item(path={:?}, name={:?}, is_dir={})",
            self.path, self.name, self.is_dir
        )
    }
}

/// 把一组 `ScanItem` 变成 Python 的 `Item` 列表（保持扫描顺序）。
pub fn items_from_scan(
    py: Python<'_>,
    scan: &[namecat_core::scan::ScanItem],
) -> PyResult<Py<PyList>> {
    let out = PyList::empty(py);
    for si in scan {
        let mut it = PyItem::build(&si.path, &si.name, si.is_dir);
        it.kind = si.kind.clone();
        it.set_meta(si.meta.clone());
        out.append(Py::new(py, it)?)?;
    }
    Ok(out.unbind())
}

// --------------------------------------------------------------------------- //
// 模块挂载
// --------------------------------------------------------------------------- //

#[pyfunction]
#[pyo3(signature = (name))]
fn split_ext(name: &str) -> (String, String) {
    namecat_core::text::split_ext(name)
}

#[pyfunction]
#[pyo3(signature = (ts, style))]
fn fmt_date(ts: f64, style: &str) -> String {
    namecat_core::timefmt::fmt_date(ts, style)
}

#[pyfunction]
#[pyo3(signature = (num))]
fn human_size(num: &Bound<'_, PyAny>) -> String {
    namecat_core::timefmt::human_size(coerce_i64(Some(num)).unwrap_or(0))
}

pub fn attach(py: Python<'_>, parent: &Bound<'_, PyModule>) -> PyResult<()> {
    let m = crate::shim::new_submodule(py, parent, "context")?;
    m.add(
        "__doc__",
        "命名上下文（Rust 实现）：NameContext 变量表 / Item 数据结构 / 扩展名与日期格式化。",
    )?;
    m.add_class::<PyNameContext>()?;
    m.add_class::<PyItem>()?;
    m.add_function(wrap_pyfunction!(split_ext, &m)?)?;
    m.add_function(wrap_pyfunction!(fmt_date, &m)?)?;
    m.add_function(wrap_pyfunction!(human_size, &m)?)?;
    crate::shim::set_all(
        &m,
        &["Item", "NameContext", "fmt_date", "human_size", "split_ext"],
    )
}
