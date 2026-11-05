//! 改名计划：从扫描结果算出新名字，并落到可执行的一批操作
//! （对应 `engine/plan.py`）。
//!
//! 本模块不落盘，只算目标名字与顺序。
//!
//! 与 Python 版的结构差异只有一处：这里把"算"和"写回对象"拆开——
//! [`assign_numbers`] / [`finalize_paths`] 只吃快照、吐结果，由绑定层写进
//! `Item`。这样核心不需要认识 Python 对象，也不用为了借一个 `&mut Item`
//! 去和借用检查器打架。

use crate::pathops;
use crate::rules;
use crate::scan::{kind_text, nkey, under};

// --------------------------------------------------------------------------- //
// 编号作用域
// --------------------------------------------------------------------------- //

pub const SCOPE_GLOBAL: &str = "global";
pub const SCOPE_FOLDER: &str = "folder";
pub const SCOPE_KIND: &str = "kind";
pub const SCOPE_FOLDER_KIND: &str = "folder_kind";

pub fn scopes() -> [&'static str; 4] {
    [SCOPE_GLOBAL, SCOPE_FOLDER, SCOPE_KIND, SCOPE_FOLDER_KIND]
}

/// `SCOPE_TEXT`：作用域 → 界面文案。
pub fn scope_text() -> Vec<(&'static str, &'static str)> {
    vec![
        (SCOPE_GLOBAL, "整批连续编号"),
        (SCOPE_FOLDER, "每个文件夹重新编号"),
        (SCOPE_KIND, "每种类型重新编号"),
        (SCOPE_FOLDER_KIND, "每个文件夹内的每种类型分别编号"),
    ]
}

/// `safe_rel`：`os.path.normcase(rel or "")`。
pub fn safe_rel(rel: &str) -> String {
    pathops::normcase(rel)
}

// --------------------------------------------------------------------------- //
// 选项
// --------------------------------------------------------------------------- //

#[derive(Clone, Debug)]
pub struct PlanOptions {
    pub start: i64,
    pub step: i64,
    pub width: i64,
    pub scope: String,
    pub reverse: bool,
    pub split_ext: bool,
    pub rename_dirs: bool,
    pub dry_run: bool,
}

impl Default for PlanOptions {
    fn default() -> Self {
        Self {
            start: 1,
            step: 1,
            width: 0,
            scope: SCOPE_GLOBAL.to_string(),
            reverse: false,
            split_ext: true,
            rename_dirs: true,
            dry_run: true,
        }
    }
}

// --------------------------------------------------------------------------- //
// 编号
// --------------------------------------------------------------------------- //

/// 发号需要的条目信息（只读快照）。
#[derive(Clone, Debug)]
pub struct NumberInput {
    pub path: String,
    pub kind: String,
    pub is_dir: bool,
}

/// 发号结果；`None` 表示这一项不参与发号（保持原样）。
#[derive(Clone, Copy, Debug)]
pub struct Numbers {
    pub index: i64,
    pub total: i64,
    pub num: i64,
    pub width: i64,
}

/// `_scope_key`：分组键。`global` 全批一组；`folder` 按目录；`kind` 按类型；
/// `folder_kind` 是两者的笛卡尔积。
pub fn scope_key(it: &NumberInput, scope: &str) -> Vec<String> {
    let rel = pathops::dirname(&it.path);
    if scope == SCOPE_GLOBAL {
        return vec![String::new()];
    }
    if scope == SCOPE_FOLDER {
        return vec![safe_rel(&rel)];
    }
    // kind 为空时回落到中文类型名（与 Python 的 KIND_TEXT 兜底一致）
    let kind = if it.kind.is_empty() {
        let want = if it.is_dir { "dir" } else { "file" };
        kind_text()
            .into_iter()
            .find(|(k, _)| *k == want)
            .map(|(_, text)| text.to_string())
            .unwrap_or_default()
    } else {
        it.kind.clone()
    };
    if scope == SCOPE_KIND {
        return vec![kind];
    }
    vec![safe_rel(&rel), kind]
}

/// 按作用域分组、按当前顺序分配 `num / index / total / width`。
///
/// 顺序沿用扫描阶段的排序结果（这就是"先排序再编号"的语义）。
/// 这里**不**按目录深度重排：混排文件夹与文件时，父目录拿到第几号由扫描顺序
/// 决定；想要"父目录先编号"就在扫描排序里解决，而不是在发号时偷偷改顺序。
pub fn assign_numbers(items: &[NumberInput], opts: &PlanOptions) -> Vec<Option<Numbers>> {
    let mut out: Vec<Option<Numbers>> = vec![None; items.len()];
    let mut ordered: Vec<usize> = (0..items.len()).collect();
    if !opts.rename_dirs {
        ordered.retain(|&i| !items[i].is_dir);
    }
    if opts.reverse {
        ordered.reverse();
    }

    // 分组：保持首次出现顺序，便于对拍（结果与顺序无关，但调试时看着一致）
    let mut keys: Vec<Vec<String>> = Vec::new();
    let mut buckets: Vec<Vec<usize>> = Vec::new();
    for &idx in &ordered {
        let key = scope_key(&items[idx], &opts.scope);
        match keys.iter().position(|k| *k == key) {
            Some(pos) => buckets[pos].push(idx),
            None => {
                keys.push(key);
                buckets.push(vec![idx]);
            }
        }
    }

    for group in buckets {
        let total = group.len() as i64;
        for (i, &idx) in group.iter().enumerate() {
            let i = i as i64;
            out[idx] = Some(Numbers {
                index: i + 1,
                total,
                num: opts.start + i * opts.step,
                width: opts.width,
            });
        }
    }
    out
}

/// 发号参数以链里的"编号"规则为准。
///
/// 规则上的起始值 / 步长 / 补零宽度 / 编号范围 / 倒序是唯一的设置处；
/// 规则没写的项才回落到 `PlanOptions`（命令行 `--start/--step/--width` 走这条路）。
pub fn with_number_rule(opts: &PlanOptions, chain: &[rules::Rule]) -> PlanOptions {
    let Some(rule) = rules::number_rule(chain) else {
        return opts.clone();
    };
    let got = rules::number_options(rule);
    let mut out = opts.clone();
    if let Some(v) = got.start {
        out.start = v;
    }
    // 步长 0 在这里就回落成 1（与 Python 的 `or 1` 一致）
    out.step = match got.step {
        Some(v) => {
            if v == 0 { 1 } else { v }
        }
        None => out.step,
    };
    if let Some(v) = got.width {
        out.width = v;
    }
    if let Some(scope) = got.scope {
        if scopes().contains(&scope.as_str()) {
            out.scope = scope;
        }
    }
    out.reverse = got.reverse || opts.reverse;
    out
}

// --------------------------------------------------------------------------- //
// 预检：落盘前先挑出"目标名字重复"
// --------------------------------------------------------------------------- //

/// 返回给用户看的提示（与 Python 的 `_precheck` 文案一致）。
pub fn precheck(items: &[(String, String)]) -> Vec<String> {
    // items: (dirname, new_name)
    let mut counts: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
    for (dir, new_name) in items {
        if new_name.is_empty() {
            continue;
        }
        let key = nkey(&pathops::join(&[dir, new_name]));
        *counts.entry(key).or_insert(0) += 1;
    }
    let dup = counts.values().filter(|v| **v > 1).count();
    if dup > 0 {
        vec![format!("{dup} 个目标名字重复，会被标成冲突")]
    } else {
        Vec::new()
    }
}

// --------------------------------------------------------------------------- //
// 父子路径修正
// --------------------------------------------------------------------------- //

/// 修正需要的条目信息（只读快照）。
#[derive(Clone, Debug)]
pub struct PathInput {
    pub path: String,
    pub name: String,
    pub is_dir: bool,
    pub new_name: String,
    pub dirname: String,
}

/// 每一项的路径修正结果。
#[derive(Clone, Debug, Default)]
pub struct PathFix {
    /// 新的相对路径（相对公共根）
    pub new_rel: Option<String>,
    /// 新的所在目录（`Item._new_dir`）
    pub new_dir: Option<String>,
}

/// `_common_root`：所有条目的公共父目录。
pub fn common_root(items: &[PathInput]) -> String {
    if items.is_empty() {
        return String::new();
    }
    let dirs: Vec<String> = items
        .iter()
        .map(|it| pathops::dirname(&pathops::abspath(&it.path)))
        .collect();
    if dirs.len() == 1 {
        return dirs[0].clone();
    }
    let refs: Vec<&str> = dirs.iter().map(|s| s.as_str()).collect();
    pathops::commonpath(&refs).unwrap_or_else(|| dirs[0].clone())
}

/// 父子路径修正 + 需要"当成已存在目录"的键集合。
///
/// 子项的目标路径要跟着父文件夹的新名字走：先把每个文件夹的
/// 「旧相对路径 → 新相对路径」算出来，再把所有条目按最长前缀替换。
///
/// 跨盘符的批次（例如从 C: 和 D: 各拖一个文件夹进来）也归这里管：
/// 不同盘符之间不存在父子关系，`relpath` 会直接给出 None，
/// 所以这类条目统一"不做前缀映射"，而不是让整批预览崩掉。
pub fn finalize_paths(items: &[PathInput]) -> (Vec<PathFix>, Vec<String>) {
    let root = common_root(items);
    let mut dir_map: Vec<(String, String, String)> = Vec::new(); // (旧相对, 旧相对 normcase, 新相对)
    for it in items {
        if !it.is_dir {
            continue;
        }
        let Some(old_rel) = pathops::relpath(&it.path, &root) else {
            continue; // 跨盘：没有可用的相对路径，跳过映射
        };
        let new_rel = if !it.new_name.is_empty() && it.new_name != it.name {
            pathops::join(&[&pathops::dirname(&old_rel), &it.new_name])
        } else {
            old_rel.clone()
        };
        if pathops::normcase(&old_rel) != pathops::normcase(&new_rel) {
            dir_map.push((old_rel.clone(), pathops::normcase(&old_rel), new_rel));
        }
    }
    // 长的先替换（分隔符多的层级更深）
    dir_map.sort_by(|a, b| {
        let ca = a.0.matches('\\').count();
        let cb = b.0.matches('\\').count();
        cb.cmp(&ca)
    });

    let mut fixes: Vec<PathFix> = vec![PathFix::default(); items.len()];

    if !dir_map.is_empty() {
        // 同一目录下的条目改名前缀完全一样：按目录缓存一次即可，
        // 否则 1500 个文件要对每个前缀各做一次 normcase 与 relpath
        let mut cache: std::collections::HashMap<String, (String, bool)> =
            std::collections::HashMap::new();
        let mut map_rel = |abs_dir: &str| -> (String, bool) {
            if let Some(got) = cache.get(abs_dir) {
                return got.clone();
            }
            let Some(rel) = pathops::relpath(abs_dir, &root) else {
                let got = (abs_dir.to_string(), false);
                cache.insert(abs_dir.to_string(), got.clone());
                return got;
            };
            let mut cur = rel.clone();
            let low = pathops::normcase(&rel);
            let mut changed = false;
            for (old_rel, old_low, new_rel_val) in &dir_map {
                if low == *old_low {
                    cur = new_rel_val.clone();
                    changed = true;
                    break;
                }
                if low.starts_with(&format!("{old_low}\\")) {
                    cur = format!("{new_rel_val}{}", &cur[old_rel.len()..]);
                    changed = true;
                    break;
                }
            }
            cache.insert(abs_dir.to_string(), (cur.clone(), changed));
            (cur, changed)
        };

        for (i, it) in items.iter().enumerate() {
            let base = if it.is_dir { &it.path } else { &it.dirname };
            let (rel, changed) = map_rel(base);
            if !changed {
                continue;
            }
            let name = if it.new_name.is_empty() { &it.name } else { &it.new_name };
            if it.is_dir {
                fixes[i].new_rel = Some(rel.clone());
                fixes[i].new_dir = Some(pathops::join(&[&root, &pathops::dirname(&rel)]));
            } else {
                fixes[i].new_rel = Some(pathops::join(&[&rel, name]));
                fixes[i].new_dir = Some(pathops::join(&[&root, &rel]));
            }
        }
    }

    let mut dir_keys: Vec<String> = dir_map
        .iter()
        .map(|(_, _, new_rel)| nkey(&pathops::join(&[&root, new_rel])))
        .collect();
    for it in items {
        if it.is_dir && !it.new_name.is_empty() && it.new_name != it.name {
            dir_keys.push(nkey(&pathops::join(&[&it.dirname, &it.new_name])));
        }
    }
    dir_keys.sort();
    dir_keys.dedup();
    (fixes, dir_keys)
}

/// 由 [`finalize_paths`] 的修正结果推出 `Item.new_path` 用的目标目录。
pub fn new_dir_of(fix: &PathFix, fallback_dirname: &str) -> String {
    match &fix.new_dir {
        Some(d) => d.clone(),
        None => fallback_dirname.to_string(),
    }
}

// --------------------------------------------------------------------------- //
// 命令行预览
// --------------------------------------------------------------------------- //

/// 一行预览需要的条目信息。
pub struct DescribeRow {
    pub status: String,
    pub name: String,
    pub new_name: String,
    pub note: String,
    pub dirname: String,
    pub changed: bool,
}

/// 命令行预览用的表格文本。
pub fn describe_plan(rows: &[DescribeRow], limit: i64) -> String {
    let shown: &[DescribeRow] = if limit == 0 { rows } else { &rows[..rows.len().min(limit as usize)] };
    let mut lines: Vec<String> = Vec::new();
    for it in shown {
        let mark = match it.status.as_str() {
            "ok" => "✓",
            "same" => "=",
            "conflict" => "!",
            "error" => "×",
            "skip" => "-",
            _ => "?",
        };
        let new = if it.new_name.is_empty() { &it.name } else { &it.new_name };
        let body = if it.changed && it.status == "ok" {
            format!("{} → {}", it.name, new)
        } else {
            let note = if it.note.is_empty() { "未变" } else { &it.note };
            format!("{}（{}）", it.name, note)
        };
        let parent = if it.dirname.is_empty() { "." } else { it.dirname.as_str() };
        lines.push(format!("{mark} {body}  [{parent}]"));
    }
    if limit != 0 && rows.len() > limit.max(0) as usize {
        lines.push(format!(
            "… 共 {} 项，仅显示前 {} 项（--limit 0 显示全部）",
            rows.len(),
            limit
        ));
    }
    lines.join("\n")
}

/// `under` 的转发，方便 plan 侧只 use 一个模块。
pub fn is_under(path_key: &str, base_key: &str) -> bool {
    under(path_key, base_key)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ni(path: &str, is_dir: bool) -> NumberInput {
        NumberInput {
            path: path.into(),
            kind: if is_dir { "dir".into() } else { "file".into() },
            is_dir,
        }
    }

    #[test]
    fn global_numbering_is_continuous() {
        let items = vec![ni("C:\\a\\x.txt", false), ni("C:\\b\\y.txt", false)];
        let got = assign_numbers(&items, &PlanOptions::default());
        assert_eq!(got[0].unwrap().num, 1);
        assert_eq!(got[1].unwrap().num, 2);
        assert_eq!(got[1].unwrap().total, 2);
    }

    #[test]
    fn folder_scope_restarts() {
        let items = vec![
            ni("C:\\a\\x.txt", false),
            ni("C:\\a\\y.txt", false),
            ni("C:\\b\\z.txt", false),
        ];
        let opts = PlanOptions { scope: SCOPE_FOLDER.into(), width: 2, ..Default::default() };
        let got = assign_numbers(&items, &opts);
        assert_eq!(got[0].unwrap().num, 1);
        assert_eq!(got[1].unwrap().num, 2);
        assert_eq!(got[2].unwrap().num, 1);
        assert_eq!(got[2].unwrap().total, 1);
    }

    #[test]
    fn reverse_numbers_in_reverse_order() {
        let items = vec![ni("C:\\a\\x.txt", false), ni("C:\\a\\y.txt", false)];
        let opts = PlanOptions { reverse: true, ..Default::default() };
        let got = assign_numbers(&items, &opts);
        assert_eq!(got[0].unwrap().num, 2);
        assert_eq!(got[1].unwrap().num, 1);
        // index 仍然是"在组里的第几个"
        assert_eq!(got[0].unwrap().index, 2);
    }

    #[test]
    fn dirs_skipped_when_rename_dirs_off() {
        let items = vec![ni("C:\\a\\d", true), ni("C:\\a\\x.txt", false)];
        let opts = PlanOptions { rename_dirs: false, ..Default::default() };
        let got = assign_numbers(&items, &opts);
        assert!(got[0].is_none());
        assert_eq!(got[1].unwrap().num, 1);
    }

    #[test]
    fn parent_rename_moves_children() {
        let items = vec![
            PathInput {
                path: "C:\\box\\old".into(),
                name: "old".into(),
                is_dir: true,
                new_name: "new".into(),
                dirname: "C:\\box".into(),
            },
            PathInput {
                path: "C:\\box\\old\\a.txt".into(),
                name: "a.txt".into(),
                is_dir: false,
                new_name: "a.txt".into(),
                dirname: "C:\\box\\old".into(),
            },
        ];
        let (fixes, keys) = finalize_paths(&items);
        assert_eq!(fixes[1].new_rel.as_deref(), Some("new\\a.txt"));
        assert_eq!(fixes[1].new_dir.as_deref(), Some("C:\\box\\new"));
        assert!(keys.iter().any(|k| k.ends_with("box\\new")));
    }

    #[test]
    fn cross_drive_does_not_panic() {
        let items = vec![
            PathInput {
                path: "Q:\\probe\\A".into(),
                name: "A".into(),
                is_dir: true,
                new_name: "A_n".into(),
                dirname: "Q:\\probe".into(),
            },
            PathInput {
                path: "Z:\\probe\\B".into(),
                name: "B".into(),
                is_dir: true,
                new_name: "B_n".into(),
                dirname: "Z:\\probe".into(),
            },
        ];
        let (_, keys) = finalize_paths(&items);
        assert_eq!(keys.len(), 2);
    }
}
