//! 名字合法性校验（`engine/common.py` 的 `name_problem` / `nameless_problem`）。
//!
//! 这两条判定直接决定「用户能不能落盘」，所以口径必须与 Python 逐字一致：
//! 非法字符表复用 `rules::INVALID_NAME_CHARS`（规则与校验共用一张表），
//! 保留名同理走 `rules::is_reserved_name`。

use crate::pathops;
use crate::rules;
use crate::text;

/// Python 的 `str.isspace()`：比 Rust 的 `char::is_whitespace` 多认定
/// `\x1c`~`\x1f` 这四个控制分隔符（Excel 导出的名字里偶尔会混进来）。
pub fn py_isspace(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{1c}'..='\u{1f}')
}

/// Python 的 `str.strip()`（无参数版）。
pub fn py_strip(text: &str) -> &str {
    text.trim_matches(py_isspace)
}

/// 返回不合法原因；合法返回空串。与 `name_problem(name, is_dir=False)` 等价。
///
/// `name` 为 `None` 对应 Python 传了 `None`（返回"名字为空"）。
pub fn name_problem(name: Option<&str>, is_dir: bool) -> String {
    let name = match name {
        Some(n) => n,
        None => return "名字为空".to_string(),
    };
    if py_strip(name).is_empty() {
        return "名字为空".to_string();
    }
    for ch in name.chars() {
        if rules::INVALID_NAME_CHARS.contains(ch) {
            return format!("含非法字符 {ch}");
        }
        if (ch as u32) < 32 {
            return "含控制字符".to_string();
        }
    }
    if name == "." || name == ".." {
        return "不能是 . 或 ..".to_string();
    }
    if name.ends_with(' ') {
        return "结尾不能是空格".to_string();
    }
    if name.ends_with('.') {
        return "结尾不能是点".to_string();
    }
    // `name.split(".")[0]`：第一个点之前的部分（没有点时就是整串）
    let head = match name.find('.') {
        Some(idx) => &name[..idx],
        None => name,
    };
    let stem = if is_dir {
        head
    } else if let Some(rest) = name.strip_prefix('.') {
        match rest.find('.') {
            Some(idx) => &rest[..idx],
            None => rest,
        }
    } else {
        head
    };
    if rules::is_reserved_name(py_strip(stem)) {
        return "是 Windows 保留名".to_string();
    }
    let chars = name.chars().count();
    if chars as i64 > rules::MAX_NAME_LEN {
        return format!("名字过长（{chars} > {}）", rules::MAX_NAME_LEN);
    }
    String::new()
}

/// 规则把**文件**的名字改没了？（返回原因，空串表示没问题）
///
/// 单看新名字是看不出来的：`abc.txt` 被删掉主干后是 `.txt`，而 `.txt` 本身
/// 在 Windows 上合法（`.gitignore` 这类点文件也合法）。所以只能拿原名做对照：
/// 新名字恰好只剩**原来的扩展名**时，才算"名字被删空"。
pub fn nameless_problem(new: &str, old: &str, is_dir: bool) -> String {
    if is_dir || new.is_empty() {
        return String::new();
    }
    let (_stem, ext) = text::split_ext(old);
    if !ext.is_empty() && pathops::normcase(new) == pathops::normcase(&ext) {
        return format!("名字不能为空（只剩扩展名 {ext}）");
    }
    String::new()
}

/// `safe_rel(rel)`：路径比较用的归一化形式。
pub fn safe_rel(rel: &str) -> String {
    pathops::normcase(rel)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_normal_names() {
        for name in ["报告.txt", "a", ".gitignore", "IMG_0001.JPG", "名字 带空格.txt"] {
            assert_eq!(name_problem(Some(name), false), "", "{name} 应当合法");
        }
    }

    #[test]
    fn rejects_syntax_errors() {
        assert_eq!(name_problem(None, false), "名字为空");
        assert_eq!(name_problem(Some("   "), false), "名字为空");
        assert_eq!(name_problem(Some("a/b.txt"), false), "含非法字符 /");
        assert_eq!(name_problem(Some("a\u{7}b"), false), "含控制字符");
        assert_eq!(name_problem(Some("."), false), "不能是 . 或 ..");
        assert_eq!(name_problem(Some(".."), false), "不能是 . 或 ..");
        assert_eq!(name_problem(Some("a "), false), "结尾不能是空格");
        assert_eq!(name_problem(Some("a."), false), "结尾不能是点");
    }

    #[test]
    fn rejects_reserved_names_except_with_extension() {
        // 保留名判定看主干：con.txt 也不行，但 con 后面跟点再跟别的就行
        assert_eq!(name_problem(Some("con.txt"), false), "是 Windows 保留名");
        assert_eq!(name_problem(Some("CON"), false), "是 Windows 保留名");
        assert_eq!(name_problem(Some("com3.log"), false), "是 Windows 保留名");
        assert_eq!(name_problem(Some("con."), false), "结尾不能是点");
        assert_eq!(name_problem(Some("com0.txt"), false), "");
        assert_eq!(name_problem(Some("com10.txt"), false), "");
        // 点文件的主干从第一个点之后算
        assert_eq!(name_problem(Some(".con"), false), "是 Windows 保留名");
    }

    #[test]
    fn length_limit_counts_chars() {
        let long = "字".repeat(256);
        assert!(name_problem(Some(&long), false).starts_with("名字过长"));
        let ok = "字".repeat(255);
        assert_eq!(name_problem(Some(&ok), false), "");
    }

    #[test]
    fn nameless_only_when_left_with_extension() {
        assert_eq!(nameless_problem(".txt", "a.txt", false), "名字不能为空（只剩扩展名 .txt）");
        assert_eq!(nameless_problem(".TXT", "a.txt", false), "名字不能为空（只剩扩展名 .txt）");
        assert_eq!(nameless_problem(".txt", "a.txt", true), "");
        assert_eq!(nameless_problem("", "a.txt", false), "");
        assert_eq!(nameless_problem("b.txt", "a.txt", false), "");
    }
}
