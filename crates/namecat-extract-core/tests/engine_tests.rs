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
    // 计划生成是只读预览，不应被「尚未确认」拦住（用户得先看到覆盖清单）。
    let plan = e::plan::build_plan(&o).unwrap_or_else(|err| {
        panic!("覆盖预览不应要求确认：{err}")
    });
    assert!(plan.items.iter().any(|i| i.overwritten), "应标出将被覆盖的项");
    // 真正写盘时才强制要求确认：模拟运行零写入，允许；真实执行未确认必须拒绝。
    let stop = e::StopFlag::new();
    assert!(e::execute::execute(&o, &plan, false, &stop, &Silent).is_err(),
            "真实执行未确认必须拒绝");
    let sim = e::execute::execute(&o, &plan, true, &stop, &Silent).unwrap();
    assert!(sim.dry_run, "模拟运行不受确认位限制");
    o.overwrite_confirmed = true;
    let plan = e::plan::build_plan(&o).unwrap();
    let it = plan.items.iter().find(|i| i.overwritten).unwrap();
    assert_eq!(it.status, e::FileStatus::Ready);
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

// --------------------------------------------------------------------------- //
// 3.0.1 回归：覆盖策略 / 计划重入 / 空日志 / 崩溃检查点
// --------------------------------------------------------------------------- //

#[test]
fn overwrite_multiple_sources_keep_unique_targets_and_undo_restores() {
    let s = Sandbox::new("overwrite_multi");
    let root = s.root();
    write(&root, "a.txt", "OLD");
    write(&root, "s1/a.txt", "ONE");
    write(&root, "s2/a.txt", "TWO");
    let o = opts(&s, e::ExtractMode::Flatten, e::ConflictPolicy::Overwrite);
    let plan = e::plan::build_plan(&o).unwrap();
    // 同一个目标只允许第一个源覆盖，其余必须自动改名
    let ow: Vec<_> = plan.items.iter().filter(|i| i.overwritten).collect();
    assert_eq!(ow.len(), 1, "覆盖项必须唯一：{:?}",
               plan.items.iter().map(|i| (i.name.as_str(), i.overwritten, i.target.clone())).collect::<Vec<_>>());
    let mut targets: Vec<_> = plan.items.iter()
        .filter(|i| i.status != e::FileStatus::Skip)
        .map(|i| i.target.clone()).collect();
    targets.sort();
    targets.dedup();
    assert_eq!(targets.len(), plan.items.iter().filter(|i| i.status != e::FileStatus::Skip).count(),
               "本批次目标必须两两不同");
    let stop = e::StopFlag::new();
    let res = e::execute::execute(&o, &plan, false, &stop, &Silent).unwrap();
    // 根目录 a.txt 是 Skip（已在目标位置），只有 s1/s2 两个源需要搬运
    assert_eq!(res.succeeded, 2, "两个子目录源都应成功落位：{:?}", res.errors);
    // OLD 在备份里，ONE/TWO 各占一个独立目标名，谁都没被悄悄吃掉
    let backups = s.rollback().join(e::model::BACKUP_SUBDIR);
    assert_eq!(fs::read_dir(&backups).unwrap().flatten().count(), 1);
    let undo = e::rollback::undo_latest(&s.program_root(), &Silent).unwrap();
    assert!(undo.complete && undo.failed == 0 && undo.skipped == 0,
            "撤销应完全成功：{:?}", undo.errors);
    assert_eq!(undo.succeeded, 2);
    assert_eq!(fs::read_to_string(root.join("a.txt")).unwrap(), "OLD");
    assert_eq!(fs::read_to_string(root.join("s1").join("a.txt")).unwrap(), "ONE");
    assert_eq!(fs::read_to_string(root.join("s2").join("a.txt")).unwrap(), "TWO");
}

#[test]
fn reexecuted_plan_is_rejected_without_second_log() {
    let s = Sandbox::new("reexec");
    let root = s.root();
    write(&root, "a/f.txt", "F");
    let o = opts(&s, e::ExtractMode::Flatten, e::ConflictPolicy::Rename);
    let plan = e::plan::build_plan(&o).unwrap();
    let stop = e::StopFlag::new();
    assert!(e::execute::execute(&o, &plan, false, &stop, &Silent).is_ok());
    let before = e::rollback::list_logs(&s.program_root()).len();
    assert_eq!(before, 1);
    // 拿同一份 plan 再跑：必须拒绝，且不能多出一份空日志
    assert!(e::execute::execute(&o, &plan, false, &stop, &Silent).is_err(),
            "已执行过的计划必须拒绝重跑");
    assert_eq!(e::rollback::list_logs(&s.program_root()).len(), before,
               "拒绝重跑不得产生新日志");
    // GUI 把执行结果（DryRun/Done）回传成 plan 时同样要拦住
    let mut stale = plan.clone();
    stale.items[0].status = e::FileStatus::Done;
    assert!(e::execute::execute(&o, &stale, true, &stop, &Silent).is_err());
}

#[test]
fn empty_execution_writes_no_log_and_undo_skips_empty_record() {
    let s = Sandbox::new("empty_log");
    let root = s.root();
    write(&root, "a.txt", "A"); // 根目录已就位 → 计划里只有 Skip
    let o = opts(&s, e::ExtractMode::Flatten, e::ConflictPolicy::Rename);
    let plan = e::plan::build_plan(&o).unwrap();
    assert!(plan.items.iter().all(|i| i.status == e::FileStatus::Skip));
    let stop = e::StopFlag::new();
    let res = e::execute::execute(&o, &plan, false, &stop, &Silent).unwrap();
    assert!(res.log_path.is_none(), "零写入执行不得生成撤销日志");
    assert_eq!(e::rollback::list_logs(&s.program_root()).len(), 0);
    // 即便历史里已经存在一份 0 条目日志，undo_latest 也不该把它当成目标
    let dir = e::rollback::prepare_log(&o).unwrap().0;
    let _ = dir;
    let undo = e::rollback::undo_latest(&s.program_root(), &Silent).unwrap();
    assert!(!undo.complete, "没有可撤销记录时应明确报告，而不是「撤销完成」");
}

#[test]
fn checkpoint_recovers_entries_after_crash() {
    let s = Sandbox::new("crash");
    let root = s.root();
    write(&root, "a/f.txt", "F");
    let o = opts(&s, e::ExtractMode::Flatten, e::ConflictPolicy::Rename);
    let (_dir, _op_id, path) = e::rollback::prepare_log(&o).unwrap();
    let mut log = e::model::RollbackLog {
        schema: e::model::SCHEMA,
        app: "FileCat".into(),
        operation_id: "crash".into(),
        time: "2026-01-01 00:00:00".into(),
        root: root.clone(),
        mode: e::ExtractMode::Flatten,
        conflict: e::ConflictPolicy::Rename,
        dry_run: false,
        result: "running".into(),
        state: e::model::RollbackState::Active,
        entries: Vec::new(),
        cleaned_dirs: Vec::new(),
        errors: Vec::new(),
    };
    let entry = e::model::RollbackEntry {
        index: 0,
        source: root.join("a").join("f.txt"),
        target: root.join("f.txt"),
        size: 1,
        status: e::FileStatus::Done,
        message: "已完成".into(),
        renamed: false,
        overwritten: false,
        backup: None,
        source_mtime: 0,
        target_mtime: 0,
    };
    // 模拟「文件已搬走、正式日志还没 finalize」的崩溃点
    let mut writer = e::rollback::PendingWriter::open(&path).unwrap();
    e::rollback::checkpoint(&mut writer, &mut log, entry, &[], &[]).unwrap();
    drop(writer);
    // list_logs 是纯只读：把 .pending 合成进内存供展示，但不写回日志，
    // 否则执行中刷新日志页会固化不完整状态、后续成功项永远不再被合成。
    let logs = e::rollback::list_logs(&s.program_root());
    assert_eq!(logs.len(), 1);
    assert_eq!(logs[0].1.entries.len(), 1, "崩溃后应能从检查点恢复成功项");
    assert_eq!(logs[0].1.result, "running");
    // 真正要撤销时才做恢复并原子写回完整日志
    let _ = e::rollback::undo_latest(&s.program_root(), &Silent).unwrap();
    let logs2 = e::rollback::list_logs(&s.program_root());
    assert_ne!(logs2[0].1.result, "running",
               "undo_one 应把检查点恢复结果落盘，不再停留在 running");
}

#[test]
fn undo_partial_keeps_skipped_entries_for_retry() {
    let s = Sandbox::new("undo_partial");
    let root = s.root();
    write(&root, "a/f.txt", "F");
    let o = opts(&s, e::ExtractMode::Flatten, e::ConflictPolicy::Rename);
    let plan = e::plan::build_plan(&o).unwrap();
    let stop = e::StopFlag::new();
    e::execute::execute(&o, &plan, false, &stop, &Silent).unwrap();
    // 把目标文件临时移走，制造「目标已不存在，跳过」
    let moved = root.join("moved_away.txt");
    fs::rename(root.join("f.txt"), &moved).unwrap();
    let first = e::rollback::undo_latest(&s.program_root(), &Silent).unwrap();
    assert_eq!(first.skipped, 1, "目标缺失应跳过");
    // 关键：跳过的条目必须留在日志里，否则重入时它已消失、文件永远撤不回
    let logs = e::rollback::list_logs(&s.program_root());
    assert_eq!(logs[0].1.entries.len(), 1, "跳过的条目不得从日志里丢失");
    assert_eq!(logs[0].1.state, e::RollbackState::Partial);
    // 用户把文件放回后再次撤销，应能成功
    fs::rename(&moved, root.join("f.txt")).unwrap();
    let second = e::rollback::undo_latest(&s.program_root(), &Silent).unwrap();
    assert_eq!(second.succeeded, 1, "重入后应能恢复：{:?}", second.errors);
    assert!(root.join("a").join("f.txt").is_file());
}

#[test]
fn forged_plan_with_out_of_root_target_is_rejected() {
    let s = Sandbox::new("forged_plan");
    let root = s.root();
    write(&root, "a/f.txt", "F");
    let o = opts(&s, e::ExtractMode::Flatten, e::ConflictPolicy::Rename);
    let mut plan = e::plan::build_plan(&o).unwrap();
    // 伪造一份「目标指向扫描根之外」的计划（外部 JSON 输入无需签名）
    plan.items[0].target = PathBuf::from(r"C:\\Windows\\System32\\evil.txt");
    plan.items[0].overwritten = true;
    let stop = e::StopFlag::new();
    assert!(e::execute::execute(&o, &plan, false, &stop, &Silent).is_err(),
            "越界目标必须被拒绝，不能触发任意路径删除");
    // 含 `..` 的路径虽以 root 开头，但系统解析后越界，同样必须拒绝
    let mut plan3 = e::plan::build_plan(&o).unwrap();
    plan3.items[0].target = root.join("sub").join("..").join("..").join("outside.txt");
    assert!(e::execute::execute(&o, &plan3, false, &stop, &Silent).is_err(),
            "含 .. 的越界路径必须被拒绝");
    // 冲突策略与选项不一致也要拒绝
    let mut plan2 = e::plan::build_plan(&o).unwrap();
    plan2.conflict = e::ConflictPolicy::Overwrite;
    assert!(e::execute::execute(&o, &plan2, false, &stop, &Silent).is_err());
}

#[test]
fn one_level_overwrite_nested_same_name_keeps_both_files() {
    // a/x.txt 自身要上移，不能被 a/b/x.txt 当成可覆盖的盘上文件；
    // 否则 b/x.txt 覆盖掉 a/x.txt 后，后者执行时报源不存在，
    // 两份最终只剩一份，撤销也还原不回来。
    let s = Sandbox::new("onelevel_overwrite_nested");
    let root = s.root();
    write(&root, "a/x.txt", "OUTER");
    write(&root, "a/b/x.txt", "INNER");
    let mut o = opts(&s, e::ExtractMode::OneLevel, e::ConflictPolicy::Overwrite);
    o.overwrite_confirmed = true;
    let plan = e::plan::build_plan(&o).unwrap();
    let stop = e::StopFlag::new();
    let res = e::execute::execute(&o, &plan, false, &stop, &Silent).unwrap();
    assert_eq!(res.failed, 0, "不应有源不存在的失败：{:?}", res.errors);
    assert!(root.join("x.txt").is_file(), "外层文件应上移到根");
    assert!(root.join("a").join("x (2).txt").is_file()
            || root.join("a").join("x.txt").is_file(),
            "内层文件应单独占一个目标，不覆盖外层");
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
