# 交接文档：NameCat 引擎 Rust 化（给下一位 Agent）

> 交接时间：2026-11-05 11:00（P5 完成，工程收尾）
> 当前阶段：**P0–P5 + 2.0.1 清理，全部完成**。12/12 引擎模块是 Rust（PyO3）实现，
> `engine/` 只剩应用层（`__init__` / `cli` / `llm_resolve`），转发壳已删；
> 打包链路已带上引擎扩展并带打包后自检；性能与验收数据在
> `docs/TEST_REPORT.md` 第 8 节。
> 读这一份就够开工；细节再看引用的文档。

---

## 1. 项目是什么

NameCat：Windows 批量重命名工具（Python 3 + PySide6 GUI + CLI 共用引擎）。
仓库根 = `C:/Users/LocalAcc/WorkBuddy/NameCAT/NameCAT`（含 .git，远程
`https://github.com/Utpop/NameCat.git`）。版本唯一来源 `engine/__init__.py`
的 `APP_VER`（当前 1.6.3），README 页眉页脚 + 更新记录随动。

**代码规模**（2026-11-04 实测）：

| 模块 | 规模 | 说明 |
|---|---|---|
| `engine/` | 14 文件 / 6,293 行 | 纯逻辑，零 PySide6 依赖，纯标准库 |
| `ui/` | 10 文件 / 4,319 行 | PySide6，工作台 1,801 行 + 自研控件库 |
| `adapters/` | 1,084 行 | UI ↔ 引擎唯一通道（约 60 个引擎符号） |
| `verify/` | 5 文件 / 3,228 行 | 自检 410 断言 + 边界 190 断言 + UI 冒烟 |
| `workers/` + 入口 | ~300 行 | 后台线程、main.py、build_exe.py |

**自检**（验收金标准，运行需 `PYTHONPATH=_vendor`）：
`python verify/run_tests.py`（410 断言）+ `verify/test_edges.py`（190 断言）+
`verify/ui_smoke.py`。**用户要求：非明确要求不要跑测试套件。**

## 2. 任务与决策（已定，勿推翻重议）

**任务**：将 `engine/` 用 Rust 重写，编译为 PyO3 扩展（`.pyd`），
UI/适配层继续 Python。**不是**全量重写，**不**去 Python 运行时。

**已产出设计文档（权威）**：`docs/DESIGN_RUST_ENGINE.md`，要点：
- cargo workspace 双 crate：`crates/namecat-core`（纯 Rust，无 PyO3）+
  `crates/namecat-py`（绑定层，类型转换 + 异常映射）。
- **strangler 迁移**：`engine/*.py` 逐模块变一行 re-export
  （`from namecat_py.rules import *`），适配层/UI 零改动，每步跑全量自检。
- 正则选 **fancy-regex**（不是 regex crate！后者无回引用/环视，语义不兼容）。
- 错误模型：thiserror + `Result`，panic 用 catch_unwind 兜住不过边界。
- 边界签名 1:1 复刻现有函数（`make_rule`/`build`/`scan`/`apply`/`undo`…）。
- 路径用 `OsString/PathBuf`，文本面才转 `String`；按码点计数对齐 Python 语义。
- HTTP 用 `reqwest::blocking`，不开 tokio；JSON 用 serde（schema 兼容）；
  DPAPI 用 `windows` crate + Drop 守卫。
- Token 预算：产出 15-25 万，项目总消耗 50-100 万（llm/provider/secrets
  留 Python 可压到 35-70 万）。路线图 P0-P5 见设计文档 §8。

**下一步就是 P0**：搭 workspace + PyO3 hello + `dir()` 符号对比脚本 +
maturin 打通 → 全量自检绿（壳全走 Python）。

### P0 已交付（2026-11-05，实测）

| 产出 | 路径 | 状态 |
|---|---|---|
| cargo workspace（双 crate） | `Cargo.toml` / `crates/namecat-core` / `crates/namecat-py` | 编译通过，`cargo test` 6 项全绿 |
| PyO3 扩展 | `namecat_py.cp315-win_amd64.pyd`（0.2 MB）装进 `_vendor/` | `import` + 12 个子模块独立 import 均通过 |
| 一键构建 | `python rust_build.py`（`--debug` / `--check`） | 版本校验 → maturin → 装 `_vendor` → 冒烟，全链路跑通 |
| 符号闸门 | `python verify/rust_check.py` | 12 模块公开符号对比 + 根常量取值比对，当前 0/12 换壳 |

**关键实现点**（改之前先看懂，不然会白折腾一轮）：
- 子模块不是 `add_submodule` 就够：PyO3 的 `add_submodule` 只让属性访问能到，
  **不注册 `sys.modules` 就不能 `import namecat_py.rules`**。`shim::new_submodule`
  三步都做了（建模块 → 挂父模块 → 写 `sys.modules`）。strangler 壳靠这个成立。
- `__all__` 必须自己维护：PyO3 给模块预设的 `__all__` 只有 `["__doc__"]`，
  `from namecat_py.rules import *` 会导入个寂寞。每个模块用
  `shim::set_all(&module, &[...])` 显式声明，它会逐个 `hasattr` 校验，
  漏名字在自检阶段就炸。**P1 起每个迁移模块的 `__all__` 要等于 rust_check 的期望集**。
- 错误映射**偏离了设计稿**（有意）：设计稿说统一 `EngineError`，但现实现对用户侧
  错误一律 `ValueError`，`test_edges.py` 也按 `ValueError` 断言。所以
  `ErrorKind::Value → ValueError`、`Os → OSError`，只有 panic 兜底才走 `EngineError`。
- `cargo test` 里碰 Python API 前要 `Python::initialize()`（`extension-module`
  特性关掉了 auto-initialize），否则测试直接 assert 失败。

**P1 开工清单**：读 `engine/rules.py` → 在 `namecat-core/src/rules/` 落 `Rule`
（enum + serde，字段 `kind/a/b/c/id/extras`）→ `crates/namecat-py/src/py_rules.rs`
复刻 `make_rule/normalize/describe_chain/rules_to_json/rules_from_json/parse_cli_rule/
number_rule/number_options` 与 `RULE_KIND_*` 常量 → `engine/rules.py` 改一行
`from namecat_py.rules import *` → `python rust_build.py && python verify/rust_check.py -m rules`
→ 再跑 `verify/run_tests.py`。

### P1 已交付（2026-11-05，实测）

| 模块 | Rust 落点 | 状态 |
|---|---|---|
| `rules`（899 行） | `core/src/rules.rs` + `py/src/py_rules.rs` | 已换壳；28 种规则/替换串解析/CLI 解析全部对拍通过 |
| `template`（456 行） | `core/src/template.rs` + `py/src/py_template.rs` | 已换壳；选择器/渲染/expand_refs 对拍通过 |
| `zhconv`（303 行 + 52KB 表） | `core/src/zhconv.rs` + `zhconv_data.rs`（生成） | 已换壳；8172 个表内字 + BMP 全区间对拍 0 差异 |

配套新增：`core/src/ctx.rs`（`VarSource` 抽象）、`core/src/text.rs`（split_ext /
Python 的 title·sentence·capitalize·swapcase）、`py/src/pyctx.rs`（Python 对象 →
trait 的适配）、`py/src/pyjson.rs`（Py↔JSON，extras 往返不丢类型）、
`tools/gen_zhconv.py`（映射表生成/校验）、`verify/engine_symbols.json`（符号固化清单）。

**验收方式**（用户要求不主动跑测试套件，P1 用的是等价性对拍 + 端到端冒烟）：
`_verify_tmp/template_equiv.py`、`_verify_tmp/rules_equiv.py` 逐函数对比 Python 旧实现
（约 3000 组断言，含 28 种规则 × 多种文本 × ctx 的三种形态），换壳后
`main.py preview / rename / undo` 全流程冒烟通过。这两个脚本在换壳后已失去参考实现，
只作留档，别指望重跑。

**P1 必须知道的实现要点**：

1. **`Rule.extras` 必须是活的 Python dict**。`adapters/engine_adapter.py` 用
   `rule.extras.setdefault(...)`、`engine/cli.py` 用 `r.extras["instruction"] = body`
   **原地改写**。所以 `PyRule` 直接持有 `Py<PyDict>`（getter 返回同一对象），
   其余字段是普通 Rust 字段；调核心前把 dict 快照成 `serde_json::Map`，归一化后
   写回同一个 dict。**别"优化"成每次返回副本**，适配层会静默丢数据。
2. **`normalize` 必须返回同一个对象**。调用方普遍写 `rule = normalize(rule)`，
   绑定层写成返回 `()` 会把它变成 None。
3. **边界收参要按 `str()` 语义**：`Rule(selector=3)` / `Rule(extras=None)` /
   `rules_from_json({"a": 5})` 在 `test_edges.py` 里明确要求**不抛异常**
   （normalize 再回落）。`pyctx::text_arg` / `bool_arg` / `coerce_i64` 就是干这个的。
4. **PyO3 0.29 的 API 改名**：`downcast` → `cast` / `cast_into`；`PyObject` →
   `Py<PyAny>`；`fancy_regex::Captures<'_, str>`（两个泛型参数）。
5. **Rust 切片会炸、Python 不会**：`parse_range("5-2", 0)` 会吐出 `lo=0`，
   Python 的 `text[-1:1]` 只是空串，Rust 直接 panic 越界。`template::py_slice`
   统一处理负下标与夹紧。**任何拿计算出来的下标切字符串的地方都要用它**。
6. **`splice` 的 `from` 分支没有尾巴**：`text[:idx] + new`（选中段一直到结尾）。
   抄成 `head+new+tail` 会让 `until`/`from` 的结果多一截。
7. **`expand_refs` 里 `{left}/{right}` 是死代码**：`REF_KEYS` 本来就含 left/right，
   先被当普通变量查表（得到空串），后面那两个专门分支永远走不到。这是现有行为。
8. **`{date:格式}` 的格式被忽略**：`canon_key("date") == "now"`，于是 date 分支
   永远进不去，`{date:yyyy}` 等价于 `{date}`。只有 mtime/ctime/atime 能吃 `:格式`。
9. **`num_text` 的 zfill 把符号位算进宽度**：`-5` 宽 4 → `-005`（不是 `-0005`）。
10. **随机串不求位级一致**：`randomize` 与 `@@N@@` 用自研确定性 PRNG（FNV+LCG），
    契约只有"同一次计划内 谁+第几号+规则 相同 ⇒ 同一串"。Python 的 MT 复刻不值当。
11. **zhconv 表**：`zhconv_data.rs` 由 `tools/gen_zhconv.py` 从**换壳前**的
    `engine/zhconv.py` 生成（脚本现在要求 `--python-file`，因为 engine 那份已是壳）。
    改表流程：`git show <旧提交>:engine/zhconv.py > _verify_tmp/zhconv_old.py` →
    改 → `python tools/gen_zhconv.py --python-file _verify_tmp/zhconv_old.py`。
12. **符号闸门靠固化清单**：`verify/engine_symbols.json` 是换壳前 `--snapshot` 出来的
    期望集。换壳后再 `dir(engine.x)` 只能拿到空壳的符号表，检查会形同虚设。
    新增 Python 侧符号时才需要重跑 `--snapshot`（须先 git 回退该模块的壳）。

### P2 已交付（2026-11-05，实测）

| 模块 | Rust 落点 | 状态 |
|---|---|---|
| `scan`（415 行） | `core/src/scan.rs` + `py/src/py_scan.rs` | 已换壳；29 组扫描参数 × 选中项语义全部对拍一致 |
| `plan`（413 行） | `core/src/plan.rs` + `py/src/py_plan.rs` | 已换壳；18 种规则 × 编号作用域 × 父子改名全部对拍一致 |
| `context`（340 行） | `core/src/context.rs` + `py/src/py_context.rs` | 已换壳；变量表 / 格式化 / digest 全部对拍一致 |

配套新增：`core/src/pathops.rs`（Windows 路径语义，ntpath 的逐函数移植）、
`core/src/timefmt.rs`（`fmt_date` / `human_size` / 本地时间）、
`tools/gen_path_cases.py` + `crates/namecat-core/tests/path_semantics.rs`
（**417 组路径样本**，语料由真 Python 采集）、`engine/llm_resolve.py`
（LLM 阶段留在 Python，由 Rust 回调）。

**验收方式**：`_verify_tmp/p2_equiv.py` 把**换壳前的 Python 实现**（`git show HEAD:engine/...`
拉到 `_verify_tmp/ref/refengine/` 当参考实现）与新实现逐字段对比，
**1182 项断言 0 差异**。另跑了 CLI 全流程（preview / rename / undo / leftovers）、
适配层全链路（`scan_only → build_plan → set_excluded → export_csv → apply_plan`）、
跨线程（后台线程扫描 → 界面线程读属性）、预设往返。

**性能**（2000 个文件，同一台机器，5 轮取最好值）：

| 场景 | 旧（Python） | 新（Rust） |
|---|---|---|
| 扫描 | 29.2 ms | **23.0 ms**（−21%） |
| 算名 `compute_names` | 30.3 ms | **25.9 ms**（−15%） |
| `finalize`（含 Python 侧校验） | 42.0 ms | 66.6 ms（+59%） |

第三条是**预期成本**：`finalize` 里仍然调 `engine.common.validate_items`（P3 才迁），
它逐项读 `it.path/dirname/name/new_name/status/note` —— 每个属性都要过一次
PyO3 getter（含 String 复制），2000 项 × 6 次读写就是那 20 多毫秒。
**P3 把 common 迁完就会消失**，别为了这个数字现在去动 `Item` 的字段设计。

**P2 必须知道的实现要点**：

1. **pyclass 必须 `Sync`**：`Item` / `NameContext` 会跨线程流转（后台线程扫描、
   界面线程读），所以内部状态用 `std::sync::Mutex`，**不是 `RefCell`**
   （`RefCell` 编译不过；`#[pyclass(unsendable)]` 会在换线程时 panic）。
   加锁一律走 `py_context::lock`（忽略中毒）。
2. **`os.stat` 有三级快慢**：`GetFileInformationByName`（Win11 22H2+，7 µs）→
   `GetFileAttributesExW`（23 µs）→ `std::fs::metadata`（29 µs，会 CreateFileW）。
   2000 个文件里 stat 占九成时间，选错一级就是 3 倍差距。前者**必须动态解析**
   （`GetProcAddress`），静态导入会让进程在 Win10 上根本起不来；用之前先拿
   `current_exe()` 跟 `GetFileAttributesExW` 对一次（`self_check_ok`），
   结构体布局对不上就永久退回慢路径。
3. **排序键要先算好再比**：`sort_by(|a,b| natural_key(a.name).cmp(...))` 会变成
   O(n log n) 次分配。必须像 Python 的 `key=` 那样先 `Vec<NKey>` 再排
   （2000 项实测 45 ms → 6 ms）。
4. **`normcase` 必须用 `LCMapStringEx`**，不能用 `str::to_lowercase`：
   `İ`（U+0130）、`ǅ`（U+01C5）、希腊词尾 `Σ` 三处结论都不同，而这条路径要
   和 Python 侧的 `os.path.normcase` 逐字一致（否则"是不是同一个文件"会判错）。
5. **路径函数全在 `pathops`**：`dirname/basename/splitext/join/normpath/normcase/
   abspath/relpath/commonpath` 都是 ntpath 的移植，用
   `tests/path_semantics.rs` + `tests/data/path_cases.json` 钉住。
   语料里**不能放相对路径**（`abspath`/`relpath` 依赖 cwd，换台机器就飘）。
6. **`Item.st` 不做惰性 stat**：Python 版就是一句 `self.st = st`，手工
   `Item(path)` 出来的对象 `st` 是 `None`（`export_csv` 靠这个判空）。
   扫描出来的条目在扫描阶段就有元数据，getter 按需拼一个 `os.stat_result`。
7. **`Item` 必须是 `dict` 类**：适配层用 `it.__dict__["_excluded"]` 记勾选排除，
   换实现后这些临时字段照样能挂。
8. **`__dataclass_fields__` 要补**：`engine/presets.py` 用
   `if k in ScanOptions.__dataclass_fields__` 过滤字段，两个 Options 类不再是
   dataclass，得用 `shim::attach_field_names` 挂个同名 dict。
9. **`parentdir` / `filepath` 变量取不到值**：`NameContext.var()` 的快表把这两个
   映射到同名属性，而那个属性不存在 → Python 版抛 AttributeError → 被上层吞成
   空串。Rust 里返回 `None` 表示"这里会抛"，别"修正"成父目录。
10. **`with_meta(None)` 是"没查过"，不是"查过没有"**：`StState` 有三个状态，
    把 None 当成 Missing 会让 `NameContext(path)` 永远拿不到 stat。
11. **`fmt_date` 的两个边界**：时间戳换算成本地时间后年份必须在 1..=9999
    （UTC 253402300799 在 UTC+8 会溢出到 10000 年，Python 那边抛
    OverflowError 回落"现在"）；`(style or 默认值).strip()` 对纯空白风格返回空串。
12. **FILETIME 先减纪元再除**：`ft/1e7 - 11644473600` 与 `(ft-epoch)/1e7`
    差 1~2 个 ulp，Python 用的是后者。

### P3 已交付（2026-11-05，实测）

| 模块 | Rust 落点 | 状态 |
|---|---|---|
| `common`（756 行） | `core/src/{names,validate,rename,logs}.rs` + `py/src/py_common.rs` | 已换壳；名字校验 / 两遍校验 / 落盘回滚 / 日志 / 撤销全部对拍一致 |
| `ops`（145 行） | `core/src/ops.rs` + `py/src/py_ops.rs` | 已换壳；预览 / apply / 导出 CSV / scan_and_plan 对拍一致 |

配套：`validate::ValidateItem`/`FileProbe`（把 Python 条目与磁盘探测抽象掉，
算法本体可在 Rust 侧用假对象测）、`scan::path_exists`（快路径存在性探测）、
`logs` 用 `serde_json` 的 `preserve_order` 保持字段顺序。

**验收方式**：`_verify_tmp/p3_equiv.py` 对 8 个分区做逐字段对拍，
**287 项 0 差异**：名字校验（37 个刁钻名字 × 文件/文件夹）、两遍校验
（11 个场景 × 状态/说明/改后名）、落盘执行（6 种批次 + 回滚，比步骤与**结果树**）、
临时名残留（3 档深度）、日志（文件名字形 / 字段与**键序** / 脏 pairs）、
撤销（往返 / 只改大小写 / 部分失败写回）、ops 全链路（含 CSV 字节）、
apply 失败路径。另跑了 CLI 全流程与适配层全链路。

**性能**（2000 个文件，5 轮取最好）：扫描 49 ms（旧 57）；算名+校验 91 ms（旧 87，+5%）。
P2 遗留的 `finalize` +59% 已消除——校验不再绕回 Python。

**P3 必须知道的实现要点**：

1. **`std::fs::rename` 会在 Windows 上覆盖目标**（它带 `MOVEFILE_REPLACE_EXISTING`），
   而 `os.rename` 不会。这是数据安全级差异：本该整批回滚的场景，Rust 会把别人的
   文件悄悄冲掉。统一走 `rename::do_rename`（`MoveFileExW(flags = 0)`）。
2. **OSError 文案用 `%R` 渲染**：Python 的 `[WinError 3] ...: 'C:////a' -> 'C:////b'`
   里反斜杠是**翻倍**的。`describe_rename_error` 因此手写了 `py_repr`。
3. **存在性探测别用 `meta_of`**：它有三级回退，目标不存在时三级全试一遍（3 次系统调用），
   校验阶段是逐项调用的，2000 项就多几十毫秒。用 `scan::path_exists`（只走最快一级）。
4. **`normcase` 加 ASCII 快路径**：纯 ASCII 直接 `to_ascii_lowercase()`，不走
   `LCMapStringEx`。2000 项校验从 57 ms 降到 44 ms（旧实现 40 ms）。
5. **日志目录仍问 `engine.backup_dir()`**：环境变量覆盖 + 程序目录可写探测那套留在根包，
   Rust 侧只 `py.import("engine").backup_dir()` 取结果，不重复实现。
6. **日志 JSON 要保序**：`serde_json` 默认按字母序输出键，已开 `preserve_order`，
   否则 `_rewrite_log` 会把用户看惯的字段顺序打乱。
7. **`find_temp_leftovers` 的裁剪顺序**：`os.walk` 在深度到顶时先把 `dirnames` 清空
   **再**收集名字，所以那一层的 `__nk_tmp_*` **目录**不算残留、文件算。顺序写反结果就不同。
8. **`PlanOptions.dry_run` 默认是 True**（沿用原实现）：`apply()` 不显式关掉就只预览。
9. **只改大小写的撤销必然报"已存在，跳过"**：Windows 上 old 与 new 是同一个文件，
   `os.path.exists(old)` 恒真。这是既有行为，两边一致，不要"修好"。
10. **私有符号也要导出**：`_root_hash` / `_split_log_name` 被 `verify/test_edges.py`
    直接调用，壳里显式 import（同 P1 的 `_num_text`、P2 的 `_common_root`）。
11. **`validate_items` 直接在 Rust 里被 `plan.finalize` 调用**，不再经 Python 中转；
    条目用 `ItemAdapter`（PyItem 走字段快路径，其它对象走 `getattr` 鸭子类型）。

### P4 已交付（2026-11-05，实测）

| 模块 | Rust 落点 | 状态 |
|---|---|---|
| `secrets`（182 行） | `core/src/secrets.rs` + `py/src/py_secrets.rs` | 已换壳；DPAPI 加解密、`DPAPI1:`/`BASE64:` 双格式、空串不覆盖 |
| `provider`（726 行） | `core/src/{http,provider}.rs` + `py/src/{py_provider,py_net}.rs` | 已换壳；预设表 / 配置往返 / 模型列表 / 连通性三协议 |
| `llm`（838 行） | `core/src/llm.rs` + `py/src/py_llm.rs` | 已换壳；提示词、回复解析、离线替身、三种协议的请求与抽取 |
| `presets`（136 行） | `core/src/presets.rs` + `py/src/py_presets.rs` | 已换壳；读写 / 内置清单 / 脏 JSON 兜底 / `scan_options` |

配套：`core::http::{Transport, Response, http_reason}`（核心只决定"发什么、
怎么解释回什么"，字节出口下沉到绑定层）、`py_net::PyNet`（用 urllib 发请求，
保持代理/回环/证书语义）。

**验收方式**：`_verify_tmp/p4_equiv.py`（新实现 vs `_verify_tmp/ref/refengine/`
里换壳前那四份），**1701 项 0 差异**。分区：
① `sanitize_name` 30 组文本 × 6 风格 × 6 长度、`build_prompt` 参数组合、
`parse_suggestions` 24 种脏回复 × 3 个 count、`EchoClient.simplify` 23 个名字 ×
keep_ext、端点/请求体/请求头/回复抽取（三协议）；
② 配置：`from_env` 5 组环境变量、`problem()` 五分支、`make_client` 三类客户端、
不可用降级、Python 子类覆盖 `_chat`、批量分块、`progress` 回调、要求文件
（BOM / 注释 / 环境变量 / 缺失回落）；
③ HTTP：本地 mock 服务打三种协议（请求路径/请求体/请求头逐字比）、401 文案、
连接被拒文案、`_request` 状态码、opener 身份断言；
④ provider：预设表、`apply_preset` 9×3、`models_url_for` 7 组、`as_dict` 无密钥、
7 种脏 JSON、`env_config` 6 组环境、配置文件**逐字节**、`effective_*` 两种前
提、`STATUS` 状态机与采样窗口；
⑤ 密钥：空串不覆盖、**新旧实现互相能解**、磁盘非明文、未知前缀读空、BASE64 兼容；
⑥ 预设：4 个内置逐字段、脏 JSON、`scan_options`/`plan_options`（含 `**over`）、
文件逐字节（id 抹平后）、CRLF、坏 JSON 异常口径、目录 CRUD。
另跑了 CLI 全流程（`preview → rename --llm-provider echo → undo`）与适配层
所有 LLM 入口。

**P4 必须知道的实现要点**：

1. **HTTP 字节出口**留在宿主 Python（urllib），这是对设计稿 §3 唯一的偏离。
   理由写在 `core/src/http.rs` 模块头：系统代理（注册表 + 环境变量两套口径）、
   回环绕代理、TLS 证书这三件事换实现等于换行为，而 `test_edges.py` 的 H5 是拿
   **对象身份**断言的（`opener_for(url) is _LOOPBACK_OPENER`）。用 reqwest/ureq
   要重写一遍还得重新证明这三条。代价是每次请求跨一次 Python 边界（毫秒级）。
2. **`LLMClient` 必须能被 Python 继承并派发**：`run_tests.py` 里
   `class Boom(L.LLMClient)` 覆盖 `_chat`、`class Spy(L.EchoClient)` 覆盖 `plan`
   并 `super().plan(...)`。所以 `LLMClient`/`EchoClient`/`HttpChatClient`/
   `OllamaClient`/`_Unavailable` 用 `#[pyclass(extends=…, subclass)]` 串成一条继承链，
   且 `plan()` 一律 `call_method1("_chat", ...)` 动态派发，不直接调 Rust 函数。
3. **`plan_one` 里不许用 `?`**：Python 是 `except Exception` 把网络/鉴权错误折进
   `errors` 并让整批继续。用 `?` 会把异常抛给调用方，`plan()` 从此会炸
   （对拍里 `Boom` 一测就现形）。
4. **`ProviderStatus.model` 不能用 `#[pyo3(get, set)]`**：setter 生成的函数名正是
   `set_model`，与方法 `set_model()` 撞车（PyO3 报 `duplicate definitions`）。
   字段改名 `model_name`，再用 `#[getter(model)]` / `#[setter(model)]` 暴露属性。
5. **`secrets` 必须排在 `provider` 之前挂载**：provider 挂载时要把
   `namecat_py.secrets` 这个**模块对象**挂成属性，此刻它得已在 `sys.modules` 里；
   否则报 `No module named 'namecat_py.secrets'; 'namecat_py' is not a package`。
6. **换行风格两套，别统一**：`provider.save_config` 是 `newline="
"`（LF、末尾
   不补换行）；`presets.save` 用的是文本模式默认（Windows 上翻成 CRLF）。
   两边都按原样复刻，否则"新版本写的文件"和"旧版本写的"逐字节不同。
7. **DPAPI 跨实现对读**：`CryptProtectData` 的 `szDataDescr` 不参与派生，但两边
   都传 `"NameCat"`；密文每次不同（随机盐），所以对拍比的是"能解 + 非明文 + 前缀"，
   并且**双向**（新写旧读、旧写新读）都要过。
8. **Anthropic 抽取的 `or []`**：`(data.get("content") or [])` 在缺字段/null 时是
   空串而不是异常；只有"有值但不是列表"才 `回复格式异常`。照抄成空列表，否则
   `parse_suggestions` 的严格重试路径会少走一轮。
9. **两个端点函数的 strip 口径不同**：`llm.HttpChatClient._endpoint` 只
   `rstrip("/")`（不去首尾空白），`provider._chat_endpoint` 会 `strip()`。
   别顺手统一——对拍里带空格的 base_url 就是这个差异。
10. **`EchoClient.plan` 要按 `dict.fromkeys` 去重**：同名只算一次，否则
    `used` 会虚高（预览提示的"N 项"就不对了）。

### 2.0.1：删掉兼容层（2026-11-05）

`engine/<模块>.py` 那 12 个转发壳已全部删除 —— 它们是迁移期的脚手架，留着只会让人
以为"引擎有两套接口"。现在 `engine/` 只剩应用层三件：`__init__.py`（应用常量 /
appdata / 旧目录迁移）、`cli.py`（命令行）、`llm_resolve.py`（LLM 批量桥，被 Rust 的
plan 回调）。引擎接口就是 `namecat_py.<模块>`，没有第二层。

**如果还要动这块代码，记住三条：**

1. 新增/修改引擎函数 → 直接进 `crates/`，然后 `py -3 rust_build.py`；
   `verify/rust_check.py` 用迁移前固化的 193 个符号当基准，少一个就红。
2. Rust 侧**不要**再 `py.import("engine.<模块>")` —— 那些模块已经不存在。
   只有三个根包调用是合法的：`engine.appdata_dir`、`engine.backup_dir`、
   `engine.llm_resolve`（都定义在 `engine/__init__.py` 或 `llm_resolve.py`）。
3. 别把验证脚本的临时目录设在别人也用的地方：`run_tests.py` 曾经把整个
   `_verify_tmp/` 当沙箱并在结束时删掉，顺手删了放在那里的对拍脚本。

### P5 已交付（2026-11-05，实测）

| 改动 | 文件 | 内容 |
|---|---|---|
| 打包带上引擎 | `build_exe.py` | 三道前置闸（扩展存在且唯一 / ABI 匹配 / 不比源码旧）+ 显式加二进制 + 打包后自检 |
| exe 瘦身 | `build_exe.py` | 改由脚本生成 `.spec` 过滤 `a.binaries`：41.2 → **29.8 MB**（`--keep-opengl` 可保留软件 OpenGL） |
| 源码运行提示 | `main.py` | `_engine_hint()`：缺扩展时给一句人话（跑 `rust_build.py`），而不是抛 `No module named 'namecat_py'` |
| 性能基准 | `verify/bench.py` | 当前引擎各阶段耗时，7 轮取中位数，附 1.6.3 参考值 |
| 数据与口径 | `docs/TEST_REPORT.md` 第 8 节 / `README.md` | 性能表、打包链路、"源码运行要先构建引擎" |

**验收命令（按顺序，都不慢）**：

```bat
py -3 rust_build.py                          :: 构建 + 装 .pyd
python verify/rust_check.py --fail-on-any    :: 符号闸门 12/12
PYTHONPATH=_vendor python verify/run_tests.py       :: 410 断言（引擎 + 端到端）
PYTHONPATH=_vendor python verify/test_edges.py      :: 190 断言（边界与异常路径）
py -3 build_exe.py --zip                     :: 打包 + 自检 + 出 zip
PYTHONPATH=_vendor python verify/bench.py           :: 性能表
```

**P5 必须知道的坑**：

1. **`--paths _vendor` 不能把 Rust 扩展带进包**。PyInstaller 静态分析看到的是
   `from namecat_py.rules import *`，而 `namecat_py` 是扩展模块、不是包，
   于是 12 个子导入全被判成 "missing module"，**父模块一并不收**。
   实测：不加 `--add-binary` 时所有 TOC 里 `namecat_py` 命中数为 0，
   打出来的 exe 启动即崩。现在显式 `--add-binary <pyd>:.`。
2. **窗口程序没有控制台，包装失败只表现为"双击没反应"**。所以打包后自检
   不看 stdout，只看**文件效果**（沙箱里 `rename → undo`）。同理，
   `NameCat.exe rules` 这类命令在双击/无控制台的场景下什么都看不到，
   从 cmd 里跑是正常的（stdout 会被继承）。
3. **单文件 exe 要往 `%TEMP%` 解包自身**。系统 Temp 不可写时弹
   `Could not create temporary directory!` 并卡住 —— 自检第一次跑就因此
   60 秒超时（sandbox 环境）。现在自检把 `TEMP`/`TMP` 也指进沙箱。
4. **`.pyd` 的 ABI 必须和打包用的解释器一致**（现在是 `cp315` / Python 3.15）。
   不一致时编译期不报错、运行期 ImportError。`build_exe.py` 已拦。
5. **PyInstaller 没有"排除某个二进制"的命令行参数**，只有 `--exclude-module`
   （管 Python 模块）。而体积大头是二进制：`opengl32sw.dll` 压缩后 7.5 MB、
   QtNetwork 一套 1.4 MB。所以 `build_exe.py` 改成每次生成 `NameCat.spec`，
   在 spec 里按 `DROP_BINARIES` 过滤 `a.binaries`。清单在 `build_exe.py` 里，
   **别去改 `NameCat.spec`** —— 那是构建产物，每次打包都会覆盖。
   删完必须验窗口真能渲染（`opengl32sw` 是软件 OpenGL 兜底）：用
   `user32.FindWindowW` + `GetWindowRect` 读几何，别只看进程活着。
6. **校验段不是 CPU 密集，别指望它变快**：2000 项里每个目标名都要
   `path_exists` 探一次盘，加上 `LCMapStringEx` 规范化（3.6 µs/次）。
   已优化掉"源路径 nkey 重复计算"（98 → 83 ms），剩下与 Python 版持平。
   想再快得改探测策略（按目录缓存目录项），属于换行为，需要重新对拍。

## 3. 环境（2026-11-04 全部装好并验收 ✅）

| 组件 | 状态 | 备注 |
|---|---|---|
| Rust | 1.99.1 `stable-x86_64-pc-windows-msvc` | `~/.cargo/bin`。**新开 shell 才进 PATH**；本工具的 Bash 每次要 `export PATH="/c/Users/LocalAcc/.cargo/bin:$PATH"` |
| MSVC | Build Tools 2022，MSVC **14.44.35207** | cl.exe 已落盘，vswhere 注册正常。装时补了 VC 工作负载 |
| maturin | 1.16.0（pip 装在系统 Python） | **用 `py -3 -m maturin` 调用**（maturin.exe 不在 PATH）；`rust_build.py` 已封装 |
| Python | 3.15.1 64 位（唯一解释器，`py -3`） | PyO3 产物按 cp315 编；`--interpreter` 已钉死（见 §4） |
| 代理 | Clash 端口**现在是 7897**（7900 已停） | git `http.proxy` 已改 7897；`~/.cargo/config.toml` 里的 7900 已注释掉（直连 crates.io 通） |

**已做验收**（临时工程在 `%TEMP%/namecat_hello/`，可删）：
① `cargo new hello && cargo build` → 编译链接运行 ✓
② `maturin new -b pyo3 smoke` + `maturin develop` → wheel cp315-win_amd64
  编译 + `import smoke` ✓

## 4. 血泪坑（务必绕开）

**本次环境类（新）**：
- **cargo 会读 git config 的 `http.proxy`**，git 里曾挂着过时的 Clash 端口
  7897 → crates.io 永远连不上（curl 却正常，极具迷惑性）。已修：git proxy
  改 **127.0.0.1:7900**（系统代理，Clash），并写死 `~/.cargo/config.toml`
  的 `[http] proxy = "http://127.0.0.1:7900"`。不开 Clash 时把该行注释掉。
  WorkBuddy 沙箱内链路代理是 `127.0.0.1:1805`，若 7900 被拦：
  `CARGO_HTTP_PROXY=http://127.0.0.1:1805` 覆盖。
  **2026-11-05 更新**：Clash 端口又变回 **7897**（7900 已无监听），且直连
  crates.io 可用 → `~/.cargo/config.toml` 的 proxy 行已注释、git `http.proxy`
  已改 7897。**报错信息里的 "over proxy 127.0.0.1" 指的就是这一步配的代理，
  别以为在走 127.0.0.1 本机服务。**
- **maturin 必须钉死解释器**：不带 `--interpreter` 时它自己去 PATH 找 python，
  本机 PATH 上挂着 WorkBuddy 自带的 3.13 → 编出 cp313 的 `.pyd`，主程序（3.15）
  import 报 "No module named 'namecat_py'"。`rust_build.py` 已传
  `--interpreter sys.executable`，手敲命令时别忘。
- winget 装 VS Build Tools **不带任何工作负载**；`winget install` 对已装包
  只走升级检查会无视 `--override`，要加 `--force`。VC 组件验落盘：
  `.../VC/Tools/MSVC/*/bin/Hostx64/x64/cl.exe`。
- pip 装的工具不在 PATH：一律 `py -3 -m <pkg>`。

**项目历史类（来自长期记忆，别踩回去）**：
- **QSS 里绝不能写 `#` 行注释**（吞掉其后全部规则）；只能 `/* */`。
- 改 CRLF 文件：python 脚本先 `replace("\r\n","\n")` 再写回，否则静默失败。
- 别反复 `deleteLater` + 重建行（离屏截图旧控件残留叠字）；行建一次只改文字。
- 自适应列要设 `QSizePolicy.Ignored`，否则长文件名撑爆列宽。
- 扫描跳过日志目录用小写比对表 `scan.LOG_DIR_SKIP`（大小写陷阱）。
- `ops.Result` 有显式 `success` 字段，撤销成败不能靠 steps 推断。
- `rules_from_json`/`Preset.from_dict` 对脏 JSON 一律兜底（能救的照常读回）。
- 设计内行为勿"修正"：`--step 0` 静默回落 1；模板未知变量渲染成空串。

## 5. 硬约束（验收线）

1. `verify/run_tests.py` + `test_edges.py` 共 600 断言全绿 = 行为对齐完成。
   Rust 侧额外加 cargo test 表驱动测试钉文本语义（title/case/替换解析）
   与路径语义（ntpath）。换壳前后还必须过 `verify/rust_check.py`（符号 + 根常量）。
   **P2 起新增口径**：每个阶段的换壳都要有一份"新旧逐字段对拍"脚本
   （P1: `_verify_tmp/rules_equiv.py`；P2: `_verify_tmp/p2_equiv.py`；
   P3: `_verify_tmp/p3_equiv.py`，三者都是先 `git show HEAD:engine/<m>.py`
   拉一份参考实现再比）。参考实现里的 `refengine/__init__.py` 要**用当前工作区
   的那份**，否则版本号/日志 schema 会对不上（P3 踩过）。
2. 磁盘格式兼容：撤销日志（LOG_SCHEMA=5 / UNDO_SCHEMA=3）、预设、
   `llm_credential.dat`（DPAPI 密文）、`llm_instruction.txt`，旧数据照读。
3. 语义对齐"现有行为"而非正确性——Python `str.title()` 的怪癖也要复现。
4. 非法正则 → 原样返回不抛异常（RULE_NOTE 明文承诺）。

## 6. 用户偏好

- **不要主动跑测试套件**（"除非我要求"）；写完代码给关键路径验证即可。
- 迭代节奏快，多给截图/对照；颜色克制：蓝只留给交互，大面积用中性灰。
- 中文交流；结论先行，给数据支撑。

## 7. 文件索引

| 文件 | 内容 |
|---|---|
| `Cargo.toml` | workspace 根：成员、版本（唯一版本源之一，需与 APP_VER 一致）、release 优化档 |
| `crates/namecat-core/` | 纯 Rust 核心：`consts` / `error` / `ctx`（VarSource）/ `text` / `timefmt` / `pathops` / `template` / `rules` / `zhconv`（+生成的 `zhconv_data.rs`）/ `scan` / `context` / `plan` / `names` / `validate` / `rename` / `logs` / `ops` / `http`（Transport 抽象）/ `llm` / `provider` / `secrets`（DPAPI）/ `presets` |
| `crates/namecat-py/` | PyO3 绑定：`lib.rs`（子模块表 + 根常量）、`shim`（异常/panic/子模块/`__all__`/`__dataclass_fields__`/`appdata_dir`）、`pyctx`、`pyjson`、`py_template`、`py_rules`、`py_zhconv`、`py_scan`、`py_context`、`py_plan`、`py_common`、`py_ops`、`py_net`（urllib 出口 + opener 缓存）、`py_llm`、`py_provider`、`py_secrets`、`py_presets` |
| `crates/namecat-core/tests/` | `path_semantics.rs` + `data/path_cases.json`（417 组路径样本，与 ntpath 逐字对拍） |
| `rust_build.py` | 开发构建：版本校验 → maturin build → 装 `_vendor/` → import 冒烟 |
| `tools/gen_zhconv.py` | 简繁表生成/校验（数据源须传换壳前的 zhconv.py） |
| `tools/gen_path_cases.py` | 路径语义语料采集（`--check` 校验现有 JSON 是否过期） |
| `engine/llm_resolve.py` | LLM 规则的批量解析桥（`plan` 出现 llm 规则时回调它；留在 Python 是为了不改 `plan` 的调用约定） |
| `verify/bench.py` | 性能基准：当前引擎各阶段耗时 + 1.6.3 参考值 |
| `verify/run_tests.py` / `test_edges.py` / `ui_smoke.py` | 三套自检（410 / 190 断言 + 界面全流程） |
| `_verify_tmp/ref/refengine/` | 换壳前的参考实现（rules/template/zhconv/scan/plan/context/common/ops/llm/provider/secrets/presets） |
| `verify/rust_check.py` | 符号闸门：按 `engine_symbols.json` 逐模块对比 + 根常量取值 + 子模块可导入 |
| `verify/engine_symbols.json` | 换壳前固化的期望符号集（12 模块 / 193 个符号），**只读** |
| `docs/DESIGN_RUST_ENGINE.md` | Rust 重写设计（权威，含 API 草案/路线图/token 估算） |
| `docs/TEST_REPORT.md` | 测试与缺陷记录（1.6.2 修复在 0.1 节） |
| `docs/GUI_DESIGN_GUIDE.md` | UI 设计规范 |
| `engine/__init__.py` | 常量/路径助手/状态码；APP_VER 唯一来源 |
| `adapters/engine_adapter.py` | 引擎符号消费方（绑定 API 的需求清单） |
| `verify/run_tests.py` / `test_edges.py` | 行为规格书（410+190 断言） |

工作区记忆：`C:\Users\LocalAcc\WorkBuddy\NameCAT\.workbuddy\memory\`
（MEMORY.md = 长期备忘；2026-11-04.md = 今日详细记录）。

— 完。**P0–P5 全部完成**：引擎 12/12 模块 Rust 化，打包链路带上扩展并带自检，
性能与验收数据落在 `docs/TEST_REPORT.md` 第 8 节。

还剩的可选项（都不影响发布，做之前先问一句要不要）：
- **原生 CLI（clap）**：可直接复用 `namecat-core`，省掉 Python 启动开销
  （现在冷启动约 0.3 s）。优先级低 —— 命令行本来就是脚本化用，慢那点无所谓。
- **`.pyd` 多版本 wheel**：现在只出 `cp315`，源码运行要求 Python 3.15。
  真要支持 3.10–3.14，就得为每个版本出 wheel 并加 CI 矩阵。
- **macOS / Linux**：`core` 里 Windows 特化部分（`MoveFileExW` / `LCMapStringEx` /
  DPAPI / `GetFileInformationByName`）都有 `#[cfg]` 分支的余地，但没验过。

工作区已在 2026-11-05 清过一轮：`target/`（2.6 GB）、`build/`、`__pycache__` 全删，
`dist/` 里的发布包与 `_verify_tmp/` 的对拍脚本保留。改 Rust 代码后第一次
`rust_build.py` 是全量编译（几分钟），之后增量。

**如果还要动这块代码**，先跑这三条（都很快，且互不替代）：
`python rust_build.py`（构建+装 .pyd）→ `python verify/rust_check.py --fail-on-any`
（符号闸门，12/12）→ `py -3 verify/run_tests.py`（410 断言）。
迁移期的对拍脚本（`p{2,3,4}_equiv.py`）与参考实现已在 2.0.1 清理，需要复核时从
`v1.6.3` 标签取回 Python 实现重跑。
**注意：任何"顺手修正"都可能踩到上面那 10 条"照抄"的实现要点，改之前先看对拍。**
