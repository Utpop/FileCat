//! 简繁单字转换（对应 `engine/zhconv.py`）。
//!
//! 数据表由 `tools/gen_zhconv.py` 从 `engine/zhconv.py` 生成（OpenCC 的
//! STCharacters / TSCharacters，Apache-2.0），**不手改**。
//!
//! 语义要点（与 Python 版 `str.translate` + `str.maketrans` 对齐）：
//! · 逐**码点**转换，不做词级转换（软件→軟體 这种不在本规则目标里）；
//! · 表外字符（数字、字母、符号、日文假名…）原样保留；
//! · 同一原字出现多次时**后来者胜**（`str.maketrans` 建 dict 的覆盖顺序）；
//! · 模式非法时原样返回（`engine/rules.py` 会先把非法模式归一成 s2t，
//!   但直接调 `convert(text, "bogus")` 时行为是"返回原文"，这里照抄）。

use std::collections::HashMap;
use std::sync::OnceLock;

mod data {
    include!("zhconv_data.rs");
}

/// 转换模式。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    /// 简体 → 繁体
    S2T,
    /// 繁体 → 简体
    T2S,
}

impl Mode {
    /// 与 Python 侧一致：只认 `s2t` / `t2s`（大小写不敏感、两端空白忽略），
    /// 其它一律 `None`（调用方据此原样返回）。
    pub fn parse(raw: &str) -> Option<Mode> {
        match raw.trim().to_lowercase().as_str() {
            "s2t" => Some(Mode::S2T),
            "t2s" => Some(Mode::T2S),
            _ => None,
        }
    }

    fn table(self) -> &'static HashMap<char, char> {
        static S2T: OnceLock<HashMap<char, char>> = OnceLock::new();
        static T2S: OnceLock<HashMap<char, char>> = OnceLock::new();
        match self {
            Mode::S2T => S2T.get_or_init(|| build(data::S2T_FROM, data::S2T_TO)),
            Mode::T2S => T2S.get_or_init(|| build(data::T2S_FROM, data::T2S_TO)),
        }
    }
}

fn build(from: &str, to: &str) -> HashMap<char, char> {
    // 两张表必须等长（生成脚本已校验，这里是运行期兜底：不等长就取短的，
    // 绝不 panic —— 一个映射表损坏不该让整批改名崩掉）
    let mut map = HashMap::with_capacity(from.chars().count());
    for (src, dst) in from.chars().zip(to.chars()) {
        map.insert(src, dst);       // 后出现的覆盖先出现的，对齐 maketrans
    }
    map
}

/// 按模式逐字转换；模式非法时原样返回。
pub fn convert(text: &str, mode: &str) -> String {
    match Mode::parse(mode) {
        Some(m) => convert_with(text, m),
        None => text.to_string(),
    }
}

/// 已解析模式版本的转换（内部调用走这个，省一次模式解析）。
pub fn convert_with(text: &str, mode: Mode) -> String {
    let table = mode.table();
    if text.chars().all(|c| !table.contains_key(&c)) {
        // 纯 ASCII / 表外字符：不分配新串，直接返回（名字里这种情况很常见）
        return text.to_string();
    }
    text.chars()
        .map(|c| table.get(&c).copied().unwrap_or(c))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 生成的表长度必须与生成时记录的常数一致（防止生成物被截断/错位）。
    #[test]
    fn tables_have_expected_lengths() {
        assert_eq!(data::S2T_FROM.chars().count(), data::S2T_FROM_LEN);
        assert_eq!(data::S2T_TO.chars().count(), data::S2T_TO_LEN);
        assert_eq!(data::T2S_FROM.chars().count(), data::T2S_FROM_LEN);
        assert_eq!(data::T2S_TO.chars().count(), data::T2S_TO_LEN);
    }

    #[test]
    fn converts_common_words() {
        assert_eq!(convert("风景_写真集", "s2t"), "風景_寫真集");
        assert_eq!(convert("風景_寫真集", "t2s"), "风景_写真集");
        assert_eq!(convert("ABC123_name", "s2t"), "ABC123_name");
    }

    #[test]
    fn invalid_mode_returns_input() {
        assert_eq!(convert("风景", "bogus"), "风景");
        assert_eq!(convert("风景", ""), "风景");
        assert_eq!(convert("风景", "S2T"), "風景");
        assert_eq!(convert("风景", " s2t "), "風景");
    }

    #[test]
    fn mode_parse_is_lenient() {
        assert_eq!(Mode::parse("S2T"), Some(Mode::S2T));
        assert_eq!(Mode::parse(" T2s "), Some(Mode::T2S));
        assert_eq!(Mode::parse("s2tw"), None);
    }

    #[test]
    fn empty_input() {
        assert_eq!(convert("", "s2t"), "");
    }
}
