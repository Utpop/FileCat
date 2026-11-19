//! 计划生成：两种搬运方式的目标计算 + 三种冲突策略。
//!
//! 关键点：
//! - 目标路径在**扫描快照**上一次性算完，执行不重新推导（逐级上移不串层）；
//! - 冲突处理顺序由 `ScannedFile` 的路径字典序确定；
//! - 同一目标位置被多个源命中时，`occupied` 集合保证确定性与不意外覆盖；
//! - 自动改名保留原扩展名，且生成后再次验证不冲突。

use crate::model::{
    ConflictPolicy, Error, ExtractMode, ExtractOptions, ExtractPlan, FileStatus, PlanItem,
    ScannedFile,
};
use crate::scan::{scan, ScanOutput};
use std::collections::HashSet;
use std::path::{Path, PathBuf};

/// 生成完整提取计划（只扫描 + 计算，不碰文件系统写入）。
///
/// 计划生成是只读操作，**不要求** `overwrite_confirmed`：用户需要先看到覆盖
/// 预览，才可能在真正执行前确认。危险确认只在 `execute` 的真实执行分支校验。
pub fn build_plan(opts: &ExtractOptions) -> crate::model::Result<ExtractPlan> {
    let scanned: ScanOutput = scan(opts, None)?;
    let root = PathBuf::from(&opts.root);
    let root = crate::fs::canonical_plain(&root).unwrap_or(root);
    Ok(plan_from_scanned(opts, &root, &scanned))
}

/// 检查计划是否仍可安全执行（预览即承诺，禁止拿已执行过的计划重跑）。
///
/// 计划是从 Python dict 反序列化来的**外部输入**，不能只信任它的字段：
/// `it.overwritten=true` 会让执行阶段走去 `remove_file(it.target)`，若不校验
/// 目标位置，一份伪造的 plan 就能删除/搬移任意路径的文件。
///
/// - 任一项已经是执行后的终态（Done / DryRun / Stopped）→ 拒绝；
///   `Error` 是计划阶段自身判定的无效项，不算「已执行」，仍允许提交（会按跳过处理）。
/// - `plan.root` 必须与 `opts.root` 指向同一目录；`plan.conflict` 必须与 opts 一致。
/// - 每个 source / target 都必须位于 root 之内，且 target != source。
/// - `overwritten` 只允许在 Overwrite 策略下为 true；且同一目标最多一个覆盖项。
pub fn validate_for_execute(
    plan: &ExtractPlan,
    opts: &ExtractOptions,
) -> crate::model::Result<()> {
    if plan.conflict != opts.conflict {
        return Err(Error::Invalid("计划的冲突策略与当前选项不一致，请重新扫描预览"));
    }
    let root = crate::fs::canonical_plain(Path::new(&opts.root))
        .unwrap_or_else(|_| PathBuf::from(&opts.root));
    // plan.root 必须就是本次扫描的根，防止拿 A 目录的计划去 B 目录执行
    let plan_root = crate::fs::canonical_plain(&plan.root)
        .unwrap_or_else(|_| plan.root.clone());
    if norm(&plan_root) != norm(&root) {
        return Err(Error::Invalid("计划对应的目标文件夹与当前选项不一致，请重新扫描预览"));
    }
    // 用组件比较而非字符串前缀：C:\data 不能匹配 C:\data2\...；
    // fs::is_within 还会拒绝含 `..` 的越界路径（伪造计划校验）。
    let within = |p: &Path| -> bool {
        crate::fs::is_within(p, &root)
    };
    let mut overwrite_targets: HashSet<String> = HashSet::new();
    for it in &plan.items {
        if matches!(
            it.status,
            FileStatus::Done | FileStatus::DryRun | FileStatus::Stopped
        ) {
            return Err(Error::Invalid("计划已执行过，请重新扫描预览后再执行"));
        }
        if !within(&it.source) || !within(&it.target) {
            return Err(Error::Invalid("计划中存在超出目标文件夹范围的路径，请重新扫描预览"));
        }
        // Skip 项（已在目标位置）本来就是 target == source，属合法计划；
        // 只有真正要搬运的项才禁止自我搬运。
        if it.status != FileStatus::Skip && norm(&it.source) == norm(&it.target) {
            return Err(Error::Invalid("计划中存在源与目标相同的项，请重新扫描预览"));
        }
        if it.overwritten && opts.conflict != ConflictPolicy::Overwrite {
            return Err(Error::Invalid("非覆盖策略的计划里出现了覆盖标记，请重新扫描预览"));
        }
        if it.overwritten {
            let key = norm(&it.target);
            if !overwrite_targets.insert(key) {
                return Err(Error::Invalid("覆盖策略下多个文件指向同一目标，请重新扫描或改用自动改名"));
            }
        }
    }
    Ok(())
}

/// 磁盘侧兜底：同一份 plan 对象被重复 `execute` 时，内存状态仍是「未执行」，
/// 上面的校验拦不住。若**所有**需要搬运的项都已经呈现「源不在、目标在」，
/// 说明这批文件早已搬完，必须拒绝，否则会生成一份空撤销日志挡住真正的记录。
pub fn plan_looks_executed(plan: &ExtractPlan) -> bool {
    let actionable: Vec<&PlanItem> = plan
        .items
        .iter()
        .filter(|it| !matches!(it.status, FileStatus::Skip | FileStatus::Error | FileStatus::Done))
        .collect();
    if actionable.is_empty() {
        return false;
    }
    // 只要存在「源已消失、目标已出现」的项就说明这批至少被部分执行过；
    // 用 all() 会让部分搬移（停止/失败后）蒙混过关，再执行会生成第二份日志，
    // 把第一批文件的撤销记录挤到 undo_latest 够不着的位置。
    actionable.iter().any(|it| {
        !crate::fs::file_exists(&it.source) && crate::fs::file_exists(&it.target)
    })
}

/// 纯函数版：给定扫描快照算计划，便于单元测试注入数据。
pub fn plan_from_scanned(opts: &ExtractOptions, root: &Path, scanned: &ScanOutput) -> ExtractPlan {
    // 目标占用表：扫描时盘上已有的文件（含被排除的、根目录已就位的），
    // 加上本批次已经规划好的目标。大小写规范化，Windows 语义。
    let mut occupied: HashSet<String> = scanned.existing_files.iter().cloned().collect();
    // 本批次将要搬走的文件位置。覆盖策略只能覆盖「盘上常驻、不会被搬走」的文件；
    // 若目标恰好是另一个待搬源（one_level 嵌套同名：a/b/x.txt 的目标是 a/x.txt，
    // 而 a/x.txt 自己也要上移），把它当可覆盖文件会先备份删掉它，导致那一项
    // 执行时报「源不存在」，最终两份文件只剩一份、撤销也还原不回来。
    // 这些位置在规划时仍被占用（源文件还在盘上），
    // 所以保留在 occupied 里以触发冲突处理；但不能走「覆盖」分支——覆盖一个
    // 即将被别的规则搬走的文件没有意义，还会让那一项执行时报源不存在/被跳过。
    // 因此它们只在覆盖判定时被排除，最终走自动改名。
    let occupied_by_moving: HashSet<String> = scanned
        .files
        .iter()
        .map(|f| norm(&f.source))
        .collect();
    // 本批次已经分配出去的目标。与 occupied 分开：occupied 含盘上原有文件，
    // 覆盖策略下第一个源允许覆盖盘上原文件，但后续源不能再指向同一位置。
    let mut planned_targets: HashSet<String> = HashSet::new();
    // 同名文件批量改名时从上次序号继续，避免 O(n²) 重复试 (2)、(3)…
    let mut name_counters: std::collections::HashMap<(PathBuf, String, String), u64> =
        std::collections::HashMap::new();
    let mut items = Vec::with_capacity(scanned.files.len());

    for (index, f) in scanned.files.iter().enumerate() {
        let (base_dir, same_place) = target_dir_for(opts.mode, root, f);
        if same_place {
            // 目标就是原位置：无需搬运，也无需冲突处理
            items.push(PlanItem {
                index,
                name: f.name.clone(),
                source: f.source.clone(),
                source_dir: f.source_dir.clone(),
                target: f.source.clone(),
                target_dir: f.source_dir.clone(),
                size: f.size,
                depth: f.depth,
                status: FileStatus::Skip,
                message: "已在目标位置，无需搬运".into(),
                renamed: false,
                overwritten: false,
                backup: None,
            });
            continue;
        }

        let mut target = base_dir.join(&f.name);
        let key = norm(&target);
        let mut renamed = false;
        let mut overwritten = false;
        let mut status = FileStatus::Ready;
        let mut message = String::new();

        if occupied.contains(&key) {
            // 覆盖策略只允许「第一个」源覆盖盘上原有的同名文件：后续源若也指向
            // 同一目标，会被反复覆盖，中间源只留在备份里，撤销时两条 entry 指向
            // 同一个 target 会互相打架（实测会把旧文件搬错位置）。因此后续源一律
            // 走自动改名兜底，保证每个目标在本批次内唯一。
            let overwrite_first = opts.conflict == ConflictPolicy::Overwrite
                && !planned_targets.contains(&key)
                && !occupied_by_moving.contains(&key);
            match opts.conflict {
                ConflictPolicy::Skip => {
                    status = FileStatus::Skip;
                    message = "目标位置已有同名文件，按策略跳过".into();
                }
                ConflictPolicy::Overwrite if overwrite_first => {
                    status = FileStatus::Ready;
                    overwritten = true;
                    message = "危险：将覆盖目标位置已有文件（执行前会备份）".into();
                }
                _ => {
                    // Rename，或 Overwrite 的后续同目标源：生成 name (2).ext
                    let (stem, ext) = split_name(&f.name);
                    let key = (base_dir.clone(), stem, ext);
                    let start = name_counters.get(&key).copied().unwrap_or(2);
                    let ((new_name, path), used) = unique_name_checked(
                        &base_dir, &f.name, &occupied, start,
                        &|p| crate::fs::file_exists(p),
                    );
                    name_counters.insert(key, used + 1);
                    target = path;
                    renamed = true;
                    status = FileStatus::Renamed;
                    message = if opts.conflict == ConflictPolicy::Overwrite {
                        format!("同名目标已被前一个文件占用，自动改名为 {}", new_name)
                    } else {
                        format!("同名冲突，自动改名为 {}", new_name)
                    };
                }
            }
        }
        if status != FileStatus::Skip {
            let final_key = norm(&target);
            if overwritten {
                // 覆盖是有意为之：目标已在盘上属于预期。这里仍把目标登记进
                // occupied / planned_targets，后续源才能识别「已被占用」并改名。
                // 执行阶段会先备份再替换，绝不盲目覆盖。
                occupied.insert(final_key.clone());
                planned_targets.insert(final_key);
            } else if occupied.contains(&final_key) {
                // 二次验证：改名后仍冲突（理论不可达，兜底）
                status = FileStatus::Error;
                message = "生成的名称仍然存在冲突".into();
            } else {
                occupied.insert(final_key.clone());
                planned_targets.insert(final_key);
            }
        }
        items.push(PlanItem {
            index,
            name: f.name.clone(),
            source: f.source.clone(),
            source_dir: f.source_dir.clone(),
            target,
            target_dir: base_dir,
            size: f.size,
            depth: f.depth,
            status,
            message,
            renamed,
            overwritten,
            backup: None,
        });
    }

    ExtractPlan {
        root: root.to_path_buf(),
        mode: opts.mode,
        conflict: opts.conflict,
        max_depth: opts.max_depth,
        items,
        empty_dir_candidates: scanned.dirs_deepest_first.clone(),
        scanned_count: scanned.files.len(),
        excluded_count: scanned.excluded_count,
        error_count: scanned.error_count,
    }
}

/// 返回 (目标目录, 是否原地不动)。
pub fn target_dir_for(mode: ExtractMode, root: &Path, f: &ScannedFile) -> (PathBuf, bool) {
    match mode {
        ExtractMode::Flatten => {
            let same = f.source_dir == root;
            (root.to_path_buf(), same)
        }
        ExtractMode::OneLevel => {
            let parent = f
                .source_dir
                .parent()
                .map(Path::to_path_buf)
                .unwrap_or_else(|| root.to_path_buf());
            // 只能上移一级；如果父目录不在扫描根内，退到根（防止越界）
            let parent = if parent.starts_with(root) {
                parent
            } else {
                root.to_path_buf()
            };
            let same = parent == f.source_dir;
            (parent, same)
        }
    }
}

/// 生成 `name (2).ext` / `name (3).ext`…，保留原扩展名，同时查内存占用表
/// **与磁盘实际状态**。`start` 是上一次为同名文件用过的序号，用 counter map
/// 从那里继续，避免 10000 个同名文件时 O(n²) 的字符串构造（约 5000 万次）。
/// `probe` 由调用方注入（core 层不直接依赖 fs，便于纯内存单测）。
pub fn unique_name_checked(
    dir: &Path,
    name: &str,
    occupied: &HashSet<String>,
    start: u64,
    probe: &dyn Fn(&Path) -> bool,
) -> ((String, PathBuf), u64) {
    let (stem, ext) = split_name(name);
    let mut n = start.max(2);
    loop {
        let candidate = format!("{stem} ({n}){ext}");
        let path = dir.join(&candidate);
        if !occupied.contains(&norm(&path)) && !probe(&path) {
            return ((candidate, path), n);
        }
        n += 1;
        if n > 1_000_000 {
            // 兜底：进程号 + 序号后缀，仍保留扩展名，并继续查占用
            let fallback = format!("{stem}_fc{}_{}{ext}", std::process::id(), n);
            let p = dir.join(&fallback);
            if !occupied.contains(&norm(&p)) && !probe(&p) {
                return ((fallback, p), n);
            }
        }
    }
}

/// 纯内存版本（单元测试 / 不需要磁盘探测的调用方）。
pub fn unique_name(dir: &Path, name: &str, occupied: &HashSet<String>) -> (String, PathBuf) {
    unique_name_checked(dir, name, occupied, 2, &|_| false).0
}

/// 按最后一个点切分；`.gitignore` 这类无扩展名文件整体当 stem。
fn split_name(name: &str) -> (String, String) {
    match name.rfind('.') {
        Some(i) if i > 0 => (name[..i].to_string(), name[i..].to_string()),
        _ => (name.to_string(), String::new()),
    }
}

fn norm(path: &Path) -> String {
    path.to_string_lossy().to_ascii_lowercase()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn split_name_keeps_extension() {
        assert_eq!(split_name("a.tar.gz"), ("a.tar".into(), ".gz".into()));
        assert_eq!(split_name(".gitignore"), (".gitignore".into(), "".into()));
        assert_eq!(split_name("photo.jpg"), ("photo".into(), ".jpg".into()));
    }

    #[test]
    fn unique_name_skips_taken_candidates() {
        let dir = Path::new(r"C:\data");
        let mut occupied = HashSet::new();
        occupied.insert(norm(&dir.join("a.jpg")));
        occupied.insert(norm(&dir.join("a (2).jpg")));
        let (name, path) = unique_name(dir, "a.jpg", &occupied);
        assert_eq!(name, "a (3).jpg");
        assert_eq!(path, dir.join("a (3).jpg"));
    }
}
