//! 撤销日志：写入、列举、清理与还原（`engine/common.py` 的日志那一半）。
//!
//! 日志统一放在**程序目录**下的 `Name_backup` 里，不往用户的数据目录里丢垃圾。
//! 同一个文件夹里混着各个目标目录的日志，靠两样东西区分：
//!   · 文件名里的 8 位目录指纹 —— 列表与清理都靠它，不用读 JSON；
//!   · JSON 里的 `root` 字段 —— 界面上显示"这是哪个目录的记录"。
//! 旧版本写在 `<目标目录>/_rename_logs/` 里的日志仍然认（只读兼容），
//! 这样升级不会让老用户丢掉可撤销的机会。

use serde_json::{Map, Value};

use crate::consts::{APP_NAME, LOG_SCHEMA, MAX_LOGS, MAX_LOGS_TOTAL};
use crate::pathops;
use crate::rename::rename_with_retry;
use crate::scan::{nkey, natural_key};

/// 目标目录的 8 位指纹（Windows 下大小写不敏感）。
pub fn root_hash(root: &str) -> String {
    use sha1::{Digest, Sha1};
    let key = nkey(root);
    let mut hasher = Sha1::new();
    hasher.update(key.as_bytes());
    let digest = hasher.finalize();
    let mut hex = String::with_capacity(40);
    for byte in digest {
        hex.push_str(&format!("{byte:02x}"));
    }
    hex[..8].to_string()
}

/// 日志文件名：指纹在前，时间戳在后 —— 排序按时间戳走。
///
/// 段数刻意比旧格式多一段（5 段），这样新旧两种文件名绝不会互相误认：
///     新： rename_<8位指纹>_<日期>_<时分秒>_<随机>.json
///     旧： rename_<日期>_<时分秒>_<随机>.json
pub fn log_name(root: &str, stamp: &str) -> String {
    format!("rename_{}_{stamp}.json", root_hash(root))
}

/// 拆出 (目录指纹, 排序键)。不是本程序写的日志返回 ("", "")。
///
/// 指纹必须是 8 位十六进制 —— 旧格式段数对不上，会被判成"认不出来"，
/// 免得被误分到某个目录下再被清理掉。
pub fn split_log_name(name: &str) -> (String, String) {
    // 与 Python 一样：先去掉 ".json" 再**连 rename 前缀一起**切，
    // 否则段数会少一段（新格式 5 段 / 旧格式 4 段是靠这个数区分的）
    let stem = match name.strip_suffix(".json") {
        Some(s) => s,
        None => return (String::new(), String::new()),
    };
    if !stem.starts_with("rename_") {
        return (String::new(), String::new());
    }
    let parts: Vec<&str> = stem.split('_').collect();
    if parts.len() != 5 {
        return (String::new(), String::new());
    }
    let (tag, date, clock) = (parts[1], parts[2], parts[3]);
    if tag.len() != 8 || !tag.chars().all(|c| c.is_ascii_hexdigit()) {
        return (String::new(), String::new());
    }
    if date.len() != 8 || !date.chars().all(|c| c.is_ascii_digit()) {
        return (String::new(), String::new());
    }
    if clock.len() != 6 || !clock.chars().all(|c| c.is_ascii_digit()) {
        return (String::new(), String::new());
    }
    (tag.to_string(), format!("{date}{clock}"))
}

/// 旧格式日志（`rename_<日期>_<时分秒>_<随机>.json`）的时间排序键。
pub fn legacy_sort_key(name: &str) -> String {
    let stem = match name.strip_suffix(".json") {
        Some(s) => s,
        None => return String::new(),
    };
    if !stem.starts_with("rename_") {
        return String::new();
    }
    let parts: Vec<&str> = stem.split('_').collect();
    if parts.len() != 4 {
        return String::new();
    }
    let (date, clock) = (parts[1], parts[2]);
    if date.chars().all(|c| c.is_ascii_digit()) && clock.chars().all(|c| c.is_ascii_digit()) {
        format!("{date}{clock}")
    } else {
        String::new()
    }
}

/// 旧版本把日志放在目标目录下的 `_rename_logs`（只读兼容用）。
pub fn legacy_log_dir(root: &str) -> String {
    pathops::join(&[
        &pathops::abspath(root),
        crate::consts::LOG_DIR_NAME,
    ])
}

/// 写日志要携带的元信息（都是 JSON，源数据由绑定层从 Python 侧取）。
pub struct LogMeta {
    pub kind: String,
    pub summary: Value,
    pub executed: Value,
    pub options: Value,
    pub rules: Value,
    pub note: String,
}

/// 写撤销日志。`pairs` 是 [(旧绝对路径, 新绝对路径)]。
///
/// `summary` 是**校验阶段**的统计（可改/未变/冲突…），`exec` 是**执行阶段**
/// 的真实成败。两者不是一回事：部分失败时前者仍然按计划计数，界面显示
/// "成功 N" 就会骗人 —— 所以分开存。
///
/// 返回日志路径；写不进去（目录不可写）返回空串，调用方必须把这件事说出来。
pub fn save_log(directory: &str, root: &str, pairs: &[(String, String)], meta: &LogMeta) -> String {
    if std::fs::create_dir_all(directory).is_err() {
        return String::new();
    }
    // 时间戳只到秒，同一秒内两次落盘会互相覆盖 —— 加随机尾码兜底
    let now = chrono::Local::now();
    let tag = uuid::Uuid::new_v4().simple().to_string();
    let stamp = format!("{}_{}", now.format("%Y%m%d_%H%M%S"), &tag[..6]);
    let path = pathops::join(&[directory, &log_name(root, &stamp)]);

    let mut data = Map::new();
    data.insert("app".into(), Value::String(APP_NAME.to_string()));
    data.insert("version".into(), Value::String(crate::APP_VER.to_string()));
    data.insert("schema".into(), Value::from(LOG_SCHEMA));
    data.insert("kind".into(), Value::String(meta.kind.clone()));
    data.insert(
        "time".into(),
        Value::String(now.format("%Y-%m-%d %H:%M:%S").to_string()),
    );
    data.insert("root".into(), Value::String(pathops::abspath(root)));
    data.insert("summary".into(), meta.summary.clone());
    data.insert("exec".into(), meta.executed.clone());
    data.insert("options".into(), meta.options.clone());
    data.insert("rules".into(), meta.rules.clone());
    data.insert("note".into(), Value::String(meta.note.clone()));
    data.insert("pairs".into(), pairs_to_json(pairs));

    let text = serde_json::to_string_pretty(&Value::Object(data)).unwrap_or_default();
    if std::fs::write(&path, text.as_bytes()).is_err() {
        return String::new();
    }
    trim_logs(directory, MAX_LOGS as usize, MAX_LOGS_TOTAL as usize);
    path
}

fn pairs_to_json(pairs: &[(String, String)]) -> Value {
    Value::Array(
        pairs
            .iter()
            .map(|(old, new)| {
                let mut item = Map::new();
                item.insert("old".into(), Value::String(old.clone()));
                item.insert("new".into(), Value::String(new.clone()));
                Value::Object(item)
            })
            .collect(),
    )
}

/// 清理旧日志。
///
/// 同一个文件夹里混着各个目标目录的日志，所以**按目录**各留 `keep` 份，
/// 再对总量压一道上限 —— 否则 A 目录的日志会把 B 目录的挤掉。
/// 认不出来的文件（不是本程序写的）一律不动。
pub fn trim_logs(directory: &str, keep: usize, keep_total: usize) {
    let entries = match std::fs::read_dir(directory) {
        Ok(it) => it,
        Err(_) => return,
    };
    let mut files: Vec<String> = entries
        .flatten()
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|n| n.starts_with("rename_") && n.ends_with(".json"))
        .collect();
    files.sort();

    let mut by_root: Vec<(String, Vec<(String, String)>)> = Vec::new();
    let mut known: Vec<String> = Vec::new();
    for name in files {
        let (tag, order) = split_log_name(&name);
        if tag.is_empty() {
            continue; // 认不出来的（老格式/别的程序）不参与清理
        }
        match by_root.iter_mut().find(|(t, _)| *t == tag) {
            Some((_, list)) => list.push((order, name.clone())),
            None => by_root.push((tag, vec![(order, name.clone())])),
        }
        known.push(name);
    }

    let mut dropped: Vec<String> = Vec::new();
    for (_tag, mut items) in by_root {
        items.sort();
        if items.len() > keep {
            let cut = items.len() - keep;
            for (_order, name) in items.drain(..cut) {
                dropped.push(name);
            }
        }
    }
    // 总量兜底：超出上限时按时间从旧到新删
    let alive: Vec<String> = known
        .iter()
        .filter(|n| !dropped.iter().any(|d| d == *n))
        .cloned()
        .collect();
    if alive.len() > keep_total {
        let mut ordered: Vec<(String, String)> = alive
            .iter()
            .map(|n| {
                let (_tag, order) = split_log_name(n);
                (if order.is_empty() { n.clone() } else { order }, n.clone())
            })
            .collect();
        ordered.sort_by(|a, b| a.0.cmp(&b.0));
        let cut = alive.len() - keep_total;
        for (_order, name) in ordered.iter().take(cut) {
            dropped.push(name.clone());
        }
    }
    for name in dropped {
        let _ = std::fs::remove_file(pathops::join(&[directory, &name]));
    }
}

/// 列出撤销日志（按时间从旧到新排）。
///
/// `root` 给了 = 只列那个目标目录的；空 = 列出全部。旧版本留在
/// `<目标目录>/_rename_logs/` 里的日志也一并认。
pub fn list_logs(directory: &str, root: &str) -> Vec<String> {
    let mut picked: Vec<(String, String)> = Vec::new();
    let tag = if root.is_empty() {
        String::new()
    } else {
        root_hash(root)
    };
    if let Ok(entries) = std::fs::read_dir(directory) {
        for name in entries
            .flatten()
            .map(|e| e.file_name().to_string_lossy().into_owned())
        {
            if !(name.starts_with("rename_") && name.ends_with(".json")) {
                continue;
            }
            let (ftag, order) = split_log_name(&name);
            if !tag.is_empty() && ftag != tag {
                continue;
            }
            let key = if order.is_empty() {
                let legacy = legacy_sort_key(&name);
                if legacy.is_empty() {
                    name.clone()
                } else {
                    legacy
                }
            } else {
                order
            };
            picked.push((key, pathops::join(&[directory, &name])));
        }
    }

    if !root.is_empty() {
        let legacy = legacy_log_dir(root);
        if let Ok(entries) = std::fs::read_dir(&legacy) {
            for name in entries
                .flatten()
                .map(|e| e.file_name().to_string_lossy().into_owned())
            {
                if name.starts_with("rename_") && name.ends_with(".json") {
                    let key = legacy_sort_key(&name);
                    let key = if key.is_empty() { name.clone() } else { key };
                    picked.push((key, pathops::join(&[&legacy, &name])));
                }
            }
        }
    }

    picked.sort();
    picked.into_iter().map(|(_k, path)| path).collect()
}

/// 读日志。文件不存在/不是合法 JSON 时返回 `Err`（错误文案与 Python 的异常信息等价）。
pub fn load_log(path: &str) -> Result<Value, String> {
    let text = std::fs::read_to_string(path).map_err(|e| e.to_string())?;
    serde_json::from_str(&text).map_err(|e| e.to_string())
}

/// 从日志里取 [(旧路径, 新路径)]。
///
/// 日志文件是用户可编辑的普通 JSON，结构可能被改坏（数组、字符串、甚至
/// `"pairs": [1, 2]`）。这里一律按"能读的读、读不了的跳过"处理 ——
/// 一份坏日志不该让整份记录（乃至整个「执行记录」页）都读不出来。
pub fn log_pairs(data: &Value) -> Vec<(String, String)> {
    let mut out = Vec::new();
    let items = match data.get("pairs").and_then(|v| v.as_array()) {
        Some(v) => v,
        None => return out,
    };
    for p in items {
        let old = p.get("old").and_then(|v| v.as_str());
        let new = p.get("new").and_then(|v| v.as_str());
        if let (Some(old), Some(new)) = (old, new) {
            if !old.is_empty() && !new.is_empty() {
                out.push((old.to_string(), new.to_string()));
            }
        }
    }
    out
}

/// 把"还没还原成功的项"写回日志，其余字段原样保留。
///
/// 这样重复点撤销时只会重试剩下的那几项，而不会把已经还原过的条目又算成
/// "找不到"（旧实现因此永远删不掉日志，每次都报"未完成"）。
pub fn rewrite_log(path: &str, data: &Value, pairs: &[(String, String)], missing: &[String]) {
    let mut payload = match data.clone() {
        Value::Object(map) => map,
        _ => Map::new(),
    };
    payload.insert("pairs".into(), pairs_to_json(pairs));
    let mut note = format!("仍有 {} 项未还原", pairs.len());
    if !missing.is_empty() {
        note.push_str(&format!("（{} 项原文件已不存在，已跳过）", missing.len()));
    }
    payload.insert("note".into(), Value::String(note));

    let text = serde_json::to_string_pretty(&Value::Object(payload)).unwrap_or_default();
    let tmp = format!("{path}.tmp");
    if std::fs::write(&tmp, text.as_bytes()).is_ok() {
        if std::fs::rename(&tmp, path).is_err() {
            let _ = std::fs::remove_file(&tmp);
        }
    } else {
        let _ = std::fs::remove_file(&tmp);
    }
}

/// 按日志逆序还原。绝不覆盖已存在的文件（沿用 BatchRenamer 的保守策略）。
///
/// 幂等：还原成功的条目不会再参与下一次撤销。还有没还原成的项时，把
/// **剩下的项**写回日志（便于人工处理 / 下次重试）；全部还原完（或只剩
/// "原文件已不存在"）时删掉日志。
///
/// 返回 (全部还原成功?, 错误清单)。注意"原文件已不存在"算跳过不算失败。
pub fn undo(
    log_path: &str,
    progress: &mut dyn FnMut(usize, usize, &str),
) -> Result<(bool, Vec<String>), String> {
    let data = load_log(log_path)?;
    let pairs = log_pairs(&data);

    // 日志里的路径记的是「当时所在的父目录」：父目录改名之前，子项记的是
    // 老父目录下的路径（d1\d2\X_d3）。所以还原必须**父项先还原**——父项
    // 回到原名之后，子项日志里那条路径才重新成立；反过来先还原子项，
    // 子项的目标路径还不存在，只能靠 makedirs 造出一个同名空目录，
    // 父项随后还原时就撞名失败（多层嵌套 a/b/c 同时改名时必然踩到）。
    // 深度升序 = 先父后子；同深度按自然序保证结果稳定。
    let mut ordered = pairs.clone();
    ordered.sort_by(|a, b| {
        let da = a.1.matches('\\').count();
        let db = b.1.matches('\\').count();
        da.cmp(&db)
            .then_with(|| natural_key(&pathops::basename(&a.1)).cmp(&natural_key(&pathops::basename(&b.1))))
    });

    let mut errors: Vec<String> = Vec::new();
    let mut missing: Vec<String> = Vec::new();
    let mut pending: Vec<(String, String)> = Vec::new();
    let total = ordered.len();

    for (i, (old, new)) in ordered.iter().enumerate() {
        progress(i + 1, total, &format!("还原 {}", pathops::basename(new)));
        if !std::path::Path::new(new).exists() {
            // 目标已经不在了（用户删了/挪了）：没什么可还原的，跳过即可。
            // 这不算失败，否则"撤销过一次"就会让日志永远删不掉。
            missing.push(new.clone());
            continue;
        }
        if std::path::Path::new(old).exists() {
            errors.push(format!("已存在，跳过：{old}"));
            pending.push((old.clone(), new.clone()));
            continue;
        }
        let dir = pathops::dirname(old);
        if !dir.is_empty() {
            let _ = std::fs::create_dir_all(&dir);
        }
        if let Err(exc) = rename_with_retry(new, old, 3) {
            errors.push(format!(
                "{new} -> {old}：{}",
                crate::rename::describe_rename_error(&exc, new, old)
            ));
            pending.push((old.clone(), new.clone()));
        }
    }

    if !pending.is_empty() {
        rewrite_log(log_path, &data, &pending, &missing);
        return Ok((false, errors));
    }
    // 该还原的都还原了（其余是"原文件已不存在"）：日志没有继续存在的意义
    let _ = std::fs::remove_file(log_path);
    Ok((true, Vec::new()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recognises_only_own_log_names() {
        let (tag, order) = split_log_name("rename_0a1b2c3d_20261105_091500_abcdef.json");
        assert_eq!(tag, "0a1b2c3d");
        assert_eq!(order, "20261105091500");
        // 旧格式（4 段）不认
        assert_eq!(
            split_log_name("rename_20261105_091500_abcdef.json"),
            (String::new(), String::new())
        );
        assert_eq!(split_log_name("别的文件.json"), (String::new(), String::new()));
        // 指纹不是十六进制也不认
        assert_eq!(
            split_log_name("rename_zzzzzzzz_20261105_091500_abcdef.json").0,
            ""
        );
    }

    #[test]
    fn legacy_sort_key_needs_four_segments() {
        assert_eq!(
            legacy_sort_key("rename_20261105_091500_abcdef.json"),
            "20261105091500"
        );
        assert_eq!(legacy_sort_key("rename_a_b_c_d_e.json"), "");
    }

    #[test]
    fn root_hash_is_stable_and_case_insensitive() {
        assert_eq!(root_hash("C:\\Temp\\A"), root_hash("c:/temp/a"));
        assert_eq!(root_hash("C:\\Temp").len(), 8);
    }

    #[test]
    fn bad_pairs_are_skipped() {
        let data: Value = serde_json::from_str(
            r#"{"pairs":[{"old":"a","new":"b"},{"old":"x"},3,"s",{"old":"","new":"y"}]}"#,
        )
        .unwrap();
        assert_eq!(log_pairs(&data), vec![("a".into(), "b".into())]);
        assert!(log_pairs(&Value::from(3)).is_empty());
    }
}
