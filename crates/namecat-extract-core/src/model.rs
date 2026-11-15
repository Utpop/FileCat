//! 公开数据结构。Python 绑定层只做 JSON 互转，不在这里引入任何 Python 类型。

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

/// 引擎日志 Schema 版本。结构变化时递增；旧版本仍需可读。
pub const SCHEMA: u32 = 1;
pub const INTERNAL_DIR_NAME: &str = "Extract";
pub const ROLLBACK_DIR_NAME: &str = "Rollback_Log";
pub const BACKUP_SUBDIR: &str = "backups";

/// 搬运方式。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum ExtractMode {
    /// 一次到底：所有符合条件的文件直接进目标根目录。
    #[default]
    Flatten,
    /// 逐级上移：每个文件只向直接父目录移动一级（基于扫描时的快照）。
    OneLevel,
}

impl ExtractMode {
    pub fn from_str(value: &str) -> Self {
        match value.trim().to_ascii_lowercase().as_str() {
            "one_level" | "onelevel" | "level" | "逐级上移" => Self::OneLevel,
            _ => Self::Flatten,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Flatten => "flatten",
            Self::OneLevel => "one_level",
        }
    }
}

/// 同名冲突策略。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum ConflictPolicy {
    #[default]
    Rename,
    Skip,
    Overwrite,
}

impl ConflictPolicy {
    pub fn from_str(value: &str) -> Self {
        match value.trim().to_ascii_lowercase().as_str() {
            "skip" | "跳过" => Self::Skip,
            "overwrite" | "覆盖" => Self::Overwrite,
            _ => Self::Rename,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Rename => "rename",
            Self::Skip => "skip",
            Self::Overwrite => "overwrite",
        }
    }

    pub fn is_dangerous(self) -> bool {
        self == Self::Overwrite
    }
}

/// 文件在计划/执行中的状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum FileStatus {
    #[default]
    Pending,
    Ready,
    Renamed,
    Conflict,
    Skip,
    Error,
    Done,
    Stopped,
    DryRun,
}

impl FileStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Ready => "ready",
            Self::Renamed => "renamed",
            Self::Conflict => "conflict",
            Self::Skip => "skip",
            Self::Error => "error",
            Self::Done => "done",
            Self::Stopped => "stopped",
            Self::DryRun => "dry_run",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Pending => "待处理",
            Self::Ready => "可执行",
            Self::Renamed => "已改名",
            Self::Conflict => "冲突",
            Self::Skip => "跳过",
            Self::Error => "失败",
            Self::Done => "完成",
            Self::Stopped => "已停止",
            Self::DryRun => "模拟",
        }
    }
}

/// 扫描/执行选项。`max_depth = 0` 表示不限深度。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExtractOptions {
    /// 目标根目录（也是扫描根目录）。
    pub root: String,
    /// 程序根目录：固定 `Extract/Rollback_Log` 的位置。源码运行 = 项目根，EXE = exe 所在目录。
    pub program_root: String,
    pub mode: ExtractMode,
    pub conflict: ConflictPolicy,
    pub max_depth: usize,
    /// 逗号分隔的排除目录名（忽略大小写与首尾空格）。
    pub exclude_dirs: Vec<String>,
    /// 逗号分隔的排除扩展名（可带点，忽略大小写与首尾空格）。
    pub exclude_exts: Vec<String>,
    pub clean_empty_dirs: bool,
    /// 覆盖策略的用户确认（绑定层必须在真正执行前校验）。
    pub overwrite_confirmed: bool,
}

impl Default for ExtractOptions {
    fn default() -> Self {
        Self {
            root: String::new(),
            program_root: String::new(),
            mode: ExtractMode::Flatten,
            conflict: ConflictPolicy::Rename,
            max_depth: 0,
            exclude_dirs: Vec::new(),
            exclude_exts: Vec::new(),
            clean_empty_dirs: false,
            overwrite_confirmed: false,
        }
    }
}

impl ExtractOptions {
    /// 解析逗号分隔列表：`" .TMP, tmp ;bak"` → `[".tmp", "bak"]`。
    pub fn parse_list(text: &str, dotted: bool) -> Vec<String> {
        let mut out = Vec::new();
        for part in text.replace(';', ",").split(',') {
            let mut s = part.trim().to_ascii_lowercase();
            if s.is_empty() {
                continue;
            }
            if dotted && !s.starts_with('.') {
                s.insert(0, '.');
            }
            if !out.contains(&s) {
                out.push(s);
            }
        }
        out
    }

    pub fn normalized_exclude_dirs(&self) -> Vec<String> {
        self.exclude_dirs
            .iter()
            .map(|s| s.trim().to_ascii_lowercase())
            .filter(|s| !s.is_empty())
            .collect()
    }

    pub fn normalized_exclude_exts(&self) -> Vec<String> {
        self.exclude_exts
            .iter()
            .map(|s| {
                let s = s.trim().to_ascii_lowercase();
                if s.starts_with('.') { s } else { format!(".{s}") }
            })
            .filter(|s| s.len() > 1)
            .collect()
    }
}

/// 扫描得到的一个文件条目。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScannedFile {
    /// 绝对路径。
    pub source: PathBuf,
    pub name: String,
    /// 来源目录（绝对路径）。
    pub source_dir: PathBuf,
    /// 相对目标根目录的深度：根目录下为 1。
    pub depth: usize,
    pub size: u64,
}

/// 计划项：预览表的一行，也是执行与 CSV 的唯一依据。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PlanItem {
    pub index: usize,
    pub name: String,
    pub source: PathBuf,
    pub source_dir: PathBuf,
    /// 预计/实际目标路径。
    pub target: PathBuf,
    /// 目标所在目录（分组用）。
    pub target_dir: PathBuf,
    pub size: u64,
    pub depth: usize,
    pub status: FileStatus,
    /// 状态说明 / 错误信息。
    pub message: String,
    /// 是否自动改名（保留双方）。
    pub renamed: bool,
    /// 是否发生覆盖（目标原文件已备份）。
    pub overwritten: bool,
    /// 覆盖前的备份路径（相对 `Rollback_Log`）。
    pub backup: Option<PathBuf>,
}

impl PlanItem {
    pub fn status_text(&self) -> &'static str {
        self.status.label()
    }
}

/// 一次扫描的完整计划。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct ExtractPlan {
    pub root: PathBuf,
    pub mode: ExtractMode,
    pub conflict: ConflictPolicy,
    pub max_depth: usize,
    pub items: Vec<PlanItem>,
    /// 扫描时发现的候选空目录（绝对路径，最深优先）；执行清理时重新验证。
    pub empty_dir_candidates: Vec<PathBuf>,
    pub scanned_count: usize,
    pub excluded_count: usize,
    pub error_count: usize,
}

impl ExtractPlan {
    pub fn stats(&self) -> PlanStats {
        let mut s = PlanStats::default();
        for it in &self.items {
            match it.status {
                FileStatus::Pending | FileStatus::Ready | FileStatus::DryRun => s.pending += 1,
                FileStatus::Done => s.done += 1,
                FileStatus::Skip | FileStatus::Stopped => s.skipped += 1,
                FileStatus::Conflict => s.conflict += 1,
                FileStatus::Error => s.error += 1,
                FileStatus::Renamed => s.renamed += 1,
            }
        }
        s.total = self.items.len();
        s
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct PlanStats {
    pub total: usize,
    pub pending: usize,
    pub done: usize,
    pub skipped: usize,
    pub conflict: usize,
    pub error: usize,
    pub renamed: usize,
}

/// 单次执行结果。
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExecResult {
    pub dry_run: bool,
    pub started: usize,
    pub completed: usize,
    pub succeeded: usize,
    pub failed: usize,
    pub skipped: usize,
    pub stopped: bool,
    pub cleaned_dirs: usize,
    /// 本次操作唯一 ID；模拟运行为空。
    pub operation_id: String,
    /// 撤销日志绝对路径；模拟运行为空。
    pub log_path: Option<PathBuf>,
    pub items: Vec<PlanItem>,
    pub cleaned: Vec<PathBuf>,
    pub errors: Vec<String>,
}

/// 撤销日志中一条文件的记录。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RollbackEntry {
    /// 执行成功的项在 `items` 里的下标。
    pub index: usize,
    pub source: PathBuf,
    pub target: PathBuf,
    pub size: u64,
    pub status: FileStatus,
    pub message: String,
    pub renamed: bool,
    pub overwritten: bool,
    /// 覆盖前原目标文件的备份（相对日志目录）。
    pub backup: Option<PathBuf>,
    /// 执行时源文件的 mtime（Windows 100ns ticks，UTC），用于撤销前判断是否被改动。
    pub source_mtime: i64,
    /// 执行后目标文件的 mtime，用于检查目标是否被用户后续修改。
    pub target_mtime: i64,
}

/// 目录清理记录。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CleanedDir {
    pub path: PathBuf,
    /// 相对日志目录的备份（目录本身不备份，只记结构）。
    pub relative: String,
}

/// 撤销状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum RollbackState {
    #[default]
    Active,
    Partial,
    Undone,
}

/// 撤销日志文件（schema 版本化）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RollbackLog {
    pub schema: u32,
    pub app: String,
    pub operation_id: String,
    pub time: String,
    pub root: PathBuf,
    pub mode: ExtractMode,
    pub conflict: ConflictPolicy,
    pub dry_run: bool,
    pub result: String,
    pub state: RollbackState,
    pub entries: Vec<RollbackEntry>,
    pub cleaned_dirs: Vec<CleanedDir>,
    pub errors: Vec<String>,
}

/// 撤销结果。
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct UndoResult {
    pub operation_id: String,
    pub succeeded: usize,
    pub failed: usize,
    pub skipped: usize,
    pub restored_dirs: usize,
    pub errors: Vec<String>,
    /// 是否可以把日志标记为已撤销（无失败、无跳过时才允许）。
    pub complete: bool,
}

/// 进度回调接口：绑定层实现，跨线程由 `Arc` 共享。
pub trait Progress: Send + Sync {
    fn on_progress(&self, current: usize, total: usize, message: &str);
}

/// 空进度实现（测试与无回调场景）。
pub struct NoProgress;
impl Progress for NoProgress {
    fn on_progress(&self, _current: usize, _total: usize, _message: &str) {}
}

/// 停止标志：执行循环在**每个文件之间**检查，保证当前文件操作完整结束。
#[derive(Debug, Default)]
pub struct StopFlag(std::sync::atomic::AtomicBool);

impl StopFlag {
    pub fn new() -> Self {
        Self(std::sync::atomic::AtomicBool::new(false))
    }

    pub fn stop(&self) {
        self.0.store(true, std::sync::atomic::Ordering::SeqCst);
    }

    pub fn is_stopped(&self) -> bool {
        self.0.load(std::sync::atomic::Ordering::SeqCst)
    }

    pub fn reset(&self) {
        self.0.store(false, std::sync::atomic::Ordering::SeqCst);
    }
}

/// 引擎错误。
#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("IO 错误：{0}")]
    Io(#[from] std::io::Error),
    #[error("参数错误：{0}")]
    Invalid(&'static str),
    #[error("路径错误：{0}")]
    Path(String),
    #[error("日志错误：{0}")]
    Log(String),
    #[error("数据错误：{0}")]
    Json(String),
}

impl Error {
    pub fn io(msg: impl Into<String>) -> Self {
        Self::Io(std::io::Error::other(msg.into()))
    }
}

pub type Result<T> = std::result::Result<T, Error>;
