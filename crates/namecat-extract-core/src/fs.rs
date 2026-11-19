//! 安全文件系统原语。
//!
//! 硬规则：
//! - 所有路径统一用 `\\?\` 长路径前缀（解决 MAX_PATH 与 UNC）；
//! - **禁止** `std::fs::rename`（Windows 上会覆盖已存在目标）；
//! - 移动一律 `MoveFileExW(flags=0)`，跨卷自动 `MOVEFILE_COPY_ALLOWED`；
//! - 不跟随符号链接 / Junction / 其它重解析点；
//! - 覆盖前先把原目标复制进 `Rollback_Log/backups/`，成功才允许替换。

use crate::model::{Error, Result};
use std::path::{Path, PathBuf};

/// 把路径转成 `\\?\` 前缀的绝对形式。
///
/// 用 `OsStr::encode_wide` 而不是 `to_string_lossy().encode_utf16()`：后者会把
/// Windows 上合法的未配对 surrogate（NTFS 允许）替换成 U+FFFD，导致
/// `MoveFileExW` 找不到真实文件、搬移失败。`encode_wide` 原样保留 UTF-16 单元。
pub fn long_path(path: &Path) -> Result<Vec<u16>> {
    let abs = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()?.join(path)
    };
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        let mut units: Vec<u16> = abs.as_os_str().encode_wide().collect();
        let is_verbatim = units.starts_with(&[92, 92, 63, 92]); // \\?\
        let is_unc = !is_verbatim
            && units.starts_with(&[92, 92]); // \\server\share
        let mut wide: Vec<u16> = if is_verbatim {
            Vec::new()
        } else if is_unc {
            units.drain(..2); // 去掉开头的 \\，由 \\?\UNC\ 补上
            r"\\?\UNC\".encode_utf16().collect()
        } else {
            r"\\?\".encode_utf16().collect()
        };
        wide.extend(units);
        wide.push(0);
        Ok(wide)
    }
    #[cfg(not(windows))]
    {
        let text = abs.to_string_lossy().replace('/', "\\");
        let normalized = if text.starts_with(r"\\?\") {
            text
        } else if text.starts_with(r"\\") {
            format!(r"\\?\UNC\{}", &text[2..])
        } else {
            format!(r"\\?\{}", text)
        };
        let mut wide: Vec<u16> = normalized.encode_utf16().collect();
        wide.push(0);
        Ok(wide)
    }
}

/// 直接调用 Win32，绕过 `std` 的规范化，避免把 `\\?\` 又拆回普通路径。
fn win_path(path: &Path) -> Result<Vec<u16>> {
    long_path(path)
}

pub fn file_exists(path: &Path) -> bool {
    match fs_metadata(path) {
        Some(m) => m.is_file,
        None => false,
    }
}

pub fn dir_exists(path: &Path) -> bool {
    match fs_metadata(path) {
        Some(m) => m.is_dir,
        None => false,
    }
}

#[derive(Debug, Clone, Copy)]
pub struct MetadataLite {
    pub is_file: bool,
    pub is_dir: bool,
    pub reparse_point: bool,
    pub size: u64,
    /// Windows FILETIME（100ns ticks, UTC）。
    pub mtime: i64,
}

/// 用 `std::fs::symlink_metadata` + `GetFileAttributesW` 组合：
/// 前者绝不跟随链接，后者用来识别 reparse point（Junction / symlink）。
pub fn fs_metadata(path: &Path) -> Option<MetadataLite> {
    let meta = std::fs::symlink_metadata(path).ok()?;
    let mut reparse_point = meta.file_type().is_symlink();
    #[cfg(windows)]
    {
        use windows_sys::Win32::Storage::FileSystem::{
            GetFileAttributesW, FILE_ATTRIBUTE_REPARSE_POINT, INVALID_FILE_ATTRIBUTES,
        };
        let wide = win_path(path).ok()?;
        let attrs = unsafe { GetFileAttributesW(wide.as_ptr()) };
        if attrs != INVALID_FILE_ATTRIBUTES {
            reparse_point |= (attrs & FILE_ATTRIBUTE_REPARSE_POINT) != 0;
        }
    }
    let mtime = filetime(&meta);
    Some(MetadataLite {
        is_file: meta.is_file(),
        is_dir: meta.is_dir(),
        reparse_point,
        size: meta.len(),
        mtime,
    })
}

/// 文件 mtime → Windows FILETIME ticks。跨平台分支保持可编译（测试用）。
fn filetime(meta: &std::fs::Metadata) -> i64 {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        meta.last_write_time() as i64
    }
    #[cfg(not(windows))]
    {
        let _ = meta;
        0
    }
}

pub fn mtime_of(path: &Path) -> i64 {
    fs_metadata(path).map(|m| m.mtime).unwrap_or(0)
}

/// 目录是否为空（不跟随重解析点；目录本身是重解析点时视为非空，禁止清理）。
pub fn is_dir_empty(path: &Path) -> Result<bool> {
    let meta = fs_metadata(path).ok_or_else(|| Error::io("目录不存在"))?;
    if !meta.is_dir || meta.reparse_point {
        return Ok(false);
    }
    let mut it = std::fs::read_dir(path)?;
    Ok(it.next().is_none())
}

/// 原子式非覆盖移动：`MoveFileExW` + `MOVEFILE_COPY_ALLOWED`，目标已存在直接失败。
///
/// 不再带 `MOVEFILE_WRITE_THROUGH`：那个标志强制每次移动都同步刷盘，实测
/// 3000 个小文件执行要 ~9s，是整条链路最大的性能瓶颈。撤销安全性由「逐条
/// 追加检查点日志 + 覆盖前备份」保证，不依赖每次 rename 的 fsync。
pub fn move_no_replace(source: &Path, target: &Path) -> Result<()> {
    if file_exists(target) {
        return Err(Error::io("目标已存在，拒绝覆盖"));
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::Storage::FileSystem::{
            MoveFileExW, MOVEFILE_COPY_ALLOWED,
        };
        let src = win_path(source)?;
        let dst = win_path(target)?;
        let flags = MOVEFILE_COPY_ALLOWED;
        let ok = unsafe { MoveFileExW(src.as_ptr(), dst.as_ptr(), flags) };
        if ok != 0 {
            return Ok(());
        }
        return Err(Error::io(format!(
            "移动失败（Win32 {}）：{} → {}",
            last_error(),
            source.display(),
            target.display()
        )));
    }
    #[cfg(not(windows))]
    {
        // 非 Windows 测试环境：先判存在，再用 std 移动（语义保持非覆盖）
        std::fs::rename(source, target)?;
        Ok(())
    }
}

/// 用备份恢复被 `replace_with_backup` 删掉但没能用源文件替换的目标。
/// 目标位置此刻应为空；若被第三方抢先放了文件，绝不覆盖，直接报错。
pub fn restore_from_backup(backup: &Path, target: &Path) -> Result<()> {
    if file_exists(target) {
        return Err(Error::io("恢复位置已被占用，拒绝覆盖"));
    }
    std::fs::copy(backup, target).map_err(Error::Io)?;
    Ok(())
}

/// 覆盖移动：调用方**必须**先 `backup_file` 成功。先用非覆盖删除原目标，
/// 再 `move_no_replace`，最后删不掉也不影响源（源还在原处）。
pub fn replace_with_backup(source: &Path, target: &Path) -> Result<()> {
    if !file_exists(target) {
        return move_no_replace(source, target);
    }
    // 先删原目标（已备份），再移动。任意一步失败源都保持原样。
    std::fs::remove_file(target).map_err(Error::Io)?;
    match move_no_replace(source, target) {
        Ok(()) => Ok(()),
        Err(e) => Err(Error::io(format!(
            "覆盖移动失败（原目标已备份，可撤销）：{e}"
        ))),
    }
}

/// 把文件复制进备份目录，返回相对 `Rollback_Log` 的路径。
/// 备份名带操作 ID 与序号，保证与日志可准确关联。
pub fn backup_file(target: &Path, rollback_dir: &Path, op_id: &str, seq: usize) -> Result<PathBuf> {
    let backups = rollback_dir.join(crate::model::BACKUP_SUBDIR);
    std::fs::create_dir_all(&backups)?;
    let name = target
        .file_name()
        .ok_or_else(|| Error::io("目标没有文件名"))?
        .to_string_lossy();
    let backup_name = format!("{op_id}_{seq:05}_{name}");
    let dest = backups.join(&backup_name);
    // 备份文件绝不能覆盖已有备份
    if dest.exists() {
        return Err(Error::io("备份文件已存在，拒绝覆盖"));
    }
    std::fs::copy(target, &dest)?;
    Ok(PathBuf::from(crate::model::BACKUP_SUBDIR).join(backup_name))
}

/// 删除空目录（不递归、不跟随重解析点）。
pub fn remove_empty_dir(path: &Path) -> Result<()> {
    if !is_dir_empty(path)? {
        return Err(Error::io("目录非空或为链接，拒绝删除"));
    }
    std::fs::remove_dir(path)?;
    Ok(())
}

/// 递归创建目录（撤销时恢复结构），跳过重解析点祖先。
pub fn ensure_dir(path: &Path) -> Result<()> {
    if path.as_os_str().is_empty() {
        return Ok(());
    }
    std::fs::create_dir_all(path)?;
    Ok(())
}

#[cfg(windows)]
pub fn last_error() -> u32 {
    use windows_sys::Win32::Foundation::GetLastError;
    unsafe { GetLastError() }
}

#[cfg(not(windows))]
pub fn last_error() -> u32 {
    0
}

/// 安全判断 `child` 是否位于 `root` 之内（按规范化后的组件比较，不做字符串前缀）。
///
/// `Path::starts_with` 是词法比较，不解析 `..`；外部传入的伪造计划可以写
/// `<root>\sub\..\..\outside.txt` 骗过校验，再由系统解析 `..` 越界操作。
/// 这里显式拒绝任何含 ParentDir 的路径，并消解 `.` 后再比较。
pub fn is_within(child: &Path, root: &Path) -> bool {
    use std::path::Component;
    if child.components().any(|c| c == Component::ParentDir)
        || root.components().any(|c| c == Component::ParentDir)
    {
        return false;
    }
    let clean: PathBuf = child
        .components()
        .filter(|c| *c != Component::CurDir)
        .collect();
    let base: PathBuf = root
        .components()
        .filter(|c| *c != Component::CurDir)
        .collect();
    clean.starts_with(&base)
}

/// `canonicalize` 在 Windows 上返回 `\\?\C:\...`（verbatim）前缀，与 Python
/// 侧 / 用户输入的普通路径拼在一起会四处不匹配。这里统一剥掉前缀，保证
/// 整个引擎内部只用一种路径形态；真正调 Win32 API 时再由 `long_path` 加回去。
pub fn canonical_plain(path: &Path) -> std::io::Result<PathBuf> {
    let abs = std::fs::canonicalize(path)?;
    let text = abs.to_string_lossy();
    let stripped = if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        format!(r"\\{}", rest)
    } else if let Some(rest) = text.strip_prefix(r"\\?\") {
        rest.to_string()
    } else {
        return Ok(abs);
    };
    Ok(PathBuf::from(stripped))
}
