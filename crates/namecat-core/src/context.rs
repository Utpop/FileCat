//! 命名上下文：模板变量的取值表（对应 `engine/context.py` 的 `NameContext`）。
//!
//! 只放纯逻辑与缓存；`Item` 那层"对象属性面"在绑定层，因为它的语义完全
//! 由 Python 侧怎么用决定（`__dict__` 里挂临时字段、属性可写）。

use crate::pathops;
use crate::scan::{kind_of, KIND_DIR};
use crate::text;
use crate::timefmt;
use std::collections::HashMap;

/// `num` 的入参：可能是整数，也可能是"取不到数"的脏值。
///
/// 区分 `Bad` 与 `None` 很重要：`counter()` 在 `num` 为 None 时回落 `index`，
/// 而在 `int(num)` 失败时回落 `1`——两者结果不同。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum NumArg {
    Int(i64),
    Bad,
}

/// `_FAST` 表：常用键直接算，绕开整张变量表。
///
/// 注意 `parentdir` / `filepath` 指向的是 `parentdir` 这个**不存在的属性**——
/// Python 版走到那里会抛 AttributeError，被上层 `try/except` 吞成空串。
/// 也就是说 `{parentdir}` 渲染出来是空的（而不是父目录）。这是现有行为，
/// 用 `None` 表达"这里会抛"，别顺手"修正"成父目录。
fn fast_key(low: &str) -> Option<&'static str> {
    Some(match low {
        "n" | "num" => "counter",
        "index" => "index",
        "total" => "total",
        "name" | "stem" => "stem",
        "fullname" | "filename" => "name",
        "ext" | "extension" => "e",
        "extdot" => "ext",
        "path" | "fullpath" => "path",
        "parentdir" => "parentdir",
        "filepath" => "parentdir",
        _ => return None,
    })
}

/// `os.stat` 的缓存状态：没查过 / 查过但没有 / 有。
#[derive(Clone, Debug)]
enum StState {
    Unknown,
    Missing,
    Loaded(crate::scan::Meta),
}

/// 一个条目的变量表：只读、缓存一次。
#[derive(Clone, Debug)]
pub struct NameCtx {
    pub path: String,
    pub name: String,
    pub is_dir: bool,
    pub index: i64,
    pub total: i64,
    pub num: Option<NumArg>,
    pub width: i64,
    st: StState,
    vars: Option<Vec<(String, String)>>,
    digests: HashMap<String, String>,
    split: Option<(String, String)>,
    dirname: Option<String>,
}

impl NameCtx {
    pub fn new(path: &str, name: &str, is_dir: bool, index: i64, total: i64,
               num: Option<NumArg>, width: i64) -> Self {
        Self {
            path: path.to_string(),
            name: name.to_string(),
            is_dir,
            index,
            total,
            num,
            width,
            st: StState::Unknown,
            vars: None,
            digests: HashMap::new(),
            split: None,
            dirname: None,
        }
    }

    /// 直接塞入已知的元数据（扫描阶段已经取过 stat，别重复系统调用）。
    ///
    /// 传 `None` 表示"还没查过"（保持惰性），**不是**"查过但没有"——
    /// 后者只可能来自 `ensure_stat` 里那次真正失败的 stat。
    pub fn with_meta(mut self, meta: Option<crate::scan::Meta>) -> Self {
        if let Some(m) = meta {
            self.st = StState::Loaded(m);
        }
        self
    }

    fn ensure_stat(&mut self) -> Option<&crate::scan::Meta> {
        if matches!(self.st, StState::Unknown) {
            self.st = match crate::scan::meta_of(&self.path) {
                Some(m) => StState::Loaded(m),
                None => StState::Missing,
            };
        }
        match &self.st {
            StState::Loaded(m) => Some(m),
            _ => None,
        }
    }

    /// `os.stat` 结果；拿不到返回 `None`（Python 版是 `False` 后转 None）。
    pub fn stat(&mut self) -> Option<crate::scan::Meta> {
        self.ensure_stat().cloned()
    }

    /// `(主干, 扩展名含点)`：文件夹没有扩展名。
    pub fn split(&mut self) -> (String, String) {
        if self.split.is_none() {
            self.split = Some(if self.is_dir {
                (self.name.clone(), String::new())
            } else {
                text::split_ext(&self.name)
            });
        }
        self.split.clone().unwrap_or_default()
    }

    /// 扩展名（含点，保留原大小写）；文件夹无扩展名。
    pub fn ext(&mut self) -> String {
        self.split().1
    }

    /// 扩展名（不含点）。
    pub fn e(&mut self) -> String {
        self.ext().trim_start_matches('.').to_string()
    }

    pub fn stem(&mut self) -> String {
        self.split().0
    }

    pub fn dirname(&mut self) -> String {
        if self.dirname.is_none() {
            self.dirname = Some(pathops::dirname(&self.path));
        }
        self.dirname.clone().unwrap_or_default()
    }

    /// `file` / `image` / `video` / `dir`。
    pub fn kind(&self) -> String {
        if self.is_dir {
            return KIND_DIR.to_string();
        }
        kind_of(&self.name, false, None, None)
    }

    fn num_value(&self) -> i64 {
        match &self.num {
            None => self.index,
            Some(NumArg::Int(n)) => *n,
            Some(NumArg::Bad) => 1,
        }
    }

    /// `str(num).zfill(width)`；宽度上限兜底见 `WIDTH_CAP`。
    pub fn counter(&self) -> String {
        let n = self.num_value();
        let w = self.width;
        if w > 0 {
            return zfill(&n.to_string(), w as usize);
        }
        n.to_string()
    }

    /// 变量表（缓存一次）。`st` 取不到时所有时间/大小类字段按 0 处理。
    pub fn variables(&mut self) -> &[(String, String)] {
        if self.vars.is_some() {
            return self.vars.as_deref().unwrap_or(&[]);
        }
        let meta = self.ensure_stat().cloned();
        let size = meta.as_ref().map(|m| m.size).unwrap_or(0);
        let mtime = meta.as_ref().map(|m| m.mtime).unwrap_or(0.0);
        let ctime = meta.as_ref().map(|m| m.ctime).unwrap_or(0.0);
        let atime = meta.as_ref().map(|m| m.atime).unwrap_or(0.0);
        let parent = self.dirname();
        let base = {
            let b = pathops::basename(&parent);
            if b.is_empty() { parent.clone() } else { b }
        };
        let counter = self.counter();
        let stem = self.stem();
        let extdot = self.ext();
        let e = extdot.trim_start_matches('.').to_string();
        let kind = self.kind();
        let mut v: Vec<(String, String)> = Vec::with_capacity(29);
        {
            let mut add = |k: &str, val: String| v.push((k.to_string(), val));
            add("n", counter.clone());
            add("num", counter);
            add("index", self.index.to_string());
            add("total", self.total.to_string());
            add("name", stem.clone());
            add("stem", stem);
            add("fullname", self.name.clone());
            add("ext", e);
            add("extdot", extdot);
            add("dir", base.clone());
            add("parent", base);
            add("parentdir", parent.clone());
            add("filepath", parent);
            add("path", self.path.clone());
            add("fullpath", self.path.clone());
            add("size", size.to_string());
            add("size_kb", size.div_euclid(1024).to_string());
            add("size_h", timefmt::human_size(size));
            add("mtime", timefmt::fmt_date(mtime, "yyyy-mm-dd"));
            add("ctime", timefmt::fmt_date(ctime, "yyyy-mm-dd"));
            add("atime", timefmt::fmt_date(atime, "yyyy-mm-dd"));
            add("year", timefmt::fmt_date(mtime, "yyyy"));
            add("month", timefmt::fmt_date(mtime, "mm"));
            add("day", timefmt::fmt_date(mtime, "dd"));
            add("time", timefmt::fmt_date(mtime, "hh-nn-ss"));
            add("now", timefmt::today_dash());
            add("date", timefmt::today_compact());
            add("kind", kind);
        }
        self.vars = Some(v);
        self.vars.as_deref().unwrap_or(&[])
    }

    /// 取一个变量。`None` 表示"Python 版这里会抛异常"（调用方一律当空串）。
    pub fn var(&mut self, key: &str) -> Option<String> {
        let low = key
            .trim()
            .trim_matches(|c| c == '{' || c == '}' || c == '$')
            .to_lowercase();
        if low.is_empty() {
            return Some(String::new());
        }
        if let Some(fast) = fast_key(&low) {
            return Some(match fast {
                "counter" => self.counter(),
                "index" => self.index.to_string(),
                "total" => self.total.to_string(),
                "stem" => self.stem(),
                "name" => self.name.clone(),
                "e" => self.e(),
                "ext" => self.ext(),
                "path" => self.path.clone(),
                // Python 到这两个会 AttributeError，被上层吞成空串
                "parentdir" => return None,
                _ => String::new(),
            });
        }
        if let Some(style) = low.strip_prefix("date:") {
            let mtime = self.ensure_stat().map(|m| m.mtime).unwrap_or(0.0);
            return Some(timefmt::fmt_date(mtime, style));
        }
        if matches!(low.as_str(), "md5" | "sha1" | "sha256" | "crc32") {
            return Some(self.digest(&low));
        }
        if self.vars.is_none() {
            let _ = self.variables();
        }
        Some(
            self.vars
                .as_ref()
                .and_then(|v| v.iter().find(|(k, _)| *k == low).map(|(_, val)| val.clone()))
                .unwrap_or_default(),
        )
    }

    /// 文件校验值；同一上下文里同一算法只读一次文件（hash 规则常用）。
    pub fn digest(&mut self, algo: &str) -> String {
        let algo = algo.to_lowercase();
        if let Some(hit) = self.digests.get(&algo) {
            return hit.clone();
        }
        // `hashlib.new("")` 抛 ValueError → 空串；只保留常用四种
        let val = if !matches!(algo.as_str(), "md5" | "sha1" | "sha256" | "sha512" | "crc32") {
            String::new()
        } else {
            crate::rules::file_digest(&self.path, &algo)
        };
        self.digests.insert(algo, val.clone());
        val
    }
}

/// `zfill`：符号位算进宽度里（`-5` 宽 4 → `-005`）。
///
/// 宽度上限 4096：Python 版没有上限，但 `PlanOptions.width` 是外部可传的整数，
/// 一个手滑的 `--width 20000000` 就能直接吃掉 20MB 内存，这里兜一层。
fn zfill(text: &str, width: usize) -> String {
    const WIDTH_CAP: usize = 4096;
    let width = width.min(WIDTH_CAP);
    let (sign, digits) = match text.strip_prefix('-') {
        Some(rest) => ("-", rest),
        None => ("", text),
    };
    // `str.zfill` 的宽度是"整串长度"，符号位也算在内：`"-5".zfill(4)` → `"-005"`
    let total = digits.chars().count() + sign.len();
    if total >= width {
        return text.to_string();
    }
    format!("{sign}{}{digits}", "0".repeat(width - total))
}

/// `split_ext`：点开头的文件（`.gitignore`）视为无扩展名。
pub fn split_ext(name: &str) -> (String, String) {
    text::split_ext(name)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ctx(name: &str) -> NameCtx {
        NameCtx::new(&format!("C:\\nope\\{name}"), name, false, 1, 1, None, 0)
    }

    #[test]
    fn counter_uses_index_when_num_missing() {
        let mut c = ctx("a.txt");
        c.index = 7;
        assert_eq!(c.counter(), "7");
        c.num = Some(NumArg::Bad);
        assert_eq!(c.counter(), "1");
        c.num = Some(NumArg::Int(7));
        c.width = 3;
        assert_eq!(c.counter(), "007");
    }

    #[test]
    fn variables_have_expected_keys() {
        let mut c = ctx("报告 2026.JPG");
        let keys: Vec<String> = c.variables().iter().map(|(k, _)| k.clone()).collect();
        for expect in ["n", "name", "ext", "extdot", "parent", "size_h", "mtime", "date", "kind"] {
            assert!(keys.contains(&expect.to_string()), "缺少 {expect}");
        }
    }

    #[test]
    fn parentdir_raises_like_python() {
        let mut c = ctx("a.txt");
        assert_eq!(c.var("parentdir"), None);
        assert_eq!(c.var("filepath"), None);
        // 但 variables() 里是有值的
        let vals: HashMap<String, String> = c.variables().iter().cloned().collect();
        assert!(vals.contains_key("parentdir"));
    }

    #[test]
    fn var_strips_braces_and_dollars() {
        let mut c = ctx("a.txt");
        assert_eq!(c.var("{name}").as_deref(), Some("a"));
        assert_eq!(c.var("$Counter"), Some(String::new()));  // "counter" 不在快表里
        assert_eq!(c.var("  {EXT}  ").as_deref(), Some("txt"));
    }

    #[test]
    fn zfill_keeps_sign() {
        assert_eq!(zfill("-5", 4), "-005");
        assert_eq!(zfill("5", 1), "5");
    }
}
