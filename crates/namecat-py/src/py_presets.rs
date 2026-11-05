//! `engine/presets.py` 的绑定：规则预设的读写与内置清单。
//!
//! 存成 JSON（`ensure_ascii=False`、`indent=2`），放在 `<appdata>\NameCat\presets\`
//! 下，也支持任意路径导入导出。序列化本身在 [`namecat_core::presets`]，
//! 这里只做「Python 对象 ↔ core 结构」的翻译。
//!
//! `scan_options()` / `plan_options()` 刻意**不改写**那两个类：它们仍是
//! `namecat_py.scan.ScanOptions` / `namecat_py.plan.PlanOptions`，按
//! `__dataclass_fields__` 过滤后原样构造（`engine/presets.py` 就是这么写的）。

use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyList, PyType};
use serde_json::Value;

use namecat_core::presets as core;

use crate::pyjson;
use crate::shim::appdata_dir;

/// 预设目录：`%APPDATA%\NameCat\presets`。
#[pyfunction]
fn preset_dir(py: Python<'_>) -> PyResult<String> {
    appdata_dir(py, &["presets"])
}

#[pyclass(module = "namecat_py.presets", name = "Preset", dict)]
pub struct PyPreset {
    #[pyo3(get, set)]
    pub name: String,
    #[pyo3(get, set)]
    pub rules: Py<PyList>,
    #[pyo3(get, set)]
    pub scan: Py<PyDict>,
    #[pyo3(get, set)]
    pub plan: Py<PyDict>,
    #[pyo3(get, set)]
    pub note: String,
}

impl PyPreset {
    fn from_value(py: Python<'_>, data: &Value) -> PyResult<Py<PyPreset>> {
        let dict = pyjson::to_py(py, data)?;
        build_preset(py, dict.bind(py))
    }
}

/// `Preset.from_dict` 的实际实现（classmethod 的签名要带 cls，单独抽出来复用）。
fn build_preset(py: Python<'_>, data: &Bound<'_, PyAny>) -> PyResult<Py<PyPreset>> {
    let map = match data.cast::<PyDict>() {
        Ok(dict) => dict.clone(),
        Err(_) => {
            // 顶层不是字典：一切按默认（与 `_as_dict` 的兜底同口径）
            return Py::new(py, PyPreset::new(py, "", None, None, None, "")?);
        }
    };
    let text = |key: &str| -> String {
        match map.get_item(key) {
            Ok(Some(value)) if !value.is_none() => value
                .str()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_default(),
            _ => String::new(),
        }
    };
    let name = text("name");
    let name = if name.is_empty() {
        core::UNNAMED.to_string()
    } else {
        name
    };
    let note = text("note");
    let rules_api = py.import("namecat_py.rules")?;
    let rules_json = rules_api.getattr("rules_from_json")?;
    let rules = match map.get_item("rules") {
        Ok(Some(value)) if !value.is_none() => rules_json.call1((value,))?,
        _ => rules_json.call1((PyList::empty(py),))?,
    };
    let scan = dict_field(py, &map, "scan")?;
    let plan = dict_field(py, &map, "plan")?;
    Py::new(
        py,
        PyPreset {
            name,
            rules: rules.cast_into::<PyList>()?.unbind(),
            scan,
            plan,
            note,
        },
    )
}

#[pymethods]
impl PyPreset {
    #[new]
    #[pyo3(signature = (name = "未命名预设", rules = None, scan = None, plan = None, note = ""))]
    fn new(
        py: Python<'_>,
        name: &str,
        rules: Option<&Bound<'_, PyAny>>,
        scan: Option<&Bound<'_, PyAny>>,
        plan: Option<&Bound<'_, PyAny>>,
        note: &str,
    ) -> PyResult<Self> {
        let rule_list = match rules {
            Some(value) if !value.is_none() => value.clone().cast_into::<PyList>()?.unbind(),
            _ => PyList::empty(py).unbind(),
        };
        let scan_dict = match scan {
            Some(value) if !value.is_none() => value.clone().cast_into::<PyDict>()?.unbind(),
            _ => PyDict::new(py).unbind(),
        };
        let plan_dict = match plan {
            Some(value) if !value.is_none() => value.clone().cast_into::<PyDict>()?.unbind(),
            _ => PyDict::new(py).unbind(),
        };
        Ok(Self {
            name: if name.is_empty() {
                core::UNNAMED.to_string()
            } else {
                name.to_string()
            },
            rules: rule_list,
            scan: scan_dict,
            plan: plan_dict,
            note: note.to_string(),
        })
    }

    fn to_dict(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let rules_api = py.import("namecat_py.rules")?;
        let rules_json = rules_api
            .getattr("rules_to_json")?
            .call1((self.rules.bind(py),))?;
        let out = PyDict::new(py);
        out.set_item("app", namecat_core::consts::APP_NAME)?;
        out.set_item("version", namecat_core::APP_VER)?;
        out.set_item("name", &self.name)?;
        out.set_item("note", &self.note)?;
        out.set_item("rules", rules_json)?;
        out.set_item("scan", self.scan.bind(py).copy()?)?;
        out.set_item("plan", self.plan.bind(py).copy()?)?;
        Ok(out.unbind())
    }

    #[classmethod]
    fn from_dict(
        cls: &Bound<'_, PyType>,
        py: Python<'_>,
        data: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyPreset>> {
        let _ = cls;
        build_preset(py, data)
    }

    /// 按 `ScanOptions` 的字段名过滤后原样构造（`**over` 优先）。
    #[pyo3(signature = (**over))]
    fn scan_options(
        &self,
        py: Python<'_>,
        over: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Py<PyAny>> {
        build_options(py, self.scan.bind(py), over, "namecat_py.scan", "ScanOptions")
    }

    #[pyo3(signature = (**over))]
    fn plan_options(
        &self,
        py: Python<'_>,
        over: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Py<PyAny>> {
        build_options(py, self.plan.bind(py), over, "namecat_py.plan", "PlanOptions")
    }

    fn __repr__(&self, py: Python<'_>) -> String {
        format!(
            "Preset(name={:?}, rules={}, plan={:?})",
            self.name,
            self.rules.bind(py).len(),
            self.plan.bind(py)
        )
    }
}

/// `crate::Error` → Python 异常：IO 归 OSError，JSON 坏归 ValueError（`json` 模块的口径）。
fn preset_err(err: namecat_core::Error) -> PyErr {
    match err {
        namecat_core::Error::Json(text) => pyo3::exceptions::PyValueError::new_err(text),
        other => crate::shim::into_pyerr(other),
    }
}

fn dict_field(py: Python<'_>, map: &Bound<'_, PyDict>, key: &str) -> PyResult<Py<PyDict>> {
    let out = PyDict::new(py);
    if let Ok(Some(found)) = map.get_item(key) {
        if let Ok(dict) = found.cast::<PyDict>() {
            for (k, v) in dict.iter() {
                out.set_item(k, v)?;
            }
        }
    }
    Ok(out.unbind())
}

fn build_options(
    py: Python<'_>,
    base: &Bound<'_, PyDict>,
    over: Option<&Bound<'_, PyDict>>,
    module: &str,
    class: &str,
) -> PyResult<Py<PyAny>> {
    let data = base.copy()?;
    if let Some(over) = over {
        for (key, value) in over.iter() {
            data.set_item(key, value)?;
        }
    }
    let cls = py.import(module)?.getattr(class)?;
    let fields = cls
        .getattr("__dataclass_fields__")
        .ok()
        .and_then(|fields| fields.cast_into::<PyDict>().ok());
    let kwargs = PyDict::new(py);
    for (key, value) in data.iter() {
        let key_text = key.str()?.to_string_lossy().into_owned();
        let allowed = match &fields {
            Some(fields) => fields.contains(&key_text)?,
            None => true,
        };
        if allowed {
            kwargs.set_item(key_text, value)?;
        }
    }
    Ok(cls.call((), Some(&kwargs))?.unbind())
}

// --------------------------------------------------------------------------- //
// 读写
// --------------------------------------------------------------------------- //

/// 保存预设（`path` 省略时存进预设目录）。
#[pyfunction]
#[pyo3(signature = (preset, path = None))]
fn save(py: Python<'_>, preset: &Bound<'_, PyAny>, path: Option<&str>) -> PyResult<String> {
    let value = pyjson::to_value(preset.call_method0("to_dict")?.as_any());
    let dir = preset_dir(py)?;
    core::save_value(&value, path.unwrap_or(""), &dir).map_err(preset_err)
}

#[pyfunction]
fn load(py: Python<'_>, path: &str) -> PyResult<Py<PyPreset>> {
    let value = core::load_value(path).map_err(preset_err)?;
    PyPreset::from_value(py, &value)
}

#[pyfunction]
fn list_presets(py: Python<'_>) -> PyResult<Vec<String>> {
    Ok(core::list_presets(&preset_dir(py)?))
}

#[pyfunction]
fn delete(py: Python<'_>, name_or_path: &str) -> PyResult<bool> {
    Ok(core::delete(name_or_path, &preset_dir(py)?))
}

#[pyfunction]
fn builtin_list() -> Vec<&'static str> {
    core::builtin_list()
}

fn builtin_presets(py: Python<'_>) -> PyResult<Vec<(String, Py<PyPreset>)>> {
    let mut out = Vec::new();
    for (key, preset) in core::builtin() {
        let value = preset.to_dict();
        out.push((key.to_string(), PyPreset::from_value(py, &value)?));
    }
    Ok(out)
}

pub fn attach(py: Python<'_>, parent: &Bound<'_, pyo3::types::PyModule>) -> PyResult<()> {
    let m = crate::shim::new_submodule(py, parent, "presets")?;
    m.add(
        "__doc__",
        "规则预设（Rust 实现）：预设目录 + 可分享的单文件预设。",
    )?;
    m.add("APP_NAME", namecat_core::consts::APP_NAME)?;
    m.add("APP_VER", namecat_core::APP_VER)?;
    m.add_class::<PyPreset>()?;

    let builtin = PyDict::new(py);
    for (key, preset) in builtin_presets(py)? {
        builtin.set_item(key, preset)?;
    }
    m.add("BUILTIN", builtin)?;

    m.add_function(wrap_pyfunction!(preset_dir, &m)?)?;
    m.add_function(wrap_pyfunction!(save, &m)?)?;
    m.add_function(wrap_pyfunction!(load, &m)?)?;
    m.add_function(wrap_pyfunction!(list_presets, &m)?)?;
    m.add_function(wrap_pyfunction!(delete, &m)?)?;
    m.add_function(wrap_pyfunction!(builtin_list, &m)?)?;

    crate::shim::set_all(
        &m,
        &[
            "APP_NAME",
            "APP_VER",
            "BUILTIN",
            "Preset",
            "builtin_list",
            "delete",
            "list_presets",
            "load",
            "preset_dir",
            "save",
        ],
    )
}
