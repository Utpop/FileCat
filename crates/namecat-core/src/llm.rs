//! 大模型改名建议层（对应 `engine/llm.py`）。
//!
//! 分工：**一切"怎么问、怎么解释"都在这里**；真正发字节的事在绑定层
//! （`py_net` 调 urllib，见 [`crate::http`] 的模块头）。
//!
//! 三条硬约束（改之前先读）：
//! 1. 模型输出不可信 —— 一律过 [`sanitize_name`]，空结果 / 与原名相同都算"不改"。
//! 2. 没配 Key 不是错误 —— 造一个不可用客户端，`plan()` 给出 `available=false`
//!    与一句人话原因，整条规则链照常跑完（等同"这条规则没启用"）。
//! 3. `parse_suggestions` 认三种形状（JSON 数组 / 逐行编号 / 纯行），并且
//!    **认不出来要返回 None** 去触发"严格重试"，别悄悄按行硬凑。

use crate::error::{Error, Result};
use crate::pathops;
use serde_json::{Map, Value};
use std::sync::OnceLock;

// --------------------------------------------------------------------------- //
// 默认要求（可被使用者整段改写）
// --------------------------------------------------------------------------- //

pub const DEFAULT_INSTRUCTION: &str = "把文件名里没有意义的片段去掉，只留下能让人一眼认出的语义信息。\n\
应当去掉：设备/相机/软件前缀（DSC、IMG、PXL、Screenshot、微信图片）、\
纯数字编号与时间戳（20230815、0432、_1）、下载站与广告后缀\
（_无水印、_高清、www.xxx.com、[更多资源]）、重复与临时词\
（副本、拷贝、copy、新建、未命名、final、final2）、\
无意义分隔符堆叠与括号空壳。\n\
应当保留：主体内容、地点、人物、项目、主题等真正有辨识度的信息。\n\
不要杜撰原文里没有的信息；信息本就不多时，简短即可。";

pub const DEFAULT_LANG: &str = "zh";
pub const DEFAULT_MAX_LEN: i64 = 24;

/// 使用者可编辑的要求文件（不存在时按 DEFAULT_INSTRUCTION 自动建一份）
pub const INSTRUCTION_ENV: &str = "NAMECAT_LLM_INSTRUCTION_FILE";
/// 改名前的变量名：仍然要认，否则老用户的配置会"突然失效"
pub const LEGACY_INSTRUCTION_ENV: &str = "NAMEKIT_LLM_INSTRUCTION_FILE";
pub const INSTRUCTION_NAME: &str = "llm_instruction.txt";

pub const INSTRUCTION_HEADER: &str = "\
# NameCat —— 交给大模型的文件名整理要求
# 这个文件就是提示词里「整理要求」那一段，可以随便改、随便扩写。
# 以单个 # 开头的行是注释，不会发给模型；空行会保留。
# 改完保存即可，下次运行自动生效（--llm-instruction 可临时换一个文件）。
";

/// 命名风格：影响提示词里的一句话约束，界面可做成下拉框。
pub const STYLES: &[(&str, &str)] = &[
    ("short", "尽量短，只留最关键的 2~4 个词"),
    ("natural", "像正常标题一样可读，词与词之间可保留空格"),
    ("kebab", "用短横线连接，全小写，适合做 URL"),
    ("snake", "用下划线连接，全小写"),
    ("camel", "驼峰式，无分隔符"),
];
pub const DEFAULT_STYLE: &str = "natural";

pub const SYSTEM_PROMPT: &str = "你是一个文件名整理助手。你会收到一批文件名（不含扩展名），\
对每一个，只输出精简后的名字。";

pub fn style_text(style: &str) -> &'static str {
    STYLES
        .iter()
        .find(|(key, _)| *key == style)
        .map(|(_, text)| *text)
        .unwrap_or_else(|| {
            STYLES
                .iter()
                .find(|(key, _)| *key == DEFAULT_STYLE)
                .map(|(_, text)| *text)
                .expect("STYLES 里必须有默认风格")
        })
}

// --------------------------------------------------------------------------- //
// 可编辑的「给 LLM 的要求」
// --------------------------------------------------------------------------- //

/// 要求文件位置：显式路径 > 环境变量 > `default_path`（`%APPDATA%\\NameCat\\…`）。
///
/// 不放在工作目录里：换个目录跑同一套要求，行为应当一致。
/// `default_path` 由绑定层用 `engine.appdata_dir()` 算好传进来 —— 那是
/// Python 侧唯一一份"用户数据目录 + 旧目录迁移"的实现，不在这里复制一份。
pub fn instruction_path(custom: &str, default_path: &str) -> String {
    if !custom.is_empty() {
        return pathops::abspath(&expanduser(custom));
    }
    let env = std::env::var(INSTRUCTION_ENV)
        .ok()
        .filter(|v| !v.is_empty())
        .or_else(|| {
            std::env::var(LEGACY_INSTRUCTION_ENV)
                .ok()
                .filter(|v| !v.is_empty())
        });
    match env {
        Some(value) => pathops::abspath(&expanduser(&value)),
        None => default_path.to_string(),
    }
}

/// `os.path.expanduser` 的 Windows 口径：只认 `~` / `~/` / `~\`。
///
/// `~user` 形式 Windows 上本来就查不到用户目录，Python 也会原样返回，故不处理。
fn expanduser(path: &str) -> String {
    let is_home = path == "~"
        || path.starts_with("~/")
        || path.starts_with("~\\");
    if !is_home {
        return path.to_string();
    }
    let home = std::env::var("USERPROFILE")
        .ok()
        .filter(|v| !v.is_empty())
        .or_else(|| {
            let drive = std::env::var("HOMEDRIVE").ok().unwrap_or_default();
            let rest = std::env::var("HOMEPATH").ok().unwrap_or_default();
            Some(format!("{drive}{rest}")).filter(|v| v != "")
        })
        .unwrap_or_else(|| path.to_string());
    if path == "~" {
        home
    } else {
        format!("{home}{}", &path[1..])
    }
}

/// 去掉整行注释（`#` 开头）与首尾空行；正文原样保留（含内部空行与缩进）。
pub fn strip_comment_lines(text: &str) -> String {
    let mut keep: Vec<&str> = Vec::new();
    for line in splitlines(text) {
        if line.trim_start().starts_with('#') {
            continue;
        }
        keep.push(line);
    }
    while keep.first().is_some_and(|line| line.trim().is_empty()) {
        keep.remove(0);
    }
    while keep.last().is_some_and(|line| line.trim().is_empty()) {
        keep.pop();
    }
    keep.join("\n")
}

/// 读使用者写的要求；文件缺失或为空时回落 DEFAULT_INSTRUCTION（绝不报错）。
pub fn read_instruction(path: &str) -> String {
    let body = match std::fs::read(path) {
        Ok(bytes) => {
            // Python 用 utf-8-sig 打开：开头有 BOM 就去掉
            let text = String::from_utf8_lossy(&bytes).into_owned();
            strip_comment_lines(text.strip_prefix('\u{feff}').unwrap_or(&text))
        }
        Err(_) => return DEFAULT_INSTRUCTION.to_string(),
    };
    if body.is_empty() {
        DEFAULT_INSTRUCTION.to_string()
    } else {
        body
    }
}

/// 把要求写进文件（自动建目录），返回文件路径。
pub fn write_instruction(path: &str, text: &str, with_header: bool) -> Result<String> {
    if let Some(parent) = std::path::Path::new(path).parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }
    let body = if with_header {
        format!("{INSTRUCTION_HEADER}\n{}\n", text.trim())
    } else {
        text.to_string()
    };
    std::fs::write(path, body.as_bytes())?;
    Ok(path.to_string())
}

/// 确保要求文件存在：缺失时用默认要求建一份，使用者随后直接改即可。
pub fn ensure_instruction_file(path: &str) -> Result<String> {
    if !std::path::Path::new(path).exists() {
        // 建不出来就原样返回路径（Python 版也是吞掉 OSError）
        let _ = write_instruction(path, DEFAULT_INSTRUCTION, true);
    }
    Ok(path.to_string())
}

// --------------------------------------------------------------------------- //
// 配置
// --------------------------------------------------------------------------- //

pub const DEFAULT_BASE_URL: &str = "https://api.deepseek.com";
pub const DEFAULT_MODEL: &str = "deepseek-chat";
pub const DEFAULT_TIMEOUT: i64 = 60;
/// 一次请求带多少条名字（太少浪费往返，太多模型容易漏项）
pub const DEFAULT_BATCH: i64 = 20;

pub const PROTOCOL_CHAT: &str = "openai_chat";
pub const PROTOCOL_RESPONSES: &str = "openai_responses";
pub const PROTOCOL_ANTHROPIC: &str = "anthropic_messages";

/// 接入配置（`LLMConfig`）。
#[derive(Debug, Clone, PartialEq)]
pub struct Config {
    pub provider: String,
    pub base_url: String,
    pub api_key: String,
    pub model: String,
    pub timeout: i64,
    pub batch: i64,
    pub temperature: f64,
    pub max_retries: i64,
    pub protocol: String,
    pub headers: Vec<(String, String)>,
    pub max_tokens: i64,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            provider: "openai".to_string(),
            base_url: DEFAULT_BASE_URL.to_string(),
            api_key: String::new(),
            model: DEFAULT_MODEL.to_string(),
            timeout: DEFAULT_TIMEOUT,
            batch: DEFAULT_BATCH,
            temperature: 0.2,
            max_retries: 2,
            protocol: PROTOCOL_CHAT.to_string(),
            headers: Vec::new(),
            max_tokens: 1024,
        }
    }
}

impl Config {
    /// 按 `NAMECAT_LLM_*` → `NAMEKIT_LLM_*` → `NAMEXIT_LLM_*` → `OPENAI_*` → `DEEPSEEK_*`
    /// 的顺序读环境变量（改名前的旧变量仍然可用）。
    pub fn from_env() -> Self {
        let pick = |names: &[&str], default: &str| -> String {
            for name in names {
                if let Ok(value) = std::env::var(name) {
                    if !value.is_empty() {
                        return value;
                    }
                }
            }
            default.to_string()
        };

        let mut cfg = Self {
            provider: pick(
                &[
                    "NAMECAT_LLM_PROVIDER",
                    "NAMEKIT_LLM_PROVIDER",
                    "NAMEXIT_LLM_PROVIDER",
                    "LLM_PROVIDER",
                ],
                "openai",
            ),
            base_url: pick(
                &[
                    "NAMECAT_LLM_BASE_URL",
                    "NAMEKIT_LLM_BASE_URL",
                    "NAMEXIT_LLM_BASE_URL",
                    "OPENAI_BASE_URL",
                    "DEEPSEEK_BASE_URL",
                ],
                DEFAULT_BASE_URL,
            ),
            api_key: pick(
                &[
                    "NAMECAT_LLM_API_KEY",
                    "NAMEKIT_LLM_API_KEY",
                    "NAMEXIT_LLM_API_KEY",
                    "OPENAI_API_KEY",
                    "DEEPSEEK_API_KEY",
                ],
                "",
            ),
            model: pick(
                &[
                    "NAMECAT_LLM_MODEL",
                    "NAMEKIT_LLM_MODEL",
                    "NAMEXIT_LLM_MODEL",
                    "OPENAI_MODEL",
                    "DEEPSEEK_MODEL",
                ],
                DEFAULT_MODEL,
            ),
            ..Self::default()
        };
        let proto = pick(
            &["NAMECAT_LLM_PROTOCOL", "NAMEKIT_LLM_PROTOCOL"],
            "",
        );
        if !proto.is_empty() {
            cfg.protocol = proto;
        }
        let raw_headers = pick(&["NAMECAT_LLM_HEADERS", "NAMEKIT_LLM_HEADERS"], "");
        if !raw_headers.is_empty() {
            if let Ok(Value::Object(map)) = serde_json::from_str::<Value>(&raw_headers) {
                cfg.headers = header_pairs(&map);
            }
        }
        cfg
    }

    /// 绝不把 api_key 序列化出去（可能进日志 / CSV / 预设）。
    pub fn as_dict(&self) -> Value {
        let mut map = Map::new();
        map.insert("provider".into(), Value::String(self.provider.clone()));
        map.insert("base_url".into(), Value::String(self.base_url.clone()));
        map.insert("model".into(), Value::String(self.model.clone()));
        map.insert("timeout".into(), Value::from(self.timeout));
        map.insert("batch".into(), Value::from(self.batch));
        map.insert("has_key".into(), Value::Bool(!self.api_key.is_empty()));
        Value::Object(map)
    }

    /// 返回一句"为什么不可用"，空串表示可用。
    pub fn problem(&self) -> String {
        if self.api_key.is_empty() && self.provider != "ollama" {
            return "未配置 API Key：设置环境变量 NAMECAT_LLM_API_KEY\
（旧名 NAMEKIT_LLM_API_KEY 仍然可用；或 OPENAI_API_KEY / \
DEEPSEEK_API_KEY），或把 provider 设为 ollama 走本地模型"
                .to_string();
        }
        if self.base_url.is_empty() {
            return "未配置 base_url".to_string();
        }
        if self.model.is_empty() {
            return "未配置 model".to_string();
        }
        String::new()
    }
}

/// JSON 字典 → 请求头列表（丢掉空键，值统一 `str()`）。
pub fn header_pairs(map: &Map<String, Value>) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for (key, value) in map {
        let key = key.trim();
        if key.is_empty() {
            continue;
        }
        out.push((key.to_string(), json_text(value)));
    }
    out
}

// --------------------------------------------------------------------------- //
// 名字净化（模型输出不可信，必须先过一遍）
// --------------------------------------------------------------------------- //

const INVALID_CHARS: &str = "\\/:*?\"<>|";
const RESERVED: &[&str] = &[
    "con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8",
    "com9", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
];

macro_rules! cached_re {
    ($name:ident, $pat:expr) => {
        fn $name() -> &'static fancy_regex::Regex {
            static RE: OnceLock<fancy_regex::Regex> = OnceLock::new();
            RE.get_or_init(|| {
                fancy_regex::Regex::new($pat).expect(concat!("正则编译失败：", $pat))
            })
        }
    };
}

cached_re!(re_ws, r"\s+");
cached_re!(re_bad_sep, r"[_\-.\s]{2,}");
cached_re!(re_kebab_snake, r"[\s_\-]+");

/// 把模型输出收拾成合法文件名片段。返回空串表示"不能用"。
pub fn sanitize_name(text: &str, max_len: i64, style: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    // 照抄：先 strip 空白，再 strip 引号（含中文引号），然后只取第一行
    let mut out = trim_quotes(text.trim());
    out = first_line(&out).trim().to_string();
    // 非法字符与控制字符 → 下划线
    out = out
        .chars()
        .map(|c| {
            if INVALID_CHARS.contains(c) || (c as u32) < 32 {
                '_'
            } else {
                c
            }
        })
        .collect();
    out = re_ws().replace_all(&out, " ").trim().to_string();

    let style = if style.is_empty() { DEFAULT_STYLE } else { style }.to_lowercase();
    match style.as_str() {
        "kebab" | "snake" => {
            let sep = if style == "kebab" { "-" } else { "_" };
            out = re_kebab_snake().replace_all(&out, sep).to_string();
            let sep_char = if style == "kebab" { '-' } else { '_' };
            out = out.trim_matches(sep_char).to_string();
        }
        "camel" => {
            let parts: Vec<&str> = re_kebab_snake().split(&out).flatten().collect();
            out = parts
                .iter()
                .filter(|p| !p.is_empty())
                .map(|p| {
                    let mut chars = p.chars();
                    match chars.next() {
                        Some(first) => {
                            first.to_uppercase().collect::<String>() + chars.as_str()
                        }
                        None => String::new(),
                    }
                })
                .collect();
        }
        _ => {
            out = re_bad_sep().replace_all(&out, " ").to_string();
            out = out.trim().to_string();
        }
    }
    out = out.trim_matches(|c| c == ' ' || c == '.').to_string();

    let n = max_len;
    if n > 0 && out.chars().count() as i64 > n {
        let cut: String = out.chars().take(n as usize).collect();
        out = cut.trim_end_matches(|c| c == ' ' || c == '.' || c == '-' || c == '_').to_string();
    }
    let stem = out.split('.').next().unwrap_or("").trim().to_lowercase();
    if RESERVED.contains(&stem.as_str()) {
        out = format!("_{out}");
    }
    out
}

fn trim_quotes(text: &str) -> String {
    const QUOTES: &str = "\"'`“”‘’";
    text.trim_matches(|c| QUOTES.contains(c)).to_string()
}

/// `str.splitlines()[0]`：空串仍是空串。
pub fn first_line(text: &str) -> String {
    splitlines(text)
        .first()
        .copied()
        .unwrap_or_default()
        .to_string()
}

/// Python `str.splitlines()` 的换行集合（比 `\n` 多得多）。
///
/// 注意空串返回**空表**（`"".splitlines() == []`），这是 [first_line] 的前置条件。
pub fn splitlines(text: &str) -> Vec<&str> {
    if text.is_empty() {
        return Vec::new();
    }
    let mut out = Vec::new();
    let mut start = 0usize;
    let bytes = text.as_bytes();
    let mut i = 0usize;
    while i < bytes.len() {
        let ch = text[i..].chars().next().unwrap();
        let width = ch.len_utf8();
        let is_break = matches!(
            ch,
            '\n' | '\r' | '\u{0b}' | '\u{0c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}'
                | '\u{2028}' | '\u{2029}'
        );
        if is_break {
            out.push(&text[start..i]);
            // \r\n 算一个换行
            if ch == '\r' && text[i + width..].starts_with('\n') {
                i += 1;
            }
            start = i + width;
        }
        i += width;
    }
    if start < text.len() || out.is_empty() {
        out.push(&text[start..]);
    }
    out
}

// --------------------------------------------------------------------------- //
// 提示词
// --------------------------------------------------------------------------- //

/// 拼出一次请求的用户消息。要求原文逐字插入，不做改写。
pub fn build_prompt(
    names: &[String],
    instruction: &str,
    lang: &str,
    max_len: i64,
    style: &str,
    strict: bool,
) -> String {
    let req = if instruction.is_empty() {
        DEFAULT_INSTRUCTION
    } else {
        instruction
    }
    .trim();
    let lang = if lang.is_empty() { DEFAULT_LANG } else { lang };
    let width = if max_len == 0 {
        DEFAULT_MAX_LEN
    } else {
        max_len
    };
    let mut lines: Vec<String> = vec![
        "## 整理要求（使用者自定义，优先遵守）".to_string(),
        req.to_string(),
        String::new(),
        "## 输出约定".to_string(),
        format!("· 语言：{lang}"),
        format!("· 每个新名字不超过 {width} 个字符"),
        format!("· 命名风格：{}", style_text(style)),
        "· 不要输出扩展名、不要输出路径、不要加引号".to_string(),
        "· 保持顺序一一对应，不要合并或删除条目".to_string(),
    ];
    if strict {
        lines.push(String::new());
        lines.push("## 输出格式（严格）".to_string());
        lines.push(
            "只输出一个 JSON 数组，长度等于输入条目数。每项形如 \
{\"i\": 序号, \"n\": 新名字}。不要输出任何解释文字。"
                .to_string(),
        );
    }
    lines.push(String::new());
    lines.push(format!("## 输入文件名（共 {} 条）", names.len()));
    for (i, name) in names.iter().enumerate() {
        lines.push(format!("{}. {}", i + 1, name));
    }
    lines.join("\n")
}

// --------------------------------------------------------------------------- //
// 回复解析
// --------------------------------------------------------------------------- //

cached_re!(re_fence, r"(?s)```(?:json)?\s*(.+?)\s*```");
cached_re!(re_numbered, r"^(\d+)\s*[.、,，:：|)\]]\s*(.+)$");

/// 从模型回复里抠出 `{序号: 新名}`。解析不了返回 None（→ 触发严格重试）。
///
/// 顺序：去代码围栏 → 标准 JSON（原样 / 切出第一个数组或对象）→ 逐行编号 → 纯行。
pub fn parse_suggestions(raw: &str, count: usize) -> Option<Vec<(i64, String)>> {
    if raw.is_empty() {
        return None;
    }
    let text = if let Ok(Some(caps)) = re_fence().captures(raw.trim()) {
        caps.get(1).map(|m| m.as_str().to_string()).unwrap_or_default()
    } else {
        raw.trim().to_string()
    };

    for candidate in [Some(text.clone()), Some(slice_json(&text))] {
        let Some(candidate) = candidate.filter(|c| !c.is_empty()) else {
            continue;
        };
        let Ok(data) = serde_json::from_str::<Value>(&candidate) else {
            continue;
        };
        let out = coerce_pairs(&data, count);
        if !out.is_empty() {
            return Some(out);
        }
    }

    let mut out: Vec<(i64, String)> = Vec::new();
    for line in splitlines(&text) {
        let line = line.trim();
        if line.is_empty() || line.starts_with("##") || line.starts_with('#') {
            continue;
        }
        if let Ok(Some(caps)) = re_numbered().captures(line) {
            let idx = caps.get(1).map(|m| m.as_str()).unwrap_or("0");
            let name = caps.get(2).map(|m| m.as_str()).unwrap_or("");
            if let Ok(idx) = idx.parse::<i64>() {
                upsert(&mut out, idx, name.trim().to_string());
            }
        } else if out.len() < count {
            let next = out.len() as i64 + 1;
            upsert(&mut out, next, line.to_string());
        }
    }
    if out.is_empty() {
        None
    } else {
        Some(out)
    }
}

/// 从夹杂解释文字的回复里切出第一个完整 JSON 数组/对象。
fn slice_json(text: &str) -> String {
    for (opener, closer) in [('[', ']'), ('{', '}')] {
        if let (Some(i), Some(j)) = (text.find(opener), text.rfind(closer)) {
            if i < j {
                return text[i..=j].to_string();
            }
        }
    }
    String::new()
}

/// 字典语义的插入：已有键就改值（位置不变），没有就追加。
fn upsert(out: &mut Vec<(i64, String)>, key: i64, value: String) {
    if let Some(slot) = out.iter_mut().find(|(k, _)| *k == key) {
        slot.1 = value;
    } else {
        out.push((key, value));
    }
}

/// 把各种合理形状（`[{i,n}]` / `[[i,name]]` / `{name: new}` / `[name, ...]`）
/// 统一成 `{序号: 名字}`。
pub fn coerce_pairs(data: &Value, _count: usize) -> Vec<(i64, String)> {
    let mut out: Vec<(i64, String)> = Vec::new();
    match data {
        Value::Object(map) => {
            if let Some(Value::Array(items)) = map.get("items") {
                return coerce_pairs(&Value::Array(items.clone()), _count);
            }
            for (key, value) in map {
                let Ok(idx) = py_int_text(key) else {
                    continue;
                };
                if let Value::String(text) = value {
                    upsert(&mut out, idx, text.clone());
                }
            }
        }
        Value::Array(items) => {
            for (pos, item) in items.iter().enumerate() {
                let pos = pos as i64 + 1;
                match item {
                    Value::String(text) => upsert(&mut out, pos, text.clone()),
                    Value::Array(pair) if pair.len() >= 2 => {
                        let idx = value_to_int(&pair[0]).unwrap_or(pos);
                        upsert(&mut out, idx, json_text(&pair[1]));
                    }
                    Value::Object(obj) => {
                        let mut idx: Option<i64> = None;
                        for key in ["i", "index", "id", "no", "idx"] {
                            if let Some(value) = obj.get(key) {
                                if let Some(parsed) = value_to_int(value) {
                                    idx = Some(parsed);
                                    break;
                                }
                            }
                        }
                        let mut val: Option<String> = None;
                        for key in [
                            "n",
                            "name",
                            "new",
                            "new_name",
                            "result",
                            "output",
                            "value",
                        ] {
                            if let Some(Value::String(text)) = obj.get(key) {
                                val = Some(text.clone());
                                break;
                            }
                        }
                        if let Some(text) = val {
                            // 照抄：`out[idx if idx else pos] = val` —— 0 也算 falsy
                            let key = match idx {
                                Some(0) | None => pos,
                                Some(other) => other,
                            };
                            upsert(&mut out, key, text);
                        }
                    }
                    _ => {}
                }
            }
        }
        _ => {}
    }
    out
}

/// Python `int(text)` 的近似（`" 12 "` / `"+12"` / `"12"` 都认）。
fn py_int_text(text: &str) -> std::result::Result<i64, ()> {
    let trimmed = text.trim();
    let normalized = trimmed.replace('_', "");
    normalized.parse::<i64>().map_err(|_| ())
}

/// JSON 值 → int（截断小数；布尔按 Python `int(True) == 1`）。
fn value_to_int(value: &Value) -> Option<i64> {
    match value {
        Value::Number(num) => {
            if let Some(i) = num.as_i64() {
                Some(i)
            } else {
                num.as_f64().map(|f| f.trunc() as i64)
            }
        }
        Value::String(text) => py_int_text(text).ok(),
        Value::Bool(flag) => Some(i64::from(*flag)),
        _ => None,
    }
}

/// Python `str(value)` 的近似（JSON 标量）。
pub fn json_text(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        other => other.to_string(),
    }
}

/// Python 的真值语义（`if data["error"]:` 这类判断用）。
pub fn truthy_json(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(num) => num.as_f64().is_some_and(|f| f != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

/// `json.dumps(value, ensure_ascii=False)` 的近似（只用于错误文案里的前 200 字）。
pub fn dumps_py(value: &Value) -> String {
    match value {
        Value::Null => "null".to_string(),
        Value::Bool(true) => "true".to_string(),
        Value::Bool(false) => "false".to_string(),
        Value::Number(num) => num.to_string(),
        Value::String(text) => serde_json::to_string(text).unwrap_or_else(|_| "\"\"".into()),
        Value::Array(items) => {
            let body: Vec<String> = items.iter().map(dumps_py).collect();
            format!("[{}]", body.join(", "))
        }
        Value::Object(map) => {
            let body: Vec<String> = map
                .iter()
                .map(|(k, v)| {
                    let key = serde_json::to_string(k).unwrap_or_else(|_| "\"\"".into());
                    format!("{key}: {}", dumps_py(v))
                })
                .collect();
            format!("{{{}}}", body.join(", "))
        }
    }
}

// --------------------------------------------------------------------------- //
// HTTP 客户端（三种协议）—— 请求构造与回复抽取
// --------------------------------------------------------------------------- //

/// 对话端点。`base` 末尾带 `/v1` 这类版本段时不重复加。
pub fn chat_endpoint(cfg: &Config) -> String {
    // 照抄：`(self.config.base_url or "").rstrip("/")` —— 这里**不**去首尾空白
    // （`engine.provider._chat_endpoint` 那边才 strip，两条路径的口径不同）
    let base = cfg.base_url.trim_end_matches('/').to_string();
    let tail = if cfg.protocol == PROTOCOL_ANTHROPIC {
        "/messages"
    } else if cfg.protocol == PROTOCOL_RESPONSES {
        "/responses"
    } else {
        "/chat/completions"
    };
    if base.ends_with(tail) {
        return base;
    }
    if has_version_tail(&base) {
        return format!("{base}{tail}");
    }
    format!("{base}/v1{tail}")
}

/// 基础地址是否以 `/v1`、`/v2`… 结尾。
pub fn has_version_tail(base: &str) -> bool {
    let Some(idx) = base.rfind('/') else {
        return false;
    };
    let tail = &base[idx + 1..];
    let Some(rest) = tail.strip_prefix('v') else {
        return false;
    };
    !rest.is_empty() && rest.chars().all(|c| c.is_ascii_digit())
}

/// 请求头。Anthropic 用 `x-api-key`，其余用 `Authorization: Bearer`；
/// 自定义请求头最后覆盖同名项。
///
/// `with_auth = false` 时**不带鉴权头**（`engine.provider._headers` 的口径：
/// 没填 Key 就干脆不发，别发一个空的 Bearer 出去）。
pub fn headers_for(cfg: &Config, with_auth: bool) -> Vec<(String, String)> {
    let mut head: Vec<(String, String)> = vec![
        ("Content-Type".to_string(), "application/json".to_string()),
        ("Accept".to_string(), "application/json".to_string()),
    ];
    if with_auth {
        if cfg.protocol == PROTOCOL_ANTHROPIC {
            if !cfg.api_key.is_empty() {
                head.push(("x-api-key".to_string(), cfg.api_key.clone()));
                head.push(("anthropic-version".to_string(), "2023-06-01".to_string()));
            }
        } else {
            head.push((
                "Authorization".to_string(),
                format!("Bearer {}", cfg.api_key),
            ));
        }
    }
    for (key, value) in &cfg.headers {
        let key = key.trim();
        if key.is_empty() {
            continue;
        }
        if let Some(slot) = head.iter_mut().find(|(k, _)| k == key) {
            slot.1 = value.clone();
        } else {
            head.push((key.to_string(), value.clone()));
        }
    }
    head
}

/// 三种协议各自的请求体。
pub fn payload(cfg: &Config, system: &str, user: &str, json_mode: bool) -> Value {
    let mut map = Map::new();
    if cfg.protocol == PROTOCOL_ANTHROPIC {
        map.insert("model".into(), Value::String(cfg.model.clone()));
        map.insert("max_tokens".into(), Value::from(cfg.max_tokens));
        let mut content = user.to_string();
        if json_mode {
            content.push_str("\n只输出 JSON，不要任何解释文字。");
        }
        map.insert(
            "messages".into(),
            Value::Array(vec![message("user", &content)]),
        );
        if !system.is_empty() {
            map.insert("system".into(), Value::String(system.to_string()));
        }
        return Value::Object(map);
    }
    if cfg.protocol == PROTOCOL_RESPONSES {
        map.insert("model".into(), Value::String(cfg.model.clone()));
        map.insert("input".into(), Value::String(user.to_string()));
        map.insert("max_output_tokens".into(), Value::from(cfg.max_tokens));
        if !system.is_empty() {
            map.insert("instructions".into(), Value::String(system.to_string()));
        }
        if json_mode {
            let mut format = Map::new();
            format.insert("type".into(), Value::String("json_object".into()));
            let mut text = Map::new();
            text.insert("format".into(), Value::Object(format));
            map.insert("text".into(), Value::Object(text));
        }
        return Value::Object(map);
    }
    map.insert("model".into(), Value::String(cfg.model.clone()));
    map.insert("temperature".into(), json_number(cfg.temperature));
    map.insert(
        "messages".into(),
        Value::Array(vec![
            message("system", system),
            message("user", user),
        ]),
    );
    if json_mode {
        let mut format = Map::new();
        format.insert("type".into(), Value::String("json_object".into()));
        map.insert("response_format".into(), Value::Object(format));
    }
    Value::Object(map)
}

fn message(role: &str, content: &str) -> Value {
    let mut map = Map::new();
    map.insert("role".into(), Value::String(role.to_string()));
    map.insert("content".into(), Value::String(content.to_string()));
    Value::Object(map)
}

fn json_number(value: f64) -> Value {
    serde_json::Number::from_f64(value)
        .map(Value::Number)
        .unwrap_or(Value::Null)
}

/// 从三种协议的回复里抽出"一段文本"（上层解析逻辑不用改）。
pub fn extract(data: &Value, protocol: &str) -> Result<String> {
    let bad = || Error::Llm(format!("回复格式异常：{}", truncate_chars(&dumps_py(data), 200)));
    if protocol == PROTOCOL_ANTHROPIC {
        // 照抄：`(data.get("content") or [])` —— 缺字段 / null 都当空列表，
        // 只有"有值但不是列表"才是格式异常
        let parts: &[Value] = match data.get("content") {
            None | Some(Value::Null) => &[],
            Some(Value::Array(items)) => items.as_slice(),
            Some(_) => return Err(bad()),
        };
        let mut out = String::new();
        for part in parts {
            let Some(obj) = part.as_object() else {
                return Err(bad());
            };
            out.push_str(&json_text(obj.get("text").unwrap_or(&Value::Null)));
        }
        return Ok(out);
    }
    if protocol == PROTOCOL_RESPONSES {
        if let Some(Value::String(text)) = data.get("output_text") {
            return Ok(text.clone());
        }
        let mut out = String::new();
        if let Some(Value::Array(items)) = data.get("output") {
            for item in items {
                if let Some(Value::Array(content)) = item.get("content") {
                    for part in content {
                        if let Some(Value::String(text)) = part.get("text") {
                            out.push_str(text);
                        }
                    }
                }
            }
        }
        if out.is_empty() {
            return Err(bad());
        }
        return Ok(out);
    }
    data.get("choices")
        .and_then(|c| c.get(0))
        .and_then(|c| c.get("message"))
        .and_then(|m| m.get("content"))
        .map(|content| match content {
            Value::String(text) => Ok(text.clone()),
            Value::Null => Ok(String::new()),
            other => Ok(json_text(other)),
        })
        .unwrap_or_else(|| Err(bad()))
}

pub fn truncate_chars(text: &str, limit: usize) -> String {
    text.chars().take(limit).collect()
}

// --------------------------------------------------------------------------- //
// 离线替身：机械精简
// --------------------------------------------------------------------------- //

cached_re!(
    re_junk_prefix,
    r"(?i)^(?:(?:dsc|dscn|img|image|pxl|vid|video|photo|pic|screenshot|screen|snipaste|微信图片|微信截图|新建|未命名|untitled|copy|scan|output|屏幕截图|截屏)(?:[\s_\-.]*\d+)*[\s_\-.]*)+"
);
cached_re!(
    re_junk_word,
    r"(?i)(?<![A-Za-z])(?:screenshot|snipaste|screen shot|copy)(?![A-Za-z])"
);
cached_re!(
    re_junk_suffix,
    r"(?i)(?:[\s_\-.]*(?:副本|拷贝|copy|复件|backup|bak|final\d*|修改版?|已编辑|edited|无水印|高清|超清|原版|更多资源|下载))+$"
);
cached_re!(
    re_site,
    r"(?i)(?:www\.[a-z0-9.\-]+|https?://\S+|\[[^\]]*\]|【[^】]*】)"
);
cached_re!(re_longnum, r"(?i)(?<![a-z0-9])\d{4,}(?![a-z0-9])");
cached_re!(
    re_timestamp,
    r"(?i)\d{4}[\-_年]\d{1,2}[\-_月]\d{1,2}日?(?:[\s_\-T]*(?:at\s*)?\d{1,2}[.\-:：时]\d{2}(?:[.\-:：分]\d{2}秒?)?)?|\d{8}"
);
cached_re!(
    re_time_only,
    r"(?i)(?:^|(?<=[\s_\-.]))(?:at\s*)?\d{1,2}[.\-:：时]\d{2}(?:[.\-:：分]\d{2}秒?)?(?=$|[\s_\-.])"
);
cached_re!(re_ext, r"^\.[A-Za-z0-9]{1,8}$");
cached_re!(re_filler_word, r"(?i)(?:^|(?<=[\s_]))(?:at|on|of)(?=$|[\s_])");
cached_re!(re_paren_num, r"\(\s*\d+\s*\)");
cached_re!(re_dup_sep, r"[_\-. ]{2,}");

/// 机械精简。`keep_ext = false` 时名字**不含**扩展名（规则链里由 skip_ext 保护）。
pub fn echo_simplify(name: &str, keep_ext: bool) -> String {
    let mut t = name.to_string();
    if !keep_ext {
        let (stem, ext) = pathops::splitext(&t);
        if !ext.is_empty() && re_ext().is_match(&ext).unwrap_or(false) {
            t = stem;
        }
    }
    // 前缀与后缀都是"垃圾词 _ 垃圾词"连成的串，交替剥到不动为止
    for _ in 0..6 {
        let before = t.clone();
        t = re_junk_prefix().replace_all(&t, "").to_string();
        t = re_junk_suffix().replace_all(&t, "").to_string();
        t = t.trim_matches(|c| c == ' ' || c == '_' || c == '-' || c == '.').to_string();
        if t == before {
            break;
        }
    }
    for _ in 0..4 {
        let before = t.clone();
        t = re_site().replace_all(&t, " ").to_string();
        t = re_timestamp().replace_all(&t, " ").to_string();
        t = re_time_only().replace_all(&t, " ").to_string();
        t = re_longnum().replace_all(&t, " ").to_string();
        t = re_filler_word().replace_all(&t, " ").to_string();
        t = t.trim_matches(|c| c == ' ' || c == '_' || c == '-' || c == '.').to_string();
        if t == before {
            break;
        }
    }
    // Screenshot/截图 这类纯垃圾词最后剥
    t = re_junk_word().replace_all(&t, " ").to_string();
    t = re_paren_num().replace_all(&t, " ").to_string();
    t = re_dup_sep().replace_all(&t, " ").to_string();
    t = re_ws()
        .replace_all(&t, " ")
        .trim_matches(|c| c == ' ' || c == '_' || c == '-' || c == '.')
        .to_string();
    t
}

#[cfg(test)]
mod tests {
    use super::*;

    fn names(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn sanitize_matches_reference_behaviour() {
        assert_eq!(sanitize_name("CON", 24, "natural"), "_CON");
        assert_eq!(sanitize_name("", 24, "natural"), "");
        assert_eq!(sanitize_name("  海边 日落  ", 24, "natural"), "海边 日落");
        assert_eq!(sanitize_name("a/b:c*d?e\"f<g>h|i", 24, "natural"), "a_b_c_d_e_f_g_h_i");
        assert_eq!(sanitize_name("海边日落风景照片", 4, "natural"), "海边日落");
        assert_eq!(sanitize_name("Hello World Again", 24, "kebab"), "Hello-World-Again");
        assert_eq!(sanitize_name("Hello World Again", 24, "snake"), "Hello_World_Again");
        assert_eq!(sanitize_name("hello world_again", 24, "camel"), "HelloWorldAgain");
        // 第一行之后的内容丢掉
        assert_eq!(sanitize_name("海边\n日落", 24, "natural"), "海边");
        // 引号剥掉
        assert_eq!(sanitize_name("\"海边\"", 24, "natural"), "海边");
        // 截断后再去掉尾部标点
        assert_eq!(sanitize_name("abcdefg.", 6, "natural"), "abcdef");
        // 未知风格回落 natural
        assert_eq!(sanitize_name("a  b", 24, "??"), "a b");
    }

    #[test]
    fn prompt_has_expected_sections() {
        let text = build_prompt(&names(&["a.jpg", "b.jpg"]), "", "zh", 24, "natural", false);
        assert!(text.starts_with("## 整理要求（使用者自定义，优先遵守）\n"));
        assert!(text.contains("· 语言：zh"));
        assert!(text.contains("· 每个新名字不超过 24 个字符"));
        assert!(text.ends_with("1. a.jpg\n2. b.jpg"));
        assert!(!text.contains("输出格式（严格）"));
        let strict = build_prompt(&names(&["a.jpg"]), "只留主题", "en", 8, "kebab", true);
        assert!(strict.contains("## 输出格式（严格）"));
        assert!(strict.contains("只留主题"));
        assert!(strict.contains("· 命名风格：用短横线连接，全小写，适合做 URL"));
        // 空要求用默认要求；空语言/0 长度回落默认
        let fallback = build_prompt(&[], "", "", 0, "", false);
        assert!(fallback.contains(DEFAULT_INSTRUCTION));
        assert!(fallback.contains("· 语言：zh"));
        assert!(fallback.contains("不超过 24 个字符"));
        assert!(fallback.ends_with("## 输入文件名（共 0 条）"));
    }

    #[test]
    fn parse_accepts_four_shapes() {
        assert_eq!(
            parse_suggestions("[{\"i\":1,\"n\":\"海边日落\"}]", 1),
            Some(vec![(1, "海边日落".to_string())])
        );
        assert_eq!(
            parse_suggestions("```json\n[{\"i\":1,\"n\":\"A\"},{\"i\":2,\"n\":\"B\"}]\n```", 2),
            Some(vec![(1, "A".to_string()), (2, "B".to_string())])
        );
        assert_eq!(
            parse_suggestions("1. 海边日落\n2. 会议纪要", 2),
            Some(vec![(1, "海边日落".to_string()), (2, "会议纪要".to_string())])
        );
        assert_eq!(
            parse_suggestions("随便说说\n[{\"i\":2,\"n\":\"B\"}]", 2),
            Some(vec![(2, "B".to_string())])
        );
        assert_eq!(
            parse_suggestions("完全不是这三个形状的回答", 1),
            Some(vec![(1, "完全不是这三个形状的回答".to_string())])
        );
        assert_eq!(parse_suggestions("", 3), None);
    }

    #[test]
    fn parse_handles_other_pair_shapes() {
        assert_eq!(
            parse_suggestions("{\"1\":\"甲\"}", 1),
            Some(vec![(1, "甲".to_string())])
        );
        assert_eq!(
            parse_suggestions("[[1,\"甲\"],[2,\"乙\"]]", 2),
            Some(vec![(1, "甲".to_string()), (2, "乙".to_string())])
        );
        assert_eq!(
            parse_suggestions("{\"items\":[{\"index\":2,\"name\":\"乙\"}]}", 2),
            Some(vec![(2, "乙".to_string())])
        );
        // 序号 0 视为没给（照抄 `idx if idx else pos`）
        assert_eq!(
            parse_suggestions("[{\"i\":0,\"n\":\"甲\"}]", 1),
            Some(vec![(1, "甲".to_string())])
        );
        // 空数组/空对象解析不出条目 → 退回逐行，整行当成名字（照抄 Python）
        assert_eq!(
            parse_suggestions("[]", 1),
            Some(vec![(1, "[]".to_string())])
        );
        assert_eq!(
            parse_suggestions("{}", 1),
            Some(vec![(1, "{}".to_string())])
        );
    }

    #[test]
    fn echo_simplify_strips_junk() {
        assert_eq!(
            echo_simplify("DSC_04321_IMG_20230815_海边日落_副本.jpg", false),
            "海边日落"
        );
        assert_eq!(
            echo_simplify("2024-05-01_会议纪要_final_v3.docx", false),
            "会议纪要_final_v3"
        );
        assert_eq!(
            echo_simplify("Screenshot_2023-01-02_at_10.30.00.png", false),
            ""
        );
        assert_eq!(echo_simplify("工作报告.docx", false), "工作报告");
    }

    #[test]
    fn instruction_file_roundtrip_strips_comments() {
        let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../_verify_tmp/core_tmp/llm_instr");
        let _ = std::fs::remove_dir_all(&dir);
        let path = dir.join("req.txt").to_string_lossy().into_owned();
        write_instruction(&path, "只留地名", true).unwrap();
        assert_eq!(read_instruction(&path), "只留地名");
        std::fs::write(&path, "# 注释\n\n# 又一行\n正文\n\n内部空行\n\n").unwrap();
        assert_eq!(read_instruction(&path), "正文\n\n内部空行");
        std::fs::write(&path, "\u{feff}# 注释\nBOM 之后的正文").unwrap();
        assert_eq!(read_instruction(&path), "BOM 之后的正文");
        assert_eq!(read_instruction(&dir.join("missing.txt").to_string_lossy()), DEFAULT_INSTRUCTION);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn endpoint_rules() {
        let mut cfg = Config::default();
        cfg.base_url = "https://api.deepseek.com".into();
        assert_eq!(chat_endpoint(&cfg), "https://api.deepseek.com/v1/chat/completions");
        cfg.base_url = "https://api.openai.com/v1/".into();
        assert_eq!(chat_endpoint(&cfg), "https://api.openai.com/v1/chat/completions");
        cfg.base_url = "https://api.openai.com/v1/chat/completions".into();
        assert_eq!(chat_endpoint(&cfg), "https://api.openai.com/v1/chat/completions");
        cfg.protocol = PROTOCOL_ANTHROPIC.into();
        cfg.base_url = "https://api.anthropic.com".into();
        assert_eq!(chat_endpoint(&cfg), "https://api.anthropic.com/v1/messages");
        cfg.protocol = PROTOCOL_RESPONSES.into();
        cfg.base_url = "https://api.openai.com/v1".into();
        assert_eq!(chat_endpoint(&cfg), "https://api.openai.com/v1/responses");
    }

    #[test]
    fn extract_three_protocols() {
        let chat = serde_json::json!({"choices":[{"message":{"content":"pong"}}]});
        assert_eq!(extract(&chat, PROTOCOL_CHAT).unwrap(), "pong");
        let anthropic = serde_json::json!({"content":[{"type":"text","text":"po"},{"text":"ng"}]});
        assert_eq!(extract(&anthropic, PROTOCOL_ANTHROPIC).unwrap(), "pong");
        let responses = serde_json::json!({"output":[{"content":[{"text":"po"},{"text":"ng"}]}]});
        assert_eq!(extract(&responses, PROTOCOL_RESPONSES).unwrap(), "pong");
        let flat = serde_json::json!({"output_text":"pong"});
        assert_eq!(extract(&flat, PROTOCOL_RESPONSES).unwrap(), "pong");
        let bad = extract(&serde_json::json!({"nope": 1}), PROTOCOL_CHAT).unwrap_err();
        assert!(bad.to_string().starts_with("回复格式异常："));
    }

    #[test]
    fn headers_follow_protocol() {
        let mut cfg = Config::default();
        cfg.api_key = "sk-1".into();
        let chat = headers_for(&cfg, true);
        assert!(chat.contains(&("Authorization".into(), "Bearer sk-1".into())));
        cfg.protocol = PROTOCOL_ANTHROPIC.into();
        let anth = headers_for(&cfg, true);
        assert!(anth.contains(&("x-api-key".into(), "sk-1".into())));
        assert!(!anth.iter().any(|(k, _)| k.eq_ignore_ascii_case("authorization")));
        // 自定义头覆盖同名
        cfg.headers = vec![("Accept".into(), "text/plain".into())];
        let over = headers_for(&cfg, true);
        assert!(over.contains(&("Accept".into(), "text/plain".into())));
        assert!(!over.contains(&("Accept".into(), "application/json".into())));
        // 不带鉴权时连 Bearer 都不发
        cfg.protocol = PROTOCOL_CHAT.into();
        let anon = headers_for(&cfg, false);
        assert!(!anon.iter().any(|(k, _)| k.eq_ignore_ascii_case("authorization")));
    }

    #[test]
    fn problem_messages() {
        let mut cfg = Config::default();
        cfg.api_key = String::new();
        assert!(cfg.problem().contains("NAMECAT_LLM_API_KEY"));
        assert!(cfg.problem().contains("NAMEKIT_LLM_API_KEY"));
        cfg.api_key = "k".into();
        assert_eq!(cfg.problem(), "");
        cfg.base_url = String::new();
        assert_eq!(cfg.problem(), "未配置 base_url");
    }
}
