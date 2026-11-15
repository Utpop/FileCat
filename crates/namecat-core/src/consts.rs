//! 引擎级常量与状态码。
//!
//! 逐条对齐 `engine/__init__.py`：数值/文案都不许自作主张改动，
//! Python 壳与 Rust 侧读到的必须是同一套值（`verify/rust_check.py` 会比对）。

/// 应用名（界面标题、日志文件名前缀都用它）。
pub const APP_NAME: &str = "FileCat";

/// 旧版撤销日志目录名（历史遗留，只读兼容用）。
pub const LOG_DIR_NAME: &str = "_rename_logs";

/// 撤销日志统一存放的文件夹名（放在**程序目录**下，与目标目录无关）。
pub const BACKUP_DIR_NAME: &str = "Name_backup";

/// 撤销日志目录的环境变量覆盖（自检靠它把日志写进沙箱）。
pub const LOG_DIR_ENV: &str = "NAMECAT_LOG_DIR";

/// 单个目录保留的日志份数。
pub const MAX_LOGS: i64 = 20;

/// 目标目录很多时的总量兜底。
pub const MAX_LOGS_TOTAL: i64 = 200;

/// 两阶段改名的临时前缀（`__nk_tmp_`）。
pub const TEMP_PREFIX: &str = "__nk_tmp_";

/// 用户数据目录名：`%APPDATA%\<DATA_DIR_NAME>`。
pub const DATA_DIR_NAME: &str = "FileCat";

/// 旧版数据目录名，首次访问时把其中缺失的文件补到新目录。
pub const LEGACY_DATA_DIRS: &[&str] = &["NameCat", "NameKit"];

// 状态码：沿用 BatchRenamer 的 5 种，界面与日志共用一套文案
pub const ST_OK: &str = "ok";
pub const ST_SAME: &str = "same";
pub const ST_CONFLICT: &str = "conflict";
pub const ST_ERROR: &str = "error";
pub const ST_SKIP: &str = "skip";

/// 状态码 → 中文文案。顺序与 `engine/__init__.py` 的 `STATUS_TEXT` 一致。
pub const STATUS_TEXT: &[(&str, &str)] = &[
    (ST_OK, "待重命名"),
    (ST_SAME, "名称未变"),
    (ST_CONFLICT, "冲突"),
    (ST_ERROR, "无效"),
    (ST_SKIP, "已跳过"),
];

/// 5 = 增加 exec 段（执行阶段真实成败；4 及更早只有 summary）。
pub const LOG_SCHEMA: i64 = 5;

/// 撤销日志格式版本：3 = BatchRenamer 原格式（可读旧日志）。
pub const UNDO_SCHEMA: i64 = 3;

/// 按状态码取文案；未知状态码返回空串（与 Python `STATUS_TEXT.get(k, "")` 等价）。
pub fn status_text(code: &str) -> &'static str {
    match STATUS_TEXT.iter().find(|(k, _)| *k == code) {
        Some((_, v)) => v,
        None => "",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_codes_match_python_values() {
        assert_eq!(
            [ST_OK, ST_SAME, ST_CONFLICT, ST_ERROR, ST_SKIP],
            ["ok", "same", "conflict", "error", "skip"]
        );
        assert_eq!(status_text("conflict"), "冲突");
        assert_eq!(status_text("nope"), "");
    }

    #[test]
    fn schema_versions_are_frozen() {
        // 这两个数字变了等于磁盘格式变更，必须同步 design 文档与迁移说明
        assert_eq!(LOG_SCHEMA, 5);
        assert_eq!(UNDO_SCHEMA, 3);
    }
}
