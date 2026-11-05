//! 导出与"共同父目录"（`engine/ops.py` 里的纯逻辑部分）。

use crate::pathops;

/// 取共同的父目录当日志根。跨盘（commonpath 无解）时退到首项。
pub fn common_base(dirs: &[String]) -> String {
    let alive: Vec<&str> = dirs.iter().map(|s| s.as_str()).filter(|d| !d.is_empty()).collect();
    if alive.is_empty() {
        return std::env::current_dir()
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_default();
    }
    pathops::commonpath(&alive).unwrap_or_else(|| alive[0].to_string())
}

/// 单个 CSV 字段的转义，等价于 Python `csv.writer` 的 QUOTE_MINIMAL：
/// 含分隔符、引号或行终止符才加引号，内部引号翻倍。
pub fn csv_field(value: &str) -> String {
    if value.contains(',') || value.contains('"') || value.contains('\r') || value.contains('\n') {
        format!("\"{}\"", value.replace('"', "\"\""))
    } else {
        value.to_string()
    }
}

/// 整份 CSV 文本（含 BOM），行终止符是 `\r\n` —— 与 Python 的
/// `csv.writer` 默认行为一致（Excel 打开才不会串行）。
pub fn csv_text(rows: &[Vec<String>]) -> String {
    let mut out = String::from("\u{feff}");
    for row in rows {
        let fields: Vec<String> = row.iter().map(|f| csv_field(f)).collect();
        out.push_str(&fields.join(","));
        out.push_str("\r\n");
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quotes_only_when_needed() {
        assert_eq!(csv_field("普通"), "普通");
        assert_eq!(csv_field("a,b"), "\"a,b\"");
        assert_eq!(csv_field("a\"b"), "\"a\"\"b\"");
        assert_eq!(csv_field("a\nb"), "\"a\nb\"");
    }

    #[test]
    fn csv_uses_bom_and_crlf() {
        let text = csv_text(&[vec!["序号".into(), "原名".into()], vec!["1".into(), "a,b".into()]]);
        assert!(text.starts_with('\u{feff}'));
        assert!(text.ends_with("\r\n"));
        assert!(text.contains("1,\"a,b\""));
    }

    #[test]
    fn common_base_falls_back_to_first() {
        assert_eq!(
            common_base(&["C:\\a\\b".to_string(), "C:\\a\\c".to_string()]),
            "C:\\a"
        );
        // 跨盘：commonpath 无解，退回首项
        assert_eq!(
            common_base(&["C:\\a".to_string(), "D:\\b".to_string()]),
            "C:\\a"
        );
    }
}
