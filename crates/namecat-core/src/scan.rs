//! 目录扫描：过滤、排序、成链（对应 `engine/scan.py`）。
//!
//! 只做 IO 扫描，不碰改名计算（见 [`crate::plan`]）。
//!
//! 过滤顺序刻意安排成"廉价在前"：纯字符串（名字/扩展名）→ stat → 读文件头，
//! 这与 Python 版一致，别为了"看起来更整齐"把 stat 提到前面。

use crate::pathops;
use std::collections::HashSet;
use std::fs;
use std::path::Path;

// --------------------------------------------------------------------------- //
// 类型常量
// --------------------------------------------------------------------------- //

pub const KIND_FILE: &str = "file";
pub const KIND_DIR: &str = "dir";
pub const KIND_IMAGE: &str = "image";
pub const KIND_VIDEO: &str = "video";

pub const ALL_KINDS: [&str; 4] = [KIND_FILE, KIND_DIR, KIND_IMAGE, KIND_VIDEO];

pub const MAX_DEPTH: i64 = 12;

/// 扫描时要跳过的目录名（旧日志目录 + 新的统一备份目录）。
pub const LOG_DIR_NAMES: [&str; 2] = ["_rename_logs", "Name_backup"];

/// 比较一律走小写（Windows 目录名大小写不敏感）——别拿 `LOG_DIR_NAMES` 直接比对，
/// 否则 `Name_backup` 永远对不上 `name_backup`，跳过等于没写。
pub fn log_dir_skip() -> Vec<String> {
    LOG_DIR_NAMES.iter().map(|s| s.to_lowercase()).collect()
}

/// `KIND_TEXT`：类型 → 中文名。
pub fn kind_text() -> Vec<(&'static str, &'static str)> {
    vec![
        (KIND_FILE, "文件"),
        (KIND_DIR, "文件夹"),
        (KIND_IMAGE, "图片"),
        (KIND_VIDEO, "视频"),
    ]
}

/// 覆盖常见位图 / 相机 RAW（沿用 BatchRenamer 的清单，补了几个新容器）。
pub const IMAGE_EXTS: [&str; 31] = [
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff",
    ".heic", ".heif", ".avif", ".ico", ".svg", ".tga", ".dds", ".psd", ".jxl", ".raw",
    ".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".raf", ".pef", ".sr2",
    ".srw",
];

pub const VIDEO_EXTS: [&str; 27] = [
    ".mp4", ".m4v", ".mov", ".qt", ".avi", ".mkv", ".webm", ".wmv", ".asf", ".flv",
    ".f4v", ".mpg", ".mpeg", ".mpe", ".m2v", ".ts", ".m2ts", ".mts", ".3gp", ".3g2",
    ".vob", ".rm", ".rmvb", ".ogv", ".divx", ".mxf", ".dv",
];

/// `SORT_MODES`：(键, 界面文案)。
pub fn sort_modes() -> Vec<(&'static str, &'static str)> {
    vec![
        ("name_asc", "名称（自然序）"),
        ("name_desc", "名称（倒序）"),
        ("mtime_asc", "修改时间（旧 → 新）"),
        ("mtime_desc", "修改时间（新 → 旧）"),
        ("ctime_asc", "创建时间（旧 → 新）"),
        ("ctime_desc", "创建时间（新 → 旧）"),
        ("size_asc", "大小（小 → 大）"),
        ("size_desc", "大小（大 → 小）"),
        ("path_asc", "路径（先目录后名称）"),
        ("none", "不改顺序（按扫描得到的顺序）"),
    ]
}

/// 文件头特征：仅用于"校验文件内容"。未列出的扩展名一律放行。
///
/// `None` 表示"该格式需要专门的判定逻辑"（webp/avi 要看 RIFF 子类型，
/// heic/mp4 这类容器看 `ftyp`），见 [`check_file_magic`]。
pub fn magic_table() -> Vec<(&'static str, Option<Vec<&'static [u8]>>)> {
    vec![
        (".jpg", Some(vec![b"\xff\xd8\xff"])),
        (".jpeg", Some(vec![b"\xff\xd8\xff"])),
        (".jpe", Some(vec![b"\xff\xd8\xff"])),
        (".jfif", Some(vec![b"\xff\xd8\xff"])),
        (".png", Some(vec![b"\x89PNG\r\n\x1a\n"])),
        (".gif", Some(vec![b"GIF87a", b"GIF89a"])),
        (".bmp", Some(vec![b"BM"])),
        (".tif", Some(vec![b"II*\x00", b"MM\x00*"])),
        (".tiff", Some(vec![b"II*\x00", b"MM\x00*"])),
        (".ico", Some(vec![b"\x00\x00\x01\x00"])),
        (".psd", Some(vec![b"8BPS"])),
        (".dds", Some(vec![b"DDS "])),
        (".webp", None),
        (".heic", None),
        (".heif", None),
        (".avif", None),
        (".mp4", None),
        (".m4v", None),
        (".mov", None),
        (".qt", None),
        (".3gp", None),
        (".3g2", None),
        (".f4v", None),
        (".avi", None),
        (".mkv", Some(vec![b"\x1a\x45\xdf\xa3"])),
        (".webm", Some(vec![b"\x1a\x45\xdf\xa3"])),
        (".wmv", Some(vec![b"0&\xb2u\x8ef\xcf\x11"])),
        (".asf", Some(vec![b"0&\xb2u\x8ef\xcf\x11"])),
        (".flv", Some(vec![b"FLV"])),
        (".mpg", Some(vec![b"\x00\x00\x01\xba"])),
        (".mpeg", Some(vec![b"\x00\x00\x01\xba"])),
        (".mpe", Some(vec![b"\x00\x00\x01\xba"])),
        (".m2v", Some(vec![b"\x00\x00\x01\xba"])),
        (".vob", Some(vec![b"\x00\x00\x01\xba"])),
        (".ts", Some(vec![b"\x47"])),
        (".m2ts", Some(vec![b"\x47"])),
        (".mts", Some(vec![b"\x47"])),
        (".rm", Some(vec![b".RMF"])),
        (".rmvb", Some(vec![b".RMF"])),
        (".ogv", Some(vec![b"OggS"])),
    ]
}

// --------------------------------------------------------------------------- //
// 元数据
// --------------------------------------------------------------------------- //

/// 文件元数据（对齐 Python `os.stat_result` 里我们用到的字段）。
#[derive(Clone, Debug, Default)]
pub struct Meta {
    pub mode: u32,
    pub ino: u64,
    pub dev: u64,
    pub nlink: u64,
    pub uid: u32,
    pub gid: u32,
    pub size: i64,
    pub atime: f64,
    pub mtime: f64,
    pub ctime: f64,
    /// `st_file_attributes`（Windows）；非 Windows 恒为 0。
    pub file_attributes: u32,
}

/// FILETIME（自 1601-01-01 起的 100ns）→ Unix 秒。
///
/// **必须先减纪元再除**：`ft as f64 / 1e7 - 11644473600.0` 会在大数上多做一次
/// 浮点减法，结果差 1~2 个 ulp（实测 `...6031418` vs Python 的 `...603142`）。
/// `test_edges` 不比浮点，但"跟 Python 逐位一致"能省掉一堆怀疑。
fn filetime_to_unix(ft: u64) -> f64 {
    const EPOCH: u64 = 116_444_736_000_000_000;
    if ft >= EPOCH {
        (ft - EPOCH) as f64 / 10_000_000.0
    } else {
        -((EPOCH - ft) as f64) / 10_000_000.0
    }
}

/// `os.path.exists(path)`：只要"在不在"，不取元数据。
///
/// 别把它写成 `meta_of(path).is_some()`：`meta_of` 为了正确性有三级回退，
/// 目标不存在时会把三级全部试一遍（三次系统调用）；校验阶段这是逐项调用的，
/// 2000 个文件就多花几十毫秒。这里只走最快的那一级，失败即"不存在"。
pub fn path_exists(path: &str) -> bool {
    if path.is_empty() {
        return false;
    }
    #[cfg(windows)]
    {
        if let Some(found) = win_probe_exists(path) {
            return found;
        }
    }
    std::path::Path::new(path).exists()
}

/// 返回 `Some(存在与否)`；`None` 表示"这台机器没有可用的快接口"。
#[cfg(windows)]
fn win_probe_exists(path: &str) -> Option<bool> {
    use windows_sys::Win32::Storage::FileSystem::{
        GetFileAttributesExW, GetFileExInfoStandard, WIN32_FILE_ATTRIBUTE_DATA,
    };
    if !pathops::isabs(path) {
        return None;
    }
    if let Some(func) = by_name_proc() {
        if self_check_ok(&func) {
            let wide: Vec<u16> = path.encode_utf16().chain(std::iter::once(0)).collect();
            let mut info = FileStatBasicInfo::default();
            let ok = unsafe {
                func(
                    wide.as_ptr(),
                    0,
                    &mut info as *mut FileStatBasicInfo as *mut core::ffi::c_void,
                    std::mem::size_of::<FileStatBasicInfo>() as u32,
                )
            };
            return Some(ok != 0);
        }
    }
    let wide: Vec<u16> = path.encode_utf16().chain(std::iter::once(0)).collect();
    let mut data: WIN32_FILE_ATTRIBUTE_DATA = unsafe { std::mem::zeroed() };
    let ok = unsafe {
        GetFileAttributesExW(
            wide.as_ptr(),
            GetFileExInfoStandard,
            &mut data as *mut WIN32_FILE_ATTRIBUTE_DATA as *mut core::ffi::c_void,
        )
    };
    Some(ok != 0)
}

/// `os.stat(path)`：跟随符号链接。
///
/// Windows 上按"快 → 慢"三级回退，最后一级才是 `std::fs::metadata`：
///
/// 1. `GetFileInformationByName`（Win11 22H2+）——一次轻量调用，实测 7 µs/次，
///    与 CPython 的 `os.stat` 同速；**动态解析**导出表，老系统没有就直接跳过
///    （静态导入会让进程在 Win10 上根本起不来）。
/// 2. `GetFileAttributesExW`——老系统兜底，约 23 µs/次。
/// 3. `std::fs::metadata`——会 CreateFileW 打开文件（约 29 µs/次），只在遇到
///    重解析点（符号链接/挂载点）时用它，因为前两个拿到的是链接本身。
///
/// 为什么值得这么折腾：2000 个文件的扫描里 stat 占九成时间，走第 1 级
/// 整条 scan 从 65 ms 降到 20 ms 量级。
pub fn meta_of(path: &str) -> Option<Meta> {
    #[cfg(windows)]
    {
        if let Some(m) = win_by_name_stat(path) {
            if m.file_attributes & 0x400 == 0 {
                return Some(m);
            }
        }
        if let Some(m) = win_fast_stat(path) {
            if m.file_attributes & 0x400 == 0 {
                return Some(m);
            }
        }
    }
    let md = fs::metadata(Path::new(path)).ok()?;
    Some(meta_from(&md))
}

/// `FILE_STAT_BASIC_INFORMATION`（`FILE_INFO_BY_NAME_CLASS = FileStatBasicByNameInfo`）。
///
/// 末尾留 32 字节余量：宁可给多也不能给少——给少了 API 会往缓冲区外面写。
#[cfg(windows)]
#[repr(C)]
#[derive(Clone, Copy, Default)]
struct FileStatBasicInfo {
    file_id: i64,
    creation_time: i64,
    last_access_time: i64,
    last_write_time: i64,
    change_time: i64,
    allocation_size: i64,
    end_of_file: i64,
    file_attributes: u32,
    reparse_tag: u32,
    number_of_links: u32,
    device_type: u32,
    device_characteristics: u32,
    reserved: u32,
    volume_serial_number: i64,
    file_id_128: [u8; 16],
    _pad: [u8; 32],
}

#[cfg(windows)]
type GetFileInfoByName = unsafe extern "system" fn(*const u16, i32, *mut core::ffi::c_void, u32) -> i32;

/// 运行时解析 `GetFileInformationByName`；系统不支持时返回 None。
#[cfg(windows)]
fn by_name_proc() -> Option<GetFileInfoByName> {
    use std::sync::OnceLock;
    use windows_sys::Win32::System::LibraryLoader::{GetModuleHandleW, GetProcAddress};
    static FN: OnceLock<Option<GetFileInfoByName>> = OnceLock::new();
    *FN.get_or_init(|| unsafe {
        let name: Vec<u16> = "kernel32.dll\0".encode_utf16().collect();
        let module = GetModuleHandleW(name.as_ptr());
        if module.is_null() {
            return None;
        }
        let sym = b"GetFileInformationByName\0";
        let proc = GetProcAddress(module, sym.as_ptr() as *const u8)?;
        Some(std::mem::transmute::<
            unsafe extern "system" fn() -> isize,
            GetFileInfoByName,
        >(proc))
    })
}

/// 用 `GetFileInformationByName` 取元数据。非绝对路径直接放弃（该 API 要求全路径）。
#[cfg(windows)]
fn win_by_name_stat(path: &str) -> Option<Meta> {
    if !pathops::isabs(path) {
        return None;
    }
    let func = by_name_proc()?;
    if !self_check_ok(&func) {
        return None;
    }
    let wide: Vec<u16> = path.encode_utf16().chain(std::iter::once(0)).collect();
    let mut info = FileStatBasicInfo::default();
    let ok = unsafe {
        func(
            wide.as_ptr(),
            0, // FileStatBasicByNameInfo
            &mut info as *mut FileStatBasicInfo as *mut core::ffi::c_void,
            std::mem::size_of::<FileStatBasicInfo>() as u32,
        )
    };
    if ok == 0 {
        return None;
    }
    let is_dir = info.file_attributes & 0x10 != 0;
    let readonly = info.file_attributes & 0x1 != 0;
    Some(Meta {
        mode: if is_dir {
            0o40000 | 0o777
        } else {
            0o100000 | if readonly { 0o444 } else { 0o666 }
        },
        ino: 0,
        dev: 0,
        nlink: if info.number_of_links == 0 { 1 } else { info.number_of_links as u64 },
        uid: 0,
        gid: 0,
        size: info.end_of_file,
        atime: filetime_to_unix(info.last_access_time as u64),
        mtime: filetime_to_unix(info.last_write_time as u64),
        ctime: filetime_to_unix(info.creation_time as u64),
        file_attributes: info.file_attributes,
    })
}

/// 首次使用前拿同一个文件跟 `GetFileAttributesExW` 对一次。
///
/// 结构体布局是我照着文档写的，万一哪天 Windows 改了布局，宁可退到慢的那级，
/// 也不要拿错位的数字去改用户文件名。
#[cfg(windows)]
fn self_check_ok(func: &GetFileInfoByName) -> bool {
    use std::sync::OnceLock;
    static OK: OnceLock<bool> = OnceLock::new();
    *OK.get_or_init(|| {
        let exe = match std::env::current_exe() {
            Ok(p) => p.to_string_lossy().into_owned(),
            Err(_) => return false,
        };
        let wide: Vec<u16> = exe.encode_utf16().chain(std::iter::once(0)).collect();
        let mut info = FileStatBasicInfo::default();
        let ok = unsafe {
            func(
                wide.as_ptr(),
                0,
                &mut info as *mut FileStatBasicInfo as *mut core::ffi::c_void,
                std::mem::size_of::<FileStatBasicInfo>() as u32,
            )
        };
        if ok == 0 {
            return false;
        }
        let expected = match win_fast_stat(&exe) {
            Some(m) => m,
            None => return false,
        };
        info.end_of_file == expected.size
            && info.file_attributes == expected.file_attributes
            && filetime_to_unix(info.last_write_time as u64) == expected.mtime
    })
}

/// `GetFileAttributesExW`：老系统（无 `GetFileInformationByName`）的兜底。
#[cfg(windows)]
fn win_fast_stat(path: &str) -> Option<Meta> {
    use windows_sys::Win32::Storage::FileSystem::{
        GetFileAttributesExW, GetFileExInfoStandard, WIN32_FILE_ATTRIBUTE_DATA,
    };
    let wide: Vec<u16> = path.encode_utf16().chain(std::iter::once(0)).collect();
    let mut data: WIN32_FILE_ATTRIBUTE_DATA = unsafe { std::mem::zeroed() };
    let ok = unsafe {
        GetFileAttributesExW(
            wide.as_ptr(),
            GetFileExInfoStandard,
            &mut data as *mut WIN32_FILE_ATTRIBUTE_DATA as *mut core::ffi::c_void,
        )
    };
    if ok == 0 {
        return None;
    }
    let attrs = data.dwFileAttributes;
    let is_dir = attrs & 0x10 != 0; // FILE_ATTRIBUTE_DIRECTORY
    let readonly = attrs & 0x1 != 0;
    let size = ((data.nFileSizeHigh as u64) << 32) | data.nFileSizeLow as u64;
    let conv = |ft: windows_sys::Win32::Foundation::FILETIME| -> f64 {
        filetime_to_unix(((ft.dwHighDateTime as u64) << 32) | ft.dwLowDateTime as u64)
    };
    Some(Meta {
        mode: if is_dir {
            0o40000 | 0o777
        } else {
            0o100000 | if readonly { 0o444 } else { 0o666 }
        },
        ino: 0,
        dev: 0,
        nlink: 1,
        uid: 0,
        gid: 0,
        size: size as i64,
        atime: conv(data.ftLastAccessTime),
        mtime: conv(data.ftLastWriteTime),
        ctime: conv(data.ftCreationTime),
        file_attributes: attrs,
    })
}

fn meta_from(md: &fs::Metadata) -> Meta {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        let attrs = md.file_attributes();
        let readonly = attrs & 0x1 != 0;
        let mode = if md.is_dir() {
            0o40000 | 0o777
        } else {
            0o100000 | if readonly { 0o444 } else { 0o666 }
        };
        Meta {
            mode,
            // st_ino / st_dev / st_nlink 在 Windows 上要额外的
            // GetFileInformationByHandle，且全项目无人读取（只用于 stat_result
            // 的字段形状），这里一律给 0/1，省掉一次系统调用
            ino: 0,
            dev: 0,
            nlink: 1,
            uid: 0,
            gid: 0,
            size: md.file_size() as i64,
            atime: filetime_to_unix(md.last_access_time()),
            mtime: filetime_to_unix(md.last_write_time()),
            ctime: filetime_to_unix(md.creation_time()),
            file_attributes: attrs,
        }
    }
    #[cfg(not(windows))]
    {
        use std::os::unix::fs::MetadataExt;
        Meta {
            mode: md.mode(),
            ino: md.ino(),
            dev: md.dev(),
            nlink: md.nlink(),
            uid: md.uid(),
            gid: md.gid(),
            size: md.size() as i64,
            atime: md.atime() as f64,
            mtime: md.mtime() as f64,
            ctime: md.ctime() as f64,
            file_attributes: 0,
        }
    }
}

// --------------------------------------------------------------------------- //
// 小工具
// --------------------------------------------------------------------------- //

/// 自然序键：`img2` 排在 `img10` 前。
///
/// 数字段用**十进制字符串**比较（先比位数再比字典序），等价于整数比较但不溢出。
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum KeyPart {
    /// 文本段：小写后比较
    Text(String),
    /// 数字段：已去掉前导零（至少保留一位）
    Num(String),
}

/// 文本段始终排在数字段之前（对应 Python 键里的第一个元素 0 / 1）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NKey(pub Vec<KeyPart>);

impl PartialOrd for NKey {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for NKey {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        use std::cmp::Ordering;
        // 逐段比较；文本段（0）永远小于数字段（1）
        let n = self.0.len().min(other.0.len());
        for i in 0..n {
            let ord = match (&self.0[i], &other.0[i]) {
                (KeyPart::Text(a), KeyPart::Text(b)) => a.cmp(b),
                (KeyPart::Num(a), KeyPart::Num(b)) => cmp_number(a, b),
                (KeyPart::Text(_), KeyPart::Num(_)) => Ordering::Less,
                (KeyPart::Num(_), KeyPart::Text(_)) => Ordering::Greater,
            };
            if ord != Ordering::Equal {
                return ord;
            }
        }
        self.0.len().cmp(&other.0.len())
    }
}

/// 无前导零的十进制串比较 = 整数比较。
fn cmp_number(a: &str, b: &str) -> std::cmp::Ordering {
    let (a, b) = (a.trim_start_matches('0'), b.trim_start_matches('0'));
    let (a, b) = (if a.is_empty() { "0" } else { a }, if b.is_empty() { "0" } else { b });
    a.len().cmp(&b.len()).then_with(|| a.cmp(b))
}

/// 自然序键：`img2` 排在 `img10` 前。
///
/// 只把 ASCII 数字当数字段——Python 的 `\d` 还认阿拉伯-印度数字等 Unicode
/// 十进制数，但那些字符做文件名序时按文本处理同样稳定，且极罕见。
pub fn natural_key(text: &str) -> NKey {
    let mut parts = Vec::new();
    let mut buf = String::new();
    let mut digits = String::new();
    let flush_text = |buf: &mut String, parts: &mut Vec<KeyPart>| {
        if !buf.is_empty() {
            parts.push(KeyPart::Text(buf.to_lowercase()));
            buf.clear();
        }
    };
    for ch in text.chars() {
        if ch.is_ascii_digit() {
            flush_text(&mut buf, &mut parts);
            digits.push(ch);
        } else {
            if !digits.is_empty() {
                parts.push(KeyPart::Num(digits.trim_start_matches('0').to_string()));
                digits.clear();
            }
            buf.push(ch);
        }
    }
    if !digits.is_empty() {
        parts.push(KeyPart::Num(digits.trim_start_matches('0').to_string()));
    }
    flush_text(&mut buf, &mut parts);
    NKey(parts)
}

/// `path_key` 是否落在 `base_key` 之下（含自身）。两个键都应是 `nkey` 归一化过的。
pub fn under(path_key: &str, base_key: &str) -> bool {
    !base_key.is_empty()
        && (path_key == base_key || path_key.starts_with(&format!("{base_key}\\")))
}

/// `key` 的某个祖先目录是否在 `keys` 里（逐步上溯，每层一次哈希查找）。
///
/// 这是 `selected` 语义的"条目属于某个被选中的子树"判定。以前是拿整个选中
/// 集合逐个 `under()` 比较（O(n)），拖进上万个文件时扫描会退化成二次复杂度。
fn ancestor_in(key: &str, keys: &HashSet<String>) -> bool {
    let mut parent = pathops::dirname(key);
    while !parent.is_empty() {
        if keys.contains(&parent) {
            return true;
        }
        let nxt = pathops::dirname(&parent);
        if nxt == parent {
            return false;
        }
        parent = nxt;
    }
    false
}

/// 子条目的路径键：父目录键 + 名字（名字同样小写）。
///
/// 不能直接对每个条目调 `nkey()` —— 它内部走 `abspath`（GetFullPathNameW），
/// 9000 个条目要 100ms；而父目录键已经是归一化过的绝对路径，拼一下就够了。
fn child_key(dir_key: &str, name: &str) -> String {
    let low = pathops::normcase(name);
    if dir_key.is_empty() {
        return low;
    }
    let mut out = String::with_capacity(dir_key.len() + low.len() + 1);
    out.push_str(dir_key);
    if !dir_key.ends_with('\\') && !dir_key.ends_with('/') {
        out.push('\\');
    }
    out.push_str(&low);
    out
}

/// 选中集合里所有目录项的祖先前缀集（含选中项自身所在目录的上层）。
///
/// 用来在 `walk_dir` 里 O(1) 回答"这个目录下面还有没有选中项"。
fn selected_ancestors(keys: &HashSet<String>) -> HashSet<String> {
    let mut set: HashSet<String> = HashSet::with_capacity(keys.len() * 2);
    for k in keys {
        let mut cur = pathops::dirname(k);
        while !cur.is_empty() {
            if !set.insert(cur.clone()) {
                break;      // 上层已插入过，再往上必然也插过了
            }
            let nxt = pathops::dirname(&cur);
            if nxt == cur {
                break;
            }
            cur = nxt;
        }
    }
    set
}

/// 路径归一化键：Windows 大小写不敏感。
pub fn nkey(path: &str) -> String {
    pathops::normcase(&pathops::abspath(path))
}

pub fn is_hidden_name(name: &str) -> bool {
    name.starts_with('.')
}

pub fn is_hidden_attrs(attrs: u32) -> bool {
    attrs & 0x2 != 0 || attrs & 0x4 != 0
}

/// 类型判定。`image_exts` / `video_exts` 为 None 时用内置表。
pub fn kind_of(
    name: &str,
    is_dir: bool,
    image_exts: Option<&HashSet<String>>,
    video_exts: Option<&HashSet<String>>,
) -> String {
    if is_dir {
        return KIND_DIR.to_string();
    }
    let low = pathops::splitext(name).1.to_lowercase();
    let is_image = match image_exts {
        Some(set) => set.contains(&low),
        None => IMAGE_EXTS.contains(&low.as_str()),
    };
    if is_image {
        return KIND_IMAGE.to_string();
    }
    let is_video = match video_exts {
        Some(set) => set.contains(&low),
        None => VIDEO_EXTS.contains(&low.as_str()),
    };
    if is_video {
        return KIND_VIDEO.to_string();
    }
    KIND_FILE.to_string()
}

/// 校验文件头。无法判断时一律放行，避免误伤 PSD/RAW 等未收录格式。
pub fn check_file_magic(path: &str, ext: &str) -> bool {
    let ext = ext.to_lowercase();
    let Some(spec) = magic_table().into_iter().find(|(e, _)| *e == ext) else {
        return true;
    };
    let mut head = [0u8; 16];
    use std::io::Read;
    let read = match fs::File::open(path) {
        Ok(mut fh) => fh.read(&mut head).unwrap_or(0),
        Err(_) => return true,
    };
    let head = &head[..read];
    if head.is_empty() {
        return false;
    }
    match spec.1 {
        None => {
            if ext == ".webp" {
                return head.len() >= 12 && &head[..4] == b"RIFF" && &head[8..12] == b"WEBP";
            }
            if ext == ".avi" {
                return head.len() >= 12 && &head[..4] == b"RIFF" && &head[8..12] == b"AVI ";
            }
            head.len() >= 8 && &head[4..8] == b"ftyp"
        }
        Some(sigs) => sigs.iter().any(|sig| head.starts_with(sig)),
    }
}

// --------------------------------------------------------------------------- //
// 扫描选项
// --------------------------------------------------------------------------- //

#[derive(Clone, Debug)]
pub struct ScanOptions {
    pub root: String,
    pub kinds: Vec<String>,
    pub recursive: bool,
    pub include_hidden: bool,
    pub only_match: String,
    pub exclude_match: String,
    pub include_exts: Option<Vec<String>>,
    pub exclude_exts: Option<Vec<String>>,
    pub verify_content: bool,
    pub sort_mode: String,
    pub max_depth: i64,
    pub follow_log_dirs: bool,
    /// 勾选的路径集合（**原始**路径，函数内部统一做 nkey 归一化）；None = 全部
    pub selected: Option<Vec<String>>,
    pub strict_selected: bool,
}

impl Default for ScanOptions {
    fn default() -> Self {
        Self {
            root: String::new(),
            kinds: vec![KIND_FILE.to_string()],
            recursive: false,
            include_hidden: false,
            only_match: String::new(),
            exclude_match: String::new(),
            include_exts: None,
            exclude_exts: None,
            verify_content: false,
            sort_mode: "path_asc".to_string(),
            max_depth: MAX_DEPTH,
            follow_log_dirs: false,
            selected: None,
            strict_selected: false,
        }
    }
}

impl ScanOptions {
    /// 该条目要不要收；要收则返回它的 kind。
    pub fn wanted_kind(&self, name: &str, is_dir: bool) -> Option<String> {
        let k = kind_of(name, is_dir, None, None);
        if self.kinds.is_empty() {
            return Some(k);
        }
        if self.kinds.iter().any(|x| *x == k) {
            return Some(k);
        }
        // 选「文件」时图片/视频也算文件；选图片/视频时不额外收普通文件
        if (k == KIND_IMAGE || k == KIND_VIDEO) && self.kinds.iter().any(|x| x == KIND_FILE) {
            return Some(k);
        }
        None
    }
}

// --------------------------------------------------------------------------- //
// 扫描结果
// --------------------------------------------------------------------------- //

/// 一个扫描到的条目（还没被包装成 `Item`）。
#[derive(Clone, Debug)]
pub struct ScanItem {
    pub path: String,
    pub name: String,
    pub is_dir: bool,
    pub kind: String,
    pub meta: Option<Meta>,
}

#[derive(Clone, Debug, Default)]
pub struct ScanResult {
    pub items: Vec<ScanItem>,
    pub skipped: usize,
    pub dirs: usize,
    pub files: usize,
}

impl ScanResult {
    pub fn total(&self) -> usize {
        self.items.len()
    }
}

/// 扫描目标目录，按 kind 过滤并排序。
///
/// `opts.selected` 非空时语义是"只要这些条目"（界面的"扫描对象"与拖放都靠它）：
/// - 选中的目录代表它**里面的整棵子树**（目录自身不算，只有其内容）；
/// - 位于 root 之下但更深的选中项，会自动下探到它们所在目录——否则关掉
///   "包含子文件夹"时，从子目录里拖进来的条目会被静默丢掉；
/// - 不在 root 之下的选中项（例如跨盘符拖入）会各自作为扫描起点，不会被忽略；
/// - 一旦有选中项在 root 之外，"只要这些"会放宽成"root 里全部 + 这些"。
pub fn scan(opts: &ScanOptions, progress: &mut dyn FnMut(&str)) -> ScanResult {
    let root = if opts.root.is_empty() {
        String::new()
    } else {
        pathops::abspath(&opts.root)
    };
    let mut res = ScanResult::default();
    let root_key = if root.is_empty() { String::new() } else { nkey(&root) };

    let selected_keys: Option<HashSet<String>> = opts
        .selected
        .as_ref()
        .map(|sel| sel.iter().map(|p| nkey(p)).collect::<HashSet<String>>());

    // 扫描起点 = root + 不在 root 之下的选中项（跨盘符拖入也能扫到）
    let mut roots: Vec<String> = Vec::new();
    if !root.is_empty() && Path::new(&root).is_dir() {
        roots.push(root.clone());
    }
    if let Some(keys) = &selected_keys {
        let mut sorted: Vec<&String> = keys.iter().collect();
        sorted.sort();
        for path in sorted {
            if !root_key.is_empty() && under(&nkey(path), &root_key) {
                continue; // root 之内，交给下探逻辑处理
            }
            let base = if Path::new(path).is_dir() {
                path.clone()
            } else {
                pathops::dirname(path)
            };
            if !base.is_empty() && Path::new(&base).is_dir() {
                roots.push(base);
            }
        }
    }
    if roots.is_empty() {
        return res;
    }

    // 选中的条目里只要有一个在 root 之外（例如从别的文件夹拖进来），
    // 语义就从"只要这些"变成"root 全都要 + 额外这些"。
    let mixed_scope = match (&selected_keys, root_key.is_empty()) {
        (Some(keys), false) => {
            !opts.strict_selected && keys.iter().any(|key| !under(key, &root_key))
        }
        _ => false,
    };

    let mut skipped = 0usize;
    let mut dirs_n = 0usize;
    let mut files_n = 0usize;
    let include_exts: Option<HashSet<String>> = opts
        .include_exts
        .as_ref()
        .map(|v| v.iter().map(|s| s.to_lowercase()).collect());
    let exclude_exts: Option<HashSet<String>> = opts
        .exclude_exts
        .as_ref()
        .map(|v| v.iter().map(|s| s.to_lowercase()).collect());

    // 递归主体。用显式栈太绕（要保留"先收本层再下探"的顺序），这里用内部递归函数。
    struct Walker<'a> {
        opts: &'a ScanOptions,
        include_exts: &'a Option<HashSet<String>>,
        exclude_exts: &'a Option<HashSet<String>>,
        res_items: &'a mut Vec<ScanItem>,
        skipped: &'a mut usize,
        dirs_n: &'a mut usize,
        files_n: &'a mut usize,
        log_skip: Vec<String>,
        progress: &'a mut dyn FnMut(&str),
    }

    impl Walker<'_> {
        fn take(&mut self, path: &str, name: &str, is_dir: bool) {
            let low = if is_dir {
                String::new()
            } else {
                pathops::splitext(name).1.to_lowercase()
            };
            if let Some(inc) = self.include_exts {
                if !is_dir && !inc.contains(&low) {
                    return;
                }
            }
            if let Some(exc) = self.exclude_exts {
                if !low.is_empty() && exc.contains(&low) {
                    return;
                }
            }
            if !self.opts.only_match.is_empty() && !name.contains(&self.opts.only_match) {
                return;
            }
            if !self.opts.exclude_match.is_empty() && name.contains(&self.opts.exclude_match) {
                return;
            }
            let Some(kind) = self.opts.wanted_kind(name, is_dir) else {
                return;
            };
            if !self.opts.include_hidden && is_hidden_name(name) {
                return;
            }
            let Some(meta) = meta_of(path) else { return };
            if is_dir && meta.mode & 0o170000 != 0o040000 {
                return;
            }
            if !is_dir && meta.mode & 0o170000 != 0o100000 {
                return;
            }
            if !self.opts.include_hidden && is_hidden_attrs(meta.file_attributes) {
                return;
            }
            if self.opts.verify_content && !is_dir && !check_file_magic(path, &low) {
                *self.skipped += 1;
                return;
            }
            self.res_items.push(ScanItem {
                path: path.to_string(),
                name: name.to_string(),
                is_dir,
                kind,
                meta: Some(meta),
            });
            if is_dir {
                *self.dirs_n += 1;
            } else {
                *self.files_n += 1;
            }
        }

        /// `deep=true` 表示身处"选中的子树/起点"内，此时不受递归开关与深度上限约束。
        fn walk_dir(
            &mut self,
            dir_path: &str,
            dir_key: &str,
            depth: i64,
            deep: bool,
            selected: &Option<HashSet<String>>,
            mixed_scope: bool,
            root_key: &str,
            ancestors: Option<&HashSet<String>>,
        ) {
            let Ok(entries) = fs::read_dir(dir_path) else {
                return;
            };
            let entries: Vec<fs::DirEntry> = entries.filter_map(|e| e.ok()).collect();
            (self.progress)(dir_path);
            let mut dirs_kept: Vec<(String, String, String)> = Vec::new();
            for ent in &entries {
                // is_dir(follow_symlinks=False)：符号链接到目录不算目录
                let Ok(ft) = ent.file_type() else { continue };
                let is_dir = ft.is_dir();
                let name = ent.file_name().to_string_lossy().into_owned();
                let path = ent.path().to_string_lossy().into_owned();
                // 键由「父目录键 + 名」直接拼出来，不再对每个条目跑一次
                // nkey()（abspath 会走 GetFullPathNameW：9000 个条目要 100ms）
                let key = child_key(dir_key, &name);
                if is_dir {
                    let low = name.to_lowercase();
                    if self.log_skip.contains(&low) && !self.opts.follow_log_dirs {
                        continue;
                    }
                    dirs_kept.push((path.clone(), name.clone(), key.clone()));
                }
                self.take_selected(path, key, name, is_dir, selected, deep,
                                   mixed_scope, root_key);
            }
            dirs_kept.sort_by(|a, b| natural_key(&a.1).cmp(&natural_key(&b.1)));
            let recursive_ok = self.opts.recursive;
            let max_depth = self.opts.max_depth;
            // 选中集合的**祖先目录集**（scan() 里算一次，这里只读）：
            // 「这个目录下面还有没有选中项」= child_key 在祖先集里，O(1)。
            // 旧写法拿 9000 个选中键逐个 under()，是「目录数 × 选中数」的
            // 二次复杂度：拖进一个上万文件的目录时实测 680ms。
            let ancestors = ancestors;
            for (child_path, _name, child_key) in dirs_kept {
                let (sel_tree, sel_below) = match (selected, ancestors) {
                    (Some(keys), Some(anc)) => {
                        // sel_tree：自己就是选中项，或自己落在某个选中目录之下
                        //（= 某个祖先在选中集合里，按路径深度查几次哈希即可）
                        let selected_self = keys.contains(&child_key);
                        let under_selected_dir =
                            ancestor_in(&child_key, keys);
                        (selected_self || under_selected_dir, anc.contains(&child_key))
                    }
                    _ => (false, false),
                };
                let child_deep = deep || sel_tree;
                if (recursive_ok && depth < max_depth) || child_deep || sel_below {
                    self.walk_dir(&child_path, &child_key, depth + 1, child_deep,
                                  selected, mixed_scope, root_key, ancestors);
                }
            }
        }

        fn take_selected(
            &mut self,
            path: String,
            key: String,
            name: String,
            is_dir: bool,
            selected: &Option<HashSet<String>>,
            deep: bool,
            mixed_scope: bool,
            root_key: &str,
        ) {
            // 已经身处"选中的子树 / 起点"内部时，里面的一切都是要的 ——
            // 不必再按 selected 过滤一遍。旧写法在这里对每个条目都做一次
            // nkey()+上溯（选中项命中时还要 Path::is_dir() 再 stat 一次），
            // 拖进上万个文件时就是纯粹的浪费：实测 9000 个文件 733ms，
            // 而同样的目录整扫只要 133ms。
            if let Some(keys) = selected {
                if !deep {
                    // 语义：选中的文件 = 就要它；选中的目录 = 要它里面的内容
                    // （目录自身不算），子树由 deep 覆盖。is_dir 是现成的，别再 stat。
                    let ok = if keys.contains(&key) {
                        !is_dir
                    } else {
                        ancestor_in(&key, keys)
                    };
                    if !ok {
                        let widened = mixed_scope && under(&key, root_key);
                        if !widened {
                            return;
                        }
                    }
                }
            }
            self.take(&path, &name, is_dir);
        }
    }

    let mut walker = Walker {
        opts,
        include_exts: &include_exts,
        exclude_exts: &exclude_exts,
        res_items: &mut res.items,
        skipped: &mut skipped,
        dirs_n: &mut dirs_n,
        files_n: &mut files_n,
        log_skip: log_dir_skip(),
        progress,
    };

    let mut started: Vec<String> = Vec::new();
    // 祖先目录集只算一次；只在真有选中项时才需要
    let ancestors: Option<HashSet<String>> = selected_keys.as_ref().map(selected_ancestors);
    for start in &roots {
        let start_key = nkey(start);
        if started.iter().any(|seen| under(&start_key, &nkey(seen))) {
            continue; // 已经包含在更上层的起点里
        }
        started.push(start.clone());
        // 从 root 出发时递归与否听用户的；从"root 之外的选中目录"出发时，
        // 那是用户明确拖进来的完整子树，一律扫全。
        let deep = !(root_key.is_empty() || under(&start_key, &root_key));
        walker.walk_dir(start, &start_key, 0, deep, &selected_keys, mixed_scope,
                        &root_key, ancestors.as_ref());
    }

    res.skipped = skipped;
    res.dirs = dirs_n;
    res.files = files_n;
    sort_items(&mut res.items, &opts.sort_mode);
    res
}

/// 排序用的最小信息（绑定层可以拿 `Item` 现拼，不必造 `ScanItem`）。
pub struct SortItem<'a> {
    pub name: &'a str,
    pub path: &'a str,
    pub meta: Option<&'a Meta>,
}

/// 算出排序后的下标序列。
///
/// 先排次级键（名称）再排主键，稳定排序保证可复现——Python 版就是两次
/// `list.sort(key=...)`，`key=` 会**先算好每项一次**再排；这里同样先把键
/// 全部算进数组再比较。别写成"在比较函数里现算 `natural_key`"：
/// 那会变成 O(n log n) 次分配，2000 项实测从 6 ms 涨到 45 ms。
pub fn sort_indices(items: &[SortItem], mode: &str) -> Vec<usize> {
    let mode = if mode.is_empty() { "path_asc" } else { mode };
    let n = items.len();
    let mut order: Vec<usize> = (0..n).collect();
    if mode == "none" {
        return order;
    }
    let reverse = mode.ends_with("_desc");
    let base = mode.rsplit_once('_').map(|(b, _)| b).unwrap_or(mode);
    // 第一趟：按名称升序
    let name_keys: Vec<NKey> = items.iter().map(|it| natural_key(it.name)).collect();
    order.sort_by(|&a, &b| name_keys[a].cmp(&name_keys[b]));

    // 第二趟：主键（稳定排序 → 同键保持第一趟的顺序）
    let val = |it: &SortItem, which: &str| -> f64 {
        let m = it.meta;
        match which {
            "mtime" => m.map(|m| m.mtime).unwrap_or(0.0),
            "ctime" => m.map(|m| m.ctime).unwrap_or(0.0),
            _ => m.map(|m| m.size as f64).unwrap_or(0.0),
        }
    };
    match base {
        "name" => {
            if reverse {
                order.sort_by(|&a, &b| name_keys[b].cmp(&name_keys[a]));
            }
        }
        "mtime" | "ctime" | "size" => {
            let vals: Vec<f64> = items.iter().map(|it| val(it, base)).collect();
            order.sort_by(|&a, &b| {
                let ord = vals[a].partial_cmp(&vals[b]).unwrap_or(std::cmp::Ordering::Equal);
                if reverse { ord.reverse() } else { ord }
            });
        }
        "path" => {
            let dir_keys: Vec<NKey> = items
                .iter()
                .map(|it| natural_key(&pathops::dirname(it.path)))
                .collect();
            order.sort_by(|&a, &b| {
                let ord = dir_keys[a]
                    .cmp(&dir_keys[b])
                    .then_with(|| name_keys[a].cmp(&name_keys[b]));
                if reverse { ord.reverse() } else { ord }
            });
        }
        _ => {} // 未知排序键：只保留"按名称"这一趟（与 Python 的 no-op 分支一致）
    }
    order
}

/// 原地排序。
pub fn sort_items(items: &mut [ScanItem], mode: &str) {
    let refs: Vec<SortItem> = items
        .iter()
        .map(|it| SortItem { name: &it.name, path: &it.path, meta: it.meta.as_ref() })
        .collect();
    let order = sort_indices(&refs, mode);
    let mut taken: Vec<Option<ScanItem>> = items.iter().cloned().map(Some).collect();
    for (dst, src) in order.into_iter().enumerate() {
        items[dst] = taken[src].take().expect("排序下标不应重复");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn natural_order_basics() {
        let mut v = vec!["img10", "img2", "img1"];
        v.sort_by(|a, b| natural_key(a).cmp(&natural_key(b)));
        assert_eq!(v, vec!["img1", "img2", "img10"]);
    }

    #[test]
    fn natural_order_handles_big_numbers() {
        // 超出 i64 的号码段也要按数值比较（这里靠"先比位数"实现）
        let a = natural_key("x9999999999999999999999");
        let b = natural_key("x10000000000000000000000");
        assert!(a < b);
    }

    #[test]
    fn natural_order_text_before_numbers() {
        assert!(natural_key("a1") < natural_key("1a"));
    }

    #[test]
    fn hidden_and_kind() {
        assert!(is_hidden_name(".git"));
        assert!(!is_hidden_name("a.git"));
        assert_eq!(kind_of("a.JPG", false, None, None), KIND_IMAGE);
        assert_eq!(kind_of("a.mp4", false, None, None), KIND_VIDEO);
        assert_eq!(kind_of("a.txt", false, None, None), KIND_FILE);
        assert_eq!(kind_of("a", true, None, None), KIND_DIR);
        // os.path.splitext 忽略开头的点：".jpg" 没有扩展名
        assert_eq!(kind_of(".jpg", false, None, None), KIND_FILE);
    }
}
