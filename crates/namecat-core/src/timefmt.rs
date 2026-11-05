//! 时间与容量的文本化（对应 `engine/context.py` 的 `fmt_date` / `human_size`）。
//!
//! `fmt_date` 是给**文件名**用的模板风格（`yyyy-mm-dd`），不是 `strftime`：
//! 它按固定顺序做 9 轮字符串替换，并且每次替换后还把大写写法一起替换掉。
//! 这个"先替换、再补大写"的顺序有意义（`YYYY` 会先被 `yyyy` 那轮吃掉），
//! 所以这里原样复刻，别改成一次性扫描。

use chrono::{DateTime, Datelike, Local, NaiveDateTime, Timelike, Utc};

const MONTH_LONG: [&str; 12] = [
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
];
const MONTH_SHORT: [&str; 12] = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/// 时间戳 → 本地时间。取不到（NaN、越界）时返回 None，调用方回落"现在"。
///
/// 边界取 `datetime.fromtimestamp` 能接受的区间（公元 1 年 ~ 9999 年），
/// 超出就当作 OverflowError 走回落分支。
fn local_dt(ts: f64) -> Option<NaiveDateTime> {
    if !ts.is_finite() {
        return None;
    }
    let secs_f = ts.floor();
    if !(-62_135_596_800.0..=253_402_300_799.0).contains(&secs_f) {
        return None;
    }
    let secs = secs_f as i64;
    let nanos = (((ts - secs_f) * 1e9).round() as i64).clamp(0, 999_999_999) as u32;
    let utc = DateTime::<Utc>::from_timestamp(secs, nanos)?;
    let local = utc.with_timezone(&Local).naive_local();
    // Python 的 datetime 只到公元 9999 年：UTC 时间戳 253402300799 换算成本地
    // 时间后是"10000 年"，`fromtimestamp` 在那边会抛 OverflowError 并回落到"现在"。
    // 不挡这一下，`{mtime}` 就会渲染成 `10000-01-01`。
    if !(1..=9999).contains(&local.year()) {
        return None;
    }
    Some(local)
}

fn now_dt() -> NaiveDateTime {
    Local::now().naive_local()
}

/// `datetime.strftime` 里用到的那 9 个占位符的取值（一次算出，避免重复格式化）。
fn piece(dt: &NaiveDateTime, fmt: &str) -> String {
    match fmt {
        "%Y" => format!("{}", dt.year()),
        "%y" => format!("{:02}", dt.year().rem_euclid(100)),
        "%B" => MONTH_LONG[(dt.month() - 1) as usize].to_string(),
        "%b" => MONTH_SHORT[(dt.month() - 1) as usize].to_string(),
        "%m" => format!("{:02}", dt.month()),
        "%d" => format!("{:02}", dt.day()),
        "%H" => format!("{:02}", dt.hour()),
        "%M" => format!("{:02}", dt.minute()),
        "%S" => format!("{:02}", dt.second()),
        _ => String::new(),
    }
}

/// ReNamer 风格的日期格式；未知风格原样返回。
pub fn fmt_date(ts: f64, style: &str) -> String {
    let dt = local_dt(ts).unwrap_or_else(now_dt);
    // 注意是 `(style or 默认值).strip()`：空串走默认，纯空白则是"空风格"（返回空串）
    let style = if style.is_empty() { "yyyy-mm-dd" } else { style };
    let mut out = style.trim().to_string();
    // 顺序敏感：务必保持这个次序（yyyy 先于 yy、mmmm 先于 mmm 先于 mm）
    const TABLE: [(&str, &str); 9] = [
        ("yyyy", "%Y"),
        ("yy", "%y"),
        ("mmmm", "%B"),
        ("mmm", "%b"),
        ("mm", "%m"),
        ("dd", "%d"),
        ("hh", "%H"),
        ("nn", "%M"),
        ("ss", "%S"),
    ];
    for (key, fmt) in TABLE {
        let val = piece(&dt, fmt);
        if out.contains(key) {
            out = out.replace(key, &val);
        }
        // 原实现无条件补一轮大写（`if key.islower()` 恒真，表里全是小写键）
        let upper_key = key.to_uppercase();
        if out.contains(&upper_key) {
            out = out.replace(&upper_key, &val.to_uppercase());
        }
    }
    out
}

/// 今天的 `yyyy-mm-dd`（模板变量 `{now}`）。
pub fn today_dash() -> String {
    let dt = now_dt();
    format!("{:04}-{:02}-{:02}", dt.year(), dt.month(), dt.day())
}

/// 今天的 `yyyymmdd`（模板变量 `{date}`）。
pub fn today_compact() -> String {
    let dt = now_dt();
    format!("{:04}{:02}{:02}", dt.year(), dt.month(), dt.day())
}

/// `human_size`：`1234` → `1.2 KB`，字节数不带小数。
pub fn human_size(num: i64) -> String {
    let mut size = num as f64;
    for unit in ["B", "KB", "MB", "GB"] {
        if size < 1024.0 || unit == "GB" {
            if unit == "B" {
                return format!("{} B", size as i64);
            }
            return format!("{size:.1} {unit}");
        }
        size /= 1024.0;
    }
    format!("{num} B")
}

/// 当前本地时间戳（供"现在"相关的兜底）。
#[allow(dead_code)]
pub fn now_ts() -> f64 {
    Local::now().timestamp_millis() as f64 / 1000.0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn epoch_is_local_not_utc() {
        // 1970-01-01 00:00 UTC 在 UTC+8 是 08:00（本地时区由系统决定，
        // 这里只钉"年份/日期"这类与时区无关的部分）
        let got = fmt_date(0.0, "yyyy-mm-dd hh:nn:ss");
        assert!(got.starts_with("1970-01-01") || got.starts_with("1969-12-31"), "{got}");
        assert_eq!(fmt_date(0.0, "yyyy").len(), 4);
    }

    #[test]
    fn unknown_style_passes_through() {
        assert_eq!(fmt_date(0.0, "abc"), "abc");
        assert_eq!(fmt_date(0.0, ""), fmt_date(0.0, "yyyy-mm-dd"));
    }

    #[test]
    fn uppercase_keys_are_replaced_too() {
        let lower = fmt_date(0.0, "yyyy");
        let upper = fmt_date(0.0, "YYYY");
        assert_eq!(upper, lower.to_uppercase());
    }

    #[test]
    fn human_size_units() {
        assert_eq!(human_size(0), "0 B");
        assert_eq!(human_size(1234), "1.2 KB");
        assert_eq!(human_size(1024 * 1024), "1.0 MB");
        assert_eq!(human_size(5 * 1024 * 1024 * 1024), "5.0 GB");
    }
}
