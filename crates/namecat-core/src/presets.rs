//! 规则预设（对应 `engine/presets.py`）。
//!
//! 存成 JSON，放在 `%APPDATA%\\NameCat\\presets\\` 下；也支持任意路径导入导出。
//! 本模块只碰配置文件，不动用户数据。
//!
//! 脏 JSON 一律兜底（`test_edges.py` 有断言）：`plan: 3`、`scan: "x"` 这类
//! 手改坏的字段当空字典处理；规则能救的照常读回（那是 [`crate::rules`] 的事）。

use crate::consts;
use crate::rules;
use crate::rules::Rule;
use serde_json::{Map, Value};
use std::path::Path;

pub const UNNAMED: &str = "未命名预设";

fn app_name() -> &'static str {
    consts::APP_NAME
}

/// 预设对象。`scan` / `plan` 原样保留（含未来新增的键），过 [`Self::to_dict`]
/// 与 [`Self::from_dict`] 时按 JSON 往返。
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Preset {
    pub name: String,
    pub rules: Vec<Rule>,
    pub scan: Map<String, Value>,
    pub plan: Map<String, Value>,
    pub note: String,
}

impl Preset {
    pub fn new(name: &str) -> Self {
        Self {
            name: name.to_string(),
            ..Self::default()
        }
    }

    pub fn to_dict(&self) -> Value {
        let mut map = Map::new();
        map.insert("app".into(), Value::String(app_name().to_string()));
        map.insert(
            "version".into(),
            Value::String(crate::APP_VER.to_string()),
        );
        map.insert("name".into(), Value::String(self.name.clone()));
        map.insert("note".into(), Value::String(self.note.clone()));
        map.insert(
            "rules".into(),
            Value::Array(rules::rules_to_json(&self.rules)),
        );
        map.insert("scan".into(), Value::Object(self.scan.clone()));
        map.insert("plan".into(), Value::Object(self.plan.clone()));
        Value::Object(map)
    }

    pub fn from_dict(data: &Value) -> Self {
        let map = data.as_object();
        let name = map
            .and_then(|m| m.get("name"))
            .map(or_empty_text)
            .unwrap_or_default();
        let note = map
            .and_then(|m| m.get("note"))
            .map(or_empty_text)
            .unwrap_or_default();
        let rules = map
            .and_then(|m| m.get("rules"))
            .and_then(|value| value.as_array().cloned())
            .map(|items| rules::rules_from_json(&items))
            .unwrap_or_default();
        let scan = as_map(map.and_then(|m| m.get("scan")));
        let plan = as_map(map.and_then(|m| m.get("plan")));
        Self {
            name: if name.is_empty() {
                UNNAMED.to_string()
            } else {
                name
            },
            rules,
            scan,
            plan,
            note,
        }
    }
}

/// `_as_text(value)`：`None` → 空串，其它 `str(value)`。
fn or_empty_text(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        other => crate::llm::json_text(other),
    }
}

/// `_as_dict(value)`：非字典一律空表（`plan: 3` 这类脏字段当没写）。
fn as_map(value: Option<&Value>) -> Map<String, Value> {
    match value {
        Some(Value::Object(map)) => map.clone(),
        _ => Map::new(),
    }
}

/// 文件名安全化：非法字符换成下划线，空名给一个占位。
pub fn safe_name(name: &str) -> String {
    const BAD: &str = "\\/:*?\"<>|";
    let out: String = name
        .trim()
        .chars()
        .map(|c| if BAD.contains(c) { '_' } else { c })
        .collect();
    if out.is_empty() {
        UNNAMED.to_string()
    } else {
        out
    }
}

/// 默认预设目录下的目标路径。
pub fn preset_path(dir: &str, name: &str) -> String {
    Path::new(dir)
        .join(format!("{}.json", safe_name(name)))
        .to_string_lossy()
        .into_owned()
}

/// 保存预设。返回实际写入的路径（写失败由调用方拿 OSError）。
pub fn save(preset: &Preset, path: &str, dir: &str) -> crate::Result<String> {
    save_value(&preset.to_dict(), path, dir)
}

/// 直接按 JSON 值写盘（绑定层拿到的就是 Python 侧算好的 dict）。
///
/// 格式与 Python 的 `json.dump(..., ensure_ascii=False, indent=2)` 逐字节一致：
/// 2 空格缩进、`", "`/`": "` 分隔、中文不转义、末尾不加换行。
pub fn save_value(value: &Value, path: &str, dir: &str) -> crate::Result<String> {
    let target = if path.is_empty() {
        let name = value
            .get("name")
            .map(crate::llm::json_text)
            .unwrap_or_default();
        preset_path(dir, &name)
    } else {
        path.to_string()
    };
    if let Some(parent) = Path::new(&target).parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }
    let body = serde_json::to_string_pretty(value)
        .map_err(|err| crate::Error::Json(err.to_string()))?;
    // 照抄 Python：`open(..., "w", encoding="utf-8")` 走文本模式，Windows 上把
    // "\n" 翻成 "\r\n"。不翻的话旧版本读新文件没问题，但**新版本写的文件**
    // 与旧版本逐字节不同（对拍会红，用户拿 diff 看也莫名其妙）。
    #[cfg(windows)]
    let body = body.replace('\n', "\r\n");
    std::fs::write(&target, body.as_bytes())?;
    Ok(target)
}

/// 读预设。文件坏/JSON 坏都会抛（与 Python 版一致：调用方自己兜）。
pub fn load(path: &str) -> crate::Result<Preset> {
    Ok(Preset::from_dict(&load_value(path)?))
}

/// 读成 JSON 值（绑定层自己建对象，省一次结构转换）。
pub fn load_value(path: &str) -> crate::Result<Value> {
    let bytes = std::fs::read(path)?;
    serde_json::from_slice(&bytes).map_err(|err| crate::Error::Json(err.to_string()))
}

/// 预设目录下的全部 `.json`（按路径排序）。
pub fn list_presets(dir: &str) -> Vec<String> {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return Vec::new();
    };
    let mut out: Vec<String> = Vec::new();
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.to_lowercase().ends_with(".json") {
            out.push(Path::new(dir).join(&name).to_string_lossy().into_owned());
        }
    }
    out.sort();
    out
}

/// 删除预设。`name_or_path` 不是 `.json` 结尾时按"名字"处理（补目录 + 后缀）。
pub fn delete(name_or_path: &str, dir: &str) -> bool {
    let path = if name_or_path.to_lowercase().ends_with(".json") {
        name_or_path.to_string()
    } else {
        preset_path(dir, name_or_path)
    };
    std::fs::remove_file(path).is_ok()
}

// --------------------------------------------------------------------------- //
// 内置预设（首次使用即可参考，不写盘）
// --------------------------------------------------------------------------- //

fn builtin_scan(kinds: &[&str]) -> Map<String, Value> {
    let mut map = Map::new();
    map.insert(
        "kinds".into(),
        Value::Array(kinds.iter().map(|k| Value::String((*k).to_string())).collect()),
    );
    map
}

fn builtin_plan(pairs: &[(&str, Value)]) -> Map<String, Value> {
    let mut map = Map::new();
    for (key, value) in pairs {
        map.insert((*key).to_string(), value.clone());
    }
    map
}

/// 内置的常用预设。顺序即 [`builtin_list`] 的顺序（界面按这个顺序展示）。
pub fn builtin() -> Vec<(&'static str, Preset)> {
    vec![
        (
            "照片序号（四位）",
            Preset {
                name: "照片序号（四位）".to_string(),
                rules: vec![rules::make_rule("template", "{n}", "", "")],
                scan: builtin_scan(&["image", "video"]),
                plan: builtin_plan(&[
                    ("scope", Value::String("folder".into())),
                    ("start", Value::from(1)),
                    ("width", Value::from(4)),
                ]),
                note: "按文件夹各自从 0001 起编号，扩展名不变".to_string(),
            },
        ),
        (
            "去掉末尾圆括号序号",
            Preset {
                name: "去掉末尾圆括号序号".to_string(),
                rules: vec![rules::make_rule("regex", r"\s*\(\d+\)$", "", "")],
                scan: Map::new(),
                plan: Map::new(),
                note: "把「副本 (2)」这类尾部编号去掉".to_string(),
            },
        ),
        (
            "首尾去空格并规范空格",
            Preset {
                name: "首尾去空格并规范空格".to_string(),
                rules: vec![
                    rules::make_rule("trim", "", "", ""),
                    rules::make_rule("collapse", " ", "", ""),
                ],
                scan: Map::new(),
                plan: Map::new(),
                note: String::new(),
            },
        ),
        (
            "改为小写并去非法字符",
            Preset {
                name: "改为小写并去非法字符".to_string(),
                rules: vec![
                    rules::make_rule("case", "lower", "", ""),
                    rules::make_rule("filename_clean", "", "", ""),
                ],
                scan: Map::new(),
                plan: Map::new(),
                note: String::new(),
            },
        ),
    ]
}

pub fn builtin_list() -> Vec<&'static str> {
    builtin().into_iter().map(|(key, _)| key).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp(name: &str) -> std::path::PathBuf {
        let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../_verify_tmp/core_tmp")
            .join(format!("presets_{name}"));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn safe_name_replaces_bad_chars() {
        assert_eq!(safe_name("照片/序号"), "照片_序号");
        assert_eq!(safe_name("a:b*c?d\"e<f>g|h\\i"), "a_b_c_d_e_f_g_h_i");
        assert_eq!(safe_name("   "), UNNAMED);
        assert_eq!(safe_name("正常名字"), "正常名字");
    }

    #[test]
    fn dict_roundtrip_keeps_rules_and_notes() {
        let preset = Preset {
            name: "llm 预设".to_string(),
            rules: vec![rules::make_rule("llm", "只留地名", "zh", "12")],
            scan: builtin_scan(&["file"]),
            plan: builtin_plan(&[("width", Value::from(3))]),
            note: "备注".to_string(),
        };
        let data = preset.to_dict();
        assert_eq!(data["app"], Value::String(crate::consts::APP_NAME.into()));
        assert_eq!(data["version"], Value::String(crate::APP_VER.into()));
        let back = Preset::from_dict(&data);
        assert_eq!(back.name, preset.name);
        assert_eq!(back.note, preset.note);
        assert_eq!(back.rules.len(), 1);
        assert_eq!(back.rules[0].kind, "llm");
        assert_eq!(
            back.rules[0].extras.get("instruction").and_then(|v| v.as_str()),
            Some("只留地名")
        );
        assert_eq!(back.scan["kinds"][0], Value::String("file".into()));
        assert_eq!(back.plan["width"], Value::from(3));
    }

    #[test]
    fn dirty_json_falls_back_to_empty() {
        let back = Preset::from_dict(&serde_json::json!({
            "name": "", "rules": "not a list", "scan": 3, "plan": 7, "note": null
        }));
        assert_eq!(back.name, UNNAMED);
        assert!(back.rules.is_empty());
        assert!(back.scan.is_empty(), "脏 scan 当成空配置");
        assert!(back.plan.is_empty(), "脏 plan 当成空配置");
        assert_eq!(back.note, "");
        // 顶层不是字典也不炸
        assert_eq!(Preset::from_dict(&serde_json::json!(3)).name, UNNAMED);
    }

    #[test]
    fn save_load_list_delete() {
        let dir = temp("crud");
        let dir_text = dir.to_string_lossy().into_owned();
        let preset = Preset {
            name: "我的预设".to_string(),
            rules: vec![rules::make_rule("trim", "", "", "")],
            ..Preset::default()
        };
        let path = save(&preset, "", &dir_text).unwrap();
        assert!(path.ends_with("我的预设.json"));
        assert_eq!(load(&path).unwrap().rules.len(), 1);
        // 显式路径
        let custom = dir.join("custom.json").to_string_lossy().into_owned();
        assert_eq!(save(&preset, &custom, &dir_text).unwrap(), custom);
        let listed = list_presets(&dir_text);
        assert_eq!(listed.len(), 2, "两个 json 都在");
        assert!(listed[0] < listed[1], "按路径排序");
        assert!(delete("我的预设", &dir_text));
        assert_eq!(list_presets(&dir_text).len(), 1);
        assert!(delete(&custom, &dir_text), "带 .json 的按路径删");
        assert!(!delete("不存在", &dir_text), "删不掉返回 false");
        assert!(list_presets(&dir.join("missing").to_string_lossy()).is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn builtin_presets_are_usable() {
        let list = builtin();
        assert_eq!(list.len(), 4);
        assert_eq!(builtin_list()[0], "照片序号（四位）");
        let (_, photo) = &list[0];
        assert_eq!(photo.plan["scope"], Value::String("folder".into()));
        assert_eq!(photo.plan["width"], Value::from(4));
        let (_, regex) = &list[1];
        assert_eq!(regex.rules[0].kind, "regex");
        // 内置预设也能原样往返（写进文件再读回来）
        for (key, preset) in builtin() {
            let back = Preset::from_dict(&preset.to_dict());
            assert_eq!(back.name, key);
            assert_eq!(back.rules.len(), preset.rules.len());
        }
    }
}
