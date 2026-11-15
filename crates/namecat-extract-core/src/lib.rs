//! FileCat 文件批量提取核心引擎。
//!
//! 与原重命名引擎 `namecat-core` 完全独立：不依赖 PyO3 / Qt / GUI，
//! 不读写 `Name_backup`，所有撤销数据只落在
//! `<程序根目录>/Extract/Rollback_Log/`。
//!
//! 模块：
//! - [`model`]    公开数据结构（选项 / 计划项 / 结果 / 日志）
//! - [`fs`]       安全文件系统原语（长路径、重解析点、非覆盖移动、备份）
//! - [`scan`]     目录扫描（深度、排除、内部目录隔离、重解析点）
//! - [`plan`]     两种搬运方式的目标计算 + 冲突策略
//! - [`execute`]  模拟 / 实际执行 / 停止 / 目录清理 / 日志写入
//! - [`rollback`] 固定日志目录、原子写、撤销
//! - [`csv`]      UTF-8 BOM 清单导出

pub mod csv;
pub mod execute;
pub mod fs;
pub mod model;
pub mod plan;
pub mod rollback;
pub mod scan;

pub use model::{
    CleanedDir, ConflictPolicy, Error, ExecResult, ExtractMode, ExtractOptions, ExtractPlan,
    FileStatus, NoProgress, PlanItem, PlanStats, Progress, Result, RollbackEntry, RollbackLog,
    RollbackState, ScannedFile, StopFlag, UndoResult, BACKUP_SUBDIR, INTERNAL_DIR_NAME,
    ROLLBACK_DIR_NAME, SCHEMA,
};
