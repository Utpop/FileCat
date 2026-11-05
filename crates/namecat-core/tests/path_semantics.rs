//! 路径语义对拍：Rust 的 `pathops` 必须与 CPython 的 `ntpath` 逐字一致。
//!
//! 语料由 `tools/gen_path_cases.py` 用真 Python 采集（`tests/data/path_cases.json`）。
//! 换 Python 小版本导致 `ntpath` 语义变化时，重新生成即可；
//! 这里只要有一条不一致就失败——因为路径判断错一次就是改错文件。

use namecat_core::pathops;
use serde_json::Value;

fn data() -> Value {
    serde_json::from_str(include_str!("data/path_cases.json")).expect("语料 JSON 解析失败")
}

fn s(v: &Value) -> String {
    v.as_str().expect("样本里应当是字符串").to_string()
}

fn opt(v: &Value) -> Option<String> {
    if v.is_null() { None } else { Some(s(v)) }
}

struct Fails(Vec<String>);

impl Fails {
    fn check(&mut self, label: String, got: String, want: String) {
        if got != want {
            self.0.push(format!("{label}\n    期望 {want:?}\n    实得 {got:?}"));
        }
    }

    fn check_opt(&mut self, label: String, got: Option<String>, want: Option<String>) {
        if got != want {
            self.0.push(format!("{label}\n    期望 {want:?}\n    实得 {got:?}"));
        }
    }

    fn done(self, total: usize) {
        let n = self.0.len();
        for line in self.0.iter().take(20) {
            println!("✗ {line}");
        }
        println!("路径语义对拍：{}/{total} 组不一致", n);
        assert_eq!(n, 0, "路径语义与 ntpath 不一致");
    }
}

#[test]
fn matches_ntpath() {
    let d = data();
    let mut f = Fails(Vec::new());
    let mut total = 0usize;

    for case in d["splitroot"].as_array().unwrap() {
        let arr = case.as_array().unwrap();
        let p = s(&arr[0]);
        let (dr, ro, ta) = pathops::splitroot(&p);
        f.check(
            format!("splitroot({p:?})"),
            format!("{dr}|{ro}|{ta}"),
            format!("{}|{}|{}", s(&arr[1]), s(&arr[2]), s(&arr[3])),
        );
        total += 1;
    }
    for case in d["split"].as_array().unwrap() {
        let arr = case.as_array().unwrap();
        let p = s(&arr[0]);
        let (head, tail) = pathops::split(&p);
        f.check(
            format!("split({p:?})"),
            format!("{head}|{tail}"),
            format!("{}|{}", s(&arr[1]), s(&arr[2])),
        );
        total += 1;
    }
    for case in d["splitext"].as_array().unwrap() {
        let arr = case.as_array().unwrap();
        let p = s(&arr[0]);
        let (root, ext) = pathops::splitext(&p);
        f.check(
            format!("splitext({p:?})"),
            format!("{root}|{ext}"),
            format!("{}|{}", s(&arr[1]), s(&arr[2])),
        );
        total += 1;
    }
    for key in ["join2", "join3"] {
        for case in d[key].as_array().unwrap() {
            let arr = case.as_array().unwrap();
            let parts: Vec<String> = arr[0].as_array().unwrap().iter().map(s).collect();
            let refs: Vec<&str> = parts.iter().map(|x| x.as_str()).collect();
            f.check(
                format!("{key}({parts:?})"),
                pathops::join(&refs),
                s(&arr[1]),
            );
            total += 1;
        }
    }
    for case in d["normpath"].as_array().unwrap() {
        let arr = case.as_array().unwrap();
        let p = s(&arr[0]);
        f.check(format!("normpath({p:?})"), pathops::normpath(&p), s(&arr[1]));
        total += 1;
    }
    for case in d["normcase"].as_array().unwrap() {
        let arr = case.as_array().unwrap();
        let p = s(&arr[0]);
        f.check(format!("normcase({p:?})"), pathops::normcase(&p), s(&arr[1]));
        total += 1;
    }
    for case in d["abspath"].as_array().unwrap() {
        let arr = case.as_array().unwrap();
        let p = s(&arr[0]);
        f.check(format!("abspath({p:?})"), pathops::abspath(&p), s(&arr[1]));
        total += 1;
    }
    for case in d["relpath"].as_array().unwrap() {
        let arr = case.as_array().unwrap();
        let pair = arr[0].as_array().unwrap();
        let (path, start) = (s(&pair[0]), s(&pair[1]));
        f.check_opt(
            format!("relpath({path:?}, {start:?})"),
            pathops::relpath(&path, &start),
            opt(&arr[1]),
        );
        total += 1;
    }
    for case in d["commonpath"].as_array().unwrap() {
        let arr = case.as_array().unwrap();
        let paths: Vec<String> = arr[0].as_array().unwrap().iter().map(s).collect();
        let refs: Vec<&str> = paths.iter().map(|x| x.as_str()).collect();
        f.check_opt(
            format!("commonpath({paths:?})"),
            pathops::commonpath(&refs),
            opt(&arr[1]),
        );
        total += 1;
    }

    f.done(total);
}
