//! 两遍冲突检测（`engine/common.py` 的 `validate_items`）。
//!
//! 第一遍管"语法 + 批内重名"，第二遍管"盘上占用"。第二遍必须沿依赖链传播：
//! A→B、B→C 里 B 的去向被外部文件占着时，B 不会让位，A 也就不该被判成可改
//! （否则执行阶段必然撞 WinError 183，整批回滚）。
//!
//! 条目本身是 Python 对象（`Item` 或鸭子类型），所以这里用 trait 抽掉：
//! `ValidateItem` 负责读写字段，`FileProbe` 负责那两次磁盘探测。算法本体
//! 因此可以在 Rust 侧用假对象测。

use std::collections::{HashMap, HashSet};

use crate::consts::{ST_CONFLICT, ST_ERROR, ST_OK, ST_SAME, ST_SKIP};
use crate::names::{name_problem, nameless_problem};
use crate::pathops;
use crate::scan::nkey;

/// 待校验条目。所有方法都用 `&self`：绑定层拿的是 Python 对象的引用，
/// 写字段走 PyO3 的 `setattr`，不需要 `&mut`。
pub trait ValidateItem {
    fn name(&self) -> String;
    fn new_name(&self) -> String;
    fn set_new_name(&self, value: String);
    fn path(&self) -> String;
    fn dirname(&self) -> String;
    fn is_dir(&self) -> bool;
    fn status(&self) -> String;
    fn set_status(&self, value: String);
    fn note(&self) -> String;
    fn set_note(&self, value: String);
}

/// 校验阶段需要的那两次磁盘探测（单独抽出来，才能在测试里造假）。
pub trait FileProbe {
    fn exists(&self, path: &str) -> bool;
    fn is_dir(&self, path: &str) -> bool;
}

/// 真磁盘。
///
/// 走 `scan::meta_of` 那条快路（必要时是 `GetFileInformationByName`，7 µs），
/// **不要**用 `Path::exists()`：它底层是 `fs::metadata`，在 Windows 上要
/// CreateFileW（29 µs），2000 个文件光这一步就多花 30 ms。
pub struct RealFs;

impl FileProbe for RealFs {
    fn exists(&self, path: &str) -> bool {
        crate::scan::path_exists(path)
    }
    fn is_dir(&self, path: &str) -> bool {
        if path.is_empty() {
            return false;
        }
        match crate::scan::meta_of(path) {
            Some(m) => m.mode & 0o40000 != 0,
            None => false,
        }
    }
}

/// 校验结果汇总（界面/命令行展示用）。
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct ValidateReport {
    pub ok: u32,
    pub same: u32,
    pub conflict: u32,
    pub error: u32,
    pub skip: u32,
}

impl ValidateReport {
    /// 按一个条目的当前状态计数。
    pub fn count<T: ValidateItem>(&mut self, item: &T) {
        match item.status().as_str() {
            ST_OK => self.ok += 1,
            ST_SAME => self.same += 1,
            ST_CONFLICT => self.conflict += 1,
            ST_ERROR => self.error += 1,
            _ => self.skip += 1,
        }
    }

    pub fn as_pairs(&self) -> [(&'static str, u32); 5] {
        [
            ("ok", self.ok),
            ("same", self.same),
            ("conflict", self.conflict),
            ("error", self.error),
            ("skip", self.skip),
        ]
    }
}

impl std::fmt::Display for ValidateReport {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "可改 {} / 未变 {} / 冲突 {} / 无效 {} / 跳过 {}",
            self.ok, self.same, self.conflict, self.error, self.skip
        )
    }
}

/// 第二遍里每个条目的结论。
#[derive(Clone, Copy, PartialEq, Eq)]
enum Move {
    Move,
    Block,
    Error,
}

/// 两遍校验，**就地**改 `status` / `note`。
///
/// `dir_keys` 是本批次「文件夹」条目的新路径集合（已归一化）。它不是通行证，
/// 只用来识别"某个文件想把名字改成某个文件夹的新名字"——文件与文件夹不能
/// 同名，那是真冲突。
pub fn validate_items<T: ValidateItem, F: FileProbe>(
    items: &[T],
    dir_keys: Option<&HashSet<String>>,
    fs: &F,
    progress: &mut dyn FnMut(usize, usize, &str),
) -> ValidateReport {
    let mut report = ValidateReport::default();
    // 目录在整批里高度重复：normcase 与 isdir 都按目录缓存一次，
    // 1500 个文件实测能省掉大半的路径系统调用
    let mut dir_key_cache: HashMap<String, String> = HashMap::new();
    let mut dir_ok_cache: HashMap<String, bool> = HashMap::new();

    let dir_key = |cache: &mut HashMap<String, String>, d: &str| -> String {
        if let Some(got) = cache.get(d) {
            return got.clone();
        }
        let got = nkey(d);
        cache.insert(d.to_string(), got.clone());
        got
    };

    // 等价于 nkey(join(it.dirname, it.new_name))，但目录部分走缓存
    let target_key = |cache: &mut HashMap<String, String>, it: &T| -> String {
        let dir = dir_key(cache, &it.dirname());
        pathops::join(&[&dir, &pathops::normcase(&it.new_name())])
    };

    // ---- 第一遍：语法 + 批内重名 ----
    let mut live: Vec<usize> = Vec::new();
    for (idx, it) in items.iter().enumerate() {
        if it.status() == ST_SKIP {
            continue;
        }
        it.set_status(ST_OK.to_string());
        it.set_note(String::new());
        let (old, new) = (it.name(), it.new_name());
        if new.is_empty() {
            // 规则把名字整个删空（文件夹最常见）：这是"规则产出非法名字"，
            // 不是"规则没生效"。旧实现标成"名称未变"，用户会以为规则没起作用。
            it.set_new_name(old);
            it.set_status(ST_ERROR.to_string());
            it.set_note("规则把名字删空了".to_string());
            continue;
        }
        if new == old {
            it.set_status(ST_SAME.to_string());
            it.set_note("规则未改变名称".to_string());
            continue;
        }
        let is_dir = it.is_dir();
        let prob = {
            let p = name_problem(Some(&new), is_dir);
            if p.is_empty() {
                nameless_problem(&new, &old, is_dir)
            } else {
                p
            }
        };
        if !prob.is_empty() {
            it.set_status(ST_ERROR.to_string());
            it.set_note(prob);
            continue;
        }
        live.push(idx);
    }

    // 批内重名：按归一化目标路径分组，落在同一目录下才算冲突
    let mut groups: HashMap<String, Vec<usize>> = HashMap::new();
    for &idx in &live {
        let key = target_key(&mut dir_key_cache, &items[idx]);
        groups.entry(key).or_default().push(idx);
    }
    for (_key, same) in groups.iter() {
        if same.len() <= 1 {
            continue;
        }
        let n = same.len() - 1;
        for &idx in same {
            items[idx].set_status(ST_CONFLICT.to_string());
            items[idx].set_note(format!("与本批次其它 {n} 项同名"));
        }
    }

    // ---- 第二遍：盘上占用 ----
    let empty_dirs: HashSet<String> = HashSet::new();
    let new_dirs = dir_keys.unwrap_or(&empty_dirs);
    let pending: Vec<usize> = live
        .iter()
        .copied()
        .filter(|&idx| items[idx].status() != ST_CONFLICT)
        .collect();

    // nkey(path) -> 条目下标。同路径不会重复（扫描阶段去过重），后写的覆盖先写的。
    // 顺便把每项的 nkey 存下来：下面要比"目标键 == 自身键"判断是否只改大小写，
    // 现算的话等于每项再跑一遍 abspath + LCMapStringEx（2000 项约 7 ms，
    // 而这一段本来就被 syscall 挤得很紧）。
    let mut owner: HashMap<String, usize> = HashMap::new();
    let mut own_keys: HashMap<usize, String> = HashMap::new();
    for &idx in &pending {
        let key = nkey(&items[idx].path());
        owner.insert(key.clone(), idx);
        own_keys.insert(idx, key);
    }
    let mut state: HashMap<usize, Move> = HashMap::new();

    for &idx in &pending {
        let it = &items[idx];
        let key = target_key(&mut dir_key_cache, it);
        let own = match own_keys.get(&idx) {
            Some(found) => found.as_str(),
            None => "",
        };
        if key == own {
            state.insert(idx, Move::Move); // 只改大小写
            continue;
        }
        if new_dirs.contains(&key) && !it.is_dir() {
            // 目标正好是本批次某个文件夹的新名字：文件与文件夹不能同名，
            // 放行只会在执行阶段撞上（旧实现用 dir_keys 一律放行，属于漏判）
            it.set_status(ST_CONFLICT.to_string());
            it.set_note("与文件夹的目标同名".to_string());
            state.insert(idx, Move::Block);
            continue;
        }
        let target = pathops::join(&[&it.dirname(), &it.new_name()]);
        if fs.exists(&target) {
            // 被本批次另一项占着的先空着，等下面传播；否则就是外部文件挡路
            if !owner.contains_key(&key) {
                state.insert(idx, Move::Block);
            }
            continue;
        }
        let dirname = it.dirname();
        let okdir = match dir_ok_cache.get(&dirname) {
            Some(v) => *v,
            None => {
                let v = fs.is_dir(&dirname);
                dir_ok_cache.insert(dirname, v);
                v
            }
        };
        if okdir {
            state.insert(idx, Move::Move);
        } else {
            it.set_status(ST_ERROR.to_string());
            it.set_note("所在目录不存在".to_string());
            state.insert(idx, Move::Error);
        }
    }

    // 沿依赖链传播：能不能动取决于"占着我目标的那一项能不能动"，
    // 传不动了说明几项互占对方的位（A↔B 互换这类环）——执行阶段会用临时名
    // 中转，属于可落地，按可以动处理（即留在 while 外不设 block）。
    loop {
        let mut changed = false;
        for &idx in &pending {
            if state.contains_key(&idx) {
                continue;
            }
            let key = target_key(&mut dir_key_cache, &items[idx]);
            let dep = match owner.get(&key) {
                None => continue,
                Some(&d) => d,
            };
            if dep == idx || !state.contains_key(&dep) {
                continue;
            }
            let moved = matches!(state[&dep], Move::Move);
            state.insert(idx, if moved { Move::Move } else { Move::Block });
            changed = true;
        }
        if !changed {
            break;
        }
    }

    for &idx in &pending {
        if matches!(state.get(&idx), Some(Move::Block)) {
            items[idx].set_status(ST_CONFLICT.to_string());
            items[idx].set_note("目标已存在".to_string());
        }
    }

    for it in items {
        report.count(it);
    }
    progress(items.len(), items.len(), "校验完成");
    report
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::collections::HashSet;

    struct FakeItem {
        name: String,
        new_name: RefCell<String>,
        path: String,
        dirname: String,
        is_dir: bool,
        status: RefCell<String>,
        note: RefCell<String>,
    }

    impl FakeItem {
        fn new(dirname: &str, name: &str, is_dir: bool) -> Self {
            Self {
                name: name.to_string(),
                new_name: RefCell::new(name.to_string()),
                path: format!("{dirname}\\{name}"),
                dirname: dirname.to_string(),
                is_dir,
                status: RefCell::new(ST_OK.to_string()),
                note: RefCell::new(String::new()),
            }
        }
        fn renamed(self, new: &str) -> Self {
            *self.new_name.borrow_mut() = new.to_string();
            self
        }
    }

    impl ValidateItem for FakeItem {
        fn name(&self) -> String {
            self.name.clone()
        }
        fn new_name(&self) -> String {
            self.new_name.borrow().clone()
        }
        fn set_new_name(&self, value: String) {
            *self.new_name.borrow_mut() = value;
        }
        fn path(&self) -> String {
            self.path.clone()
        }
        fn dirname(&self) -> String {
            self.dirname.clone()
        }
        fn is_dir(&self) -> bool {
            self.is_dir
        }
        fn status(&self) -> String {
            self.status.borrow().clone()
        }
        fn set_status(&self, value: String) {
            *self.status.borrow_mut() = value;
        }
        fn note(&self) -> String {
            self.note.borrow().clone()
        }
        fn set_note(&self, value: String) {
            *self.note.borrow_mut() = value;
        }
    }

    /// 假磁盘：`files` 是"盘上已存在"的路径集合。
    struct FakeFs {
        files: HashSet<String>,
        dirs: HashSet<String>,
    }

    impl FakeFs {
        fn new(files: &[&str], dirs: &[&str]) -> Self {
            Self {
                files: files.iter().map(|s| s.to_string()).collect(),
                dirs: dirs.iter().map(|s| s.to_string()).collect(),
            }
        }
    }

    impl FileProbe for FakeFs {
        fn exists(&self, path: &str) -> bool {
            self.files.contains(path) || self.dirs.contains(path)
        }
        fn is_dir(&self, path: &str) -> bool {
            self.dirs.contains(path)
        }
    }

    fn run(items: &[FakeItem], dir_keys: Option<&HashSet<String>>, fs: &FakeFs) -> ValidateReport {
        validate_items(items, dir_keys, fs, &mut |_, _, _| {})
    }

    #[test]
    fn marks_unchanged_and_empty() {
        let items = vec![
            FakeItem::new("C:\\a", "x.txt", false),
            FakeItem::new("C:\\a", "y.txt", false).renamed(""),
            FakeItem::new("C:\\a", "z.txt", false).renamed("z2.txt"),
        ];
        let fs = FakeFs::new(&[], &["C:\\a"]);
        let rep = run(&items, None, &fs);
        assert_eq!(items[0].status(), ST_SAME);
        assert_eq!(items[1].status(), ST_ERROR);
        assert_eq!(items[1].new_name(), "y.txt"); // 删空时回填原名
        assert_eq!(items[2].status(), ST_OK);
        assert_eq!((rep.ok, rep.same, rep.error), (1, 1, 1));
    }

    #[test]
    fn intra_batch_duplicate_is_conflict() {
        let items = vec![
            FakeItem::new("C:\\a", "x.txt", false).renamed("同名.txt"),
            FakeItem::new("C:\\a", "y.txt", false).renamed("同名.TXT"),
        ];
        let fs = FakeFs::new(&[], &["C:\\a"]);
        let rep = run(&items, None, &fs);
        assert_eq!(rep.conflict, 2);
        assert_eq!(items[0].note(), "与本批次其它 1 项同名");
    }

    #[test]
    fn external_occupation_blocks() {
        let items = vec![FakeItem::new("C:\\a", "x.txt", false).renamed("y.txt")];
        let fs = FakeFs::new(&["C:\\a\\y.txt"], &["C:\\a"]);
        let rep = run(&items, None, &fs);
        assert_eq!(rep.conflict, 1);
        assert_eq!(items[0].note(), "目标已存在");
    }

    #[test]
    fn chain_rename_is_allowed() {
        // a→b, b→c：b 的去向被本批次另一项让出来，两项都能动
        let items = vec![
            FakeItem::new("C:\\a", "a.txt", false).renamed("b.txt"),
            FakeItem::new("C:\\a", "b.txt", false).renamed("c.txt"),
        ];
        let fs = FakeFs::new(&["C:\\a\\a.txt", "C:\\a\\b.txt"], &["C:\\a"]);
        let rep = run(&items, None, &fs);
        assert_eq!(rep.ok, 2, "{}", rep);
    }

    #[test]
    fn broken_chain_blocks_head() {
        // a→b, b→c，但 c 被外部文件占着：b 动不了，a 也就不能动
        let items = vec![
            FakeItem::new("C:\\a", "a.txt", false).renamed("b.txt"),
            FakeItem::new("C:\\a", "b.txt", false).renamed("c.txt"),
        ];
        let fs = FakeFs::new(&["C:\\a\\a.txt", "C:\\a\\b.txt", "C:\\a\\c.txt"], &["C:\\a"]);
        let rep = run(&items, None, &fs);
        assert_eq!(rep.conflict, 2);
    }

    #[test]
    fn swap_pair_counts_as_movable() {
        // a↔b 互换：互占对方的位，靠临时名中转，属于可落地
        let items = vec![
            FakeItem::new("C:\\a", "a.txt", false).renamed("b.txt"),
            FakeItem::new("C:\\a", "b.txt", false).renamed("a.txt"),
        ];
        let fs = FakeFs::new(&["C:\\a\\a.txt", "C:\\a\\b.txt"], &["C:\\a"]);
        let rep = run(&items, None, &fs);
        assert_eq!(rep.ok, 2, "{}", rep);
    }

    #[test]
    fn file_cannot_take_folder_target() {
        // 文件夹在另一个目录里改名，文件在本目录改成一模一样的名字：
        // 批内重名分组管不到（目标路径不同），必须靠 dir_keys 拦
        let items = vec![
            FakeItem::new("C:\\a", "d1", true).renamed("新夹"),
            FakeItem::new("C:\\b", "x.txt", false).renamed("新夹"),
        ];
        let mut keys = HashSet::new();
        keys.insert("c:\\b\\新夹".to_string());
        let fs = FakeFs::new(&["C:\\a\\d1"], &["C:\\a", "C:\\b", "C:\\a\\d1"]);
        let rep = run(&items, Some(&keys), &fs);
        assert_eq!(rep.conflict, 1);
        // 注意最终说明是"目标已存在"：Python 版在最后那一轮还会把 block 的
        // note 统一改写成它，前面那句"与文件夹的目标同名"会被盖掉。
        assert_eq!(items[1].note(), "目标已存在");
    }

    #[test]
    fn case_only_rename_is_movable() {
        let items = vec![FakeItem::new("C:\\a", "x.txt", false).renamed("X.txt")];
        let fs = FakeFs::new(&["C:\\a\\x.txt"], &["C:\\a"]);
        let rep = run(&items, None, &fs);
        assert_eq!(rep.ok, 1);
    }

    #[test]
    fn skip_status_is_left_alone() {
        let it = FakeItem::new("C:\\a", "x.txt", false).renamed("y.txt");
        *it.status.borrow_mut() = ST_SKIP.to_string();
        let items = vec![it];
        let fs = FakeFs::new(&[], &["C:\\a"]);
        let rep = run(&items, None, &fs);
        assert_eq!(rep.skip, 1);
        assert_eq!(items[0].status(), ST_SKIP);
    }
}

