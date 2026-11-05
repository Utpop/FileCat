//! Windows 路径语义 —— `ntpath` 的忠实移植。
//!
//! 为什么不直接用 `std::path`：批量改名里所有"同不同一个文件""是不是父子目录"
//! 的判断都建立在 `os.path` 的具体输出上（`normcase` / `abspath` / `relpath` /
//! `commonpath`），而这些函数的边界行为（盘符相对路径 `C:foo`、UNC、
//! `\\?\` 设备前缀、结尾空格与点、`.`/`..` 的折叠）并非"显然正确"，
//! 一旦和 Python 版不一致，结果就是**别的文件被改名**。所以这里逐函数对齐，
//! 并用 `tests/path_semantics.rs` + `tests/data/path_cases.json`（由
//! `tools/gen_path_cases.py` 从真 Python 采集）钉住。
//!
//! 所有函数按**码点**下标工作（与 Python 的 `str` 索引一致），
//! 内部先转 `Vec<char>`，不直接按字节切。

/// 主分隔符（Windows）。
pub const SEP: char = '\\';

/// 分隔符判定：`os.sep` 与 `os.altsep` 都算。
pub fn is_sep(c: char) -> bool {
    c == '\\' || c == '/'
}

fn chars(s: &str) -> Vec<char> {
    s.chars().collect()
}

fn to_string(v: &[char]) -> String {
    v.iter().collect()
}

/// `nt.splitroot`：拆成 (drive, root, tail)，三者拼回去等于原串。
///
/// root 保留原字符（输入写的是 `/` 就还是 `/`），只有 `normpath` 才会统一。
pub fn splitroot(p: &str) -> (String, String, String) {
    let p = chars(p);
    // normp 只用于"哪里是分隔符"的判断，长度与 p 一致（替换 1:1）
    let normp: Vec<char> = p
        .iter()
        .map(|&c| if c == '/' { '\\' } else { c })
        .collect();
    let at = |i: usize| normp.get(i).copied();

    if at(0) == Some('\\') {
        if at(1) == Some('\\') {
            // UNC（\\server\share）或设备路径（\\.\dev、\\?\C:）
            let unc: String = normp.iter().take(8).collect::<String>().to_uppercase();
            let start = if unc == "\\\\?\\UNC\\" { 8 } else { 2 };
            let index = find_from(&normp, '\\', start);
            let Some(index) = index else {
                return (to_string(&p), String::new(), String::new());
            };
            let Some(index2) = find_from(&normp, '\\', index + 1) else {
                return (to_string(&p), String::new(), String::new());
            };
            return (
                to_string(&p[..index2]),
                to_string(&p[index2..index2 + 1]),
                to_string(&p[index2 + 1..]),
            );
        }
        // 相对于当前盘符的根，例如 `\Windows`
        return (String::new(), to_string(&p[..1]), to_string(&p[1..]));
    }
    if at(1) == Some(':') {
        if at(2) == Some('\\') {
            // 盘符绝对路径，例如 `C:\Windows`
            return (to_string(&p[..2]), to_string(&p[2..3]), to_string(&p[3..]));
        }
        // 盘符相对路径，例如 `C:Windows`
        return (to_string(&p[..2]), String::new(), to_string(&p[2..]));
    }
    (String::new(), String::new(), to_string(&p))
}

fn find_from(v: &[char], target: char, from: usize) -> Option<usize> {
    if from > v.len() {
        return None;
    }
    v[from..].iter().position(|&c| c == target).map(|i| i + from)
}

/// `os.path.split` → (head, tail)。head 末尾的分隔符会被去掉（根除外）。
pub fn split(p: &str) -> (String, String) {
    let (d, r, tail) = splitroot(p);
    let t = chars(&tail);
    let mut i = t.len();
    while i > 0 && !is_sep(t[i - 1]) {
        i -= 1;
    }
    let head = to_string(&t[..i]);
    let head = head.trim_end_matches(is_sep);
    (format!("{d}{r}{head}"), to_string(&t[i..]))
}

/// `os.path.dirname`。
pub fn dirname(p: &str) -> String {
    split(p).0
}

/// `os.path.basename`。
pub fn basename(p: &str) -> String {
    split(p).1
}

/// `os.path.splitext` → (root, ext)。
///
/// 与 `context.split_ext` **不同**：这里忽略开头的连续点，
/// `".gitignore"` → `(".gitignore", "")`。
pub fn splitext(p: &str) -> (String, String) {
    let v = chars(p);
    let sep = v.iter().rposition(|&c| c == '\\');
    let altsep = v.iter().rposition(|&c| c == '/');
    let sep_index = match (sep, altsep) {
        (Some(a), Some(b)) => Some(a.max(b)),
        (a, b) => a.or(b),
    };
    let sep_index = sep_index.map(|i| i as i64).unwrap_or(-1);
    let dot_index = v.iter().rposition(|&c| c == '.').map(|i| i as i64);
    if let Some(dot) = dot_index {
        if dot > sep_index {
            let mut filename_index = (sep_index + 1) as usize;
            while filename_index < dot as usize {
                if v[filename_index] != '.' {
                    return (to_string(&v[..dot as usize]), to_string(&v[dot as usize..]));
                }
                filename_index += 1;
            }
        }
    }
    (p.to_string(), String::new())
}

/// `os.path.join`（至少一个参数）。
pub fn join(parts: &[&str]) -> String {
    let Some((first, rest)) = parts.split_first() else {
        return String::new();
    };
    let (mut rdrive, mut rroot, mut rpath) = splitroot(first);
    for p in rest {
        let (pdrive, proot, ppath) = splitroot(p);
        if !proot.is_empty() {
            // 绝对路径：前面的全作废
            if !pdrive.is_empty() || rdrive.is_empty() {
                rdrive = pdrive;
            }
            rroot = proot;
            rpath = ppath;
            continue;
        } else if !pdrive.is_empty() && pdrive != rdrive {
            if pdrive.to_lowercase() != rdrive.to_lowercase() {
                // 不同盘符：忽略前面的部分
                rdrive = pdrive;
                rroot = proot;
                rpath = ppath;
                continue;
            }
            rdrive = pdrive;
        }
        if !rpath.is_empty() && !rpath.ends_with(is_sep) {
            rpath.push(SEP);
        }
        rpath.push_str(&ppath);
    }
    // UNC 与非绝对路径之间要补一个分隔符
    if !rpath.is_empty()
        && rroot.is_empty()
        && !rdrive.is_empty()
        && !rdrive.ends_with([':', '\\', '/'])
    {
        return format!("{rdrive}{SEP}{rpath}");
    }
    format!("{rdrive}{rroot}{rpath}")
}

/// `os.path.normpath`：折叠 `.`/`..`、合并重复分隔符、统一成 `\`。
pub fn normpath(p: &str) -> String {
    let p: String = p.chars().map(|c| if c == '/' { SEP } else { c }).collect();
    let (drive, root, tail) = splitroot(&p);
    let prefix = format!("{drive}{root}");
    let mut comps: Vec<String> = tail.split(SEP).map(|s| s.to_string()).collect();
    let mut i = 0usize;
    while i < comps.len() {
        if comps[i].is_empty() || comps[i] == "." {
            comps.remove(i);
        } else if comps[i] == ".." {
            if i > 0 && comps[i - 1] != ".." {
                comps.remove(i);
                comps.remove(i - 1);
                i -= 1;
            } else if i == 0 && !root.is_empty() {
                comps.remove(i);
            } else {
                i += 1;
            }
        } else {
            i += 1;
        }
    }
    if prefix.is_empty() && comps.is_empty() {
        comps.push(".".to_string());
    }
    format!("{prefix}{}", comps.join("\\"))
}

/// `os.path.normcase`：`\` 统一 + 小写。
///
/// **必须走 Win32 的 `LCMapStringEx`**，不能图省事用 `str::to_lowercase`：
/// 后者是"完整 Unicode 小写映射"，而 Windows 用的是简单映射，两者在
/// `İ`（U+0130）、`ǅ`（U+01C5）、希腊词尾 `Σ` 上结论不同（实测：
/// `İSTANBUL` → Windows 原样保留 `İ`，Rust 会给 `i̇`；`ΣΑΣ` → Windows 给
/// `σασ`，Rust 给 `σας`）。既然同一条路径在 Python 侧（`os.path.normcase`）
/// 与 Rust 侧（本函数）都要参与"是不是同一个文件"的比较，两边就必须逐字一致。
fn windows_lower(s: &str) -> String {
    #[cfg(windows)]
    {
        use windows_sys::Win32::Globalization::{LCMAP_LOWERCASE, LCMapStringEx};
        if s.is_empty() {
            return String::new();
        }
        // 纯 ASCII 的情况（`IMG_0001.JPG` 这类）不变量区域的小写映射就是
        // ASCII 小写，直接算比过一次 LCMapStringEx 快一个数量级。
        if s.is_ascii() {
            return s.to_ascii_lowercase();
        }
        let src: Vec<u16> = s.encode_utf16().collect();
        // 简单映射不会变长，留一倍余量足够；万一不够会返回 0，走下面的兜底
        let mut buf = vec![0u16; src.len() * 2 + 8];
        let n = unsafe {
            LCMapStringEx(
                std::ptr::null(), // LOCALE_NAME_INVARIANT == ""
                LCMAP_LOWERCASE,
                src.as_ptr(),
                src.len() as i32,
                buf.as_mut_ptr(),
                buf.len() as i32,
                std::ptr::null(),
                std::ptr::null(),
                0,
            )
        };
        if n <= 0 {
            return s.to_lowercase();
        }
        return String::from_utf16_lossy(&buf[..n as usize]);
    }
    #[cfg(not(windows))]
    {
        s.to_lowercase()
    }
}

pub fn normcase(p: &str) -> String {
    // 绝大多数路径里没有 '/'，先省掉那次整串重建
    if p.contains('/') {
        let unified: String = p.chars().map(|c| if c == '/' { SEP } else { c }).collect();
        windows_lower(&unified)
    } else {
        windows_lower(p)
    }
}

/// `os.path.abspath`：`GetFullPathNameW(normpath(p))`。
///
/// 取不到（路径含非法字符等）时退回"拼当前目录再 normpath"，
/// 与 Python 的兜底分支一致。
pub fn abspath(p: &str) -> String {
    let normalized = normpath(p);
    if let Ok(abs) = std::path::absolute(&normalized) {
        if let Some(s) = abs.to_str() {
            return s.to_string();
        }
    }
    normpath(&join(&[&cwd(), &normalized]))
}

fn cwd() -> String {
    std::env::current_dir()
        .ok()
        .and_then(|p| p.to_str().map(|s| s.to_string()))
        .unwrap_or_default()
}

/// `os.path.isabs`。
pub fn isabs(p: &str) -> bool {
    let v: Vec<char> = chars(p);
    let head: String = v
        .iter()
        .take(3)
        .map(|&c| if c == '/' { SEP } else { c })
        .collect();
    let h: Vec<char> = head.chars().collect();
    (h.len() >= 3 && h[1] == ':' && h[2] == '\\') || (h.len() >= 2 && h[0] == '\\' && h[1] == '\\')
}

/// `os.path.relpath`。跨盘符时返回 `None`（Python 抛 `ValueError`）。
pub fn relpath(path: &str, start: &str) -> Option<String> {
    if path.is_empty() {
        return None;
    }
    let start = if start.is_empty() { "." } else { start };
    let start_abs = abspath(start);
    let path_abs = abspath(path);
    let (start_drive, _, start_rest) = splitroot(&start_abs);
    let (path_drive, _, path_rest) = splitroot(&path_abs);
    if normcase(&start_drive) != normcase(&path_drive) {
        return None;
    }
    let start_list: Vec<&str> = if start_rest.is_empty() {
        Vec::new()
    } else {
        start_rest.split(SEP).collect()
    };
    let path_list: Vec<&str> = if path_rest.is_empty() {
        Vec::new()
    } else {
        path_rest.split(SEP).collect()
    };
    let mut i = 0usize;
    while i < start_list.len() && i < path_list.len() {
        if normcase(start_list[i]) != normcase(path_list[i]) {
            break;
        }
        i += 1;
    }
    let mut rel: Vec<String> = Vec::new();
    for _ in i..start_list.len() {
        rel.push("..".to_string());
    }
    for item in &path_list[i..] {
        rel.push(item.to_string());
    }
    if rel.is_empty() {
        return Some(".".to_string());
    }
    Some(rel.join("\\"))
}

/// `os.path.commonpath`。盘符/根不一致时返回 `None`（Python 抛 `ValueError`）。
///
/// 返还的文本保留**第一个**路径的大小写，但比较用的是全小写——照抄原实现。
pub fn commonpath(paths: &[&str]) -> Option<String> {
    if paths.is_empty() {
        return None;
    }
    let lowered: Vec<String> = paths
        .iter()
        .map(|p| p.replace('/', "\\").to_lowercase())
        .collect();
    let split_roots: Vec<(String, String, String)> =
        lowered.iter().map(|p| splitroot(p)).collect();
    let drives: std::collections::BTreeSet<&String> =
        split_roots.iter().map(|(d, _, _)| d).collect();
    if drives.len() != 1 {
        return None;
    }
    let (drive, root, path) = splitroot(&paths[0].replace('/', "\\"));
    let roots: std::collections::BTreeSet<&String> =
        split_roots.iter().map(|(_, r, _)| r).collect();
    if roots.len() != 1 {
        return None;
    }
    let mut common: Vec<String> = path
        .split(SEP)
        .filter(|c| !c.is_empty() && *c != ".")
        .map(|s| s.to_string())
        .collect();
    let split_paths: Vec<Vec<String>> = split_roots
        .iter()
        .map(|(_, _, tail)| {
            tail.split(SEP)
                .filter(|c| !c.is_empty() && *c != ".")
                .map(|s| s.to_string())
                .collect()
        })
        .collect();
    let s1 = split_paths.iter().min()?.clone();
    let s2 = split_paths.iter().max()?.clone();
    let mut keep = s1.len();
    for (i, c) in s1.iter().enumerate() {
        if s2.get(i) != Some(c) {
            keep = i;
            break;
        }
    }
    common.truncate(keep);
    Some(format!("{drive}{root}{}", common.join("\\")))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splitroot_basics() {
        assert_eq!(
            splitroot("C:\\foo"),
            ("C:".into(), "\\".into(), "foo".into())
        );
        assert_eq!(splitroot("C:foo"), ("C:".into(), "".into(), "foo".into()));
        assert_eq!(
            splitroot("\\foo"),
            ("".into(), "\\".into(), "foo".into())
        );
        assert_eq!(
            splitroot("\\\\host\\share\\x"),
            ("\\\\host\\share".into(), "\\".into(), "x".into())
        );
        // 只有一级的 UNC 认不出盘，整串当 drive
        assert_eq!(splitroot("\\\\"), ("\\\\".into(), "".into(), "".into()));
    }

    #[test]
    fn dirname_keeps_drive_roots() {
        assert_eq!(dirname("C:\\foo\\bar.txt"), "C:\\foo");
        assert_eq!(dirname("C:\\foo"), "C:\\");
        assert_eq!(dirname("C:\\"), "C:\\");
        assert_eq!(dirname("C:foo"), "C:");
        assert_eq!(dirname("a"), "");
    }

    #[test]
    fn normpath_collapses() {
        assert_eq!(normpath("C:/a//b/../c"), "C:\\a\\c");
        assert_eq!(normpath("..\\a"), "..\\a");
        assert_eq!(normpath("C:..\\a"), "C:..\\a");
        assert_eq!(normpath(""), ".");
        assert_eq!(normpath("C:////"), "C:\\");
    }

    #[test]
    fn relpath_cross_drive_is_none() {
        assert_eq!(relpath("C:\\a\\b", "C:\\a"), Some("b".to_string()));
        assert_eq!(relpath("D:\\a", "C:\\a"), None);
        assert_eq!(relpath("C:\\a", "C:\\a"), Some(".".to_string()));
    }

    #[test]
    fn commonpath_matches_ntpath() {
        assert_eq!(
            commonpath(&["C:\\a\\b", "C:\\a\\c"]),
            Some("C:\\a".to_string())
        );
        assert_eq!(commonpath(&["C:\\a", "D:\\b"]), None);
    }
}
