//! 文本底层：扩展名拆分与 Python 的大小写语义。
//!
//! 这个模块的每个函数都以 `engine/context.py` / `engine/rules.py` 的**现有行为**
//! 为准，不是"更合理"的行为。典型例子：Python 的 `str.title()` 把 `don't`
//! 变成 `Don'T`（撇号算词边界），这里必须一起复现 —— 用户拿它改文件名，
//! 换实现后名字变了就是 bug。

/// 拆成 (主干, 扩展名含点)。点开头的文件（`.gitignore`）视为无扩展名。
///
/// 全项目只此一处实现（对应 `engine/context.py:split_ext`）。
pub fn split_ext(name: &str) -> (String, String) {
    if name.is_empty() || name.starts_with('.') || !name.contains('.') {
        return (name.to_string(), String::new());
    }
    match name.rfind('.') {
        Some(idx) => {
            let (stem, ext) = name.split_at(idx);
            (stem.to_string(), ext.to_string())
        }
        None => (name.to_string(), String::new()),
    }
}

/// Python `str.title()` 的等价实现。
///
/// 规则：字符若"前一个字符是 cased"则转小写，否则转大写。撇号、数字、空格
/// 都不是 cased，所以 `don't` → `Don'T`、`abc123def` → `Abc123Def`。
pub fn title(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut prev_cased = false;
    for ch in text.chars() {
        if prev_cased {
            out.extend(ch.to_lowercase());
        } else {
            out.extend(ch.to_uppercase());
        }
        prev_cased = ch.is_alphabetic();
    }
    out
}

/// Python `str.capitalize()`：首字符大写、其余小写。
pub fn capitalize(text: &str) -> String {
    let mut chars = text.chars();
    match chars.next() {
        None => String::new(),
        Some(first) => {
            let mut out: String = first.to_uppercase().collect();
            out.push_str(&chars.as_str().to_lowercase());
            out
        }
    }
}

/// Python `str.swapcase()`。
pub fn swapcase(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for ch in text.chars() {
        if ch.is_uppercase() {
            out.extend(ch.to_lowercase());
        } else if ch.is_lowercase() {
            out.extend(ch.to_uppercase());
        } else {
            out.push(ch);
        }
    }
    out
}

/// `text[:1].upper() + text[1:].lower()`（sentence 模式）。
pub fn sentence(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    let mut chars = text.chars();
    let first = chars.next().unwrap();
    let mut out: String = first.to_uppercase().collect();
    out.push_str(&chars.as_str().to_lowercase());
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_extension_like_python() {
        assert_eq!(split_ext("a.txt"), ("a".into(), ".txt".into()));
        assert_eq!(split_ext("a.tar.gz"), ("a.tar".into(), ".gz".into()));
        assert_eq!(split_ext(".gitignore"), (".gitignore".into(), "".into()));
        assert_eq!(split_ext("noext"), ("noext".into(), "".into()));
        assert_eq!(split_ext(""), ("".into(), "".into()));
        assert_eq!(split_ext("归档.2024"), ("归档".into(), ".2024".into()));
        assert_eq!(split_ext("a."), ("a".into(), ".".into()));
    }

    /// 这些怪癖都是 Python 的真实输出，逐条钉死。
    #[test]
    fn title_matches_python_quirks() {
        assert_eq!(title("hello world"), "Hello World");
        assert_eq!(title("don't stop"), "Don'T Stop");
        assert_eq!(title("abc123def"), "Abc123Def");
        assert_eq!(title("中文名字"), "中文名字");
        assert_eq!(title(""), "");
        assert_eq!(title("HELLO"), "Hello");
    }

    #[test]
    fn capitalize_and_sentence() {
        assert_eq!(capitalize("hELLO wORLD"), "Hello world");
        assert_eq!(capitalize(""), "");
        assert_eq!(sentence("hELLO wORLD"), "Hello world");
        assert_eq!(sentence(""), "");
    }

    #[test]
    fn swapcase_is_per_char() {
        assert_eq!(swapcase("aBc"), "AbC");
        assert_eq!(swapcase("中A"), "中a");
    }
}
