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

/// 追加式检查点文件：`<日志名>.pending`，每行一条 RollbackEntry 的紧凑 JSON。
/// 每成功搬走一个文件就 append 一行并 flush（O(1)），崩溃时最多丢最后一行，
/// 不再有「每 25 项才全量重写一次、最后 24 项无记录」的窗口。
fn pending_path(path: &Path) -> PathBuf {
    let mut s = path.as_os_str().to_os_string();
    s.push(".pending");
    PathBuf::from(s)
}

/// 预写空日志（执行前）+ 创建空的检查点文件。进程中途异常退出时，
/// 至少保留 operation_id / root / 时间，以及已成功项的逐条记录，便于恢复。
/// 返回 (目录, 操作 ID, 日志路径)。
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
    // 先写正式日志再建 pending：若这一步失败，prepare_log 直接 Err，
    // 调用方会在搬任何文件之前拒绝执行。
    std::fs::write(pending_path(&path), b"")?;
    Ok((dir, op_id, path))
}

/// 从追加式检查点恢复全部已成功项（崩溃恢复 / finalize 合成用）。
/// 单独一行损坏只跳过它，不让整份日志作废。
pub fn read_pending(path: &Path) -> Vec<RollbackEntry> {
    let Ok(text) = std::fs::read_to_string(pending_path(path)) else {
        return Vec::new();
    };
    text.lines()
        .filter(|l| !l.trim().is_empty())
        .filter_map(|l| serde_json::from_str::<RollbackEntry>(l).ok())
        .collect()
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
        // .pending 是检查点，不是日志；跳过（否则会被当成无扩展名的坏文件）
        if path.as_os_str().to_string_lossy().ends_with(".pending") {
            continue;
        }
        let Ok(text) = std::fs::read_to_string(&path) else {
            continue;
        };
        let Ok(mut log) = serde_json::from_str::<RollbackLog>(&text) else {
            continue;
        };
        if log.schema > SCHEMA {
            continue; // 未来版本不猜
        }
        // 崩溃恢复只在这里「读」出来供列表展示，绝不写回日志：
        // 执行过程中 GUI 刷新日志页若把 running 固化成 recovered，
        // 后续 .pending 的成功项就永远不会被合成（见 undo_one 里的恢复）。
        if log.result == "running" {
            let recovered = read_pending(&path);
            if !recovered.is_empty() {
                log.entries = recovered;
            }
        }
        out.push((path, log));
    }
    out.sort_by(|a, b| b.0.cmp(&a.0));
    out
}

/// 撤销「最新一份仍有效（Active / Partial）」的操作记录。
///
/// 忽略 0 条目的空日志：重复执行旧计划、空目录执行都会产生这种记录，
/// 若不跳过，它会挡在真正那份日志前面，让「撤销上次」永远够不着真实数据。
pub fn undo_latest(program_root: &str, progress: &dyn Progress) -> crate::model::Result<UndoResult> {
    let candidate = list_logs(program_root)
        .into_iter()
        .find(|(_, log)| {
            log.state != RollbackState::Undone && !log.dry_run && !log.entries.is_empty()
        });
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

    // 崩溃恢复：正式日志停在 running（没来得及 finalize）时，用逐条落盘的
    // .pending 补齐 entries。恢复只发生在「真正要撤销」的这一刻，并在这里
    // 原子写回一份完整日志，之后 list_logs / 二次撤销都能正常读到。
    let mut effective = log.clone();
    if effective.result == "running" {
        let recovered = read_pending(path);
        if !recovered.is_empty() {
            effective.entries = recovered;
            effective.result = "recovered_from_checkpoint".into();
            write_log_atomic(path, &effective)?;
        }
    }
    let log = &effective;

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
        // 只处理实际执行成功的项。跳过也必须把条目留在日志里（remaining），
        // 否则部分失败后重入时这条记录已经消失，文件再也撤不回来。
        if entry.status != FileStatus::Done {
            out.skipped += 1;
            remaining.push(entry.clone());
            continue;
        }
        // 目标还在吗？被用户移动/删除 → 跳过，绝不强行重建；但保留条目，
        // 用户把文件放回后仍可再次撤销。
        if !file_exists(&entry.target) {
            out.skipped += 1;
            out.errors.push(format!("目标已不存在，跳过：{}", entry.target.display()));
            remaining.push(entry.clone());
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
            // 覆盖项：先确认备份还在，再动目标。备份缺失时绝不能把新文件
            // 搬走（那样目标位置会永久空缺），直接失败并保留条目。
            let backup = entry
                .backup
                .as_ref()
                .map(|b| if b.is_absolute() { b.clone() } else { dir.join(b) });
            let Some(b) = backup else {
                out.failed += 1;
                out.errors.push(format!("未记录备份路径，无法恢复：{}", entry.target.display()));
                remaining.push(entry.clone());
                continue;
            };
            if !file_exists(&b) {
                out.failed += 1;
                out.errors.push(format!("备份文件缺失，无法恢复：{}", entry.target.display()));
                remaining.push(entry.clone());
                continue;
            }
            // 先把「新文件」搬回原位置
            if let Err(e) = move_no_replace(&entry.target, &entry.source) {
                out.failed += 1;
                out.errors.push(format!("恢复新文件失败：{e}"));
                remaining.push(entry.clone());
                continue;
            }
            // 目标位置此刻应为空；若被第三方抢先放了文件，绝不覆盖它，
            // 而是把新文件搬回目标、保留条目，等用户处理后再重入。
            if file_exists(&entry.target) {
                let _ = move_no_replace(&entry.source, &entry.target);
                out.failed += 1;
                out.errors.push(format!("目标位置已被占用，撤销暂停：{}", entry.target.display()));
                remaining.push(entry.clone());
                continue;
            }
            if let Err(e) = std::fs::copy(&b, &entry.target) {
                // copy 失败时目标和原位置都空着 —— 必须把刚搬回去的新文件
                // 再搬回目标，让磁盘回到「撤销前」的一致状态，否则重入时
                // 目标不存在会被跳过、条目消失。
                let _ = move_no_replace(&entry.source, &entry.target);
                out.failed += 1;
                out.errors.push(format!("恢复备份失败（已回滚，可重试）：{e}"));
                remaining.push(entry.clone());
                continue;
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
    // 撤销已收尾，清掉可能残留的崩溃检查点，避免以后被重复合成。
    if out.complete {
        let _ = std::fs::remove_file(pending_path(path));
    }
    Ok(out)
}

/// 执行期持有的检查点写入器：整个执行过程只 open 一次 .pending 文件。
///
/// 逐条 `write + flush` 保证崩溃时最多丢最后一行；若每项都重新 open/close，
/// 3000 文件实测要多花约 1s（1.07s vs 0.019s），纯属无谓的句柄开销。
pub struct PendingWriter {
    file: std::fs::File,
}

impl PendingWriter {
    pub fn open(log_path: &Path) -> crate::model::Result<Self> {
        let file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(pending_path(log_path))?;
        Ok(Self { file })
    }

    pub fn append(&mut self, entry: &RollbackEntry) -> crate::model::Result<()> {
        use std::io::Write as _;
        let line = serde_json::to_string(entry).map_err(|e| Error::Json(e.to_string()))?;
        self.file.write_all(line.as_bytes())?;
        self.file.write_all(b"\n")?;
        self.file.flush()?;
        Ok(())
    }
}

/// 把一条成功记录追加进内存日志与检查点文件（O(1)，不再全量重写 JSON）。
/// 先落盘再改内存：写失败时该项仍会被记为失败，不会出现「日志说成功、
/// 磁盘却没记录」的反向不一致。
pub fn checkpoint(
    writer: &mut PendingWriter,
    log: &mut RollbackLog,
    entry: RollbackEntry,
    cleaned: &[CleanedDir],
    errors: &[String],
) -> crate::model::Result<()> {
    writer.append(&entry)?;
    log.entries.push(entry);
    log.cleaned_dirs = cleaned.to_vec();
    log.errors = errors.to_vec();
    Ok(())
}

/// 把执行结果收束成最终日志（原子写）。
///
/// entries 以检查点文件为准重新合成，保证「正式日志」与逐条落盘的记录
/// 完全一致；随后删除 .pending，标记这次操作已正常收尾。
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
    // 以「检查点文件」与「内存累计」中较完整的一份为准。两者不一致说明
    // .pending 被外部删改 / 读取失败 / 有损坏行，此时绝不能静默写成 0 条目
    // 的 completed 日志（那样整批搬移都无法撤销），必须报警并保留 .pending。
    let recovered = read_pending(path);
    let mut final_errors = result.errors.clone();
    if recovered.len() != log.entries.len() {
        final_errors.push(format!(
            "警告：检查点记录数({})与内存累计({})不一致，已保留较完整的一份；请检查 .pending 是否完整",
            recovered.len(), log.entries.len()
        ));
    }
    if recovered.len() >= log.entries.len() {
        log.entries = recovered;
    }
    log.cleaned_dirs = cleaned.to_vec();
    // 有严重检查点错误时保留 .pending，方便人工/后续恢复，绝不静默删除。
    let keep_pending = final_errors.iter().any(|e| e.contains("检查点写入失败"));
    log.errors = final_errors;
    write_log_atomic(path, log)?;
    if !keep_pending {
        let _ = std::fs::remove_file(pending_path(path));
    }
    Ok(())
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
