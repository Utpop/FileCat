//! Python 对象 ↔ `serde_json::Value` 的互转。
//!
//! `Rule.extras` 是"任意 JSON"（预设里可能出现数字、布尔、嵌套字典），
//! 而 Python 侧的预设文件、撤销日志都用 JSON 存盘。为了往返不丢类型
//! （`false` 不能变成字符串 "False"），核心用 `serde_json::Value` 承载，
//! 边界处做这层转换。

use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use serde_json::{Map, Number, Value};

/// Python 对象 → JSON 值。无法表示的对象转成它的 `str()`（与 `json.dumps`
/// 的 default 行为不同，但 extras 里不该出现这种值）。
pub fn to_value(obj: &Bound<'_, PyAny>) -> Value {
    if obj.is_none() {
        return Value::Null;
    }
    if let Ok(b) = obj.cast::<PyBool>() {
        return Value::Bool(b.is_true());
    }
    if let Ok(i) = obj.cast::<PyInt>() {
        if let Ok(v) = i.extract::<i64>() {
            return Value::Number(Number::from(v));
        }
        if let Ok(s) = i.str() {
            return Value::String(s.to_string_lossy().into_owned());
        }
    }
    if let Ok(f) = obj.cast::<PyFloat>() {
        if let Ok(v) = f.extract::<f64>() {
            if let Some(n) = Number::from_f64(v) {
                return Value::Number(n);
            }
        }
    }
    if let Ok(s) = obj.cast::<PyString>() {
        return Value::String(s.to_string_lossy().into_owned());
    }
    if let Ok(list) = obj.cast::<PyList>() {
        return Value::Array(list.iter().map(|item| to_value(&item)).collect());
    }
    if let Ok(tuple) = obj.cast::<PyTuple>() {
        return Value::Array(tuple.iter().map(|item| to_value(&item)).collect());
    }
    if let Ok(dict) = obj.cast::<PyDict>() {
        let mut map = Map::new();
        for (k, v) in dict.iter() {
            let key = k
                .str()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_default();
            map.insert(key, to_value(&v));
        }
        return Value::Object(map);
    }
    match obj.str() {
        Ok(s) => Value::String(s.to_string_lossy().into_owned()),
        Err(_) => Value::Null,
    }
}

/// JSON 值 → Python 对象。
pub fn to_py(py: Python<'_>, value: &Value) -> PyResult<Py<PyAny>> {
    Ok(match value {
        Value::Null => py.None(),
        Value::Bool(b) => PyBool::new(py, *b).to_owned().into_any().unbind(),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i.into_pyobject(py)?.into_any().unbind()
            } else if let Some(f) = n.as_f64() {
                PyFloat::new(py, f).into_any().unbind()
            } else {
                py.None()
            }
        }
        Value::String(s) => PyString::new(py, s).into_any().unbind(),
        Value::Array(items) => {
            let list = PyList::empty(py);
            for item in items {
                list.append(to_py(py, item)?)?;
            }
            list.into_any().unbind()
        }
        Value::Object(map) => {
            let dict = PyDict::new(py);
            for (k, v) in map {
                dict.set_item(k, to_py(py, v)?)?;
            }
            dict.into_any().unbind()
        }
    })
}

/// Python 字典 → `serde_json::Map`；非字典一律当空表（脏 JSON 兜底）。
pub fn dict_to_map(obj: Option<&Bound<'_, PyAny>>) -> Map<String, Value> {
    match obj {
        Some(o) if !o.is_none() => match o.cast::<PyDict>() {
            Ok(dict) => {
                let mut map = Map::new();
                for (k, v) in dict.iter() {
                    let key = k
                        .str()
                        .map(|s| s.to_string_lossy().into_owned())
                        .unwrap_or_default();
                    map.insert(key, to_value(&v));
                }
                map
            }
            Err(_) => Map::new(),
        },
        _ => Map::new(),
    }
}

/// `serde_json::Map` → 新的 Python 字典。
pub fn map_to_dict<'py>(py: Python<'py>, map: &Map<String, Value>) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    for (k, v) in map {
        dict.set_item(k, to_py(py, v)?)?;
    }
    Ok(dict)
}
