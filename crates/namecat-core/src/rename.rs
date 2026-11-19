//! 落盘改名、回滚与临时名残留（`engine/common.py` 的 `execute` 一族）。
//!
//! 策略：能直改就直改；只要目标可能还被本批次另一项占着（链式改名、A↔B 互换），
//! 或者要改的是文件夹（父项改名会让子项路径失效），就走「临时名中转」。
//! 任何一步失败都要把已经做过的部分整体回滚，别让用户停在半成品状态。

use std::collections::HashSet;
use std::path::Path;
use std::time::Duration;

use crate::consts::TEMP_PREFIX;
use crate::pathops;
use crate::scan::nkey;

/// 一个待改名的条目（绑定层从 Python 的 `Item` 上摘下来的只读快照）。
pub struct ExecItem<'a> {
    /// 在调用方那个列表里的下标，用来把结果对回去
    pub index: usize,
    pub path: &'a str,
    pub dirname: &'a str,
    pub new_name: &'a str,
    pub is_dir: bool,
}

impl ExecItem<'_> {
    /// `os.path.join(dirname, new_name)`
    pub fn target(&self) -> String {
        pathops::join(&[self.dirname, self.new_name])
    }
}

/// 一步的执行结果。`old` / `new` 在中转方案里会被改写成"真实落位路径"
/// （父目录先改名的话，子项的起点是改名后的路径），日志与撤销都靠它。
#[derive(Clone, Debug)]
pub struct ExecStep {
    pub index: usize,
    pub old: String,
    pub new: String,
    pub ok: bool,
    pub error: String,
}

impl ExecStep {
    fn new(item: &ExecItem) -> Self {
        Self {
            index: item.index,
            old: item.path.to_string(),
            new: item.target(),
            ok: false,
            error: String::new(),
        }
    }
}

/// 真正的改名动作。
///
/// **不能用 `std::fs::rename`**：它在 Windows 上走
/// `MoveFileExW(..., MOVEFILE_REPLACE_EXISTING)`，目标已存在时会**直接覆盖**；
/// 而 Python 的 `os.rename` 用的是不带 replace 的版本，撞名时抛
/// `WinError 183`。这个差别是数据安全级：本该整批回滚的场景，Rust 会悄悄
/// 把别人的文件冲掉。
#[cfg(windows)]
fn do_rename(src: &str, dst: &str) -> std::io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::MoveFileExW;

    let wide = |s: &str| -> Vec<u16> {
        std::ffi::OsStr::new(s)
            .encode_wide()
            .chain(std::iter::once(0))
            .collect()
    };
    let (src_w, dst_w) = (wide(src), wide(dst));
    // flags = 0：不覆盖、不延迟、不跨卷拷贝 —— 与 os.rename 等价
    let ok = unsafe { MoveFileExW(src_w.as_ptr(), dst_w.as_ptr(), 0) };
    if ok == 0 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(not(windows))]
fn do_rename(src: &str, dst: &str) -> std::io::Result<()> {
    std::fs::rename(src, dst)
}

/// Windows 上被杀软/索引器短暂占用时重试（沿用 BatchRenamer 做法）。
///
/// 只对"拒绝访问/共享冲突"重试，其余错误立刻抛出——否则真·路径错误会被
/// 拖成三次 150ms 的等待再报同一个错。
pub fn rename_with_retry(src: &str, dst: &str, attempts: usize) -> std::io::Result<()> {
    let mut last = None;
    for i in 0..attempts {
        match do_rename(src, dst) {
            Ok(()) => return Ok(()),
            Err(exc) => {
                let code = exc.raw_os_error().unwrap_or(0);
                last = Some(exc);
                // 5 = ERROR_ACCESS_DENIED，32 = ERROR_SHARING_VIOLATION
                if code == 5 || code == 32 {
                    std::thread::sleep(Duration::from_millis(150 * (i as u64 + 1)));
                    continue;
                }
                return Err(last.expect("刚赋过值"));
            }
        }
    }
    Err(last.expect("attempts 至少为 1"))
}

/// 把 IO 错误说成人话，格式对齐 Python 的 `str(OSError)`：
/// `[WinError 183] Cannot create a file when that file already exists`。
///
/// Rust 的 Display 会在末尾补 ` (os error N)` 和句号，Python 没有——用户看到
/// 的是中文界面里的报错文案，两边越像越好排查。
pub fn describe_io_error(exc: &std::io::Error) -> String {
    match exc.raw_os_error() {
        Some(code) => {
            let text = exc.to_string();
            let text = match text.rfind(" (os error ") {
                Some(pos) => &text[..pos],
                None => &text[..],
            };
            format!("[WinError {code}] {}", text.trim_end_matches('.'))
        }
        None => exc.to_string(),
    }
}

/// Python 的 `repr(str)`：CPython 拼 OSError 文案时用的是 `%R`，
/// 所以路径里的反斜杠会变成两个（`C:////a////b`）。要跟它逐字一致只能用同一套转义。
fn py_repr(text: &str) -> String {
    // CPython 的 repr 优先用单引号；仅当字符串含 ' 且不含 " 时改用双引号。
    let quote = if text.contains('\'') && !text.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => { out.push('\\'); out.push(c); }
            _ => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// 带路径对的错误文案：Python 的 `os.rename` 抛错时会把两个名字带上，
/// 用户才知道是哪个文件卡住了。
pub fn describe_rename_error(exc: &std::io::Error, src: &str, dst: &str) -> String {
    format!(
        "{}: {} -> {}",
        describe_io_error(exc),
        py_repr(src),
        py_repr(dst)
    )
}

fn temp_name(dirname: &str) -> String {
    let tag = uuid::Uuid::new_v4().simple().to_string();
    pathops::join(&[dirname, &format!("{TEMP_PREFIX}{}", &tag[..8])])
}

/// 把校验通过的条目改名到位。
///
/// 返回的列表顺序：直改分支按"路径从深到浅"排（先改子项，避免父目录改名后
/// 子项路径失效）；中转分支保持调用方给的顺序。这个差异来自 Python 版对
/// `steps` 本身做了原地 sort，不是笔误。
pub fn execute(items: &[ExecItem], progress: &mut dyn FnMut(usize, usize, &str)) -> Vec<ExecStep> {
    let mut steps: Vec<ExecStep> = items.iter().map(ExecStep::new).collect();
    if steps.is_empty() {
        return steps;
    }

    // 文件夹一律走中转：父项改名会让子项路径失效，中转能把这两件事解耦。
    // 文件只有在目标被本批次另一项占着时才需要中转。
    let sources: HashSet<String> = items.iter().map(|it| nkey(it.path)).collect();
    let needs_chain = items.iter().any(|it| {
        if it.is_dir {
            return true;
        }
        let target = nkey(&it.target());
        sources.contains(&target) && target != nkey(it.path)
    });

    if !needs_chain {
        // 排成「最深的先改」，避免父目录先改名后子项路径失效
        steps.sort_by(|a, b| depth(&b.old).cmp(&depth(&a.old)));
        let total = steps.len();
        let mut done: Vec<usize> = Vec::new();
        for i in 0..total {
            let (old, new) = (steps[i].old.clone(), steps[i].new.clone());
            progress(
                i + 1,
                total,
                &format!("正在重命名 {}", pathops::basename(&old)),
            );
            match rename_with_retry(&old, &new, 3) {
                Ok(()) => steps[i].ok = true,
                Err(exc) => {
                    steps[i].error = describe_rename_error(&exc, &old, &new);
                    for &j in done.iter().rev() {
                        let (b_new, b_old) = (steps[j].new.clone(), steps[j].old.clone());
                        match rename_with_retry(&b_new, &b_old, 3) {
                            Ok(()) => {
                                steps[j].ok = false;
                                steps[j].error = "已回滚".to_string();
                            }
                            Err(_) => {
                                steps[j].error = format!("回滚失败：{b_new}");
                            }
                        }
                    }
                    return steps;
                }
            }
            done.push(i);
        }
        progress(total, total, "完成");
        return steps;
    }

    // ---- 中转方案：按「所在目录从深到浅」一组一组处理 ----
    // 同一目录内的项先全部挪到临时名（此时目录本身还没动，路径都有效），
    // 再从临时名落到目标名，然后把临时名并入目标名的前缀映射，
    // 于是父目录稍后改名时，子项已知的临时路径会自动跟着修正。
    let mut order: Vec<usize> = (0..steps.len()).collect();
    // 注意用的是 **dirname(old)** 的层级：文件本身多一层反斜杠会改变分组顺序，
    // 而这直接影响"同一目录的项是不是连续"，进而决定哪一组先落位。
    order.sort_by(|&a, &b| {
        depth(&pathops::dirname(&steps[b].old)).cmp(&depth(&pathops::dirname(&steps[a].old)))
    });

    let mut dir_map: Vec<(String, String)> = Vec::new(); // (旧前缀, 新前缀)，长的在前
    let total = steps.len() * 2;
    let mut cur = 0usize;

    let resolve = |dir_map: &Vec<(String, String)>, path: &str| -> String {
        for (old_pref, new_pref) in dir_map {
            let np = nkey(path);
            let op = nkey(old_pref);
            if np == op {
                return new_pref.clone();
            }
            if np.starts_with(&format!("{op}\\")) {
                let rest: String = path.chars().skip(old_pref.chars().count()).collect();
                return format!("{new_pref}{rest}");
            }
        }
        path.to_string()
    };

    // 分组：同一目录（归一化后）的连续项算一组
    let mut groups: Vec<(String, Vec<usize>)> = Vec::new();
    for &idx in &order {
        let key = nkey(&pathops::dirname(&steps[idx].old));
        match groups.last_mut() {
            Some((k, members)) if *k == key => members.push(idx),
            _ => groups.push((key, vec![idx])),
        }
    }

    for (_key, group) in groups {
        // (步骤下标, 中转前的真实路径, 临时名)
        let mut moved: Vec<(usize, String, String)> = Vec::new();
        for &idx in &group {
            let src = resolve(&dir_map, &steps[idx].old);
            let tmp = temp_name(&pathops::dirname(&src));
            cur += 1;
            progress(
                cur,
                total,
                &format!("临时移动 {}", pathops::basename(&src)),
            );
            match rename_with_retry(&src, &tmp, 3) {
                Ok(()) => moved.push((idx, src, tmp)),
                Err(exc) => {
                    steps[idx].error = describe_rename_error(&exc, &src, &tmp);
                    rollback_temps(&mut steps, &moved);
                    return steps;
                }
            }
        }

        // (步骤下标, 临时名, 目标名)
        let mut final_steps: Vec<(usize, String, String)> = Vec::new();
        for (idx, src, tmp) in moved.iter() {
            let (idx, src, tmp) = (*idx, src.clone(), tmp.clone());
            let dst = resolve(&dir_map, &steps[idx].new);
            cur += 1;
            progress(cur, total, &format!("落位 {}", pathops::basename(&dst)));
            match rename_with_retry(&tmp, &dst, 3) {
                Ok(()) => {}
                Err(exc) => {
                    steps[idx].error = describe_rename_error(&exc, &tmp, &dst);
                    // 已落位的先退回临时名，再让 rollback_temps 把它们连同还没
                    // 落位的一起挪回**原名**。旧实现把已落位的从回滚列表里过滤
                    // 掉了，于是这些文件永远停在 __nk_tmp_xxxx 上（内容在、名字
                    // 没了），却报告"已回滚"。
                    for (s2, t2, d2) in final_steps.iter().rev() {
                        if Path::new(d2).exists() && rename_with_retry(d2, t2, 3).is_err() {
                            steps[*s2].error = format!("回滚失败：{d2}");
                        }
                    }
                    rollback_temps(&mut steps, &moved);
                    for (s2, _t2, _d2) in final_steps.iter() {
                        steps[*s2].ok = false;
                        if steps[*s2].error.is_empty() {
                            steps[*s2].error = "已回滚".to_string();
                        }
                    }
                    return steps;
                }
            }
            // 中转链路里真实落位的是 src（可能已随父目录改名）与 dst，
            // 记进 step 才能让日志/撤销用对路径
            steps[idx].old = src;
            steps[idx].new = dst.clone();
            final_steps.push((idx, tmp.clone(), dst.clone()));
            if items[idx].is_dir {
                dir_map.push((tmp, dst));
                dir_map.sort_by(|a, b| b.0.chars().count().cmp(&a.0.chars().count()));
            }
            steps[idx].ok = true;
        }
    }

    progress(total, total, "完成");
    steps
}

/// `path.count(os.sep)`：目录层级（越深越大）。只数反斜杠，与 Python 一致。
fn depth(path: &str) -> usize {
    path.matches('\\').count()
}

/// 把临时名挪回原名。
///
/// 失败项的 error 里已经有真正的原因（哪个文件被占用/没权限），别用
/// "已回滚"盖掉它 —— 那会让用户彻底查不到为什么失败。
fn rollback_temps(steps: &mut [ExecStep], moved: &[(usize, String, String)]) {
    for (idx, src, tmp) in moved.iter().rev() {
        if Path::new(tmp).exists() {
            match rename_with_retry(tmp, src, 3) {
                Ok(()) => {
                    steps[*idx].ok = false;
                    if steps[*idx].error.is_empty() {
                        steps[*idx].error = "已回滚".to_string();
                    }
                }
                Err(_) => {
                    steps[*idx].error = format!("回滚失败：{tmp}");
                }
            }
        }
    }
}

/// 找出上次崩溃遗留的临时名文件（BatchRenamer 的抢救功能）。
///
/// 这些名字里没有原始文件名的任何线索（`__nk_tmp_` + 随机串），所以只能
/// "找出来给人看"，不能自动还原 —— 界面/命令行都只负责列清单。
pub fn find_temp_leftovers(root: &str, max_depth: usize, skip_dirs: &[&str]) -> Vec<String> {
    let mut found: Vec<String> = Vec::new();
    if root.is_empty() || !Path::new(root).is_dir() {
        return found;
    }
    let root = pathops::abspath(root);
    let skip: HashSet<String> = skip_dirs.iter().map(|s| s.to_lowercase()).collect();

    // 显式栈代替 os.walk（topdown）。出错就跳过那个目录，与 Python 默认
    // `onerror=None` 的"静默忽略"一致。
    let mut stack: Vec<(String, usize)> = vec![(root.clone(), 0)];
    while let Some((dirpath, level)) = stack.pop() {
        let entries = match std::fs::read_dir(&dirpath) {
            Ok(it) => it,
            Err(_) => continue,
        };
        // `dirpath 相对 root 的层级 >= max_depth` 时 os.walk 会把 dirnames 清空，
        // 于是**这一层的临时名子目录不会被算进去**（只算文件）。顺序很关键：
        // 先裁剪再收集，与 Python 的 topdown 行为一致。
        let pruned = level >= max_depth;
        let mut subdirs: Vec<String> = Vec::new();
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().into_owned();
            let is_dir = entry.file_type().map(|t| t.is_dir()).unwrap_or(false);
            if is_dir {
                if pruned {
                    continue;
                }
                if !skip.contains(&name.to_lowercase()) {
                    subdirs.push(name.clone());
                }
            }
            if name.starts_with(TEMP_PREFIX) {
                found.push(pathops::join(&[&dirpath, &name]));
            }
        }
        for name in subdirs {
            let child = pathops::join(&[&dirpath, &name]);
            stack.push((child, level + 1));
        }
    }
    found.sort();
    found
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn error_message_keeps_winerror_prefix() {
        let exc = std::io::Error::from_raw_os_error(183);
        assert!(describe_io_error(&exc).starts_with("[WinError 183]"));
    }

    #[test]
    fn depth_counts_backslashes() {
        assert_eq!(depth("C:\\a\\b\\c.txt"), 3);
        assert_eq!(depth("C:\\a.txt"), 1);
    }
}
