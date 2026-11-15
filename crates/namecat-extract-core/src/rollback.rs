//! 固定撤销日志目录、原子写、撤销。
//!
//! 唯一存储位置：`<程序根目录>/Extract/Rollback_Log/`
//! - 源码运行：程序根目录 = 项目程序根；
//! - EXE 运行：程序根目录 = exe 所在目录（由绑定层用 `sys.executable` 判定后传入）；
//! - 绝不取当前工作目录、绝不取 `sys._MEIPASS`、绝不落到 `%APPDATA%` 或 `Name_backup`。
//!
//! 日志用「临时文件 + 原子替换」写入，Schema 带版本号；撤销数据不会被自动删除。

use crate::fs::{backup_file, dir_exists, ensure_dir, file_exists, move_no_replace, mtime_of};
use crate::model::{
    CleanedDir, Error, ExecResult, ExtractOptions, FileStatus, Progress, RollbackEntry,
    RollbackLog, RollbackState, UndoResult, BACKUP_SUBDIR, INTERNAL_DIR_NAME, ROLLBACK_DIR_NAME,
    SCHEMA,
};
use std::path::{Path, PathBuf};

/// 固定目录：`<program_root>/Extract/Rollback_Log/`。
pub fn rollback_dir(program_root: &str) -> PathBuf {
    Path::new(program_root)
        .join(INTERNAL_DIR_NAME)
        .join(ROLLBACK_DIR_NAME)
}

/// 建目录（含 backups），并做一次真实试写。失败即返回 Err —— 调用方据此
/// 在**真正搬文件之前**阻止执行，避免产生无法撤销的后果。
pub fn ensure_rollback_dir(program_root: &str) -> crate::model::Result<PathBuf> {
    let dir = rollback_dir(program_root);
    std::fs::create_dir_all(dir.join(BACKUP_SUBDIR))?;
    let probe = dir.join(".write_probe");
    std::fs::write(&probe, b"ok")?;
    std::fs::remove_file(probe)?;
    Ok(dir)
}

/// 生成操作唯一 ID：本地时间 + 进程号 + 单调计数。
pub fn new_operation_id() -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    static SEQ: AtomicU64 = AtomicU64::new(0);
    let n = SEQ.fetch_add(1, Ordering::SeqCst);
    let stamp = chrono::Local::now().format("%Y%m%d_%H%M%S_%3f");
    format!("{stamp}_p{}_{:04}", std::process::id(), n % 10000)
}

fn log_path(dir: &Path, op_id: &str) -> PathBuf {
    dir.join(format!("rollback_{op_id}.json"))
}

fn now_text() -> String {
    chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string()
}

/// 预写空日志（执行前）。进程中途异常退出时，至少保留 operation_id / root / 时间，
/// 便于人工恢复。返回 (目录, 操作 ID, 日志路径)。
pub fn prepare_log(opts: &ExtractOptions) -> crate::model::Result<(PathBuf, String, PathBuf)> {
    let dir = ensure_rollback_dir(&opts.program_root)?;
    let op_id = new_operation_id();
    let path = log_path(&dir, &op_id);
    let log = RollbackLog {
        schema: SCHEMA,
        app: "FileCat".into(),
        operation_id: op_id.clone(),
        time: now_text(),
        root: PathBuf::from(&opts.root),
        mode: opts.mode,
        conflict: opts.conflict,
        dry_run: false,
        result: "running".into(),
        state: RollbackState::Active,
        entries: Vec::new(),
        cleaned_dirs: Vec::new(),
        errors: Vec::new(),
    };
    write_log_atomic(&path, &log)?;
    Ok((dir, op_id, path))
}

/// 原子写日志：先写 `<name>.tmp`，再 rename 成最终文件。
pub fn write_log_atomic(path: &Path, log: &RollbackLog) -> crate::model::Result<()> {
    let parent = path.parent().ok_or_else(|| Error::Log("日志没有父目录".into()))?;
    std::fs::create_dir_all(parent)?;
    let text = serde_json::to_string_pretty(log)
        .map_err(|e| Error::Json(e.to_string()))?;
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, text.as_bytes())?;
    // 同目录 rename 是原子的；目标不存在（操作 ID 唯一），不会覆盖既有日志。
    std::fs::rename(&tmp, path)?;
    Ok(())
}

/// 列出全部可解析的日志，按文件名（含时间戳）倒序 = 最新在前。
/// 坏日志只跳过，不让整页读取失败。
pub fn list_logs(program_root: &str) -> Vec<(PathBuf, RollbackLog)> {
    let dir = rollback_dir(program_root);
    let mut out = Vec::new();
    let Ok(entries) = std::fs::read_dir(&dir) else {
        return out;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|s| s.to_str()) != Some("json") {
            continue;
        }
        let Ok(text) = std::fs::read_to_string(&path) else {
            continue;
        };
        let Ok(log) = serde_json::from_str::<RollbackLog>(&text) else {
            continue;
        };
        if log.schema > SCHEMA {
            continue; // 未来版本不猜
        }
        out.push((path, log));
    }
    out.sort_by(|a, b| b.0.cmp(&a.0));
    out
}

/// 撤销「最新一份仍有效（Active / Partial）」的操作记录。
pub fn undo_latest(program_root: &str, progress: &dyn Progress) -> crate::model::Result<UndoResult> {
    let candidate = list_logs(program_root)
        .into_iter()
        .find(|(_, log)| log.state != RollbackState::Undone && !log.dry_run);
    match candidate {
        Some((path, log)) => undo_one(&path, &log, progress),
        None => Ok(UndoResult {
            errors: vec!["没有可撤销的提取记录".into()],
            complete: false,
            ..Default::default()
        }),
    }
}

/// 按日志路径撤销指定一次操作。
pub fn undo_path(path: &Path, progress: &dyn Progress) -> crate::model::Result<UndoResult> {
    let text = std::fs::read_to_string(path)?;
    let log: RollbackLog = serde_json::from_str(&text)
        .map_err(|e| Error::Json(e.to_string()))?;
    undo_one(path, &log, progress)
}

fn undo_one(path: &Path, log: &RollbackLog, progress: &dyn Progress) -> crate::model::Result<UndoResult> {
    if log.state == RollbackState::Undone {
        return Ok(UndoResult {
            operation_id: log.operation_id.clone(),
            errors: vec!["该记录已经撤销过".into()],
            ..Default::default()
        });
    }
    let dir = path.parent().ok_or_else(|| Error::Log("日志没有父目录".into()))?;
    let total = log.entries.len();
    let mut out = UndoResult {
        operation_id: log.operation_id.clone(),
        ..Default::default()
    };
    let mut remaining: Vec<RollbackEntry> = Vec::new();

    // 先恢复被清理的目录结构（浅 → 深），再还原文件：
    // 这样文件恢复时父目录已存在，不会在 ensure_dir 里被隐式重建，
    // restored_dirs 才能如实反映真正重建的目录数量。
    let mut cleaned = log.cleaned_dirs.clone();
    cleaned.sort_by_key(|c| c.path.components().count());
    for rec in &cleaned {
        if !dir_exists(&rec.path) {
            match ensure_dir(&rec.path) {
                Ok(()) => out.restored_dirs += 1,
                Err(e) => out.errors.push(format!("恢复目录失败：{:?} {e}", rec.path)),
            }
        }
    }

    for (i, entry) in log.entries.iter().enumerate() {
        progress.on_progress(i + 1, total, &format!("撤销 {}", entry.target.display()));
        // 只处理实际执行成功的项
        if entry.status != FileStatus::Done {
            out.skipped += 1;
            continue;
        }
        // 目标还在吗？被用户移动/删除 → 跳过，绝不强行重建
        if !file_exists(&entry.target) {
            out.skipped += 1;
            out.errors.push(format!("目标已不存在，跳过：{}", entry.target.display()));
            continue;
        }
        // 目标被用户后续修改过 → 跳过，避免覆盖用户新数据
        if entry.target_mtime != 0 && mtime_of(&entry.target) != entry.target_mtime {
            out.skipped += 1;
            out.errors.push(format!("目标已被后续修改，跳过：{}", entry.target.display()));
            remaining.push(entry.clone());
            continue;
        }
        // 原位置已经有别的文件 → 跳过，禁止强制覆盖
        if file_exists(&entry.source) {
            out.skipped += 1;
            out.errors.push(format!("原位置已存在其他文件，跳过：{}", entry.source.display()));
            remaining.push(entry.clone());
            continue;
        }
        // 父目录可能被清理过，按需恢复
        if let Some(parent) = entry.source.parent() {
            if !dir_exists(parent) {
                if let Err(e) = ensure_dir(parent) {
                    out.failed += 1;
                    out.errors.push(format!("恢复目录失败：{parent:?} {e}"));
                    remaining.push(entry.clone());
                    continue;
                }
            }
        }

        if entry.overwritten {
            // 覆盖操作：先把「新文件」搬回原位置，再把备份恢复到目标位置。
            if let Err(e) = move_no_replace(&entry.target, &entry.source) {
                out.failed += 1;
                out.errors.push(format!("恢复新文件失败：{e}"));
                remaining.push(entry.clone());
                continue;
            }
            let backup = entry
                .backup
                .as_ref()
                .map(|b| if b.is_absolute() { b.clone() } else { dir.join(b) });
            match backup {
                Some(b) if file_exists(&b) => {
                    if let Err(e) = std::fs::copy(&b, &entry.target) {
                        out.failed += 1;
                        out.errors.push(format!("恢复备份失败：{e}"));
                        remaining.push(entry.clone());
                        continue;
                    }
                }
                _ => {
                    out.failed += 1;
                    out.errors.push(format!("备份文件缺失，无法恢复：{}", entry.target.display()));
                    remaining.push(entry.clone());
                    continue;
                }
            }
        } else if let Err(e) = move_no_replace(&entry.target, &entry.source) {
            out.failed += 1;
            out.errors.push(format!("还原失败：{e}"));
            remaining.push(entry.clone());
            continue;
        }
        out.succeeded += 1;
    }

    out.complete = out.failed == 0 && out.skipped == 0 && remaining.is_empty();
    let mut updated = log.clone();
    updated.entries = remaining;
    updated.state = if out.complete {
        RollbackState::Undone
    } else {
        RollbackState::Partial
    };
    updated.result = if out.complete {
        "undone".into()
    } else {
        format!("undo_partial:{}/{}", out.succeeded, total)
    };
    // 失败/跳过时**保留日志与备份**，只更新状态；成功也保留日志（不自动删除有效记录）。
    write_log_atomic(path, &updated)?;
    Ok(out)
}

/// 执行期把一条成功记录追加进内存日志并按需落盘（检查点）。
pub fn checkpoint(
    path: &Path,
    log: &mut RollbackLog,
    entry: RollbackEntry,
    cleaned: &[CleanedDir],
    errors: &[String],
    force: bool,
) -> crate::model::Result<()> {
    log.entries.push(entry);
    log.cleaned_dirs = cleaned.to_vec();
    log.errors = errors.to_vec();
    // 每 25 项或强制时落盘一次，兼顾崩溃可恢复性与大量小文件性能
    if force || log.entries.len() % 25 == 0 {
        write_log_atomic(path, log)?;
    }
    Ok(())
}

/// 把执行结果收束成最终日志（原子写）。
pub fn finalize_log(
    path: &Path,
    log: &mut RollbackLog,
    result: &ExecResult,
    cleaned: &[CleanedDir],
) -> crate::model::Result<()> {
    log.result = if result.stopped {
        "stopped".into()
    } else if result.failed > 0 {
        format!("completed_with_errors:{}/{}", result.succeeded, result.failed)
    } else {
        "completed".into()
    };
    log.cleaned_dirs = cleaned.to_vec();
    log.errors = result.errors.clone();
    write_log_atomic(path, log)
}

/// 覆盖前备份：返回相对日志目录的路径。
pub fn make_backup(target: &Path, dir: &Path, op_id: &str, seq: usize) -> crate::model::Result<PathBuf> {
    backup_file(target, dir, op_id, seq)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fixed_dir_layout() {
        let d = rollback_dir(r"C:\Apps\FileCat");
        assert!(d.ends_with("Rollback_Log"));
        assert!(d.to_string_lossy().replace('\\', "/").contains("Extract/Rollback_Log"));
    }

    #[test]
    fn operation_ids_are_unique() {
        let a = new_operation_id();
        let b = new_operation_id();
        assert_ne!(a, b);
    }
}
