//! 任务执行：模拟运行 / 实际执行 / 停止 / 空目录清理。
//!
//! 硬规则：
//! - 模拟运行**完全不写文件系统**（连日志都不写）；
//! - 真实执行前先 `prepare_log` 成功，否则直接拒绝（防止不可逆操作无法撤销）；
//! - 每一项执行前重新校验源文件与目标位置，防止外部变化导致误覆盖；
//! - 覆盖前必须先备份成功，备份失败则该项失败、源文件保持原样；
//! - 停止标志在**文件之间**检查，当前文件操作一定完整结束；未处理项标 `Stopped`，
//!   绝不标成功；
//! - 单文件失败不中断整批；每 25 项写一个日志检查点，进程异常退出也能恢复。

use crate::fs::{file_exists, is_dir_empty, move_no_replace, mtime_of, remove_empty_dir, replace_with_backup};
use crate::model::{
    CleanedDir, Error, ExecResult, ExtractOptions, ExtractPlan, FileStatus, PlanItem, Progress,
    RollbackEntry, RollbackLog, StopFlag,
};
use crate::rollback;
use crate::scan::relative_text;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// 进度节流：大量小文件时 GUI 不能被每个文件刷一次。
/// 每 40ms 或每 25 项（取先到者）发一次，最后一项必发。
pub struct ThrottledProgress<'a> {
    inner: &'a dyn Progress,
    last: Mutex<Instant>,
    interval: Duration,
    stride: usize,
    total: usize,
    last_sent: Mutex<usize>,
}

impl<'a> ThrottledProgress<'a> {
    pub fn new(inner: &'a dyn Progress, total: usize) -> Self {
        Self {
            inner,
            last: Mutex::new(Instant::now() - Duration::from_secs(1)),
            interval: Duration::from_millis(40),
            stride: 25,
            total,
            last_sent: Mutex::new(0),
        }
    }

    fn emit(&self, current: usize, message: &str, force: bool) {
        let mut last = self.last.lock().unwrap_or_else(|e| e.into_inner());
        let mut sent = self.last_sent.lock().unwrap_or_else(|e| e.into_inner());
        let due_time = last.elapsed() >= self.interval;
        let due_count = current.saturating_sub(*sent) >= self.stride;
        let is_last = current >= self.total;
        // 第一项必发：让界面立刻看到进度，也让"停止"在最开始就能生效
        let is_first = current == 1;
        if force || is_last || is_first || due_time || due_count {
            self.inner.on_progress(current, self.total, message);
            *last = Instant::now();
            *sent = current;
        }
    }
}

/// 全局停止注册表：绑定层用 task_id 提交/停止任务。
/// Rust 侧持有 `Arc<StopFlag>`，Python 侧只存字符串 id，避免跨线程持有 GIL 对象。
static STOPS: std::sync::OnceLock<Mutex<HashMap<String, Arc<StopFlag>>>> = std::sync::OnceLock::new();

fn stops() -> &'static Mutex<HashMap<String, Arc<StopFlag>>> {
    STOPS.get_or_init(|| Mutex::new(HashMap::new()))
}

pub fn register_stop(task_id: &str) -> Arc<StopFlag> {
    let flag = Arc::new(StopFlag::new());
    let mut map = stops().lock().unwrap_or_else(|e| e.into_inner());
    // 同 id 重复提交视为新任务，覆盖旧标志
    map.insert(task_id.to_string(), flag.clone());
    flag
}

pub fn request_stop(task_id: &str) -> bool {
    let map = stops().lock().unwrap_or_else(|e| e.into_inner());
    match map.get(task_id) {
        Some(flag) => {
            flag.stop();
            true
        }
        None => false,
    }
}

pub fn clear_stop(task_id: &str) {
    let mut map = stops().lock().unwrap_or_else(|e| e.into_inner());
    map.remove(task_id);
}

/// 顶层入口。
///
/// `plan` 由 `build_plan` 生成（扫描快照），执行严格按它走，不重新扫描。
/// `dry_run = true` 时返回模拟结果，文件系统零写入。
pub fn execute(
    opts: &ExtractOptions,
    plan: &ExtractPlan,
    dry_run: bool,
    stop: &StopFlag,
    progress: &dyn Progress,
) -> crate::model::Result<ExecResult> {
    if opts.conflict.is_dangerous() && !opts.overwrite_confirmed {
        return Err(Error::Invalid("覆盖属于危险操作，必须先由用户确认"));
    }
    let total = plan.items.len();
    let throttle = ThrottledProgress::new(progress, total);
    throttle.emit(0, "开始执行", true);

    if dry_run {
        return simulate(plan, stop, &throttle);
    }
    real_execute(opts, plan, stop, &throttle)
}

/// 模拟运行：只改内存状态，绝不触碰文件系统。
fn simulate(
    plan: &ExtractPlan,
    stop: &StopFlag,
    throttle: &ThrottledProgress<'_>,
) -> crate::model::Result<ExecResult> {
    let mut result = ExecResult {
        dry_run: true,
        started: plan.items.len(),
        ..Default::default()
    };
    let mut items = Vec::with_capacity(plan.items.len());
    for (i, item) in plan.items.iter().enumerate() {
        if stop.is_stopped() {
            result.stopped = true;
            for rest in &plan.items[i..] {
                let mut r = rest.clone();
                r.status = FileStatus::Stopped;
                r.message = "任务已停止，未处理".into();
                items.push(r);
            }
            break;
        }
        let mut it = item.clone();
        match it.status {
            FileStatus::Skip => {
                it.message = if it.message.is_empty() {
                    "模拟：按策略跳过".into()
                } else {
                    format!("模拟：{}", it.message)
                };
                result.skipped += 1;
            }
            FileStatus::Error => {
                it.message = format!("模拟：{}", it.message);
                result.failed += 1;
            }
            _ => {
                it.status = FileStatus::DryRun;
                it.message = format!("模拟：将移动到 {}", it.target.display());
                result.succeeded += 1;
            }
        }
        result.completed += 1;
        throttle.emit(i + 1, &it.message, false);
        items.push(it);
    }
    throttle.emit(result.completed, "模拟运行完成（未修改任何文件）", true);
    result.items = items;
    Ok(result)
}

/// 真实执行。
fn real_execute(
    opts: &ExtractOptions,
    plan: &ExtractPlan,
    stop: &StopFlag,
    throttle: &ThrottledProgress<'_>,
) -> crate::model::Result<ExecResult> {
    // ---- 前置：日志目录与初始日志必须先写成功 ----
    let (log_dir, op_id, log_path) = rollback::prepare_log(opts)?;
    let mut log = RollbackLog {
        schema: crate::model::SCHEMA,
        app: "FileCat".into(),
        operation_id: op_id.clone(),
        time: chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string(),
        root: plan.root.clone(),
        mode: opts.mode,
        conflict: opts.conflict,
        dry_run: false,
        result: "running".into(),
        state: crate::model::RollbackState::Active,
        entries: Vec::new(),
        cleaned_dirs: Vec::new(),
        errors: Vec::new(),
    };

    let mut result = ExecResult {
        started: plan.items.len(),
        operation_id: op_id.clone(),
        log_path: Some(log_path.clone()),
        ..Default::default()
    };
    let mut items: Vec<PlanItem> = Vec::with_capacity(plan.items.len());
    let mut cleaned: Vec<CleanedDir> = Vec::new();
    let mut errors: Vec<String> = Vec::new();
    let mut stopped_at: Option<usize> = None;

    for (i, item) in plan.items.iter().enumerate() {
        // ---- 停止检查只在文件之间：当前项一定完整结束 ----
        if stop.is_stopped() {
            stopped_at = Some(i);
            break;
        }
        let mut it = item.clone();

        if matches!(it.status, FileStatus::Skip | FileStatus::Error) {
            // 计划里已判定跳过/无效，不动文件系统
            result.skipped += 1;
            result.completed += 1;
            throttle.emit(i + 1, &it.message, false);
            items.push(it);
            continue;
        }

        match execute_one(opts, &mut it, &log_dir, &op_id, i) {
            Ok(Some(entry)) => {
                // 成功：写检查点日志（失败不阻断执行，但记下错误）
                if let Err(e) = rollback::checkpoint(
                    &log_path, &mut log, entry, &cleaned, &errors, false,
                ) {
                    errors.push(format!("日志检查点写入失败：{e}"));
                }
                result.succeeded += 1;
            }
            Ok(None) => {
                // 执行前校验判定跳过
                result.skipped += 1;
            }
            Err(e) => {
                let msg = e.to_string();
                it.status = FileStatus::Error;
                it.message = msg.clone();
                result.failed += 1;
                errors.push(format!("[{}] {}", it.name, msg));
            }
        }
        result.completed += 1;
        throttle.emit(i + 1, &it.message, false);
        items.push(it);
    }

    // ---- 停止：未处理项标 Stopped，绝不算成功 ----
    if let Some(at) = stopped_at {
        result.stopped = true;
        for rest in &plan.items[at..] {
            let mut r = rest.clone();
            r.status = FileStatus::Stopped;
            r.message = "任务已停止，未处理".into();
            items.push(r);
        }
        errors.push("任务被用户停止；已完成项保留真实记录，未处理项未标记为成功".into());
    }

    // ---- 空目录清理：最深优先、删前重新验证、不删根、不跟随重解析点 ----
    if opts.clean_empty_dirs {
        for dir in &plan.empty_dir_candidates {
            if dir == &plan.root {
                continue; // 绝不删目标根目录
            }
            if stop.is_stopped() {
                break;
            }
            match is_dir_empty(dir) {
                Ok(true) => match remove_empty_dir(dir) {
                    Ok(()) => {
                        result.cleaned_dirs += 1;
                        result.cleaned.push(dir.clone());
                        cleaned.push(CleanedDir {
                            path: dir.clone(),
                            relative: relative_text(dir, &plan.root),
                        });
                    }
                    Err(e) => errors.push(format!("清理目录失败（保留）：{:?} {e}", dir)),
                },
                Ok(false) => { /* 非空或被排除：静默跳过，不算失败 */ }
                Err(e) => errors.push(format!("检查目录失败（保留）：{:?} {e}", dir)),
            }
        }
    }

    // ---- 最终日志：原子替换；失败必须明确告知（数据与备份仍在，可人工恢复） ----
    if let Err(e) = rollback::finalize_log(&log_path, &mut log, &result, &cleaned) {
        errors.push(format!("最终撤销日志写入失败：{e}（文件与备份仍保留）"));
    }
    result.items = items;
    result.errors = errors;
    throttle.emit(result.completed, "执行结束", true);
    Ok(result)
}

/// 执行单个文件，返回可撤销记录；`Ok(None)` 表示执行前校验判定跳过。
fn execute_one(
    opts: &ExtractOptions,
    it: &mut PlanItem,
    log_dir: &Path,
    op_id: &str,
    seq: usize,
) -> crate::model::Result<Option<RollbackEntry>> {
    // ---- 执行前重新校验（外部可能已经改动磁盘） ----
    if !file_exists(&it.source) {
        it.status = FileStatus::Error;
        it.message = "源文件已不存在或不可访问".into();
        return Err(Error::io("源文件已不存在或不可访问"));
    }
    let source_mtime = mtime_of(&it.source);
    let target_exists = file_exists(&it.target);

    if target_exists && !it.overwritten {
        // 预览时目标不存在，执行前冒出来了 —— 绝不盲目覆盖。
        // 自动改名策略就再算一个新名；其余策略一律跳过。
        match opts.conflict {
            crate::model::ConflictPolicy::Rename => {
                use std::collections::HashSet;
                let mut occupied = HashSet::new();
                occupied.insert(it.target.to_string_lossy().to_ascii_lowercase());
                let (new_name, new_path) =
                    crate::plan::unique_name(&it.target_dir, &it.name, &occupied);
                if file_exists(&new_path) {
                    it.status = FileStatus::Error;
                    it.message = "执行前出现同名文件，自动改名仍冲突".into();
                    return Err(Error::io("执行前出现同名文件，自动改名仍冲突"));
                }
                it.target = new_path;
                it.renamed = true;
                it.status = FileStatus::Renamed;
                it.message = format!("执行前发现同名，自动改名为 {}", new_name);
            }
            _ => {
                it.status = FileStatus::Skip;
                it.message = "执行前目标位置出现同名文件，已跳过以防止覆盖".into();
                return Ok(None);
            }
        }
    }

    let mut backup_rel: Option<PathBuf> = None;
    if it.overwritten {
        if !target_exists {
            // 原目标在执行前消失了：按普通移动处理，不再算覆盖
            it.overwritten = false;
            it.message = "原目标已不存在，按普通移动执行".into();
        } else {
            // 覆盖前必须备份成功，否则本项失败、源文件不动
            let rel = rollback::make_backup(&it.target, log_dir, op_id, seq)?;
            backup_rel = Some(rel);
            replace_with_backup(&it.source, &it.target)?;
        }
    }
    if !it.overwritten {
        move_no_replace(&it.source, &it.target)?;
    }

    if !file_exists(&it.target) {
        // 移动报成功但目标不在：异常状态，记为失败（源可能已被移动到别处）
        it.status = FileStatus::Error;
        it.message = "移动后目标文件不可见，需检查备份与日志".into();
        return Err(Error::io("移动后目标文件不可见"));
    }
    let target_mtime = mtime_of(&it.target);
    it.backup = backup_rel.clone();
    it.status = FileStatus::Done;
    it.message = if it.overwritten {
        format!("已覆盖（原目标已备份：{}）", backup_rel.as_ref().unwrap().display())
    } else if it.renamed {
        format!("已完成（自动改名：{}）", it.target.file_name().unwrap_or_default().to_string_lossy())
    } else {
        "已完成".into()
    };

    Ok(Some(RollbackEntry {
        index: it.index,
        source: it.source.clone(),
        target: it.target.clone(),
        size: it.size,
        status: FileStatus::Done,
        message: it.message.clone(),
        renamed: it.renamed,
        overwritten: it.overwritten,
        backup: backup_rel,
        source_mtime,
        target_mtime,
    }))
}

/// 生成 CSV 文本（UTF-8 BOM 由 `csv::to_bytes` 负责）。
pub fn result_csv(_plan: &ExtractPlan, result: &ExecResult) -> String {
    crate::csv::export(&result.items)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stop_registry_is_per_task() {
        let a = register_stop("unit_a");
        let b = register_stop("unit_b");
        request_stop("unit_a");
        assert!(a.is_stopped());
        assert!(!b.is_stopped());
        clear_stop("unit_a");
        clear_stop("unit_b");
    }
}
