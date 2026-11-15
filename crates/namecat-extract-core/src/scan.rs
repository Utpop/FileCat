//! 目录扫描：深度基准、排除规则、内部日志目录隔离、重解析点防护。
//!
//! 一次遍历收集**快照**：`ScannedFile` 与候选空目录都基于扫描时刻的路径，
//! 执行阶段不会边移动边重新推导目标（这是「逐级上移」正确性的关键）。

use crate::fs::{canonical_plain, fs_metadata, is_within};
use crate::model::{Error, ExtractOptions, ScannedFile, INTERNAL_DIR_NAME, ROLLBACK_DIR_NAME};
use std::collections::HashSet;
use std::path::{Path, PathBuf};

#[derive(Debug, Default)]
pub struct ScanOutput {
    pub files: Vec<ScannedFile>,
    /// 目录绝对路径，最深优先（清理用）。只含扫描范围内的普通目录。
    pub dirs_deepest_first: Vec<PathBuf>,
    /// 扫描时**所有**文件（含被排除的、根目录已就位的），用于冲突模拟。
    /// 存绝对路径字符串，大小写已规范化。
    pub existing_files: Vec<String>,
    pub excluded_count: usize,
    pub error_count: usize,
}

/// 内部数据目录：`<程序根>/Extract/Rollback_Log`。用户把程序根目录当扫描目标时
/// 也必须排除，防止把撤销日志/备份再次当成待提取文件。
pub fn internal_data_dir(program_root: &str) -> PathBuf {
    Path::new(program_root)
        .join(INTERNAL_DIR_NAME)
        .join(ROLLBACK_DIR_NAME)
}

fn norm(s: &str) -> String {
    s.trim().to_ascii_lowercase()
}

/// 目录名是否命中排除表（大小写不敏感、忽略首尾空格）。
fn excluded_dir(name: &str, excludes: &[String]) -> bool {
    let n = norm(name);
    excludes.iter().any(|e| e == &n)
}

/// 扩展名是否命中排除表。
fn excluded_ext(name: &str, excludes: &[String]) -> bool {
    let lower = name.to_ascii_lowercase();
    excludes.iter().any(|e| lower.ends_with(e))
}

/// 是否位于内部日志目录内（含其自身）。
fn in_internal(path: &Path, internal: &Path) -> bool {
    path == internal || is_within(path, internal)
}

/// 递归扫描。`max_depth = 0` 表示不限；否则根目录直接子目录深度为 1，
/// 其下文件深度为 2 —— 深度基准是「目录相对根」，文件深度 = 所在目录深度 + 1。
pub fn scan(opts: &ExtractOptions, root_override: Option<&Path>) -> crate::model::Result<ScanOutput> {
    let root = match root_override {
        Some(p) => p.to_path_buf(),
        None => PathBuf::from(&opts.root),
    };
    if !root.is_dir() {
        return Err(Error::Path(format!("目标文件夹不存在或不是目录：{}", root.display())));
    }
    let root = canonical_plain(&root).unwrap_or(root);

    let exc_dirs = opts.normalized_exclude_dirs();
    let exc_exts = opts.normalized_exclude_exts();
    let internal = internal_data_dir(&opts.program_root);
    let internal_abs = canonical_plain(&internal).unwrap_or(internal.clone());

    let mut out = ScanOutput::default();
    let mut seen_dirs: HashSet<PathBuf> = HashSet::new();

    walk(
        &root,
        0,
        opts.max_depth,
        &exc_dirs,
        &exc_exts,
        &internal_abs,
        &root,
        &mut out,
        &mut seen_dirs,
        true,
    )?;

    // 稳定顺序：路径字典序，保证「多个源映射到同一目标」有确定处理顺序
    out.files.sort_by(|a, b| a.source.cmp(&b.source));
    out.existing_files.sort();
    // 目录按深度降序（同深度按路径），实现「由深至浅清理」
    out.dirs_deepest_first.sort_by(|a, b| {
        let da = a.components().count();
        let db = b.components().count();
        db.cmp(&da).then_with(|| a.cmp(b))
    });
    Ok(out)
}

#[allow(clippy::too_many_arguments)]
fn walk(
    dir: &Path,
    dir_depth: usize,
    max_depth: usize,
    exc_dirs: &[String],
    exc_exts: &[String],
    internal: &Path,
    root: &Path,
    out: &mut ScanOutput,
    seen_dirs: &mut HashSet<PathBuf>,
    collect: bool,
) -> crate::model::Result<()> {
    if let Some(m) = fs_metadata(dir) {
        if m.reparse_point && dir != root {
            out.error_count += 1;
            return Ok(());
        }
    } else {
        out.error_count += 1;
        return Ok(());
    }
    if in_internal(dir, internal) {
        out.excluded_count += 1;
        return Ok(());
    }
    if !seen_dirs.insert(dir.to_path_buf()) {
        return Ok(());
    }
    if collect {
        out.dirs_deepest_first.push(dir.to_path_buf());
    }

    let entries = match std::fs::read_dir(dir) {
        Ok(e) => e,
        Err(_) => {
            out.error_count += 1;
            return Ok(());
        }
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        let meta = match fs_metadata(&path) {
            Some(m) => m,
            None => {
                out.error_count += 1;
                continue;
            }
        };
        // 所有现存文件（含被排除/已就位）都记进模拟占用表
        if meta.is_file && !meta.reparse_point {
            out.existing_files.push(path.to_string_lossy().to_ascii_lowercase());
        }
        if meta.reparse_point {
            out.error_count += 1;
            continue;
        }
        if meta.is_dir {
            if excluded_dir(&name, exc_dirs) {
                out.excluded_count += 1;
                continue;
            }
            if max_depth > 0 && dir_depth + 1 > max_depth {
                out.excluded_count += 1;
                continue;
            }
            walk(
                &path,
                dir_depth + 1,
                max_depth,
                exc_dirs,
                exc_exts,
                internal,
                root,
                out,
                seen_dirs,
                collect,
            )?;
            continue;
        }
        if !meta.is_file {
            continue;
        }
        if excluded_ext(&name, exc_exts) {
            out.excluded_count += 1;
            continue;
        }
        out.files.push(ScannedFile {
            source: path.clone(),
            name,
            source_dir: dir.to_path_buf(),
            depth: dir_depth + 1,
            size: meta.size,
        });
    }
    Ok(())
}

/// 计算相对根目录的路径文本（供日志/CSV 使用）。
pub fn relative_text(path: &Path, root: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_list_trims_and_dedups() {
        let v = ExtractOptions::parse_list(" .TMP, tmp ;BAK , , txt ", true);
        assert_eq!(v, vec![".tmp", ".bak", ".txt"]);
        let d = ExtractOptions::parse_list(" Node_Modules, SYSTEM , ,backup ", false);
        assert_eq!(d, vec!["node_modules", "system", "backup"]);
    }

    #[test]
    fn internal_dir_is_excluded_by_name() {
        let p = internal_data_dir(r"C:\Program\FileCat");
        assert!(p.ends_with("Rollback_Log"));
        assert!(p.to_string_lossy().contains("Extract"));
    }
}
