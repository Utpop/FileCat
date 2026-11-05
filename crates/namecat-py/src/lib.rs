//! `namecat_py` —— NameCat 引擎的 Python 扩展（PyO3）。
//!
//! 结构（对应 `docs/DESIGN_RUST_ENGINE.md` §2.1 / §4.1）：
//!
//! ```text
//! namecat_py            ← 根：版本、状态码、常量（对齐 engine/__init__.py）
//!   ├─ rules            ← engine/rules.py      （P1）
//!   ├─ template         ← engine/template.py   （P1）
//!   ├─ zhconv           ← engine/zhconv.py     （P1）
//!   ├─ scan             ← engine/scan.py       （P2）
//!   ├─ plan             ← engine/plan.py       （P2）
//!   ├─ context          ← engine/context.py    （P2）
//!   ├─ common           ← engine/common.py     （P3）
//!   ├─ ops              ← engine/ops.py        （P3）
//!   ├─ llm              ← engine/llm.py        （P4）
//!   ├─ provider         ← engine/provider.py  （P4）
//!   ├─ secrets          ← engine/secrets.py   （P4）
//!   └─ presets          ← engine/presets.py   （P4）
//! ```
//!
//! 子模块名**刻意**与 Python 模块一一对应，这样 `engine/rules.py` 退化成
//! `from namecat_py.rules import *` 后，`adapters/engine_adapter.py` 与 `ui/`
//! 一行都不用改。
//!
//! 12 个模块现在全是真实实现（P4 收尾），`engine/*.py` 只剩一行 re-export。
//! 换壳进度表在 `namecat_core::RUST_MODULES`，符号闸门见 `verify/rust_check.py`。

mod py_common;
mod py_context;
mod py_llm;
mod py_net;
mod py_presets;
mod py_provider;
mod py_rules;
mod py_ops;
mod py_plan;
mod py_scan;
mod py_secrets;
mod py_template;
mod py_zhconv;
mod pyctx;
mod pyjson;
mod shim;

use namecat_core::consts;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyModule};

/// 一个子模块的挂载函数签名。
type Attach = fn(Python<'_>, &Bound<'_, PyModule>) -> PyResult<()>;

/// 子模块清单：(模块名, 对应 Python 模块, docstring, 挂载函数)。
///
/// `None` = 还没迁移（空壳，行为仍来自 Python 实现）；换成 `Some(f)` 就是换壳。
/// 顺序与设计稿的迁移阶段一致，方便对着看进度。
const SUBMODULES: &[(&str, &str, &str, Option<Attach>)] = &[
    ("rules", "engine/rules.py", "规则链：make_rule / normalize / 序列化 / 描述", Some(py_rules::attach)),
    ("template", "engine/template.py", "模板渲染与占位符选择器", Some(py_template::attach)),
    ("zhconv", "engine/zhconv.py", "简繁转换映射表", Some(py_zhconv::attach)),
    ("scan", "engine/scan.py", "目录扫描、排序、类型判定", Some(py_scan::attach)),
    ("plan", "engine/plan.py", "改名计划：新旧名计算与冲突检测", Some(py_plan::attach)),
    ("context", "engine/context.py", "NameContext / Item 数据结构", Some(py_context::attach)),
    ("common", "engine/common.py", "两阶段改名、撤销日志、残留清理", Some(py_common::attach)),
    ("ops", "engine/ops.py", "预览 / 执行 / 导出 CSV", Some(py_ops::attach)),
    // secrets 必须排在 provider 之前：provider 挂载时要把 `namecat_py.secrets`
    // 这个**模块对象**挂成属性（`provider.secrets.has_secret()`），此时它得已经在
    // sys.modules 里 —— 否则 import 会去找一个叫 namecat_py 的包，直接报
    // "No module named 'namecat_py.secrets'; 'namecat_py' is not a package"。
    ("secrets", "engine/secrets.py", "密钥存储（Windows DPAPI）", Some(py_secrets::attach)),
    ("llm", "engine/llm.py", "LLM 客户端与建议解析", Some(py_llm::attach)),
    ("provider", "engine/provider.py", "供应商配置与连通性检测", Some(py_provider::attach)),
    ("presets", "engine/presets.py", "预设的读写与内置清单", Some(py_presets::attach)),
];

/// 构建信息：给自检用，不承诺语义。
#[pyfunction]
fn _hello(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let info = namecat_core::build_info();
    let d = PyDict::new(py);
    d.set_item("app", info.app_name)?;
    d.set_item("version", info.app_ver)?;
    d.set_item("profile", info.profile)?;
    d.set_item("rust_modules", info.modules)?;
    Ok(d)
}

/// 模块入口。
#[pymodule]
fn namecat_py(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();

    m.add(
        "__doc__",
        "NameCat 引擎（Rust 实现，PyO3 绑定）。子模块名与 engine/*.py 一一对应。",
    )?;
    m.add("__version__", namecat_core::APP_VER)?;

    // 自定义异常：panic 越过边界时的兜底类型（正常路径用 ValueError/OSError）
    m.add("EngineError", py.get_type::<shim::EngineError>())?;
    m.add("HttpError", py.get_type::<py_net::HttpError>())?;

    // 常量：与 engine/__init__.py 逐条对齐（verify/rust_check.py 会比对）
    m.add("APP_NAME", consts::APP_NAME)?;
    m.add("APP_VER", namecat_core::APP_VER)?;
    m.add("LOG_DIR_NAME", consts::LOG_DIR_NAME)?;
    m.add("BACKUP_DIR_NAME", consts::BACKUP_DIR_NAME)?;
    m.add("LOG_DIR_ENV", consts::LOG_DIR_ENV)?;
    m.add("MAX_LOGS", consts::MAX_LOGS)?;
    m.add("MAX_LOGS_TOTAL", consts::MAX_LOGS_TOTAL)?;
    m.add("TEMP_PREFIX", consts::TEMP_PREFIX)?;
    m.add("DATA_DIR_NAME", consts::DATA_DIR_NAME)?;
    m.add("LEGACY_DATA_DIRS", consts::LEGACY_DATA_DIRS)?;
    m.add("ST_OK", consts::ST_OK)?;
    m.add("ST_SAME", consts::ST_SAME)?;
    m.add("ST_CONFLICT", consts::ST_CONFLICT)?;
    m.add("ST_ERROR", consts::ST_ERROR)?;
    m.add("ST_SKIP", consts::ST_SKIP)?;
    m.add("LOG_SCHEMA", consts::LOG_SCHEMA)?;
    m.add("UNDO_SCHEMA", consts::UNDO_SCHEMA)?;

    let status = PyDict::new(py);
    for (code, text) in consts::STATUS_TEXT {
        status.set_item(*code, *text)?;
    }
    m.add("STATUS_TEXT", status)?;

    m.add_function(wrap_pyfunction!(_hello, m)?)?;

    // 子模块：已迁移的走各自 attach，未迁移的先挂空壳
    for (name, source, doc, attach) in SUBMODULES {
        match attach {
            Some(f) => f(py, m)?,
            None => attach_empty(py, m, name, source, doc)?,
        }
    }

    Ok(())
}

/// 建一个"还没迁过来"的空子模块。
///
/// 刻意保留 `__doc__` 里的来源说明：`python -c "import namecat_py.rules as r;
/// print(r.__doc__)"` 能直接看出它对应哪个 Python 文件、迁没迁。
fn attach_empty(
    py: Python<'_>,
    parent: &Bound<'_, PyModule>,
    name: &str,
    source: &str,
    doc: &str,
) -> PyResult<()> {
    let module = shim::new_submodule(py, parent, name)?;
    module.add(
        "__doc__",
        format!("{doc}（对应 {source}）。P0 阶段尚未迁移：本模块为空壳，行为仍来自 Python 实现。"),
    )?;
    // 空壳的 __all__ 必须是空表：PyO3 默认只放 __doc__，那会让
    // `from namecat_py.rules import *` 导入一个叫 __doc__ 的怪东西
    shim::set_all(&module, &[])?;
    Ok(())
}
