//! NameCat 引擎核心。
//!
//! 设计约束（见 `docs/DESIGN_RUST_ENGINE.md` §2.1）：
//! · 本 crate **不依赖 PyO3**，可以 `cargo test` 独立跑；
//! · 行为对齐目标是 `engine/*.py` 的**现有行为**，不是"更正确"的行为；
//! · 内部一律 `Result<T, Error>`（thiserror 枚举），不 panic；
//! · 路径面用 `PathBuf`/`OsString`，只有规则计算的文本面才转 `String`。

pub mod consts;
pub mod context;
pub mod error;
pub mod ctx;
pub mod http;
pub mod llm;
pub mod logs;
pub mod names;
pub mod ops;
pub mod pathops;
pub mod plan;
pub mod presets;
pub mod provider;
pub mod rename;
pub mod rules;
pub mod scan;
pub mod secrets;
pub mod template;
pub mod text;
pub mod timefmt;
pub mod validate;
pub mod zhconv;

pub use error::{Error, ErrorKind, Result};

/// 引擎版本号：唯一来源是 workspace 的 `Cargo.toml`。
///
/// `rust_build.py` 会校验它与 `engine/__init__.py` 的 `APP_VER` 一致，
/// 因此 `namecat_py.APP_VER` 永远不会和界面显示的版本对不上。
pub const APP_VER: &str = env!("CARGO_PKG_VERSION");

/// 构建信息，供 `namecat_py._hello()` 自检用（不对外承诺任何语义）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BuildInfo {
    pub app_name: &'static str,
    pub app_ver: &'static str,
    pub profile: &'static str,
    pub modules: &'static [&'static str],
}

/// 已完成 Rust 化的子模块名（strangler 进度表，随阶段推进增删）。
pub const RUST_MODULES: &[&str] = &[
    "common", "context", "llm", "ops", "plan", "presets", "provider", "rules", "scan", "secrets",
    "template", "zhconv",
];

pub fn build_info() -> BuildInfo {
    BuildInfo {
        app_name: consts::APP_NAME,
        app_ver: APP_VER,
        profile: if cfg!(debug_assertions) { "debug" } else { "release" },
        modules: RUST_MODULES,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 版本号是 x.y.z 三段数字（打包脚本按这个格式比对 APP_VER）。
    #[test]
    fn app_ver_is_semver_triplet() {
        let parts: Vec<&str> = APP_VER.split('.').collect();
        assert_eq!(parts.len(), 3, "APP_VER 应是 x.y.z：{APP_VER}");
        assert!(parts.iter().all(|p| p.parse::<u32>().is_ok()));
    }

    #[test]
    fn build_info_carries_app_name() {
        let info = build_info();
        assert_eq!(info.app_name, "NameCat");
        // 进度表里只放已完成换壳的模块名，且不能有重复
        let mut names = info.modules.to_vec();
        names.sort_unstable();
        let before = names.len();
        names.dedup();
        assert_eq!(before, names.len(), "RUST_MODULES 里有重复项");
        assert!(info.modules.iter().all(|m| !m.is_empty()));
    }
}
