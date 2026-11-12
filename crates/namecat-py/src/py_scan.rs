//! `engine/scan.py` 的绑定：`ScanOptions` / `ScanResult` 与模块级函数。

use std::collections::HashSet;

use namecat_core::scan as core;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBytes, PyDict, PyList, PySet, PyTuple};

use crate::py_context::{items_from_scan, PyItem};
use crate::pyctx::{bool_arg, coerce_i64, text_arg};

// --------------------------------------------------------------------------- //
// ScanOptions
// --------------------------------------------------------------------------- //

#[pyclass(module = "namecat_py.scan", name = "ScanOptions")]
pub struct PyScanOptions {
    #[pyo3(get, set)]
    pub root: String,
    pub kinds: Vec<String>,
    #[pyo3(get, set)]
    pub recursive: bool,
    #[pyo3(get, set)]
    pub include_hidden: bool,
    #[pyo3(get, set)]
    pub only_match: String,
    #[pyo3(get, set)]
    pub exclude_match: String,
    pub include_exts: Option<Vec<String>>,
    pub exclude_exts: Option<Vec<String>>,
    #[pyo3(get, set)]
    pub verify_content: bool,
    #[pyo3(get, set)]
    pub sort_mode: String,
    #[pyo3(get, set)]
    pub max_depth: i64,
    #[pyo3(get, set)]
    pub follow_log_dirs: bool,
    pub selected: Option<Vec<String>>,
    #[pyo3(get, set)]
    pub strict_selected: bool,
}

impl PyScanOptions {
    pub fn to_core(&self) -> core::ScanOptions {
        core::ScanOptions {
            root: self.root.clone(),
            kinds: self.kinds.clone(),
            recursive: self.recursive,
            include_hidden: self.include_hidden,
            only_match: self.only_match.clone(),
            exclude_match: self.exclude_match.clone(),
            include_exts: self.include_exts.clone(),
            exclude_exts: self.exclude_exts.clone(),
            verify_content: self.verify_content,
            sort_mode: self.sort_mode.clone(),
            max_depth: self.max_depth,
            follow_log_dirs: self.follow_log_dirs,
            selected: self.selected.clone(),
            strict_selected: self.strict_selected,
        }
    }
}

/// 把 set/list/None 收成 `Option<Vec<String>>`（Python 侧这两种都出现过）。
fn opt_strs(value: Option<&Bound<'_, PyAny>>) -> Option<Vec<String>> {
    let v = value?;
    if v.is_none() {
        return None;
    }
    let mut out: Vec<String> = Vec::new();
    if let Ok(iter) = v.try_iter() {
        for item in iter.flatten() {
            out.push(text_arg(Some(&item)));
        }
    }
    Some(out)
}

#[pymethods]
impl PyScanOptions {
    #[new]
    #[pyo3(signature = (root = None, kinds = None, recursive = None, include_hidden = None,
                        only_match = None, exclude_match = None, include_exts = None,
                        exclude_exts = None, verify_content = None,
                        sort_mode = None, max_depth = None,
                        follow_log_dirs = None, selected = None, strict_selected = None))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        root: Option<&Bound<'_, PyAny>>,
        kinds: Option<&Bound<'_, PyAny>>,
        recursive: Option<&Bound<'_, PyAny>>,
        include_hidden: Option<&Bound<'_, PyAny>>,
        only_match: Option<&Bound<'_, PyAny>>,
        exclude_match: Option<&Bound<'_, PyAny>>,
        include_exts: Option<&Bound<'_, PyAny>>,
        exclude_exts: Option<&Bound<'_, PyAny>>,
        verify_content: Option<&Bound<'_, PyAny>>,
        sort_mode: Option<&Bound<'_, PyAny>>,
        max_depth: Option<&Bound<'_, PyAny>>,
        follow_log_dirs: Option<&Bound<'_, PyAny>>,
        selected: Option<&Bound<'_, PyAny>>,
        strict_selected: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Self> {
        Ok(Self {
            root: text_arg(root),
            kinds: opt_strs(kinds).unwrap_or_else(|| vec![core::KIND_FILE.to_string()]),
            recursive: bool_arg(recursive, false),
            include_hidden: bool_arg(include_hidden, false),
            only_match: text_arg(only_match),
            exclude_match: text_arg(exclude_match),
            include_exts: opt_strs(include_exts),
            exclude_exts: opt_strs(exclude_exts),
            verify_content: bool_arg(verify_content, false),
            sort_mode: {
                let s = text_arg(sort_mode);
                if s.is_empty() { "path_asc".to_string() } else { s }
            },
            max_depth: coerce_i64(max_depth).unwrap_or(core::MAX_DEPTH),
            follow_log_dirs: bool_arg(follow_log_dirs, false),
            selected: opt_strs(selected),
            strict_selected: bool_arg(strict_selected, false),
        })
    }

    #[getter]
    fn kinds(&self) -> Vec<String> {
        self.kinds.clone()
    }
    #[setter]
    fn set_kinds(&mut self, value: Option<&Bound<'_, PyAny>>) {
        self.kinds = opt_strs(value).unwrap_or_default();
    }

    #[getter]
    fn include_exts(&self) -> Option<Vec<String>> {
        self.include_exts.clone()
    }
    #[setter]
    fn set_include_exts(&mut self, value: Option<&Bound<'_, PyAny>>) {
        self.include_exts = opt_strs(value);
    }
    #[getter]
    fn exclude_exts(&self) -> Option<Vec<String>> {
        self.exclude_exts.clone()
    }
    #[setter]
    fn set_exclude_exts(&mut self, value: Option<&Bound<'_, PyAny>>) {
        self.exclude_exts = opt_strs(value);
    }
    #[getter]
    fn selected(&self) -> Option<Vec<String>> {
        self.selected.clone()
    }
    #[setter]
    fn set_selected(&mut self, value: Option<&Bound<'_, PyAny>>) {
        self.selected = opt_strs(value);
    }

    /// 该条目要不要收；要收则返回 kind。
    fn wanted_kind(&self, name: &str, is_dir: bool) -> Option<String> {
        self.to_core().wanted_kind(name, is_dir)
    }

    fn __repr__(&self) -> String {
        format!(
            "ScanOptions(root={:?}, kinds={:?}, recursive={}, sort_mode={:?})",
            self.root, self.kinds, self.recursive, self.sort_mode
        )
    }
}

// --------------------------------------------------------------------------- //
// ScanResult
// --------------------------------------------------------------------------- //

#[pyclass(module = "namecat_py.scan", name = "ScanResult")]
pub struct PyScanResult {
    #[pyo3(get)]
    pub items: Py<PyList>,
    #[pyo3(get, set)]
    pub skipped: usize,
    #[pyo3(get, set)]
    pub dirs: usize,
    #[pyo3(get, set)]
    pub files: usize,
}

#[pymethods]
impl PyScanResult {
    #[new]
    #[pyo3(signature = (items = None, skipped = None, dirs = None, files = None))]
    fn new(
        py: Python<'_>,
        items: Option<&Bound<'_, PyAny>>,
        skipped: Option<usize>,
        dirs: Option<usize>,
        files: Option<usize>,
    ) -> PyResult<Self> {
        let list = PyList::empty(py);
        if let Some(v) = items {
            if let Ok(iter) = v.try_iter() {
                for it in iter.flatten() {
                    list.append(it)?;
                }
            }
        }
        Ok(Self {
            items: list.unbind(),
            skipped: skipped.unwrap_or(0),
            dirs: dirs.unwrap_or(0),
            files: files.unwrap_or(0),
        })
    }

    fn __len__(&self, py: Python<'_>) -> usize {
        self.items.bind(py).len()
    }

    #[getter]
    fn total(&self, py: Python<'_>) -> usize {
        self.items.bind(py).len()
    }
}

// --------------------------------------------------------------------------- //
// 函数
// --------------------------------------------------------------------------- //

#[pyfunction]
#[pyo3(signature = (opts, progress = None))]
fn scan(
    py: Python<'_>,
    opts: &Bound<'_, PyScanOptions>,
    progress: Option<&Bound<'_, PyAny>>,
) -> PyResult<PyScanResult> {
    let core_opts = opts.borrow().to_core();
    let mut cb = |dir_path: &str| {
        if let Some(p) = progress {
            // Python 版先按单参数调，TypeError 再退回 (0, 0, 文案)
            if p.call1((dir_path,)).is_err() {
                let _ = p.call1((0, 0, format!("扫描 {dir_path}")));
            }
        }
    };
    let res = core::scan(&core_opts, &mut cb);
    Ok(PyScanResult {
        items: items_from_scan(py, &res.items)?,
        skipped: res.skipped,
        dirs: res.dirs,
        files: res.files,
    })
}

/// 自然序键：返回 `[(0, 0, 文本), (1, 整数, ""), ...]`，可以直接当 `sorted` 的 key。
#[pyfunction]
#[pyo3(signature = (text))]
fn natural_key(py: Python<'_>, text: &str) -> PyResult<Py<PyList>> {
    use pyo3::types::{PyInt, PyString};
    let key = core::natural_key(text);
    let out = PyList::empty(py);
    for part in key.0 {
        let t = match part {
            core::KeyPart::Text(s) => PyTuple::new(
                py,
                [
                    PyInt::new(py, 0).into_any(),
                    PyInt::new(py, 0).into_any(),
                    PyString::new(py, &s).into_any(),
                ],
            )?,
            core::KeyPart::Num(s) => {
                // 超长号码段按 i128 饱和：文件里塞 40 位数字属于极端情况，
                // 真要精确比较请用 core 侧（那边比的是十进制串，不溢出）
                let n = s.parse::<i128>().unwrap_or(i128::MAX);
                PyTuple::new(
                    py,
                    [
                        PyInt::new(py, 1).into_any(),
                        PyInt::new(py, n).into_any(),
                        PyString::new(py, "").into_any(),
                    ],
                )?
            }
        };
        out.append(t)?;
    }
    Ok(out.unbind())
}

#[pyfunction]
#[pyo3(signature = (path))]
fn nkey(path: &str) -> String {
    core::nkey(path)
}

#[pyfunction]
#[pyo3(signature = (name, is_dir, image_exts = None, video_exts = None))]
fn kind_of(
    name: &str,
    is_dir: bool,
    image_exts: Option<&Bound<'_, PyAny>>,
    video_exts: Option<&Bound<'_, PyAny>>,
) -> String {
    let to_set = |v: Option<&Bound<'_, PyAny>>| -> Option<HashSet<String>> {
        opt_strs(v).map(|list| list.into_iter().map(|s| s.to_lowercase()).collect())
    };
    let images = to_set(image_exts);
    let videos = to_set(video_exts);
    core::kind_of(name, is_dir, images.as_ref(), videos.as_ref())
}

#[pyfunction]
#[pyo3(signature = (path, ext))]
fn check_file_magic(path: &str, ext: &str) -> bool {
    core::check_file_magic(path, ext)
}

#[pyfunction]
#[pyo3(signature = (name))]
fn is_hidden_name(name: &str) -> bool {
    core::is_hidden_name(name)
}

/// 注意：入参是 **stat 对象**（Python 侧签名就是 `is_hidden_attrs(st)`）。
#[pyfunction]
#[pyo3(signature = (st))]
fn is_hidden_attrs(st: &Bound<'_, PyAny>) -> bool {
    let attrs = st
        .getattr("st_file_attributes")
        .ok()
        .and_then(|v| v.extract::<u32>().ok())
        .unwrap_or(0);
    core::is_hidden_attrs(attrs)
}

/// 原地排序一组 `Item`（保持"先名称、再主键"的稳定排序语义）。
#[pyfunction]
#[pyo3(signature = (items, mode))]
fn sort_items(py: Python<'_>, items: &Bound<'_, PyAny>, mode: &str) -> PyResult<()> {
    let list = items.cast::<PyList>()?;
    let n = list.len();
    let mut metas: Vec<Option<core::Meta>> = Vec::with_capacity(n);
    let mut names: Vec<String> = Vec::with_capacity(n);
    let mut paths: Vec<String> = Vec::with_capacity(n);
    for i in 0..n {
        let obj = list.get_item(i)?;
        let item = obj.cast::<PyItem>()?;
        let r = item.borrow();
        names.push(r.name.clone());
        paths.push(r.path.clone());
        metas.push(r.meta());
    }
    let refs: Vec<core::SortItem> = (0..n)
        .map(|i| core::SortItem {
            name: &names[i],
            path: &paths[i],
            meta: metas[i].as_ref(),
        })
        .collect();
    let order = core::sort_indices(&refs, mode);
    if order.iter().enumerate().all(|(i, &j)| i == j) {
        return Ok(());
    }
    let snapshot: Vec<Bound<'_, PyAny>> = (0..n).map(|i| list.get_item(i)).collect::<Result<_, _>>()?;
    let _ = py;
    for (dst, &src) in order.iter().enumerate() {
        list.set_item(dst, &snapshot[src])?;
    }
    Ok(())
}

// --------------------------------------------------------------------------- //
// 常量
// --------------------------------------------------------------------------- //

pub fn attach(py: Python<'_>, parent: &Bound<'_, PyModule>) -> PyResult<()> {
    let m = crate::shim::new_submodule(py, parent, "scan")?;
    m.add(
        "__doc__",
        "目录扫描（Rust 实现）：过滤、自然序、文件头校验、选中项语义。",
    )?;
    m.add("KIND_FILE", core::KIND_FILE)?;
    m.add("KIND_DIR", core::KIND_DIR)?;
    m.add("KIND_IMAGE", core::KIND_IMAGE)?;
    m.add("KIND_VIDEO", core::KIND_VIDEO)?;
    m.add("ALL_KINDS", PyTuple::new(py, core::ALL_KINDS)?)?;
    m.add("MAX_DEPTH", core::MAX_DEPTH)?;

    let kind_text = PyDict::new(py);
    for (k, v) in core::kind_text() {
        kind_text.set_item(k, v)?;
    }
    m.add("KIND_TEXT", kind_text)?;

    let images = PySet::empty(py)?;
    for e in core::IMAGE_EXTS {
        images.add(e)?;
    }
    m.add("IMAGE_EXTS", images)?;
    let videos = PySet::empty(py)?;
    for e in core::VIDEO_EXTS {
        videos.add(e)?;
    }
    m.add("VIDEO_EXTS", videos)?;

    let modes = PyList::empty(py);
    for (k, t) in core::sort_modes() {
        modes.append(PyTuple::new(py, [k, t])?)?;
    }
    m.add("SORT_MODES", modes)?;
    let keys = PyList::empty(py);
    let text = PyDict::new(py);
    for (k, t) in core::sort_modes() {
        keys.append(k)?;
        text.set_item(k, t)?;
    }
    m.add("SORT_KEYS", keys)?;
    m.add("SORT_TEXT", text)?;

    let magic = PyDict::new(py);
    for (ext, sigs) in core::magic_table() {
        match sigs {
            None => magic.set_item(ext, py.None())?,
            Some(list) => {
                let mut items: Vec<Bound<'_, PyAny>> = Vec::with_capacity(list.len());
                for sig in list {
                    items.push(PyBytes::new(py, sig).into_any());
                }
                magic.set_item(ext, PyTuple::new(py, items)?)?;
            }
        }
    }
    m.add("MAGIC", magic)?;

    m.add("LOG_DIR_NAMES", PyTuple::new(py, core::LOG_DIR_NAMES)?)?;
    m.add("LOG_DIR_SKIP", PyTuple::new(py, core::log_dir_skip())?)?;

    m.add_class::<PyScanOptions>()?;
    m.add_class::<PyScanResult>()?;
    m.add_function(wrap_pyfunction!(scan, &m)?)?;
    m.add_function(wrap_pyfunction!(natural_key, &m)?)?;
    m.add_function(wrap_pyfunction!(nkey, &m)?)?;
    m.add_function(wrap_pyfunction!(kind_of, &m)?)?;
    m.add_function(wrap_pyfunction!(check_file_magic, &m)?)?;
    m.add_function(wrap_pyfunction!(is_hidden_name, &m)?)?;
    m.add_function(wrap_pyfunction!(is_hidden_attrs, &m)?)?;
    m.add_function(wrap_pyfunction!(sort_items, &m)?)?;
    // presets.py 会按 `__dataclass_fields__` 过滤字段名
    crate::shim::attach_field_names(
        &m,
        "ScanOptions",
        &[
            "root", "kinds", "recursive", "include_hidden", "only_match",
            "exclude_match", "include_exts", "exclude_exts", "verify_content",
            "sort_mode", "max_depth", "follow_log_dirs", "selected", "strict_selected",
        ],
    )?;
    crate::shim::set_all(
        &m,
        &[
            "ALL_KINDS",
            "IMAGE_EXTS",
            "KIND_DIR",
            "KIND_FILE",
            "KIND_IMAGE",
            "KIND_TEXT",
            "KIND_VIDEO",
            "LOG_DIR_NAMES",
            "LOG_DIR_SKIP",
            "MAGIC",
            "MAX_DEPTH",
            "SORT_KEYS",
            "SORT_MODES",
            "SORT_TEXT",
            "ScanOptions",
            "ScanResult",
            "VIDEO_EXTS",
            "check_file_magic",
            "is_hidden_attrs",
            "is_hidden_name",
            "kind_of",
            "natural_key",
            "nkey",
            "scan",
            "sort_items",
        ],
    )
}
