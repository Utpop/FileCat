//! 计划生成：两种搬运方式的目标计算 + 三种冲突策略。
//!
//! 关键点：
//! - 目标路径在**扫描快照**上一次性算完，执行不重新推导（逐级上移不串层）；
//! - 冲突处理顺序由 `ScannedFile` 的路径字典序确定；
//! - 同一目标位置被多个源命中时，`occupied` 集合保证确定性与不意外覆盖；
//! - 自动改名保留原扩展名，且生成后再次验证不冲突。

use crate::model::{
    ConflictPolicy, Error, ExtractMode, ExtractOptions, ExtractPlan, FileStatus, PlanItem,
    ScannedFile,
};
use crate::scan::{scan, ScanOutput};
use std::collections::HashSet;
use std::path::{Path, PathBuf};

/// 生成完整提取计划（只扫描 + 计算，不碰文件系统写入）。
pub fn build_plan(opts: &ExtractOptions) -> crate::model::Result<ExtractPlan> {
    if opts.conflict.is_dangerous() && !opts.overwrite_confirmed {
        return Err(Error::Invalid("覆盖属于危险操作，必须先由用户确认"));
    }
    let scanned: ScanOutput = scan(opts, None)?;
    let root = PathBuf::from(&opts.root);
    let root = crate::fs::canonical_plain(&root).unwrap_or(root);
    Ok(plan_from_scanned(opts, &root, &scanned))
}

/// 纯函数版：给定扫描快照算计划，便于单元测试注入数据。
pub fn plan_from_scanned(opts: &ExtractOptions, root: &Path, scanned: &ScanOutput) -> ExtractPlan {
    // 目标占用表：扫描时盘上已有的文件（含被排除的、根目录已就位的），
    // 加上本批次已经规划好的目标。大小写规范化，Windows 语义。
    let mut occupied: HashSet<String> = scanned.existing_files.iter().cloned().collect();
    let mut items = Vec::with_capacity(scanned.files.len());

    for (index, f) in scanned.files.iter().enumerate() {
        let (base_dir, same_place) = target_dir_for(opts.mode, root, f);
        if same_place {
            // 目标就是原位置：无需搬运，也无需冲突处理
            items.push(PlanItem {
                index,
                name: f.name.clone(),
                source: f.source.clone(),
                source_dir: f.source_dir.clone(),
                target: f.source.clone(),
                target_dir: f.source_dir.clone(),
                size: f.size,
                depth: f.depth,
                status: FileStatus::Skip,
                message: "已在目标位置，无需搬运".into(),
                renamed: false,
                overwritten: false,
                backup: None,
            });
            continue;
        }

        let mut target = base_dir.join(&f.name);
        let key = norm(&target);
        let mut renamed = false;
        let mut overwritten = false;
        let mut status = FileStatus::Ready;
        let mut message = String::new();

        if occupied.contains(&key) {
            match opts.conflict {
                ConflictPolicy::Skip => {
                    status = FileStatus::Skip;
                    message = "目标位置已有同名文件，按策略跳过".into();
                }
                ConflictPolicy::Overwrite => {
                    status = FileStatus::Ready;
                    overwritten = true;
                    message = "危险：将覆盖目标位置已有文件（执行前会备份）".into();
                }
                ConflictPolicy::Rename => {
                    let (new_name, path) = unique_name(&base_dir, &f.name, &occupied);
                    target = path;
                    renamed = true;
                    status = FileStatus::Renamed;
                    message = format!("同名冲突，自动改名为 {}", new_name);
                }
            }
        }
        if status != FileStatus::Skip {
            let final_key = norm(&target);
            if opts.conflict == ConflictPolicy::Overwrite && overwritten {
                // 覆盖是有意为之：目标已在盘上属于预期，不进二次冲突校验。
                // 执行阶段会先备份再替换，绝不盲目覆盖。
            } else if occupied.contains(&final_key) {
                // 二次验证：改名后仍冲突（理论不可达，兜底）
                status = FileStatus::Error;
                message = "生成的名称仍然存在冲突".into();
            } else {
                occupied.insert(final_key);
            }
        }
        items.push(PlanItem {
            index,
            name: f.name.clone(),
            source: f.source.clone(),
            source_dir: f.source_dir.clone(),
            target,
            target_dir: base_dir,
            size: f.size,
            depth: f.depth,
            status,
            message,
            renamed,
            overwritten,
            backup: None,
        });
    }

    ExtractPlan {
        root: root.to_path_buf(),
        mode: opts.mode,
        conflict: opts.conflict,
        max_depth: opts.max_depth,
        items,
        empty_dir_candidates: scanned.dirs_deepest_first.clone(),
        scanned_count: scanned.files.len(),
        excluded_count: scanned.excluded_count,
        error_count: scanned.error_count,
    }
}

/// 返回 (目标目录, 是否原地不动)。
pub fn target_dir_for(mode: ExtractMode, root: &Path, f: &ScannedFile) -> (PathBuf, bool) {
    match mode {
        ExtractMode::Flatten => {
            let same = f.source_dir == root;
            (root.to_path_buf(), same)
        }
        ExtractMode::OneLevel => {
            let parent = f
                .source_dir
                .parent()
                .map(Path::to_path_buf)
                .unwrap_or_else(|| root.to_path_buf());
            // 只能上移一级；如果父目录不在扫描根内，退到根（防止越界）
            let parent = if parent.starts_with(root) {
                parent
            } else {
                root.to_path_buf()
            };
            let same = parent == f.source_dir;
            (parent, same)
        }
    }
}

/// 生成 `name (2).ext` / `name (3).ext`…，保留原扩展名并再次查占用表。
pub fn unique_name(dir: &Path, name: &str, occupied: &HashSet<String>) -> (String, PathBuf) {
    let (stem, ext) = split_name(name);
    for n in 2..=1_000_000u64 {
        let candidate = format!("{stem} ({n}){ext}");
        let path = dir.join(&candidate);
        if !occupied.contains(&norm(&path)) {
            return (candidate, path);
        }
    }
    // 兜底：时间戳后缀（仍保留扩展名）
    let fallback = format!("{stem}_fc{}{ext}", std::process::id());
    (fallback.clone(), dir.join(fallback))
}

/// 按最后一个点切分；`.gitignore` 这类无扩展名文件整体当 stem。
fn split_name(name: &str) -> (String, String) {
    match name.rfind('.') {
        Some(i) if i > 0 => (name[..i].to_string(), name[i..].to_string()),
        _ => (name.to_string(), String::new()),
    }
}

fn norm(path: &Path) -> String {
    path.to_string_lossy().to_ascii_lowercase()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn split_name_keeps_extension() {
        assert_eq!(split_name("a.tar.gz"), ("a.tar".into(), ".gz".into()));
        assert_eq!(split_name(".gitignore"), (".gitignore".into(), "".into()));
        assert_eq!(split_name("photo.jpg"), ("photo".into(), ".jpg".into()));
    }

    #[test]
    fn unique_name_skips_taken_candidates() {
        let dir = Path::new(r"C:\data");
        let mut occupied = HashSet::new();
        occupied.insert(norm(&dir.join("a.jpg")));
        occupied.insert(norm(&dir.join("a (2).jpg")));
        let (name, path) = unique_name(dir, "a.jpg", &occupied);
        assert_eq!(name, "a (3).jpg");
        assert_eq!(path, dir.join("a (3).jpg"));
    }
}
