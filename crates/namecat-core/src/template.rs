//! 模板渲染与选择器（对应 `engine/template.py`）。
//!
//! 语义对齐要点（都是"照抄现有行为"，不是"更正确"）：
//! · 渲染顺序固定：转义 → `#Name#` → `@@N@@` → `$Var(args)` → `<x>` `%x%` `&x&`
//!   → `{x}`。顺序本身就是语义：`#` 必须在变量替换前扫完，否则变量值里自带的
//!   `#` 会被当计数器改写（a#b.txt 曾变成 a03b.txt）。
//! · 未知变量渲染成**空串**（不是原样保留），这是设计内行为。
//! · 索引/截断一律按**码点**（Python `len()` 口径），不是字节。
//! · 补零宽度上限 `MAX_WIDTH = 64`，防止 `{n:200000000}` 一次产出几十 MB。

use crate::ctx::VarSource;

/// 补零宽度硬上限。名字总长才 255，再宽毫无意义。
pub const MAX_WIDTH: i64 = 64;

pub const SELECTOR_KINDS: &[&str] = &["text", "after", "before", "from", "until", "range"];

pub const SELECTOR_TEXT: &[(&str, &str)] = &[
    ("text", "整个名字"),
    ("after", "某字符之后"),
    ("before", "某字符之前"),
    ("from", "从某字符起（含）"),
    ("until", "到某字符止（含）"),
    ("range", "字符区间（如 2-4）"),
];

/// 正则替换里可用的命名占位（`expand_refs` 用）。
pub const REF_KEYS: &[&str] = &[
    "name", "stem", "fullname", "ext", "extdot", "n", "num", "index", "total", "dir",
    "parent", "size", "size_kb", "size_h", "mtime", "ctime", "atime", "year", "month",
    "day", "time", "now", "path", "filepath", "left", "right",
];

// --------------------------------------------------------------------------- //
// 选择器：把规则限制在名字的某一段
// --------------------------------------------------------------------------- //

fn to_chars(text: &str) -> Vec<char> {
    text.chars().collect()
}

/// Python 的切片语义：负下标从尾部数，越界夹到两端，start>=end 给空串。
///
/// 这里必须自己实现：`parse_range` 在 length 很小时会吐出 `lo=0` 这类值
/// （`'5-2'` 交换后），Python 的 `text[-1:1]` 只是返回空串，Rust 直接 panic。
pub fn py_slice(chars: &[char], start: i64, end: i64) -> String {
    let n = chars.len() as i64;
    let norm = |i: i64| -> i64 {
        let v = if i < 0 { i + n } else { i };
        v.clamp(0, n)
    };
    let (s, e) = (norm(start), norm(end));
    if s >= e {
        return String::new();
    }
    chars[s as usize..e as usize].iter().collect()
}

/// 找分隔符位置（**码点**下标）。occurrence：1=第一次，-1=最后一次，N=第 N 次。
pub fn cut_index(text: &str, charset: &str, occurrence: i64) -> i64 {
    let hits: Vec<i64> = text
        .chars()
        .enumerate()
        .filter(|(_, c)| charset.contains(*c))
        .map(|(i, _)| i as i64)
        .collect();
    if hits.is_empty() {
        return -1;
    }
    let occ = occurrence;          // 调用方已把 None 归一成 1
    if occ == -1 {
        return *hits.last().unwrap();
    }
    if occ < -1 {
        let idx = hits.len() as i64 + occ;      // Python 的 hits[occ]（负下标）
        return if idx >= 0 { hits[idx as usize] } else { -1 };
    }
    if occ <= 1 {
        return hits[0];
    }
    if occ <= hits.len() as i64 {
        return hits[(occ - 1) as usize];
    }
    -1
}

/// `'2-4'` / `'-3'` / `'5-'` / `'3'` → (lo, hi) 闭区间，越界夹紧。
pub fn parse_range(expr: &str, length: i64) -> (i64, i64) {
    let expr = expr.trim();
    let mut lo = 1i64;
    let mut hi = length;
    if expr.is_empty() {
        return (lo, hi);
    }
    if let Some(pos) = expr.find('-') {
        let left = expr[..pos].trim();
        let right = expr[pos + 1..].trim();
        let (l, r) = (parse_int(left), parse_int(right));
        // 空串沿用默认值；解析失败整对回退（对齐 Python 的 except ValueError）
        match (left.is_empty() || l.is_some(), right.is_empty() || r.is_some()) {
            (true, true) => {
                lo = if left.is_empty() { 1 } else { l.unwrap() };
                hi = if right.is_empty() { length } else { r.unwrap() };
            }
            _ => return (1, length),
        }
    } else {
        match parse_int(expr) {
            Some(n) => {
                lo = n;
                hi = n;
            }
            None => return (1, length),
        }
    }
    if lo < 1 {
        lo = 1;
    }
    if hi <= 0 || hi > length {
        hi = length;
    }
    if lo > hi {
        std::mem::swap(&mut lo, &mut hi);       // '5-2' 手误：交换而不是空片段
    }
    (lo, hi)
}

fn parse_int(text: &str) -> Option<i64> {
    // Python int() 容忍前后空白与正负号
    let t = text.trim();
    if t.is_empty() {
        return None;
    }
    t.parse::<i64>().ok()
}

/// 取出要作用的片段。
pub fn select(text: &str, mode: &str, charset: &str, occurrence: i64) -> String {
    if text.is_empty() {
        return text.to_string();
    }
    let mode = mode.trim().to_lowercase();
    if mode.is_empty() || mode == "text" {
        return text.to_string();
    }
    let chars = to_chars(text);
    if mode == "range" {
        let (lo, hi) = parse_range(charset, chars.len() as i64);
        return py_slice(&chars, lo - 1, hi);
    }
    let idx = cut_index(text, charset, occurrence);
    if idx < 0 {
        return text.to_string();
    }
    let i = idx as usize;
    match mode.as_str() {
        "after" => chars[i + 1..].iter().collect(),
        "before" => chars[..i].iter().collect(),
        "from" => chars[i..].iter().collect(),
        "until" => chars[..i + 1].iter().collect(),
        _ => text.to_string(),
    }
}

/// 把 select 出来的片段替换成 new，分隔符原样保留。
pub fn splice(text: &str, mode: &str, charset: &str, occurrence: i64, new: &str) -> String {
    let mode = mode.trim().to_lowercase();
    if mode.is_empty() || mode == "text" {
        return new.to_string();
    }
    let chars = to_chars(text);
    if mode == "range" {
        let (lo, hi) = parse_range(charset, chars.len() as i64);
        let head = py_slice(&chars, 0, lo - 1);
        let tail = py_slice(&chars, hi, chars.len() as i64);
        return format!("{head}{new}{tail}");
    }
    let idx = cut_index(text, charset, occurrence);
    if idx < 0 {
        return text.to_string();
    }
    let i = idx as usize;
    match mode.as_str() {
        "after" => {
            let head: String = chars[..=i].iter().collect();
            format!("{head}{new}")
        }
        "before" => {
            let tail: String = chars[i..].iter().collect();
            format!("{new}{tail}")
        }
        // until 选中的是「含分隔符的头部」，拼回时必须是「新片段 + 分隔符之后」。
        // 这里曾抄成 after 的公式，把整段尾部丢掉了。
        "until" => {
            let tail: String = chars[i + 1..].iter().collect();
            format!("{new}{tail}")
        }
        // from：选中的是「从这里到结尾」，拼回时尾部整段被替换掉
        _ => {
            let head: String = chars[..i].iter().collect();
            format!("{head}{new}")
        }
    }
}

/// 分隔符留空时的兜底：按长度对半截断（BatchRenamer 的老行为）。
pub fn split_half(text: &str) -> String {
    let chars = to_chars(text);
    py_slice(&chars, 0, (chars.len() / 2) as i64)
}

// --------------------------------------------------------------------------- //
// 变量名归一
// --------------------------------------------------------------------------- //

const CANON: &[(&str, &str)] = &[
    ("n", "n"), ("num", "n"), ("counter", "n"), ("#", "n"), ("序号", "n"), ("编号", "n"),
    ("index", "index"), ("idx", "index"), ("total", "total"), ("count", "total"),
    ("name", "name"), ("filename", "name"), ("basename", "name"), ("stem", "name"),
    ("原名", "name"), ("名字", "name"),
    ("fullname", "fullname"), ("full_name", "fullname"), ("filenameext", "fullname"),
    ("ext", "ext"), ("extension", "ext"), ("扩展名", "ext"),
    ("extdot", "extdot"),
    ("dir", "dir"), ("folder", "dir"), ("parent", "parent"), ("parentdir", "parent"),
    ("所在文件夹", "parent"),
    ("filepath", "filepath"), ("path", "path"), ("fullpath", "path"),
    ("size", "size"), ("sizebytes", "size"), ("size_kb", "size_kb"), ("sizekb", "size_kb"),
    ("size_h", "size_h"), ("sizeh", "size_h"),
    ("mtime", "mtime"), ("modified", "mtime"), ("date_modified", "mtime"),
    ("date", "now"), ("now", "now"),
    ("ctime", "ctime"), ("created", "ctime"), ("atime", "atime"), ("accessed", "atime"),
    ("time", "time"), ("year", "year"), ("month", "month"), ("day", "day"),
    ("kind", "kind"),
];

/// 变量名归一：去空白、`<file.name>` 前缀、前导下划线，再查别名表。
pub fn canon_key(name: &str) -> String {
    let raw = name.trim();
    if raw.is_empty() {
        return String::new();
    }
    let mut low = raw.to_lowercase();
    if let Some(rest) = low.strip_prefix("file.") {
        low = rest.to_string();
    }
    if let Some(rest) = low.strip_prefix('_') {
        low = rest.to_string();
    }
    for (from, to) in CANON {
        if *from == low {
            return (*to).to_string();
        }
    }
    low
}

// --------------------------------------------------------------------------- //
// 渲染
// --------------------------------------------------------------------------- //

/// 展开 `\x41`、`\x{1F600}` 与 `\n` `\t` `\r`。
pub fn expand_escape(text: &str) -> String {
    if !text.contains('\\') {
        return text.to_string();
    }
    let out = sub_braced_hex(text);
    let out = sub_two_hex(&out);
    out.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
}

fn hex_to_char(hex: &str) -> Option<char> {
    let code = u32::from_str_radix(hex, 16).ok()?;
    char::from_u32(code)
}

/// `\x{HHHH}`（1-6 位十六进制）
fn sub_braced_hex(text: &str) -> String {
    let chars = to_chars(text);
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < chars.len() {
        if chars[i] == '\\'
            && i + 1 < chars.len()
            && (chars[i + 1] == 'x')
            && i + 2 < chars.len()
            && chars[i + 2] == '{'
        {
            // 找 '}'，最多 6 位十六进制
            let mut j = i + 3;
            while j < chars.len() && j - (i + 3) < 6 && chars[j].is_ascii_hexdigit() {
                j += 1;
            }
            let digits: String = chars[i + 3..j].iter().collect();
            if !digits.is_empty() && j < chars.len() && chars[j] == '}' {
                match hex_to_char(&digits) {
                    Some(c) => out.push(c),
                    None => {
                        out.push('\\');
                        out.push('x');
                        out.push_str(&digits);
                    }
                }
                i = j + 1;
                continue;
            }
        }
        out.push(chars[i]);
        i += 1;
    }
    out
}

/// `\xHH`（恰好两位十六进制）
fn sub_two_hex(text: &str) -> String {
    let chars = to_chars(text);
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < chars.len() {
        if chars[i] == '\\' && i + 2 < chars.len() && chars[i + 1] == 'x'
            && chars[i + 2].is_ascii_hexdigit()
            && chars.get(i + 3).is_some_and(|c| c.is_ascii_hexdigit())
        {
            // 注意：Python 的 `\x([0-9A-Fa-f]{2})` 只认两位，
            // 所以 `\x4142` 会变成 "A42"
            let digits: String = chars[i + 2..i + 4].iter().collect();
            match hex_to_char(&digits) {
                Some(c) => out.push(c),
                None => {
                    out.push('\\');
                    out.push('x');
                    out.push_str(&digits);
                }
            }
            i += 4;
            continue;
        }
        out.push(chars[i]);
        i += 1;
    }
    out
}

/// `str(n).zfill(w)` 的等价实现（保留符号位）。
pub fn num_text(num: i64, width: i64) -> String {
    let w = width.clamp(0, MAX_WIDTH) as usize;
    if w == 0 {
        return num.to_string();
    }
    let s = num.to_string();
    let (sign, digits) = match s.strip_prefix('-') {
        Some(rest) => ("-", rest),
        None => ("", s.as_str()),
    };
    // 注意：Python 的 zfill 把符号位也算进宽度（"-5".zfill(4) == "-005"）
    let need = w.saturating_sub(digits.chars().count() + sign.chars().count());
    if need == 0 {
        return s;
    }
    format!("{sign}{}{digits}", "0".repeat(need))
}

/// `{n:03}`：冒号后是补零宽度（ReNamer 的格式），不匹配则原样返回。
pub fn apply_num_format(value: &str, spec: &str) -> String {
    let spec = spec.trim();
    if spec.is_empty() {
        return value.to_string();
    }
    // Python: re.fullmatch(r"0*(\d+)", spec) —— 允许前导 0，后面必须有数字
    let rest = spec.trim_start_matches('0');
    if rest.is_empty() || !rest.chars().all(|c| c.is_ascii_digit()) {
        return value.to_string();
    }
    match rest.parse::<i64>() {
        Ok(n) => zfill_str(value, n.clamp(0, MAX_WIDTH)),
        Err(_) => value.to_string(),
    }
}

fn zfill_str(value: &str, width: i64) -> String {
    if width <= 0 {
        return value.to_string();
    }
    let w = width as usize;
    let (sign, digits) = match value.strip_prefix('-') {
        Some(rest) => ("-", rest),
        None => ("", value),
    };
    let need = w.saturating_sub(digits.chars().count() + sign.chars().count());
    if need == 0 {
        return value.to_string();
    }
    format!("{sign}{}{digits}", "0".repeat(need))
}

fn lookup(ctx: Option<&dyn VarSource>, key: &str) -> String {
    let key = canon_key(key);
    if key.is_empty() {
        return String::new();
    }
    match ctx {
        None => match key.as_str() {
            "n" | "index" | "total" => "1".to_string(),
            _ => String::new(),
        },
        Some(ctx) => ctx.var(&key).unwrap_or_default(),
    }
}

/// 从 start 处的 '(' 找配对的 ')'，允许参数里再嵌括号。
fn find_close(chars: &[char], start: usize) -> i64 {
    let mut depth = 0i64;
    for (i, ch) in chars.iter().enumerate().skip(start) {
        match ch {
            '(' => depth += 1,
            ')' => {
                depth -= 1;
                if depth == 0 {
                    return i as i64;
                }
            }
            _ => {}
        }
    }
    -1
}

/// `$Counter(起,步,宽)` / `$Date(风格)` / `$MD5` / `$FilePath` …（ReNamer 写法）
fn render_dollar(text: &str, ctx: Option<&dyn VarSource>, num: i64, width: i64) -> String {
    let chars = to_chars(text);
    let n = chars.len();
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < n {
        if chars[i] != '$' {
            out.push(chars[i]);
            i += 1;
            continue;
        }
        let mut j = i + 1;
        while j < n && (chars[j].is_alphanumeric() || chars[j] == '_') {
            j += 1;
        }
        let name: String = chars[i + 1..j].iter().collect();
        if name.is_empty() {
            out.push('$');
            i += 1;
            continue;
        }
        let mut args = String::new();
        if j < n && chars[j] == '(' {
            let close = find_close(&chars, j);
            if close > 0 {
                args = chars[j + 1..close as usize].iter().collect();
                j = close as usize + 1;
            }
        }
        let low = name.to_lowercase();
        match low.as_str() {
            "counter" | "count" | "index" | "n" | "num" => {
                if args.is_empty() {
                    out.push_str(&num_text(num, width));
                } else {
                    let parts: Vec<&str> = args.split(',').map(|p| p.trim()).collect();
                    let start = parts.first().and_then(|p| parse_int(p)).unwrap_or(1);
                    let step = parts.get(1).and_then(|p| parse_int(p)).unwrap_or(1);
                    let w = parts
                        .get(2)
                        .and_then(|p| parse_int(p))
                        .unwrap_or(width);
                    out.push_str(&num_text(start + (num - 1) * step, w));
                }
            }
            "date" | "datetime" => {
                let style = if args.is_empty() { "yyyy-mm-dd".to_string() } else { args };
                match ctx.and_then(|c| c.var(&format!("date:{style}"))) {
                    Some(v) => out.push_str(&v),
                    None => out.push_str(&style),
                }
            }
            _ => out.push_str(&lookup(ctx, &low)),
        }
        i = j;
    }
    out
}

/// 确定性伪随机（对齐"同一次计划内可复现"，**不**追求与 Python MT 位级一致）。
pub fn seeded_pick(seed: &str, alphabet: &[char], count: usize) -> String {
    let mut state = fnv1a(seed);
    let mut out = String::with_capacity(count);
    for _ in 0..count {
        state = state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        let idx = ((state >> 33) as usize) % alphabet.len();
        out.push(alphabet[idx]);
    }
    out
}

pub fn fnv1a(text: &str) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in text.as_bytes() {
        h ^= *b as u64;
        h = h.wrapping_mul(0x1000_0000_01b3);
    }
    h
}

fn pseudo_random(text: &str, ctx: Option<&dyn VarSource>, num: i64) -> String {
    let chars = to_chars(text);
    let n = chars.len();
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < n {
        if chars[i] == '@' && i + 1 < n && chars[i + 1] == '@' {
            // @@(\d+)@@
            let mut j = i + 2;
            while j < n && chars[j].is_ascii_digit() {
                j += 1;
            }
            if j > i + 2 && j + 1 < n && chars[j] == '@' && chars[j + 1] == '@' {
                let digits: String = chars[i + 2..j].iter().collect();
                let count = digits.parse::<i64>().unwrap_or(0).clamp(0, 64) as usize;
                let token: String = chars[i..j + 2].iter().collect();
                let seed = format!("{}|{}|{}", seed_key(ctx), num, token);
                out.push_str(&seeded_pick(&seed, &['0', '1', '2', '3', '4', '5', '6', '7', '8', '9'], count));
                i = j + 2;
                continue;
            }
        }
        out.push(chars[i]);
        i += 1;
    }
    out
}

fn seed_key(ctx: Option<&dyn VarSource>) -> String {
    ctx.map(|c| c.seed_key()).unwrap_or_default()
}

/// 渲染一个模板。不含任何变量时原样返回（对普通文本安全）。
pub fn render(text: &str, ctx: Option<&dyn VarSource>, num: Option<i64>, width: Option<i64>) -> String {
    // 优先级：显式传参 > ctx 里的计划值 > 默认 1 / 0
    let num = num.or_else(|| ctx.and_then(|c| c.num())).unwrap_or(1);
    let mut width = width.unwrap_or(0);
    if width == 0 {
        width = ctx.and_then(|c| c.width()).unwrap_or(0);
    }

    let mut out = text.to_string();
    if out.contains('\\') {
        out = expand_escape(&out);
    }
    if out.contains('#') {
        if out == "#" {
            out = num_text(num, width);
        } else {
            out = sub_hash_token(&out, ctx);
        }
    }
    if out.contains("@@") {
        out = pseudo_random(&out, ctx, num);
    }
    if out.contains('$') {
        out = render_dollar(&out, ctx, num, width);
    }
    if out.contains('<') {
        out = sub_simple_var(&out, '<', '>', true, ctx);
    }
    if out.contains('%') {
        out = sub_simple_var(&out, '%', '%', false, ctx);
    }
    if out.contains('&') {
        out = sub_simple_var(&out, '&', '&', false, ctx);
    }
    if out.contains('{') {
        out = sub_brace(&out, ctx, num, width);
    }
    out
}

/// `#Name#` → 变量值（要求首字符是字母/下划线，后续字母数字下划线点）
fn sub_hash_token(text: &str, ctx: Option<&dyn VarSource>) -> String {
    let chars = to_chars(text);
    let n = chars.len();
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < n {
        if chars[i] == '#' && i + 1 < n && (chars[i + 1].is_ascii_alphabetic() || chars[i + 1] == '_') {
            let mut j = i + 1;
            while j < n && (chars[j].is_ascii_alphanumeric() || chars[j] == '_' || chars[j] == '.') {
                j += 1;
            }
            if j < n && chars[j] == '#' {
                let name: String = chars[i + 1..j].iter().collect();
                out.push_str(&lookup(ctx, &name));
                i = j + 1;
                continue;
            }
        }
        out.push(chars[i]);
        i += 1;
    }
    out
}

/// `<name>` / `%name%` / `&name&` —— 开闭符号相同或不同的一组旧写法。
///
/// `allow_dot`：只有 `<file.name>` 这种允许点号，`%a.b%` 在 Python 的正则里
/// 匹配不上（`[A-Za-z0-9_]*`），这里必须一致。
fn sub_simple_var(text: &str, open: char, close: char, allow_dot: bool, ctx: Option<&dyn VarSource>) -> String {
    let chars = to_chars(text);
    let n = chars.len();
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < n {
        if chars[i] == open && i + 1 < n && (chars[i + 1].is_ascii_alphabetic() || chars[i + 1] == '_') {
            let mut j = i + 1;
            while j < n
                && (chars[j].is_ascii_alphanumeric() || chars[j] == '_' || (allow_dot && chars[j] == '.'))
            {
                j += 1;
            }
            if j < n && chars[j] == close {
                let name: String = chars[i + 1..j].iter().collect();
                out.push_str(&lookup(ctx, &name));
                i = j + 1;
                continue;
            }
        }
        out.push(chars[i]);
        i += 1;
    }
    out
}

fn is_digits(text: &str) -> bool {
    !text.is_empty() && text.chars().all(|c| c.is_numeric())
}

/// `{变量}` / `{变量:格式}` / `{1}`
fn sub_brace(text: &str, ctx: Option<&dyn VarSource>, num: i64, width: i64) -> String {
    let chars = to_chars(text);
    let n = chars.len();
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < n {
        if chars[i] == '{' {
            // `\{([^{}]*)\}`：不允许嵌套，遇到 { 就放弃本次匹配
            let mut j = i + 1;
            while j < n && chars[j] != '{' && chars[j] != '}' {
                j += 1;
            }
            if j < n && chars[j] == '}' {
                let inner: String = chars[i + 1..j].iter().collect();
                match brace_value(&inner, ctx, num, width) {
                    Some(v) => out.push_str(&v),
                    None => {
                        let whole: String = chars[i..=j].iter().collect();
                        out.push_str(&whole);
                    }
                }
                i = j + 1;
                continue;
            }
        }
        out.push(chars[i]);
        i += 1;
    }
    out
}

/// 单个 `{...}` 的取值；`None` = 原样保留（空、纯数字、格式不认）。
fn brace_value(inner_raw: &str, ctx: Option<&dyn VarSource>, num: i64, width: i64) -> Option<String> {
    let inner = inner_raw.trim();
    if inner.is_empty() {
        return None;
    }
    if let Some(pos) = inner.find(':') {
        let head = inner[..pos].trim();
        let spec = inner[pos + 1..].trim();
        let key = canon_key(head);
        if key == "n" || key == "num" || key == "counter" {
            return Some(if spec.is_empty() {
                num_text(num, width)
            } else {
                apply_num_format(&num.to_string(), spec)
            });
        }
        if matches!(key.as_str(), "date" | "mtime" | "ctime" | "atime") {
            if let Some(c) = ctx {
                let style = if spec.is_empty() { "yyyy-mm-dd" } else { spec };
                return Some(c.var(&format!("date:{style}")).unwrap_or_default());
            }
        }
        let val = lookup(ctx, head);
        return Some(if is_digits(&val) { apply_num_format(&val, spec) } else { val });
    }
    if is_digits(inner) {
        return None;            // {1} 由正则替换负责，这里原样保留
    }
    if canon_key(inner) == "n" {
        return Some(num_text(num, width));
    }
    Some(lookup(ctx, inner))
}

// --------------------------------------------------------------------------- //
// 正则替换里的命名占位
// --------------------------------------------------------------------------- //

/// 捕获组访问（Python 的 `re.Match.group`）。
pub trait MatchGroups {
    fn group(&self, index: usize) -> Option<String>;
}

/// 展开 `{1}` `{2}` `{name}` `{n}` … 供 regex 规则使用。
pub fn expand_refs(
    repl: &str,
    groups: Option<&dyn MatchGroups>,
    ctx: Option<&dyn VarSource>,
    num: i64,
    width: i64,
    left: &str,
    right: &str,
) -> String {
    if repl.is_empty() {
        return String::new();
    }
    // 这些写法是「别的语法」，整体不处理（对齐现实现）
    if repl.contains("{@") || repl.contains("{!") || repl.contains("$F") {
        return repl.to_string();
    }
    let mut out = repl.to_string();
    for gid in 1..10usize {
        let token = format!("{{{gid}}}");
        if !out.contains(&token) {
            continue;
        }
        if let Some(val) = groups.and_then(|g| g.group(gid)) {
            out = out.replace(&token, &val);
        }
    }
    for key in REF_KEYS {
        let token = format!("{{{key}}}");
        if !out.contains(&token) {
            continue;
        }
        // 注意：ctx 为 None 时，除 {n} 外一律空串（不是 NoCtx 的 index/total=1）
        let val = if *key == "n" {
            num_text(num, width)
        } else {
            match ctx {
                None => String::new(),
                Some(c) => lookup(Some(c), key),
            }
        };
        out = out.replace(&token, &val);
    }
    if out.contains("{left}") {
        out = out.replace("{left}", left);
    }
    if out.contains("{right}") {
        out = out.replace("{right}", right);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    struct FakeCtx;

    impl VarSource for FakeCtx {
        fn var(&self, key: &str) -> Option<String> {
            // 键名与取值都照 engine/context.py 的 variables() 抄
            let v = match key {
                "n" => "7",
                "name" => "报告",
                "stem" => "报告",
                "fullname" => "报告.txt",
                "ext" => "txt",
                "extdot" => ".txt",
                "now" => "2026-11-05",          // variables()["now"] = %Y-%m-%d
                "date" => "20261105",           // variables()["date"] = %Y%m%d
                "mtime" => "2026-11-05",
                "date:yyyy" => "2026",
                "date:yyyy-mm-dd" => "2026-11-05",
                "index" => "3",
                "total" => "9",
                "filepath" => "C:/tmp",
                "dir" => "tmp",
                _ => return None,
            };
            Some(v.to_string())
        }
        fn seed_key(&self) -> String {
            "C:/tmp/报告.txt".to_string()
        }
        fn path(&self) -> Option<String> {
            Some("C:/tmp/报告.txt".to_string())
        }
        fn is_dir(&self) -> Option<bool> {
            Some(false)
        }
        fn num(&self) -> Option<i64> {
            Some(7)
        }
        fn width(&self) -> Option<i64> {
            Some(3)
        }
        fn extra(&self, _key: &str) -> Option<String> {
            None
        }
    }

    #[test]
    fn cut_index_occurrences() {
        let text = "a-b-c-d";
        assert_eq!(cut_index(text, "-", 1), 1);
        assert_eq!(cut_index(text, "-", 2), 3);
        assert_eq!(cut_index(text, "-", -1), 5);
        assert_eq!(cut_index(text, "-", -2), 3);
        assert_eq!(cut_index(text, "-", 9), -1);
        assert_eq!(cut_index(text, "_", 1), -1);
    }

    #[test]
    fn parse_range_cases() {
        assert_eq!(parse_range("2-4", 10), (2, 4));
        assert_eq!(parse_range("-3", 10), (1, 3));
        assert_eq!(parse_range("5-", 10), (5, 10));
        assert_eq!(parse_range("3", 10), (3, 3));
        assert_eq!(parse_range("", 10), (1, 10));
        assert_eq!(parse_range("5-2", 10), (2, 5));      // 手误交换
        assert_eq!(parse_range("x-y", 10), (1, 10));
        assert_eq!(parse_range("1-99", 10), (1, 10));    // 上限夹紧
    }

    #[test]
    fn select_and_splice_roundtrip() {
        let text = "show-2026-10-19";
        assert_eq!(select(text, "after", "-", 1), "2026-10-19");
        assert_eq!(select(text, "before", "-", 1), "show");
        assert_eq!(select(text, "until", "-", 1), "show-");
        assert_eq!(select(text, "from", "-", 2), "-10-19");   // from 含分隔符本身
        // until 的拼回公式曾抄错，这里钉死
        assert_eq!(splice(text, "until", "-", 1, "SHOW-"), "SHOW-2026-10-19");
        assert_eq!(splice(text, "after", "-", 1, "X"), "show-X");
        assert_eq!(splice(text, "range", "1-4", 1, "SHOW"), "SHOW-2026-10-19");
    }

    #[test]
    fn split_half_counts_code_points() {
        assert_eq!(split_half("abcdef"), "abc");
        assert_eq!(split_half("中文名字"), "中文");
        assert_eq!(split_half("a"), "");
    }

    #[test]
    fn num_text_matches_python_zfill() {
        assert_eq!(num_text(7, 3), "007");
        assert_eq!(num_text(7, 0), "7");
        assert_eq!(num_text(-5, 4), "-005");
        assert_eq!(num_text(12345, 2), "12345");
        assert_eq!(num_text(1, 200), "0".repeat(63) + "1");  // 上限 64 位（含数字本身）
    }

    #[test]
    fn applies_num_format() {
        assert_eq!(apply_num_format("7", "03"), "007");
        assert_eq!(apply_num_format("7", "3"), "007");
        assert_eq!(apply_num_format("7", "abc"), "7");
        assert_eq!(apply_num_format("2026", "2"), "2026");
    }

    #[test]
    fn expands_escapes() {
        assert_eq!(expand_escape("\\x41"), "A");
        assert_eq!(expand_escape("\\x{1F600}"), "\u{1F600}");
        assert_eq!(expand_escape("a\\nb"), "a\nb");
        assert_eq!(expand_escape("\\xZZ"), "\\xZZ");
        // Python 只吃两位十六进制：\x4142 → "A42"
        assert_eq!(expand_escape("\\x4142"), "A42");
    }

    #[test]
    fn renders_braces() {
        let ctx = FakeCtx;
        assert_eq!(render("{n}-{name}{extdot}", Some(&ctx), None, None), "007-报告.txt");
        assert_eq!(render("{n:2}", Some(&ctx), None, None), "07");
        assert_eq!(render("{date}", Some(&ctx), None, None), "2026-11-05");
        assert_eq!(render("{未知}", Some(&ctx), None, None), "");   // 未知变量渲染成空串
        assert_eq!(render("{1}", Some(&ctx), None, None), "{1}");   // 留给正则替换
        assert_eq!(render("{}", Some(&ctx), None, None), "{}");
        assert_eq!(render("普通文本", None, None, None), "普通文本");
    }

    /// 现有实现的两个怪癖，换壳时必须原样保留（不是 bug 修复）：
    /// ① `canon_key("date") == "now"`，于是 `{date:格式}` 的 date 分支永远走不到，
    ///    规格被忽略、只当成 `{date}` 渲染；
    /// ② 只有 mtime/ctime/atime 三个键能吃到 `:格式`。
    #[test]
    fn date_placeholder_quirks_are_preserved() {
        let ctx = FakeCtx;
        assert_eq!(render("{date:yyyy}", Some(&ctx), None, None), "2026-11-05");
        assert_eq!(render("{mtime:yyyy-mm-dd}", Some(&ctx), None, None), "2026-11-05");
    }

    #[test]
    fn renders_legacy_styles() {
        let ctx = FakeCtx;
        assert_eq!(render("#name#", Some(&ctx), None, None), "报告");
        assert_eq!(render("%name%", Some(&ctx), None, None), "报告");
        assert_eq!(render("<file.name>", Some(&ctx), None, None), "报告");
        assert_eq!(render("&name&", Some(&ctx), None, None), "报告");
        // 单独一个 # 是计数器（ReNamer 老写法），宽度取 ctx 的 3
        assert_eq!(render("#", Some(&ctx), Some(5), Some(0)), "005");
    }

    #[test]
    fn hash_only_applies_before_variable_substitution() {
        let ctx = FakeCtx;
        // 变量值里的 # 不该被当计数器改写
        assert_eq!(render("#name#.txt", Some(&ctx), None, None), "报告.txt");
    }

    #[test]
    fn renders_dollar_counter() {
        let ctx = FakeCtx;
        assert_eq!(render("$Counter", Some(&ctx), Some(4), Some(2)), "04");
        assert_eq!(render("$Counter(10,5,3)", Some(&ctx), Some(2), Some(0)), "015");
        assert_eq!(render("$Date(yyyy)", Some(&ctx), None, None), "2026");
        assert_eq!(render("$FilePath(x)", Some(&ctx), None, None), "C:/tmp");
    }

    #[test]
    fn pseudo_random_is_reproducible() {
        let ctx = FakeCtx;
        let a = render("@@4@@", Some(&ctx), Some(1), None);
        let b = render("@@4@@", Some(&ctx), Some(1), None);
        assert_eq!(a, b);
        assert_eq!(a.chars().count(), 4);
        assert!(a.chars().all(|c| c.is_ascii_digit()));
    }

    #[test]
    fn expand_refs_replaces_groups_and_keys() {
        struct G;
        impl MatchGroups for G {
            fn group(&self, index: usize) -> Option<String> {
                match index {
                    1 => Some("左".to_string()),
                    2 => Some("右".to_string()),
                    _ => None,
                }
            }
        }
        let ctx = FakeCtx;
        let out = expand_refs("{1}-{2}-{name}", Some(&G), Some(&ctx), 3, 2, "L", "R");
        assert_eq!(out, "左-右-报告");
        // 注意：REF_KEYS 里本来就有 left/right，所以它们先被当普通变量查表（得到空串），
        // 后面那两个专门处理 {left}/{right} 的分支是死代码。这是现有行为，照抄。
        assert_eq!(expand_refs("{n}{left}{right}", Some(&G), Some(&ctx), 3, 2, "L", "R"), "03");
        // 特殊语法整体不处理
        assert_eq!(expand_refs("{@x}", Some(&G), None, 1, 0, "", ""), "{@x}");
    }

    #[test]
    fn canon_key_aliases() {
        assert_eq!(canon_key("File.Name"), "name");
        assert_eq!(canon_key("_ext"), "ext");
        assert_eq!(canon_key("序号"), "n");
        assert_eq!(canon_key("  扩展名 "), "ext");
        assert_eq!(canon_key(""), "");
    }
}
