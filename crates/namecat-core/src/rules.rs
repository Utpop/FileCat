//! 规则链（对应 `engine/rules.py`）。
//!
//! 原则：行为对齐**现实现**，不是"更正确"。本文件里带「照抄」字样的地方都是
//! 踩过的坑或 Python 的怪癖，改之前先想清楚会不会让用户的命名结果变化。
//!
//! 与 Python 版的差异（有意，且只此三处）：
//! 1. `randomize` / `@@N@@` 的随机串用自研的确定性 PRNG，**不**追求与
//!    Python 的 Mersenne Twister 位级一致。契约只有一条：同一次计划内
//!    「谁 + 第几号 + 规则」相同就一定得到同一串（预览显示的名字 = 落盘的名字）。
//! 2. 正则用 fancy-regex（回溯式，支持回引用/环视）。替换串的 Python 语法
//!    （`\1`、`\g<name>`）在 [`expand_template`] 里手工展开。
//! 3. `extras` 用 `serde_json::Map` 承载，能原样穿过 JSON 往返（布尔、数字
//!    都不变成字符串）——预设与撤销日志的格式兼容靠这个。

use crate::ctx::VarSource;
use crate::template as tpl;
use crate::text as txt;
use crate::zhconv;
use serde_json::{Map, Value};
use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};
use unicode_normalization::char::is_combining_mark;
use unicode_normalization::UnicodeNormalization;

// --------------------------------------------------------------------------- //
// 规则类型表
// --------------------------------------------------------------------------- //

/// 一种规则：标识 / 中文名 / 参数提示（顺序即展示顺序）/ 说明。
pub struct RuleKind {
    pub id: &'static str,
    pub text: &'static str,
    pub params: &'static [(&'static str, &'static str)],
    pub note: &'static str,
}

const A: &str = "a";
const B: &str = "b";
const C: &str = "c";

/// 28 种规则。与 `engine/rules.py:RULE_KINDS` 逐条对应，文案一个字都别改
/// （`--list-rules` 与界面提示直接用它）。
pub const RULE_KINDS: &[RuleKind] = &[
    RuleKind {
        id: "insert",
        text: "插入文字",
        params: &[
            (A, "位置 prefix/suffix/before/after"),
            (B, "锚点字符（before/after 用）"),
            (C, "要插入的文字"),
        ],
        note: "prefix=名字前，suffix=名字后，before/after=某字符之前/之后",
    },
    RuleKind {
        id: "delete",
        text: "删除文字",
        params: &[(A, "要删除的文字"), (B, ""), (C, "")],
        note: "删除所有出现",
    },
    RuleKind {
        id: "remove_chars",
        text: "删除指定字符集",
        params: &[(A, "要删掉的字符集合（逐个删）"), (B, ""), (C, "")],
        note: "a=\"-_. \" 去掉这些分隔符",
    },
    RuleKind {
        id: "replace",
        text: "文本替换",
        params: &[(A, "查找"), (B, "替换为"), (C, "")],
        note: "区分大小写",
    },
    RuleKind {
        id: "regex",
        text: "正则替换",
        params: &[(A, "正则表达式"), (B, "替换为（\\1 或 {name} 引用）"), (C, "")],
        note: "非法正则原样返回，不抛异常",
    },
    RuleKind {
        id: "keep_after",
        text: "只保留某字符之后",
        params: &[(A, "分隔符"), (B, ""), (C, "")],
        note: "",
    },
    RuleKind {
        id: "keep_before",
        text: "只保留某字符之前",
        params: &[(A, "分隔符"), (B, ""), (C, "")],
        note: "留空则按长度对半截断（与另外三条切取规则一致）",
    },
    RuleKind {
        id: "remove_after",
        text: "删除某字符之后",
        params: &[(A, "分隔符"), (B, ""), (C, "")],
        note: "keep_sep=1 时连同分隔符保留",
    },
    RuleKind {
        id: "remove_before",
        text: "删除某字符之前",
        params: &[(A, "分隔符"), (B, ""), (C, "")],
        note: "",
    },
    RuleKind {
        id: "prefix",
        text: "添加前缀",
        params: &[(A, "前缀"), (B, ""), (C, "")],
        note: "",
    },
    RuleKind {
        id: "suffix",
        text: "添加后缀",
        params: &[(A, "后缀"), (B, ""), (C, "")],
        note: "文件加在扩展名之前",
    },
    RuleKind {
        id: "case",
        text: "大小写",
        params: &[(A, "upper/lower/title/sentence/capitalize"), (B, ""), (C, "")],
        note: "",
    },
    RuleKind {
        id: "trim",
        text: "去首尾空格与尾点",
        params: &[(A, "要剥掉的字符集（默认空白）"), (B, ""), (C, "")],
        note: "",
    },
    RuleKind {
        id: "collapse",
        text: "合并重复字符",
        params: &[(A, "要合并的字符（如空格）"), (B, ""), (C, "")],
        note: "",
    },
    RuleKind {
        id: "number",
        text: "编号",
        params: &[
            (A, "起始值（默认 1）"),
            ("step", "步长（默认 1）"),
            (B, "补零宽度"),
            ("scope", "编号范围 global/folder/kind/folder_kind"),
            ("reverse", "倒序 1/0"),
            (C, "位置 prefix/suffix/before/after"),
        ],
        note: "发号与插号都在这一条里：起始值 + 步长逐项递增，补零宽度、编号范围、\
               倒序也挂在本规则上；before/after 的锚点字符用 ;sep= 指定",
    },
    RuleKind {
        id: "template",
        text: "模板",
        params: &[(A, "模板原文"), (B, ""), (C, "")],
        note: "可用 {n} {name} {ext} {date} {parent} 等，见 README",
    },
    RuleKind {
        id: "slug",
        text: "规范化为 URL 安全名",
        params: &[(A, "替换成什么（默认 _）"), (B, ""), (C, "")],
        note: "",
    },
    RuleKind {
        id: "translit",
        text: "转写（转拉丁字母）",
        params: &[(A, ""), (B, ""), (C, "")],
        note: "把西里尔 / 希腊字母与带音标的拉丁字母转成 ASCII；中文不做拼音转换（原样保留）",
    },
    RuleKind {
        id: "zhconv",
        text: "简繁转换",
        params: &[(A, "s2t（简转繁）/ t2s（繁转简）"), (B, ""), (C, "")],
        note: "逐字转换，表外字符（数字、字母、符号…）原样保留",
    },
    RuleKind {
        id: "hash",
        text: "追加文件校验值",
        params: &[
            (A, "md5/sha1/sha256/crc32"),
            (B, "取前 N 位（0=全取）"),
            (C, "位置 prefix/suffix"),
        ],
        note: "",
    },
    RuleKind {
        id: "filename_clean",
        text: "Windows 文件名清理",
        params: &[(A, ""), (B, "非法字符替换成（默认 _）"), (C, "")],
        note: "去非法字符、去尾随空格与点、防保留名",
    },
    RuleKind {
        id: "move",
        text: "移动字符位置",
        params: &[(A, "从第几个字符起"), (B, "取几个"), (C, "插到第几个位置")],
        note: "",
    },
    RuleKind {
        id: "pad_numbers",
        text: "数字补零",
        params: &[(A, "宽度"), (B, "填充字符（默认 0）"), (C, "")],
        note: "",
    },
    RuleKind {
        id: "renumber",
        text: "音视频文件序列化",
        params: &[
            (A, "起始值（默认 1）"),
            ("step", "步长（默认 1）"),
            (B, "补零宽度（默认 4）"),
            ("scope", "编号范围（默认 folder）"),
            ("reverse", "倒序 1/0"),
        ],
        note: "整个名字换成序号，扩展名原样保留：默认四位、每个文件夹各自从 0001 起，\
               适合照片 / 音视频批量序列化",
    },
    RuleKind {
        id: "randomize",
        text: "随机化",
        params: &[(A, "随机串长度"), (B, "字符集（默认 0-9a-z）"), (C, "位置 prefix/suffix")],
        note: "",
    },
    RuleKind {
        id: "date_insert",
        text: "插入日期",
        params: &[(A, "日期风格 yyyy-mm-dd"), (B, ""), (C, "位置 prefix/suffix")],
        note: "默认取文件修改时间",
    },
    RuleKind {
        id: "extension",
        text: "扩展名",
        params: &[
            (A, "改成什么扩展名（含点），b=remove/case 时见说明"),
            (B, "remove/lower/upper"),
            (C, ""),
        ],
        note: "a 非空则替换扩展名；b=remove 去掉扩展名",
    },
    RuleKind {
        id: "llm",
        text: "LLM 智能精简",
        params: &[(A, "整理要求（留空=用要求文件）"), (B, "输出语言（zh/en…）"), (C, "名字最大长度")],
        note: "调用大模型把名字里的无意义片段（编号、日期戳、相机前缀、下载站后缀…）\
               精简成可读短语；要求可在 %APPDATA%\\NameCat\\llm_instruction.txt 里改，\
               也可由 --llm-instruction 临时指定；整批一次请求，离线/无 Key 时自动跳过",
    },
];

/// 未知规则种类时的兜底（`--list-rules` 的可用列表也用它）。
pub const CLI_ALIASES: &[(&str, &str)] = &[
    ("keep-after", "keep_after"), ("keep_after", "keep_after"),
    ("keep-before", "keep_before"), ("keep_before", "keep_before"),
    ("remove-after", "remove_after"), ("remove_after", "remove_after"),
    ("remove-before", "remove_before"), ("remove_before", "remove_before"),
    ("remove-char", "remove_chars"), ("remove-chars", "remove_chars"),
    ("remove_char", "remove_chars"), ("strip-chars", "remove_chars"),
    ("replace", "replace"), ("regex", "regex"), ("regexp", "regex"),
    ("template", "template"), ("prefix", "prefix"), ("suffix", "suffix"),
    ("lower", "case_lower"), ("upper", "case_upper"), ("title", "case_title"),
    ("case-sentence", "case_sentence"), ("sentence", "case_sentence"),
    ("trim", "trim"), ("insert", "insert"), ("delete", "delete"),
    ("number", "number"), ("serialize", "number"), ("seq", "number"),
    ("slug", "slug"), ("translit", "translit"), ("hash", "hash"),
    ("zh", "zhconv"), ("zhconv", "zhconv"), ("s2t", "zhconv"), ("t2s", "zhconv"),
    ("简繁", "zhconv"),
    ("clean", "filename_clean"), ("filename-clean", "filename_clean"),
    ("move", "move"), ("pad", "pad_numbers"), ("pad-numbers", "pad_numbers"),
    ("collapse", "collapse"), ("randomize", "randomize"), ("random", "randomize"),
    ("date", "date_insert"), ("date-insert", "date_insert"),
    ("ext", "extension"), ("extension", "extension"),
    ("llm", "llm"), ("ai", "llm"), ("smart", "llm"), ("llm-clean", "llm"),
];

pub const INVALID_NAME_CHARS: &str = "\\/:*?\"<>|";
pub const MAX_NAME_LEN: i64 = 255;
pub const MAX_RANDOM: i64 = 64;

const SCOPE_SHORT: &[(&str, &str)] = &[
    ("folder", "每文件夹重新编号"),
    ("kind", "每类型重新编号"),
    ("folder_kind", "每文件夹每类型"),
];

/// 规则种类中文名（`RULE_KIND_TEXT`）。
pub fn rule_kind_text(kind: &str) -> Option<&'static str> {
    RULE_KINDS.iter().find(|k| k.id == kind).map(|k| k.text)
}

/// 规则参数提示（`RULE_PARAM_HINT`）。
pub fn rule_param_hint(kind: &str) -> Option<&'static [(&'static str, &'static str)]> {
    RULE_KINDS.iter().find(|k| k.id == kind).map(|k| k.params)
}

/// 规则说明（`RULE_NOTE`）。
pub fn rule_note(kind: &str) -> Option<&'static str> {
    RULE_KINDS.iter().find(|k| k.id == kind).map(|k| k.note)
}

/// 规则种类 id 列表（`RULE_KIND_IDS`）。
pub fn rule_kind_ids() -> Vec<&'static str> {
    RULE_KINDS.iter().map(|k| k.id).collect()
}

pub fn is_reserved_name(name: &str) -> bool {
    let low = name.to_lowercase();
    match low.as_str() {
        "con" | "prn" | "aux" | "nul" => return true,
        _ => {}
    }
    if let Some(rest) = low.strip_prefix("com") {
        if let Ok(n) = rest.parse::<u32>() {
            return (1..=9).contains(&n) && rest.len() == 1;
        }
    }
    if let Some(rest) = low.strip_prefix("lpt") {
        if let Ok(n) = rest.parse::<u32>() {
            return (1..=9).contains(&n) && rest.len() == 1;
        }
    }
    false
}

// --------------------------------------------------------------------------- //
// JSON 取值助手（脏数据一律兜底，不 panic）
// --------------------------------------------------------------------------- //

/// Python `str(value)` 的近似：布尔是 True/False、None 是 None。
pub fn value_text(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::String(s) => s.clone(),
        Value::Number(n) => n.to_string(),
        other => other.to_string(),
    }
}

/// Python 的真值判定（`bool(x)`）。
pub fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

fn extras_text(extras: &Map<String, Value>, key: &str) -> String {
    match extras.get(key) {
        Some(v) => value_text(v),
        None => String::new(),
    }
}

fn extras_truthy(extras: &Map<String, Value>, key: &str) -> bool {
    extras.get(key).map(truthy).unwrap_or(false)
}

// --------------------------------------------------------------------------- //
// 规则对象
// --------------------------------------------------------------------------- //

/// 一条规则。字段与 Python 的 `Rule` dataclass 一一对应。
#[derive(Debug, Clone, PartialEq)]
pub struct Rule {
    pub kind: String,
    pub a: String,
    pub b: String,
    pub c: String,
    pub enabled: bool,
    pub id: String,
    pub selector: String,
    pub charset: String,
    pub occurrence: i64,
    pub skip_ext: bool,
    pub keep_sep: bool,
    pub extras: Map<String, Value>,
}

impl Default for Rule {
    fn default() -> Self {
        Self {
            kind: "replace".to_string(),
            a: String::new(),
            b: String::new(),
            c: String::new(),
            enabled: true,
            id: String::new(),
            selector: "text".to_string(),
            charset: String::new(),
            occurrence: 1,
            skip_ext: false,
            keep_sep: false,
            extras: Map::new(),
        }
    }
}

/// 规则 id：`r_` + md5(seed + uuid4)[:12]。
///
/// 必须跨进程稳定（Python 版特意避开内置 `hash()`，因为 PYTHONHASHSEED
/// 会让同一份配置在不同进程算出不同 id，randomize 的种子也跟着漂）。
pub fn new_id(seed: &str) -> String {
    use md5::{Digest, Md5};
    let raw = format!("{seed}|{}", uuid::Uuid::new_v4().simple());
    let mut hasher = Md5::new();
    md5::Digest::update(&mut hasher, raw.as_bytes());
    let digest = hasher.finalize();
    let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    format!("r_{}", &hex[..12])
}

fn alias(kind: &str) -> Option<&'static str> {
    let lowered = kind.to_lowercase();
    CLI_ALIASES
        .iter()
        .find(|(from, _)| *from == lowered.as_str())
        .map(|(_, to)| *to)
}

/// 补齐缺省字段、把别名映射成规范 kind（原地语义，返回新值）。
pub fn normalize(mut rule: Rule) -> Rule {
    // 对齐 `str(rule.kind or "replace").strip()`：先补默认值**再**去空白，
    // 所以 kind="  " 会变成空串、接着落进下面的"未知规则"分支（extras 里会
    // 留下一个空的 unknown_kind）—— 这是现有行为，别"顺手修好"。
    let mut kind = if rule.kind.is_empty() {
        "replace".to_string()
    } else {
        rule.kind.clone()
    };
    kind = kind.trim().to_string();
    if let Some(mapped) = alias(&kind) {
        kind = mapped.to_string();
    }
    for (prefix, mode) in [
        ("case_lower", "lower"),
        ("case_upper", "upper"),
        ("case_title", "title"),
        ("case_sentence", "sentence"),
    ] {
        if kind == prefix {
            kind = "case".to_string();
            rule.a = mode.to_string();
        }
    }
    if !rule_kind_ids().contains(&kind.as_str()) {
        // 认不出来的 kind（手改过的预设/旧版本规则）：按"文本替换"兜底，
        // 但留个记号 —— 界面会明确标出来，而不是静默变成一条空规则
        rule.extras
            .insert("unknown_kind".to_string(), Value::String(kind.clone()));
        kind = "replace".to_string();
    }
    rule.kind = kind;
    if rule.id.is_empty() {
        rule.id = new_id(&format!("{}{}{}{}", rule.kind, rule.a, rule.b, rule.c));
    }
    let selector = rule.selector.trim().to_lowercase();
    rule.selector = if tpl::SELECTOR_KINDS.contains(&selector.as_str()) {
        selector
    } else {
        "text".to_string()
    };
    let occ = rule.occurrence;
    rule.occurrence = if occ == 0 { 1 } else { occ };
    if rule.kind == "llm" {
        // a/b/c 是「要求原文 / 语言 / 最大字数」，统一落进 extras：
        // 手工构造、JSON 读取、命令行解析因此得到同一种规则（单一实现）
        let instruction = non_empty_or(
            extras_text(&rule.extras, "instruction"),
            non_empty_or(rule.a.clone(), String::new()),
        );
        let lang = non_empty_or(extras_text(&rule.extras, "lang"), non_empty_or(rule.b.clone(), "zh".into()));
        let max_len = non_empty_or(
            extras_text(&rule.extras, "max_len"),
            non_empty_or(rule.c.clone(), "24".into()),
        );
        rule.extras.insert("instruction".into(), Value::String(instruction));
        rule.extras.insert("lang".into(), Value::String(lang));
        rule.extras.insert("max_len".into(), Value::String(max_len));
    }
    rule
}

fn non_empty_or(first: String, fallback: String) -> String {
    if first.is_empty() { fallback } else { first }
}

/// `make_rule(kind, a, b, c)`：构造 + 归一化。
pub fn make_rule(kind: &str, a: &str, b: &str, c: &str) -> Rule {
    normalize(Rule {
        kind: kind.to_string(),
        a: a.to_string(),
        b: b.to_string(),
        c: c.to_string(),
        ..Rule::default()
    })
}

/// 展示用：一条规则的一句话说明（界面/命令行同一套文案）。
pub fn describe(rule: &Rule) -> String {
    let unknown = extras_text(&rule.extras, "unknown_kind");
    let name = rule_kind_text(&rule.kind).unwrap_or(rule.kind.as_str());
    if !unknown.is_empty() {
        return format!("未知规则（{unknown}）已按文本替换处理");
    }
    let mut scope = String::new();
    if !rule.selector.is_empty() && rule.selector != "text" {
        let sel_text = tpl::SELECTOR_TEXT
            .iter()
            .find(|(k, _)| *k == rule.selector)
            .map(|(_, v)| *v)
            .unwrap_or(rule.selector.as_str());
        scope = format!(
            "（作用于{sel_text}{}）",
            if rule.charset.is_empty() {
                String::new()
            } else {
                format!("「{}」", rule.charset)
            }
        );
    }
    if rule.occurrence != 1
        && ["after", "before", "from", "until"].contains(&rule.selector.as_str())
    {
        scope.push_str(&format!(
            "（第{}次出现）",
            if rule.occurrence == -1 {
                "最后".to_string()
            } else {
                rule.occurrence.to_string()
            }
        ));
    }
    if rule.skip_ext {
        scope.push_str("（不含扩展名）");
    }

    if rule.kind == "number" || rule.kind == "renumber" {
        // 号码的位置才是「编号」的语义；「序列化」整名就是号码
        let head = if rule.kind == "number" {
            let pos_raw = if rule.c.is_empty() { "prefix".to_string() } else { rule.c.clone() };
            let pos = match pos_raw.trim().to_lowercase().as_str() {
                "prefix" => "名字前面",
                "suffix" => "名字后面",
                "before" => "锚点之前",
                "after" => "锚点之后",
                _ => "名字前面",
            };
            format!("{name} @{pos}")
        } else {
            String::new()
        };
        let body = number_bits(rule).join(" ");
        return format!("{head}{}{scope}", if body.is_empty() { String::new() } else { format!(" {body}") })
            .trim()
            .to_string();
    }

    let mut head = name.to_string();
    let mut params: Vec<String>;
    if rule.kind == "insert" {
        head = format!(
            "{name} @{}",
            if rule.a.is_empty() { "suffix".to_string() } else { rule.a.clone() }
        );
        params = [rule.b.clone(), rule.c.clone()]
            .into_iter()
            .filter(|p| !p.is_empty())
            .collect();
    } else {
        params = [rule.a.clone(), rule.b.clone(), rule.c.clone()]
            .into_iter()
            .filter(|p| !p.is_empty())
            .collect();
    }
    if rule.kind == "zhconv" {
        // 参数是 s2t/t2s 这种代号，直接摆出来没人看得懂，换成方向文案
        let mode = if rule.a.is_empty() { "s2t".to_string() } else { rule.a.clone() };
        head = format!(
            "{name}（{}）",
            if mode.trim().to_lowercase() != "t2s" { "简→繁" } else { "繁→简" }
        );
        params.clear();
    }
    let body = params
        .iter()
        .map(|p| format!("「{p}」"))
        .collect::<Vec<_>>()
        .join("  ");
    format!("{head}{}{scope}", if body.is_empty() { String::new() } else { format!(" {body}") })
        .trim()
        .to_string()
}

/// 发号参数的一串短说明（编号 / 序列化共用）。
pub fn number_bits(rule: &Rule) -> Vec<String> {
    let mut bits = Vec::new();
    if !rule.a.is_empty() {
        bits.push(format!("起始 {}", rule.a));
    }
    let step = extras_text(&rule.extras, "step").trim().to_string();
    if !step.is_empty() && step != "1" {
        bits.push(format!("步长 {step}"));
    }
    if !rule.b.is_empty() && rule.b != "0" {
        bits.push(format!("补零 {} 位", rule.b));
    }
    let scope = extras_text(&rule.extras, "scope").trim().to_string();
    if !scope.is_empty() && scope != "global" {
        let short = SCOPE_SHORT
            .iter()
            .find(|(k, _)| *k == scope)
            .map(|(_, v)| *v)
            .unwrap_or(scope.as_str());
        bits.push(short.to_string());
    }
    let reverse = extras_text(&rule.extras, "reverse").trim().to_lowercase();
    if !["", "0", "false"].contains(&reverse.as_str()) {
        bits.push("倒序".to_string());
    }
    bits
}

/// 序列化：字段顺序与 Python 的 `to_dict()` 一致。
pub fn to_dict(rule: &Rule) -> Value {
    let mut map = Map::new();
    map.insert("id".into(), Value::String(rule.id.clone()));
    map.insert("kind".into(), Value::String(rule.kind.clone()));
    map.insert("a".into(), Value::String(rule.a.clone()));
    map.insert("b".into(), Value::String(rule.b.clone()));
    map.insert("c".into(), Value::String(rule.c.clone()));
    map.insert("enabled".into(), Value::Bool(rule.enabled));
    map.insert("selector".into(), Value::String(rule.selector.clone()));
    map.insert("charset".into(), Value::String(rule.charset.clone()));
    map.insert("occurrence".into(), Value::from(rule.occurrence));
    map.insert("skip_ext".into(), Value::Bool(rule.skip_ext));
    map.insert("keep_sep".into(), Value::Bool(rule.keep_sep));
    map.insert("extras".into(), Value::Object(rule.extras.clone()));
    Value::Object(map)
}

/// 发号参数（计划层的输入）。空值表示"不指定"，交给 PlanOptions 的默认值。
#[derive(Debug, Default, Clone, PartialEq)]
pub struct NumberOptions {
    pub start: Option<i64>,
    pub step: Option<i64>,
    pub width: Option<i64>,
    pub scope: Option<String>,
    pub reverse: bool,
}

pub fn number_options(rule: &Rule) -> NumberOptions {
    let mut out = NumberOptions::default();
    let a = rule.a.trim().to_string();
    if !a.is_empty() {
        if let Ok(n) = a.parse::<i64>() {
            out.start = Some(n);
        }
    }
    let step = extras_text(&rule.extras, "step").trim().to_string();
    if !step.is_empty() {
        if let Ok(n) = step.parse::<i64>() {
            out.step = Some(n);
        }
    }
    let b = rule.b.trim().to_string();
    if !b.is_empty() {
        if let Ok(n) = b.parse::<i64>() {
            out.width = Some(n.clamp(0, tpl::MAX_WIDTH));
        }
    }
    let scope = extras_text(&rule.extras, "scope").trim().to_string();
    if !scope.is_empty() {
        out.scope = Some(scope);
    }
    let reverse = extras_text(&rule.extras, "reverse").trim().to_lowercase();
    out.reverse = !["", "0", "false", "no"].contains(&reverse.as_str());
    out
}

/// 链里第一条启用的发号规则（编号 / 序列化）。
pub fn number_rule(rules: &[Rule]) -> Option<&Rule> {
    rules
        .iter()
        .find(|r| matches!(r.kind.as_str(), "number" | "renumber") && r.enabled)
}

// --------------------------------------------------------------------------- //
// 各类变换
// --------------------------------------------------------------------------- //

const TRANSLIT: &[(&str, &str)] = &[
    ("а", "a"), ("б", "b"), ("в", "v"), ("г", "g"), ("д", "d"), ("е", "e"), ("ё", "yo"),
    ("ж", "zh"), ("з", "z"), ("и", "i"), ("й", "y"), ("к", "k"), ("л", "l"), ("м", "m"),
    ("н", "n"), ("о", "o"), ("п", "p"), ("р", "r"), ("с", "s"), ("т", "t"), ("у", "u"),
    ("ф", "f"), ("х", "kh"), ("ц", "ts"), ("ч", "ch"), ("ш", "sh"), ("щ", "sch"), ("ъ", ""),
    ("ы", "y"), ("ь", ""), ("э", "e"), ("ю", "yu"), ("я", "ya"),
    ("α", "a"), ("β", "b"), ("γ", "g"), ("δ", "d"), ("ε", "e"), ("ζ", "z"), ("η", "i"),
    ("θ", "th"), ("ι", "i"), ("κ", "k"), ("λ", "l"), ("μ", "m"), ("ν", "n"), ("ξ", "x"),
    ("ο", "o"), ("π", "p"), ("ρ", "r"), ("σ", "s"), ("ς", "s"), ("τ", "t"), ("υ", "y"),
    ("φ", "f"), ("χ", "ch"), ("ψ", "ps"), ("ω", "o"),
];

/// 转写成拉丁字母。表内命中就查表；否则 NFKD 分解后丢掉组合符号。
pub fn translit(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for ch in text.chars() {
        let lowered: Vec<char> = ch.to_lowercase().collect();
        // Python 的 `ch.lower() in 表` 只有单字符小写才算命中（İ.lower() 是两个字符）
        let hit = if lowered.len() == 1 {
            TRANSLIT
                .iter()
                .find(|(k, _)| k.chars().next() == Some(lowered[0]))
                .map(|(_, mapped)| *mapped)
        } else {
            None
        };
        if let Some(mapped) = hit {
            if ch.is_uppercase() && !mapped.is_empty() {
                out.push_str(&txt::capitalize(mapped));
            } else {
                out.push_str(mapped);
            }
            continue;
        }
        for c in ch.to_string().nfkd() {
            if !is_combining_mark(c) {
                out.push(c);
            }
        }
    }
    out
}

/// 规范化成 URL 安全名。
pub fn slugify(text: &str, sep: &str) -> String {
    let sep = if sep.is_empty() { "_" } else { sep };
    let mut out = translit(text);
    out = out.nfkd().filter(|c| !is_combining_mark(*c)).collect();
    out = regex_replace("[^0-9A-Za-z]+", &escape_regex(sep), &out).unwrap_or(out);
    out = regex_replace(&format!("{}{{2,}}", escape_regex(sep)), sep, &out).unwrap_or(out);
    out.trim_matches(|c| sep.contains(c)).to_lowercase()
}

/// Windows 文件名清理：非法字符、尾随空格与点、保留名。
pub fn clean_filename(text: &str, repl: &str) -> String {
    let repl: String = if repl.is_empty() { "_".into() } else { repl.to_string() };
    let repl_char = repl.chars().next().unwrap_or('_');
    let mut out: String = text
        .chars()
        .map(|c| {
            if INVALID_NAME_CHARS.contains(c) || (c as u32) < 32 {
                repl_char
            } else {
                c
            }
        })
        .collect();
    out = out.trim_end_matches([' ', '.']).to_string();
    let head = out.split('.').next().unwrap_or("").trim().to_lowercase();
    if is_reserved_name(&head) {
        out = format!("_{out}");
    }
    out.chars().take(MAX_NAME_LEN as usize).collect()
}

/// 移动字符位置（按码点）。
pub fn move_chars(text: &str, start: i64, count: i64, dest: i64) -> String {
    let chars: Vec<char> = text.chars().collect();
    let len = chars.len() as i64;
    if start < 1 || count < 1 || start - 1 >= len {
        return text.to_string();
    }
    let from = (start - 1) as usize;
    let to = ((start - 1 + count) as usize).min(chars.len());
    let chunk: String = chars[from..to].iter().collect();
    let mut rest: Vec<char> = Vec::with_capacity(chars.len());
    rest.extend_from_slice(&chars[..from]);
    rest.extend_from_slice(&chars[to..]);
    let pos = (dest - 1).clamp(0, rest.len() as i64) as usize;
    let mut out: String = rest[..pos].iter().collect();
    out.push_str(&chunk);
    out.push_str(&rest[pos..].iter().collect::<String>());
    out
}

/// 数字补零（对每一段连续数字）。
pub fn pad_numbers(text: &str, width: i64, fill: &str) -> String {
    let w = width.clamp(0, tpl::MAX_WIDTH);
    if w <= 0 {
        return text.to_string();
    }
    let fill_char = fill.chars().next().unwrap_or('0');
    let mut out = String::with_capacity(text.len());
    let mut digits = String::new();
    let flush = |digits: &mut String, out: &mut String| {
        if digits.is_empty() {
            return;
        }
        let len = digits.chars().count() as i64;
        if len < w {
            for _ in 0..(w - len) {
                out.push(fill_char);
            }
        }
        out.push_str(digits);
        digits.clear();
    };
    for ch in text.chars() {
        if ch.is_ascii_digit() {
            digits.push(ch);
        } else {
            flush(&mut digits, &mut out);
            out.push(ch);
        }
    }
    flush(&mut digits, &mut out);
    out
}

/// 文件校验值。算法不认识或读不了文件时返回空串（调用方原样返回）。
pub fn file_digest(path: &str, algo: &str) -> String {
    use std::io::Read;
    let algo = if algo.is_empty() { "md5" } else { algo }.to_lowercase();
    use md5::Digest as _;
    let mut file = match std::fs::File::open(path) {
        Ok(f) => f,
        Err(_) => return String::new(),
    };
    if algo == "crc32" {
        let mut hasher = crc32fast::Hasher::new();
        let mut buf = vec![0u8; 1 << 20];
        loop {
            match file.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => hasher.update(&buf[..n]),
                Err(_) => return String::new(),
            }
        }
        return format!("{:08x}", hasher.finalize());
    }
    let mut buf = vec![0u8; 1 << 20];
    fn drain<D: std::io::Read, H: DigestLike>(file: &mut D, buf: &mut [u8], h: &mut H) -> Option<()> {
        loop {
            match file.read(buf) {
                Ok(0) => return Some(()),
                Ok(n) => h.update(&buf[..n]),
                Err(_) => return None,
            }
        }
    }
    // hashlib.new(algo) 支持很多算法，这里只保常用四个，其余按"不认识"处理
    let hex = match algo.as_str() {
        "md5" => {
            let mut h = md5::Md5::new();
            if drain(&mut file, &mut buf, &mut h).is_none() {
                return String::new();
            }
            hex_of(h.finalize())
        }
        "sha1" => {
            let mut h = sha1::Sha1::new();
            if drain(&mut file, &mut buf, &mut h).is_none() {
                return String::new();
            }
            hex_of(h.finalize())
        }
        "sha256" => {
            use sha2::Sha256;
            let mut h = Sha256::new();
            if drain(&mut file, &mut buf, &mut h).is_none() {
                return String::new();
            }
            hex_of(h.finalize())
        }
        "sha512" => {
            use sha2::Sha512;
            let mut h = Sha512::new();
            if drain(&mut file, &mut buf, &mut h).is_none() {
                return String::new();
            }
            hex_of(h.finalize())
        }
        _ => return String::new(),
    };
    hex
}

trait DigestLike {
    fn update(&mut self, data: &[u8]);
}

impl DigestLike for md5::Md5 {
    fn update(&mut self, data: &[u8]) {
        md5::Digest::update(self, data)
    }
}

impl DigestLike for sha1::Sha1 {
    fn update(&mut self, data: &[u8]) {
        sha1::Digest::update(self, data)
    }
}

impl DigestLike for sha2::Sha256 {
    fn update(&mut self, data: &[u8]) {
        sha2::Digest::update(self, data)
    }
}

impl DigestLike for sha2::Sha512 {
    fn update(&mut self, data: &[u8]) {
        sha2::Digest::update(self, data)
    }
}

fn hex_of(bytes: impl AsRef<[u8]>) -> String {
    bytes.as_ref().iter().map(|b| format!("{b:02x}")).collect()
}

fn case_convert(text: &str, mode: &str) -> String {
    let mode = if mode.trim().is_empty() { "lower" } else { mode.trim() }.to_lowercase();
    match mode.as_str() {
        "upper" => text.to_uppercase(),
        "lower" => text.to_lowercase(),
        "title" => txt::title(text),
        "sentence" => txt::sentence(text),
        "capitalize" => txt::capitalize(text),
        "swap" => txt::swapcase(text),
        _ => text.to_string(),
    }
}

fn insert_text(text: &str, where_: &str, anchor: &str, payload: &str) -> String {
    if payload.is_empty() {
        return text.to_string();
    }
    let where_ = if where_.trim().is_empty() { "suffix" } else { where_ }.trim().to_lowercase();
    match where_.as_str() {
        "prefix" => format!("{payload}{text}"),
        "suffix" => format!("{text}{payload}"),
        _ => {
            if anchor.is_empty() {
                return format!("{text}{payload}");
            }
            let idx = match text.find(anchor) {
                Some(i) => text[..i].chars().count(),
                None => return text.to_string(),
            };
            let chars: Vec<char> = text.chars().collect();
            let head: String = chars[..idx].iter().collect();
            let rest: String = chars[idx..].iter().collect();
            if where_ == "before" {
                format!("{head}{payload}{rest}")
            } else if where_ == "after" {
                let skip = anchor.chars().count();
                let tail: String = chars[idx + skip..].iter().collect();
                let mid: String = chars[idx..idx + skip].iter().collect();
                format!("{head}{mid}{payload}{tail}")
            } else {
                text.to_string()
            }
        }
    }
}

// --------------------------------------------------------------------------- //
// 正则：Python re 语义的一层薄封装
// --------------------------------------------------------------------------- //

/// `re.escape`：非字母数字一律加反斜杠（比 Python 3.7 更保守，但语义等价）。
pub fn escape_regex(text: &str) -> String {
    let mut out = String::with_capacity(text.len() * 2);
    for ch in text.chars() {
        if ch.is_alphanumeric() || ch == '_' {
            out.push(ch);
        } else {
            out.push('\\');
            out.push(ch);
        }
    }
    out
}

thread_local! {
    /// 编译缓存：一个规则链上万个文件时，别每个文件都重新编译同一条正则。
    static CACHE: std::cell::RefCell<HashMap<String, fancy_regex::Regex>> =
        std::cell::RefCell::new(HashMap::new());
}

fn compile(pattern: &str) -> Option<fancy_regex::Regex> {
    CACHE.with(|cache| {
        if let Some(re) = cache.borrow().get(pattern) {
            return Some(re.clone());
        }
        match fancy_regex::Regex::new(pattern) {
            Ok(re) => {
                let mut cache = cache.borrow_mut();
                if cache.len() > 128 {
                    cache.clear();      // 简单兜底，不给长跑进程留无限增长
                }
                cache.insert(pattern.to_string(), re.clone());
                Some(re)
            }
            Err(_) => None,             // 非法正则 → 调用方原样返回
        }
    })
}

/// Python 替换串模板展开（`m.expand(template)` 的等价物）。
///
/// 支持 `\1`..`\99`、`\g<name>`、`\g<1>`、`\\`、`\n \t \r \f \v \a \b`、
/// `\xHH`、`\0oo` 八进制。组不存在时返回 None（Python 抛 re.error，
/// 调用方按"原样返回"处理）。
pub fn expand_template(template: &str, caps: &fancy_regex::Captures<'_, str>) -> Option<String> {
    let chars: Vec<char> = template.chars().collect();
    let mut out = String::with_capacity(template.len());
    let mut i = 0;
    while i < chars.len() {
        if chars[i] != '\\' {
            out.push(chars[i]);
            i += 1;
            continue;
        }
        if i + 1 >= chars.len() {
            // 末尾孤立反斜杠：Python 报 bad escape，这里按字面保留
            out.push('\\');
            break;
        }
        let next = chars[i + 1];
        match next {
            '1'..='9' => {
                // 最多两位数字，优先整组匹配
                let mut num = String::new();
                num.push(next);
                let mut j = i + 2;
                if j < chars.len() && chars[j].is_ascii_digit() {
                    let two = format!("{num}{}", chars[j]);
                    if group_text(caps, &two).is_some() {
                        num = two;
                        j += 1;
                    }
                }
                out.push_str(&group_text(caps, &num)?);
                i = j;
            }
            'g' => {
                if i + 2 < chars.len() && chars[i + 2] == '<' {
                    let mut j = i + 3;
                    let mut name = String::new();
                    while j < chars.len() && chars[j] != '>' {
                        name.push(chars[j]);
                        j += 1;
                    }
                    if j >= chars.len() {
                        return None;
                    }
                    out.push_str(&group_text(caps, &name)?);
                    i = j + 1;
                } else {
                    out.push('\\');
                    out.push('g');
                    i += 2;
                }
            }
            'n' => {
                out.push('\n');
                i += 2;
            }
            't' => {
                out.push('\t');
                i += 2;
            }
            'r' => {
                out.push('\r');
                i += 2;
            }
            'f' => {
                out.push('\u{c}');
                i += 2;
            }
            'v' => {
                out.push('\u{b}');
                i += 2;
            }
            'a' => {
                out.push('\u{7}');
                i += 2;
            }
            'b' => {
                out.push('\u{8}');
                i += 2;
            }
            '\\' => {
                out.push('\\');
                i += 2;
            }
            'x' => {
                let hex: String = chars.iter().skip(i + 2).take(2).collect();
                if hex.len() == 2 {
                    if let Ok(code) = u8::from_str_radix(&hex, 16) {
                        out.push(code as char);
                        i += 4;
                        continue;
                    }
                }
                out.push('\\');
                out.push('x');
                i += 2;
            }
            '0' => {
                let oct: String = chars.iter().skip(i + 1).take(3).collect();
                let trimmed = oct.trim_start_matches('0');
                let digits: String = oct.chars().filter(|c| c.is_digit(8)).collect();
                let _ = trimmed;
                if !digits.is_empty() {
                    if let Ok(code) = u32::from_str_radix(&digits, 8) {
                        if let Some(c) = char::from_u32(code) {
                            out.push(c);
                            i += 1 + digits.len();
                            continue;
                        }
                    }
                }
                out.push('\\');
                out.push('0');
                i += 2;
            }
            other => {
                // Python：未知的 ASCII 字母转义会报错；其余按字面
                if other.is_ascii_alphabetic() {
                    return None;
                }
                out.push(other);
                i += 2;
            }
        }
    }
    Some(out)
}

fn group_text(caps: &fancy_regex::Captures<'_, str>, name: &str) -> Option<String> {
    if let Ok(idx) = name.parse::<usize>() {
        return caps.get(idx).map(|m| m.as_str().to_string());
    }
    caps.name(name).map(|m| m.as_str().to_string())
}

/// `re.sub` 的等价物；非法正则返回 None（调用方原样返回）。
pub fn regex_replace(pattern: &str, repl: &str, text: &str) -> Option<String> {
    let re = compile(pattern)?;
    let mut failed = false;
    let out = re.replace_all(text, |caps: &fancy_regex::Captures<'_, str>| {
        match expand_template(repl, caps) {
            Some(s) => s,
            None => {
                failed = true;
                String::new()
            }
        }
    });
    if failed {
        return None;
    }
    Some(out.into_owned())
}

// --------------------------------------------------------------------------- //
// transform / apply
// --------------------------------------------------------------------------- //

/// 号码 + 补零。号码由计划层算好传进来，这里只决定宽度。
fn digits_text(rule: &Rule, num: i64, width: i64) -> String {
    let b = rule.b.trim().to_string();
    let w = if b.is_empty() {
        width
    } else {
        b.parse::<i64>().unwrap_or(width)
    };
    tpl::num_text(num, w.clamp(0, tpl::MAX_WIDTH))
}

fn split_ext(text: &str) -> (String, String) {
    crate::text::split_ext(text)
}

/// 对已经切好的片段应用规则本身（不含 selector / 扩展名保护）。
pub fn transform(
    rule: &Rule,
    text: &str,
    num: i64,
    width: i64,
    ctx: Option<&dyn VarSource>,
) -> String {
    let a = rule.a.as_str();
    let b = rule.b.as_str();
    let c = rule.c.as_str();
    match rule.kind.as_str() {
        "insert" => insert_text(text, a, b, c),
        "delete" => {
            if a.is_empty() { text.to_string() } else { text.replace(a, "") }
        }
        "remove_chars" | "strip" => {
            if a.is_empty() {
                text.to_string()
            } else {
                text.chars().filter(|ch| !a.contains(*ch)).collect()
            }
        }
        "replace" => {
            if a.is_empty() {
                return text.to_string();
            }
            if extras_truthy(&rule.extras, "regex") {
                return regex_replace(a, b, text).unwrap_or_else(|| text.to_string());
            }
            text.replace(a, b)
        }
        "regex" => {
            if a.is_empty() {
                return text.to_string();
            }
            let expand = match rule.extras.get("expand") {
                Some(v) => truthy(v),
                None => true,
            };
            if !expand {
                return regex_replace(a, b, text).unwrap_or_else(|| text.to_string());
            }
            let re = match compile(a) {
                Some(re) => re,
                None => return text.to_string(),
            };
            let mut failed = false;
            let result = re.replace_all(text, |caps: &fancy_regex::Captures<'_, str>| {
                let expanded = match expand_template(b, caps) {
                    Some(v) => v,
                    None => {
                        failed = true;
                        return String::new();
                    }
                };
                let (start, end) = match caps.get(0) {
                    Some(m) => (m.start(), m.end()),
                    None => (0, 0),
                };
                // 这里的 start/end 是字节下标且必然落在字符边界上
                let left = &text[..start];
                let right = &text[end..];
                tpl::expand_refs(&expanded, Some(&CapsGroup(caps)), ctx, num, width, left, right)
            });
            if failed {
                return text.to_string();
            }
            result.into_owned()
        }
        "keep_after" => {
            if a.is_empty() {
                return tpl::split_half(text);
            }
            let idx = tpl::cut_index(text, a, rule.occurrence);
            if idx < 0 {
                return text.to_string();
            }
            chars_from(text, idx + 1)
        }
        "keep_before" => {
            if a.is_empty() {
                return tpl::split_half(text);
            }
            let idx = tpl::cut_index(text, a, rule.occurrence);
            if idx < 0 {
                return text.to_string();
            }
            chars_take(text, idx)
        }
        "remove_after" => {
            if a.is_empty() {
                return tpl::split_half(text);
            }
            let idx = tpl::cut_index(text, a, rule.occurrence);
            if idx < 0 {
                return text.to_string();
            }
            let keep = rule.keep_sep || c == "1";
            chars_take(text, if keep { idx + 1 } else { idx })
        }
        "remove_before" => {
            if a.is_empty() {
                return tpl::split_half(text);
            }
            let idx = tpl::cut_index(text, a, rule.occurrence);
            if idx < 0 {
                return text.to_string();
            }
            let keep = rule.keep_sep || c == "1";
            chars_from(text, if keep { idx } else { idx + 1 })
        }
        "prefix" => format!("{a}{text}"),
        "suffix" => format!("{text}{a}"),
        "case" => case_convert(text, a),
        "trim" => {
            let out = if a.is_empty() {
                text.trim().to_string()
            } else {
                text.trim_matches(|ch| a.contains(ch)).to_string()
            };
            if a.is_empty() { out.trim_end_matches('.').to_string() } else { out }
        }
        "collapse" => {
            if a.is_empty() {
                return text.to_string();
            }
            let pattern = format!("(?:{})+", escape_regex(a));
            regex_replace(&pattern, a, text).unwrap_or_else(|| text.to_string())
        }
        "number" => {
            let digits = digits_text(rule, num, width);
            let anchor = rule.charset.trim();
            let pos = if c.trim().is_empty() { "prefix" } else { c.trim() }.to_lowercase();
            match pos.as_str() {
                "suffix" => format!("{text}{digits}"),
                "before" => match text.find(anchor) {
                    Some(i) if !anchor.is_empty() => {
                        let idx = text[..i].chars().count();
                        let (head, tail) = split_chars(text, idx as i64);
                        format!("{head}{digits}{tail}")
                    }
                    _ => format!("{digits}{text}"),
                },
                "after" => match text.find(anchor) {
                    Some(i) if !anchor.is_empty() => {
                        let idx = text[..i].chars().count() + anchor.chars().count();
                        let (head, tail) = split_chars(text, idx as i64);
                        format!("{head}{digits}{tail}")
                    }
                    _ => format!("{digits}{text}"),
                },
                _ => format!("{digits}{text}"),
            }
        }
        "renumber" => digits_text(rule, num, width),
        "template" => tpl::render(a, ctx, Some(num), Some(width)),
        "slug" => slugify(text, a),
        "translit" => translit(text),
        "zhconv" => {
            let mode = if a.trim().is_empty() { "s2t".to_string() } else { a.trim().to_string() };
            let mode = if mode == "s2t" || mode == "t2s" { mode } else { "s2t".to_string() };
            zhconv::convert(text, &mode)
        }
        "hash" => {
            let path = ctx.and_then(|c| c.path()).unwrap_or_default();
            if path.is_empty() {
                return text.to_string();
            }
            let h = file_digest(&path, if a.is_empty() { "md5" } else { a });
            if h.is_empty() {
                return text.to_string();
            }
            let mut h = h;
            if let Ok(n) = b.parse::<i64>() {
                if n > 0 {
                    h = h.chars().take(n as usize).collect();
                }
            }
            let pos = if c.is_empty() { "suffix" } else { c }.trim().to_lowercase();
            if pos == "prefix" { format!("{h}{text}") } else { format!("{text}{h}") }
        }
        "filename_clean" => clean_filename(text, b),
        "move" => move_chars(
            text,
            a.trim().parse::<i64>().unwrap_or(0),
            b.trim().parse::<i64>().unwrap_or(0),
            c.trim().parse::<i64>().unwrap_or(0),
        ),
        "pad_numbers" => pad_numbers(text, a.trim().parse::<i64>().unwrap_or(0), b),
        "randomize" => {
            let n = a.trim().parse::<i64>().unwrap_or(8).clamp(0, MAX_RANDOM) as usize;
            let alpha: Vec<char> = if b.is_empty() {
                "0123456789abcdefghijklmnopqrstuvwxyz".chars().collect()
            } else {
                b.chars().collect()
            };
            if alpha.is_empty() {
                return text.to_string();
            }
            let who = ctx.map(|x| x.seed_key()).unwrap_or_default();
            let seed = format!("{who}|{num}|{}", if rule.id.is_empty() { rule.kind.clone() } else { rule.id.clone() });
            let chunk = crate::template::seeded_pick(&seed, &alpha, n);
            let pos = if c.is_empty() { "prefix" } else { c }.trim().to_lowercase();
            if pos == "prefix" { format!("{chunk}{text}") } else { format!("{text}{chunk}") }
        }
        "date_insert" => {
            let style = if a.is_empty() { "yyyy-mm-dd" } else { a };
            let val = ctx
                .and_then(|x| x.var(&format!("date:{style}")))
                .unwrap_or_default();
            if val.is_empty() {
                return text.to_string();
            }
            let pos = if c.is_empty() { "prefix" } else { c }.trim().to_lowercase();
            if pos == "prefix" { format!("{val}{text}") } else { format!("{text}{val}") }
        }
        "extension" => {
            let mode = b.trim().to_lowercase();
            if mode == "remove" {
                return split_ext(text).0;
            }
            if mode == "lower" || mode == "upper" {
                let (stem, ext) = split_ext(text);
                let ext = if mode == "lower" { ext.to_lowercase() } else { ext.to_uppercase() };
                return format!("{stem}{ext}");
            }
            if a.is_empty() {
                return text.to_string();
            }
            let want = if a.starts_with('.') { a.to_string() } else { format!(".{a}") };
            format!("{}{want}", split_ext(text).0)
        }
        "llm" => {
            let Some(ctx) = ctx else { return text.to_string() };
            let mut val = ctx.extra_dict_get("llm_by_rule", &rule.id);
            if val.is_none() {
                val = ctx.extra("llm");
            }
            if val.is_none() {
                val = ctx.var("llm");
            }
            match val {
                Some(v) if !v.is_empty() => v,
                _ => text.to_string(),
            }
        }
        _ => text.to_string(),
    }
}

/// 捕获组 → `template::MatchGroups`（给 `expand_refs` 用）。
struct CapsGroup<'a>(&'a fancy_regex::Captures<'a, str>);

impl tpl::MatchGroups for CapsGroup<'_> {
    fn group(&self, index: usize) -> Option<String> {
        self.0.get(index).map(|m| m.as_str().to_string())
    }
}

fn chars_take(text: &str, n: i64) -> String {
    text.chars().take(n.max(0) as usize).collect()
}

fn chars_from(text: &str, n: i64) -> String {
    text.chars().skip(n.max(0) as usize).collect()
}

fn split_chars(text: &str, at: i64) -> (String, String) {
    let at = at.max(0) as usize;
    let mut head = String::new();
    let mut tail = String::new();
    for (i, ch) in text.chars().enumerate() {
        if i < at {
            head.push(ch);
        } else {
            tail.push(ch);
        }
    }
    (head, tail)
}

/// 先按 selector 切出作用段，再变换，最后拼回去（分隔符不吃掉）。
pub fn apply_rule(
    rule: &Rule,
    text: &str,
    num: i64,
    width: i64,
    ctx: Option<&dyn VarSource>,
) -> String {
    if !rule.enabled {
        return text.to_string();
    }
    if rule.kind == "template" {
        let part = tpl::select(text, &rule.selector, &rule.charset, rule.occurrence);
        let new = transform(rule, &part, num, width, ctx);
        if rule.selector.is_empty() || rule.selector == "text" {
            return new;
        }
        return tpl::splice(text, &rule.selector, &rule.charset, rule.occurrence, &new);
    }

    let sel = if rule.selector.is_empty() { "text" } else { rule.selector.as_str() };
    if sel == "text" {
        return transform(rule, text, num, width, ctx);
    }
    if sel == "range" {
        // 只要区间非空就作用（哪怕正好覆盖整个名字）
        let (lo, hi) = tpl::parse_range(&rule.charset, text.chars().count() as i64);
        let chars: Vec<char> = text.chars().collect();
        let part = tpl::py_slice(&chars, lo - 1, hi);
        if part.is_empty() {
            return text.to_string();
        }
        let new = transform(rule, &part, num, width, ctx);
        return tpl::splice(text, sel, &rule.charset, rule.occurrence, &new);
    }
    // 其余选择器靠分隔符定位：找不到分隔符就原样返回
    if tpl::cut_index(text, &rule.charset, rule.occurrence) < 0 {
        return text.to_string();
    }
    let part = tpl::select(text, sel, &rule.charset, rule.occurrence);
    let new = transform(rule, &part, num, width, ctx);
    tpl::splice(text, sel, &rule.charset, rule.occurrence, &new)
}

fn template_mentions_ext(template: &str) -> bool {
    static RE: OnceLock<Mutex<Option<fancy_regex::Regex>>> = OnceLock::new();
    let cell = RE.get_or_init(|| {
        Mutex::new(fancy_regex::Regex::new(r"(?i)\{[^{}]*(?:\bext\b|extdot|扩展名)[^{}]*\}|\$Ext\b").ok())
    });
    match cell.lock() {
        Ok(guard) => guard
            .as_ref()
            .map(|re| re.is_match(template).unwrap_or(false))
            .unwrap_or(false),
        Err(_) => template.contains("{ext}"),
    }
}

fn template_has_literal_ext(template: &str) -> bool {
    static RE: OnceLock<Mutex<Option<fancy_regex::Regex>>> = OnceLock::new();
    let cell = RE.get_or_init(|| Mutex::new(fancy_regex::Regex::new(r"\.[A-Za-z0-9]{1,8}$").ok()));
    match cell.lock() {
        Ok(guard) => guard
            .as_ref()
            .map(|re| re.is_match(template).unwrap_or(false))
            .unwrap_or(false),
        Err(_) => false,
    }
}

/// 把整条规则链作用在一个名字上。
///
/// `split_ext = true` 时，skip_ext 的规则只作用于主干，扩展名原样拼回；
/// 文件夹没有扩展名，等价于整名处理。
pub fn apply_chain(
    rules: &[Rule],
    text: &str,
    num: i64,
    width: i64,
    ctx: Option<&dyn VarSource>,
    split_ext_names: bool,
) -> String {
    let is_dir = ctx.and_then(|c| c.is_dir());
    let (mut stem, mut ext) = if split_ext_names && is_dir == Some(false) {
        split_ext(text)
    } else {
        (text.to_string(), String::new())
    };

    for rule in rules.iter().filter(|r| r.enabled) {
        if rule.kind == "template" && (rule.selector.is_empty() || rule.selector == "text") {
            // 模板产出完整名字。扩展名怎么处理看**模板原文**：
            //   ① 目录，或原文写了 {ext}/{extdot}/{扩展名} → 模板自己管扩展名
            //   ② 原文结尾就是字面量（`{name}.md`）      → 尊重模板，替换原扩展名
            //   ③ 其余（`{n}`、`{parent}-{n}`）           → 只接管主干，扩展名原样保留
            let produced = tpl::render(&rule.a, ctx, Some(num), Some(width));
            if is_dir == Some(true) || template_mentions_ext(&rule.a) {
                stem = produced;
                ext = String::new();
                continue;
            }
            if template_has_literal_ext(&rule.a) {
                let (new_stem, new_ext) = split_ext(&produced);
                if new_ext.is_empty() {
                    stem = produced;
                    ext = String::new();
                } else {
                    stem = new_stem;
                    ext = new_ext;
                }
                continue;
            }
            stem = produced;
            continue;
        }
        if rule.kind == "extension" {
            let joined = apply_rule(rule, &format!("{stem}{ext}"), num, width, ctx);
            let (s, e) = split_ext(&joined);
            stem = s;
            ext = e;
            continue;
        }
        if rule.kind == "llm" {
            stem = apply_rule(rule, &stem, num, width, ctx);
            continue;
        }
        if rule.kind == "renumber" {
            // 序列化：整名换成号码，扩展名原样保留。没有 ctx 的兼容入口（以及
            // 文件夹）进来时扩展名还没拆过，这里补一次，否则 .mp4 会被一起吃
            if split_ext_names && is_dir != Some(false) {
                let (core, tail) = split_ext(&format!("{stem}{ext}"));
                if !core.is_empty() && !tail.is_empty() {
                    stem = core;
                    ext = tail;
                }
            }
            stem = apply_rule(rule, &stem, num, width, ctx);
            continue;
        }
        if rule.skip_ext && !ext.is_empty() {
            stem = apply_rule(rule, &stem, num, width, ctx);
            continue;
        }
        let joined = format!("{stem}{ext}");
        let out = apply_rule(rule, &joined, num, width, ctx);
        let (s, e) = split_ext(&out);
        stem = s;
        ext = e;
    }
    format!("{stem}{ext}")
}

// --------------------------------------------------------------------------- //
// 序列化 / 解析 / 展示
// --------------------------------------------------------------------------- //

pub fn rules_to_json(rules: &[Rule]) -> Vec<Value> {
    rules.iter().map(|r| to_dict(&normalize(r.clone()))).collect()
}

/// 只认真正的映射：脏值（`plan: 3` 之类）一律当空字典。
fn as_extras(value: Option<&Value>) -> Map<String, Value> {
    match value {
        Some(Value::Object(map)) => map.clone(),
        _ => Map::new(),
    }
}

/// 容忍脏 JSON：非对象条目直接跳过，字段类型不对就转文本。
pub fn rules_from_json(data: &[Value]) -> Vec<Rule> {
    let mut out = Vec::new();
    for item in data {
        let Value::Object(obj) = item else { continue };
        let occurrence = match obj.get("occurrence") {
            Some(v) if truthy(v) => value_text(v).trim().parse::<i64>().unwrap_or(1),
            _ => 1,
        };
        let text_field = |key: &str| -> String {
            match obj.get(key) {
                Some(v) => value_text(v),
                None => String::new(),
            }
        };
        let rule = Rule {
            kind: {
                let k = text_field("kind");
                if k.is_empty() { "replace".to_string() } else { k }
            },
            a: text_field("a"),
            b: text_field("b"),
            c: text_field("c"),
            enabled: obj.get("enabled").map(truthy).unwrap_or(true),
            id: text_field("id"),
            selector: {
                let s = text_field("selector");
                if s.is_empty() { "text".to_string() } else { s }
            },
            charset: text_field("charset"),
            occurrence,
            skip_ext: obj.get("skip_ext").map(truthy).unwrap_or(false),
            keep_sep: obj.get("keep_sep").map(truthy).unwrap_or(false),
            extras: as_extras(obj.get("extras")),
        };
        out.push(normalize(rule));
    }
    out
}

/// 命令行规则解析：`名称=参数1=参数2=参数3`。
pub fn parse_cli_rule(spec: &str) -> Result<Rule, crate::Error> {
    let text = spec.trim();
    if text.is_empty() {
        return Err(crate::Error::InvalidRule("空规则".to_string()));
    }
    let (body, sel_part) = match text.split_once(';') {
        Some((head, rest)) => (head, rest.to_string()),
        None => (text, String::new()),
    };
    let (head, rest) = match body.split_once('=') {
        Some((h, r)) => (h, r),
        None => (body, ""),
    };
    let name = head.trim().to_string();
    let mut kind = name.to_lowercase();
    if let Some(mapped) = alias(&kind) {
        kind = mapped.to_string();
    }
    let parts: Vec<&str> = if rest.is_empty() { Vec::new() } else { rest.split('=').collect() };
    let mut a = parts.first().map(|s| s.to_string()).unwrap_or_default();
    let mut b = parts.get(1).map(|s| s.to_string()).unwrap_or_default();
    let c = parts.get(2).map(|s| s.to_string()).unwrap_or_default();
    if kind == "replace" && a.contains(':') && b.is_empty() {
        if let Some((left, right)) = a.split_once(':').map(|(l, r)| (l.to_string(), r.to_string())) {
            a = left;
            b = right;
        }
    }
    for (prefix, mode) in [
        ("case_lower", "lower"),
        ("case_upper", "upper"),
        ("case_title", "title"),
        ("case_sentence", "sentence"),
    ] {
        if kind == prefix {
            kind = "case".to_string();
            a = mode.to_string();
        }
    }
    if rule_kind_ids().contains(&kind.as_str()) == false {
        let mut available: Vec<&str> = CLI_ALIASES.iter().map(|(k, _)| *k).collect();
        available.sort();
        return Err(crate::Error::InvalidRule(format!(
            "未知规则：{}（可用：{}）",
            name,
            available.join("、")
        )));
    }
    if kind == "zhconv" && a.is_empty() && (name.to_lowercase() == "s2t" || name.to_lowercase() == "t2s") {
        a = name.to_lowercase();
    }
    let mut rule = make_rule(&kind, &a, &b, &c);
    if !sel_part.is_empty() {
        let mut opts: HashMap<String, String> = HashMap::new();
        for piece in sel_part.split(';') {
            if let Some((k, v)) = piece.split_once('=') {
                opts.insert(k.trim().to_lowercase(), v.trim().to_string());
            } else if !piece.trim().is_empty() {
                rule.selector = piece.trim().to_lowercase();
            }
        }
        if let Some(v) = opts.get("sel") {
            rule.selector = v.to_lowercase();
        }
        if let Some(v) = opts.get("sep") {
            rule.charset = v.clone();
        }
        if let Some(v) = opts.get("occ") {
            if let Ok(n) = v.parse::<i64>() {
                rule.occurrence = n;
            }
        }
        if let Some(v) = opts.get("keep_sep") {
            rule.keep_sep = ["1", "true", "yes"].contains(&v.as_str());
        }
        if let Some(v) = opts.get("skip_ext") {
            rule.skip_ext = ["1", "true", "yes"].contains(&v.as_str());
        }
    }
    let _ = body;
    Ok(normalize(rule))
}

pub fn describe_chain(rules: &[Rule]) -> String {
    let parts: Vec<String> = rules.iter().filter(|r| r.enabled).map(describe).collect();
    if parts.is_empty() {
        "（无规则）".to_string()
    } else {
        parts.join(" → ")
    }
}

/// 给 `--list-rules` 用的说明表。
pub fn describe_rules_table() -> String {
    let mut lines: Vec<String> = Vec::new();
    for kind in RULE_KINDS {
        let args = kind
            .params
            .iter()
            .filter(|(_, v)| !v.is_empty())
            .map(|(k, v)| format!("{k}={v}"))
            .collect::<Vec<_>>()
            .join(" ");
        lines.push(format!("{:<16} {:<22} {}", kind.id, kind.text, args));
        if !kind.note.is_empty() {
            lines.push(format!("{:<16} {}", "", kind.note));
        }
    }
    lines.join("\n")
}
