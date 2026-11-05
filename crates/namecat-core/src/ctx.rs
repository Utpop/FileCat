//! 命名上下文的最小抽象。
//!
//! `engine/template.py` 与 `engine/rules.py` 都从 ctx 里取变量（`{name}`、
//! `{n}`、`{date:yyyy}`…）。Python 版直接拿 `NameContext` 对象；Rust 核心
//! 不能依赖 Python，所以这里只声明**需要什么**，由绑定层用 `NameContext`
//! 实现它。
//!
//! 好处不只是解耦：核心侧可以拿一个测试用的假 ctx 把模板语义钉死，
//! 不必为了跑一条模板去磁盘上建文件。

/// 模板/规则运行期需要从上下文取的东西（按需取，不做缓存——
/// Python 侧的 `NameContext` 自己缓存了 stat 与变量表）。
pub trait VarSource {
    /// `ctx.var(key)` 的等价物。未知键返回 `None`（Python 返回空串）。
    fn var(&self, key: &str) -> Option<String>;

    /// 随机种子里代表"这一项"的字符串：`ctx.path`，拿不到就用 `ctx.name`。
    fn seed_key(&self) -> String;

    /// 条目路径（hash 规则读文件用）。
    fn path(&self) -> Option<String>;

    /// 是否文件夹；`None` = 不知道（Python 里 `is_dir` 可能是 None）。
    fn is_dir(&self) -> Option<bool>;

    /// 计划层算好的号码（`ctx.num`）。
    fn num(&self) -> Option<i64>;

    /// 补零宽度（`ctx.width`）。
    fn width(&self) -> Option<i64>;

    /// `ctx.extra[key]`，只给 LLM 规则用（批量结果挂在 extra 上）。
    fn extra(&self, key: &str) -> Option<String> {
        let _ = key;
        None
    }

    /// `ctx.extra[dict_key][key]`：LLM 规则把"每条规则的结果"存在
    /// `extra["llm_by_rule"][rule_id]`，所以需要两层取值。
    fn extra_dict_get(&self, dict_key: &str, key: &str) -> Option<String> {
        let _ = (dict_key, key);
        None
    }
}

/// 没有上下文时的占位（`render(text, ctx=None)` 走这个）。
///
/// 对齐 Python：`_lookup` 在 `ctx is None` 时只把 `n/index/total` 当 `"1"`，
/// 其余键一律空串。
pub struct NoCtx;

impl VarSource for NoCtx {
    fn var(&self, key: &str) -> Option<String> {
        match key {
            "n" | "index" | "total" => Some("1".to_string()),
            _ => None,
        }
    }
    fn seed_key(&self) -> String {
        String::new()
    }
    fn path(&self) -> Option<String> {
        None
    }
    fn is_dir(&self) -> Option<bool> {
        None
    }
    fn num(&self) -> Option<i64> {
        None
    }
    fn width(&self) -> Option<i64> {
        None
    }
}
