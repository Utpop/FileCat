//! 引擎核心集成测试：两种模式、深度、排除、冲突策略、模拟、执行、撤销、日志路径。
//!
//! 全部用临时目录，不碰用户数据；`program_root` 指到临时目录，验证固定的
//! `Extract/Rollback_Log` 布局，且扫描不会把日志/备份再当成待提取文件。

use namecat_extract_core as e;
use std::fs;
use std::path::{Path, PathBuf};

struct Sandbox {
    base: PathBuf,
}

impl Sandbox {
    fn new(tag: &str) -> Self {
        let base = std::env::temp_dir().join(format!(
            "filecat_extract_test_{tag}_{}_{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let _ = fs::remove_dir_all(&base);
        fs::create_dir_all(&base).unwrap();
        Self { base }
    }

    fn root(&self) -> PathBuf {
        let p = self.base.join("data");
        fs::create_dir_all(&p).unwrap();
        p
    }

    fn program_root(&self) -> String {
        self.base.join("program").to_string_lossy().into_owned()
    }

    fn rollback(&self) -> PathBuf {
        e::rollback::rollback_dir(&self.program_root())
    }
}

impl Drop for Sandbox {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.base);
    }
}

fn write(p: &Path, name: &str, body: &str) -> PathBuf {
    let path = p.join(name);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(&path, body).unwrap();
    path
}

fn opts(s: &Sandbox, mode: e::ExtractMode, conflict: e::ConflictPolicy) -> e::ExtractOptions {
    e::ExtractOptions {
        root: s.root().to_string_lossy().into_owned(),
        program_root: s.program_root(),
        mode,
        conflict,
        max_depth: 0,
        exclude_dirs: Vec::new(),
        exclude_exts: Vec::new(),
        clean_empty_dirs: false,
        overwrite_confirmed: conflict.is_dangerous(),
    }
}

struct Silent;
impl e::Progress for Silent {
    fn on_progress(&self, _c: usize, _t: usize, _m: &str) {}
}

fn target_names(plan: &e::ExtractPlan) -> Vec<String> {
    plan.items
        .iter()
        .map(|it| it.target.file_name().unwrap().to_string_lossy().into_owned())
        .collect()
}

// --------------------------------------------------------------------------- //

#[test]
fn flatten_collects_all_depths_to_root() {
    let s = Sandbox::new("flatten");
    let root = s.root();
    write(&root, "top.txt", "t");
    write(&root, "a/one.txt", "1");
    write(&root, "a/b/two.txt", "2");
    write(&root, "a/b/c/three.txt", "3");
    let o = opts(&s, e::ExtractMode::Flatten, e::ConflictPolicy::Rename);
    let plan = e::plan::build_plan(&o).unwrap();
    assert_eq!(plan.items.len(), 4);
    for it in &plan.items {
        assert_eq!(it.target_dir, root);
    }
    // 根目录已就位的文件不搬运
    let top = plan.items.iter().find(|i| i.name == "top.txt").unwrap();
    assert_eq!(top.status, e::FileStatus::Skip);
    assert_eq!(top.target, root.join("top.txt"));
}

#[test]
fn one_level_moves_only_one_parent() {
    let s = Sandbox::new("onelevel");
    let root = s.root();
    write(&root, "a/b/c/deep.txt", "d");
    write(&root, "a/mid.txt", "m");
    write(&root, "shallow.txt", "s");
    let o = opts(&s, e::ExtractMode::OneLevel, e::ConflictPolicy::Rename);
    let plan = e::plan::build_plan(&o).unwrap();
    let deep = plan.items.iter().find(|i| i.name == "deep.txt").unwrap();
    assert_eq!(deep.source_dir, root.join("a").join("b").join("c"));
    assert_eq!(deep.target_dir, root.join("a").join("b"));
    let mid = plan.items.iter().find(|i| i.name == "mid.txt").unwrap();
    assert_eq!(mid.target_dir, root);
    let shallow = plan.items.iter().find(|i| i.name == "shallow.txt").unwrap();
    assert_eq!(shallow.status, e::FileStatus::Skip);
}

#[test]
fn one_level_plan_is_snapshot_not_recomputed() {
    let s = Sandbox::new("onelevel_snapshot");
    let root = s.root();
    write(&root, "x/y/f.txt", "f");
    let o = opts(&s, e::ExtractMode::OneLevel, e::ConflictPolicy::Rename);
    let plan = e::plan::build_plan(&o).unwrap();
    let before = plan.items[0].target.clone();
    let stop = e::StopFlag::new();
    let res = e::execute::execute(&o, &plan, false, &stop, &Silent).unwrap();
    // 目标仍是扫描快照算出的 x/f.txt，没有因为中间目录变化再上移一层
    assert_eq!(res.items[0].target, before);
    assert_eq!(res.items[0].target, root.join("x").join("f.txt"));
    assert!(res.items[0].target.is_file());
    assert!(!root.join("f.txt").exists());
}

#[test]
fn max_depth_limits_recursion() {
    let s = Sandbox::new("depth");
    let root = s.root();
    write(&root, "d1/f1.txt", "1");
    write(&root, "d1/d2/f2.txt", "2");
    write(&root, "d1/d2/d3/f3.txt", "3");
    let mut o = opts(&s, e::ExtractMode::Flatten, e::ConflictPolicy::Rename);
    o.max_depth = 1;
    let plan = e::plan::build_plan(&o).unwrap();
    assert_eq!(plan.items.len(), 1);
    assert_eq!(plan.items[0].name, "f1.txt");
    o.max_depth = 2;
    let plan2 = e::plan::build_plan(&o).unwrap();
    assert_eq!(plan2.items.len(), 2);
    o.max_depth = 0;
    let plan3 = e::plan::build_plan(&o).unwrap();
    assert_eq!(plan3.items.len(), 3);
}

#[test]
fn exclude_dirs_and_exts_case_insensitive() {
    let s = Sandbox::new("exclude");
    let root = s.root();
    write(&root, "keep/a.txt", "a");
    write(&root, "SKIP/a.txt", "s");
    write(&root, "SKIP/deep/b.txt", "b");
    write(&root, "keep/skip.TMP", "t");
    write(&root, "keep/keep.dat", "d");
    let mut o = opts(&s, e::ExtractMode::Flatten, e::ConflictPolicy::Rename);
    o.exclude_dirs = e::ExtractOptions::parse_list(" skip , Skip ", false);
    o.exclude_exts = e::ExtractOptions::parse_list(" .TMP, tmp ", true);
    let plan = e::plan::build_plan(&o).unwrap();
    let names: Vec<&str> = plan.items.iter().map(|i| i.name.as_str()).collect();
    assert!(names.contains(&"a.txt"));
    assert!(names.contains(&"keep.dat"));
    assert!(!names.contains(&"skip.TMP"));
    // SKIP 目录及其子目录整体跳过：b.txt 不在清单里
    assert!(!names.contains(&"b.txt"));
    assert_eq!(plan.items.len(), 2);
}

#[test]
fn conflict_rename_keeps_both_and_extension() {
    let s = Sandbox::new("rename");
    let root = s.root();
    write(&root, "a.jpg", "root");
    write(&root, "sub/a.jpg", "sub");
    write(&root, "sub2/a.jpg", "sub2");
    let o = opts(&s, e::ExtractMode::Flatten, e::ConflictPolicy::Rename);
    let plan = e::plan::build_plan(&o).unwrap();
    let names = target_names(&plan);
    assert!(names.contains(&"a (2).jpg".to_string()));
    assert!(names.contains(&"a (3).jpg".to_string()));
    // 每个目标路径唯一（含保留下来的根目录 a.jpg）
    let mut all: Vec<PathBuf> = vec![root.join("a.jpg")];
    all.extend(plan.items.iter().filter(|i| i.status != e::FileStatus::Skip).map(|i| i.target.clone()));
    let mut sorted = all.clone();
    sorted.sort();
    sorted.dedup();
    assert_eq!(sorted.len(), all.len());
}

#[test]
fn conflict_skip_leaves_existing() {
    let s = Sandbox::new("skip");
    let root = s.root();
    write(&root, "same.txt", "root");
    write(&root, "sub/same.txt", "sub");
    let o = opts(&s, e::ExtractMode::Flatten, e::ConflictPolicy::Skip);
    let plan = e::plan::build_plan(&o).unwrap();
    let conflict = plan.items.iter().find(|i| i.name == "same.txt" && i.status != e::FileStatus::Skip);
    assert!(conflict.is_none() || conflict.unwrap().status == e::FileStatus::Skip);
    let stop = e::StopFlag::new();
    let _res = e::execute::execute(&o, &plan, false, &stop, &Silent).unwrap();
    assert_eq!(fs::read_to_string(root.join("same.txt")).unwrap(), "root");
    assert!(root.join("sub").join("same.txt").is_file());
}

#[test]
fn overwrite_requires_confirmation_and_backs_up() {
    let s = Sandbox::new("overwrite");
    let root = s.root();
    write(&root, "same.txt", "OLD");
    write(&root, "sub/same.txt", "NEW");
    let mut o = opts(&s, e::ExtractMode::Flatten, e::ConflictPolicy::Overwrite);
    o.overwrite_confirmed = false;
    assert!(e::plan::build_plan(&o).is_err(), "未确认必须拒绝");
    o.overwrite_confirmed = true;
    let plan = e::plan::build_plan(&o).unwrap();
    let it = plan.items.iter().find(|i| i.overwritten).unwrap();
    assert_eq!(it.status, e::FileStatus::Ready);
    let stop = e::StopFlag::new();
    let res = e::execute::execute(&o, &plan, false, &stop, &Silent).unwrap();
    assert_eq!(res.succeeded, 1);
    assert_eq!(fs::read_to_string(root.join("same.txt")).unwrap(), "NEW");
    // 备份落在固定目录 backups/
    let backups = s.rollback().join(e::model::BACKUP_SUBDIR);
    let files: Vec<_> = fs::read_dir(&backups).unwrap().flatten().collect();
    assert_eq!(files.len(), 1);
    assert_eq!(fs::read_to_string(files[0].path()).unwrap(), "OLD");
    // 撤销后恢复
    let undo = e::rollback::undo_latest(&s.program_root(), &Silent).unwrap();
    assert_eq!(undo.succeeded, 1);
    assert_eq!(fs::read_to_string(root.join("same.txt")).unwrap(), "OLD");
    assert_eq!(fs::read_to_string(root.join("sub").join("same.txt")).unwrap(), "NEW");
}

#[test]
fn dry_run_never_writes_anything() {
    let s = Sandbox::new("dryrun");
    let root = s.root();
    write(&root, "a/b/f.txt", "f");
    let o = opts(&s, e::ExtractMode::Flatten, e::ConflictPolicy::Rename);
    let plan = e::plan::build_plan(&o).unwrap();
    let before = fs::read_to_string(root.join("a").join("b").join("f.txt")).unwrap();
    let stop = e::StopFlag::new();
    let res = e::execute::execute(&o, &plan, true, &stop, &Silent).unwrap();
    assert!(res.dry_run);
    assert!(res.log_path.is_none(), "模拟运行不写日志");
    assert!(!s.rollback().exists(), "模拟运行不建日志目录");
    assert_eq!(fs::read_to_string(root.join("a").join("b").join("f.txt")).unwrap(), before);
    assert!(!root.join("f.txt").exists());
    assert!(res.items.iter().all(|i| i.status == e::FileStatus::DryRun));
}

#[test]
fn stop_keeps_completed_and_marks_rest_stopped() {
    let s = Sandbox::new("stop");
    let root = s.root();
    for i in 0..6 {
        write(&root, &format!("d/f{i}.txt"), "x");
    }
    let o = opts(&s, e::ExtractMode::Flatten, e::ConflictPolicy::Rename);
    let plan = e::plan::build_plan(&o).unwrap();
    // 用专用进度回调在第 2 项后请求停止
    struct Stopper {
        flag: std::sync::Arc<e::StopFlag>,
        n: std::sync::atomic::AtomicUsize,
    }
    impl e::Progress for Stopper {
        fn on_progress(&self, _c: usize, _t: usize, _m: &str) {
            let n = self.n.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            // 第一次回调是「开始执行」(current=0)，第二次是首项完成，此时请求停止：
            // 当前文件已完整结束，后续项应全部标 Stopped。
            if n >= 1 {
                self.flag.stop();
            }
        }
    }
    let flag = std::sync::Arc::new(e::StopFlag::new());
    let p = Stopper { flag: flag.clone(), n: Default::default() };
    let res = e::execute::execute(&o, &plan, false, &flag, &p).unwrap();
    assert!(res.stopped);
    assert!(res.succeeded >= 1);
    assert!(res.items.iter().any(|i| i.status == e::FileStatus::Stopped));
    assert!(!res.items.iter().any(|i| i.status == e::FileStatus::Stopped && i.message.contains("完成")));
}

#[test]
fn clean_empty_dirs_deepest_first_and_undo_restores() {
    let s = Sandbox::new("clean");
    let root = s.root();
    write(&root, "a/b/c/f.txt", "f");
    let mut o = opts(&s, e::ExtractMode::Flatten, e::ConflictPolicy::Rename);
    o.clean_empty_dirs = true;
    let plan = e::plan::build_plan(&o).unwrap();
    let stop = e::StopFlag::new();
    let res = e::execute::execute(&o, &plan, false, &stop, &Silent).unwrap();
    assert_eq!(res.errors, Vec::<String>::new(), "清理不应报错：{:?}", res.errors);
    assert_eq!(res.cleaned_dirs, 3, "候选目录 {:?}", plan.empty_dir_candidates);
    assert!(!root.join("a").exists());
    assert!(root.join("f.txt").is_file());
    let undo = e::rollback::undo_latest(&s.program_root(), &Silent).unwrap();
    assert_eq!(undo.succeeded, 1);
    assert_eq!(undo.restored_dirs, 3);
    assert!(root.join("a").join("b").join("c").join("f.txt").is_file());
}

#[test]
fn rollback_dir_is_fixed_and_excluded_from_scan() {
    let s = Sandbox::new("internal");
    // 用户把程序根目录当成扫描目标：日志/备份绝不能进计划
    let program = PathBuf::from(s.program_root());
    fs::create_dir_all(&program).unwrap();
    // 故意把 data 放在程序根内
    let actual_root = program.clone();
    write(&actual_root, "real.txt", "r");
    let mut o = opts(&s, e::ExtractMode::Flatten, e::ConflictPolicy::Rename);
    o.root = actual_root.to_string_lossy().into_owned();
    // 先手动造内部数据（模拟上一次执行留下的日志与备份）
    let rb = e::rollback::ensure_rollback_dir(&s.program_root()).unwrap();
    write(&rb, "rollback_old.json", "{}");
    write(&rb.join("backups"), "old.bak", "backup");
    let plan = e::plan::build_plan(&o).unwrap();
    let names: Vec<&str> = plan.items.iter().map(|i| i.name.as_str()).collect();
    assert!(names.contains(&"real.txt"));
    assert!(!names.contains(&"rollback_old.json"));
    assert!(!names.contains(&"old.bak"));
    assert!(rb.to_string_lossy().replace('\\', "/").contains("Extract/Rollback_Log"));
}

#[test]
fn log_schema_and_atomic_write() {
    let s = Sandbox::new("log");
    let (dir, op_id, path) = {
        let o = opts(&s, e::ExtractMode::Flatten, e::ConflictPolicy::Rename);
        e::rollback::prepare_log(&o).unwrap()
    };
    assert!(path.is_file());
    let text = fs::read_to_string(&path).unwrap();
    let v: serde_json::Value = serde_json::from_str(&text).unwrap();
    assert_eq!(v["schema"], e::model::SCHEMA);
    assert_eq!(v["operation_id"], op_id);
    assert!(!path.with_extension("json.tmp").exists(), "临时文件已原子替换");
    assert!(dir.join("backups").is_dir());
    let logs = e::rollback::list_logs(&s.program_root());
    assert_eq!(logs.len(), 1);
}

#[test]
fn csv_has_bom_and_required_columns() {
    let s = Sandbox::new("csv");
    let root = s.root();
    write(&root, "a/f.txt", "f");
    let o = opts(&s, e::ExtractMode::Flatten, e::ConflictPolicy::Rename);
    let plan = e::plan::build_plan(&o).unwrap();
    let out = s.base.join("report.csv");
    e::csv::export_to_path(&plan.items, &out).unwrap();
    let bytes = fs::read(&out).unwrap();
    assert_eq!(&bytes[..3], &[0xEF, 0xBB, 0xBF]);
    let text = String::from_utf8_lossy(&bytes[3..]);
    for h in ["文件名", "来源目录", "原始路径", "目标路径", "文件大小(字节)", "操作状态", "错误信息"] {
        assert!(text.contains(h), "缺列 {h}");
    }
}
