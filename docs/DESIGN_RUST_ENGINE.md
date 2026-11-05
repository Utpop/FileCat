# NameCat 引擎 Rust 化设计方案

> 状态：设计稿 v1（2026-11-04）
> 结论先行：可行。引擎 C++ 化的全部有利条件（纯逻辑、零 UI 依赖、边界现成、
> 测试可复用）对 Rust 同样成立；Rust 在内存安全与错误处理上更省心，
> 在正则语义对齐上多一个选型坑（见 §5.2）。预估纯产出 **15-25 万 tokens**，
> 全项目实际总消耗 **50-100 万 tokens**，与 C++ 方案同量级。

---

## 1. 背景与决策

### 1.1 目标

- 将 `engine/`（纯逻辑层）以 Rust 重写，编译为 Python 扩展（`.pyd`），
  UI（PySide6）与适配层继续用 Python，整体仍是一个可双击运行的程序。
- 收益：大目录扫描 / 批量 hash / 万级文件正则管道提速；无 GC 停顿、
  无 UB、借用检查器在编译期兜住内存错误；引擎核心 crate 可独立复用。
- 硬约束：**行为与现实现 100% 对齐**（`verify/run_tests.py` 410 断言 +
  `verify/test_edges.py` 190 断言全绿是验收线）；撤销日志（LOG_SCHEMA=5）/
  预设 / 配置的磁盘 JSON 格式保持兼容，旧数据照常可读。

### 1.2 非目标

- 不去掉 Python 运行时（UI 仍是 PySide6）。若最终目标是"独立 exe 无 Python"，
  应走 Rust + egui/Tauri 或 Qt 全量重写，不在本方案范围。
- 不重写 UI、不改交互、不动 QSS。
- 不做 async 运行时（tokio）——引擎的网络调用就一处（LLM），阻塞式足够。

### 1.3 为什么这个项目切得动

- `engine/` 是纯标准库逻辑，**零 PySide6 依赖**（仅 json/re/urllib/ctypes/
  hashlib/zlib/unicodedata 等），Rust 生态每样都有直接对应。
- 分层现成：UI → `adapters/engine_adapter.py` → engine，边界清晰。
- 自检体系现成：410+190 条断言可直接充当行为规格书与回归闸门。

---

## 2. 总体架构

### 2.1 目标形态：双 crate 工作区 + 进程内绑定

```
┌──────────────────────────────────────────────┐
│  ui/ (PySide6, 不动)                          │
│  workers/ (不动)                              │
├──────────────────────────────────────────────┤
│  adapters/engine_adapter.py (基本不动)         │
├──────────────────────────────────────────────┤
│  engine/  ← Python 壳（strangler 过渡层）      │
│    rules.py  → from namecat_py.rules import * │
│    plan.py   → from namecat_py.plan  import * │
│    ...                                        │
├──────────────────────────────────────────────┤
│  namecat_py.cp313-win_amd64.pyd  (PyO3 shim)  │
├──────────────────────────────────────────────┤
│  namecat-core  (纯 Rust crate，无 Python 依赖) │
│    rules / plan / scan / ops / fsops / text   │
│    template / zhconv / presets / provider     │
│    llm / secrets / context                    │
└──────────────────────────────────────────────┘

cargo workspace:
  crates/namecat-core   # 纯逻辑，cargo test 可独立跑
  crates/namecat-py     # PyO3 绑定，只做类型转换 + 异常映射
```

**关键点：核心逻辑与绑定分家。** `namecat-core` 不依赖 PyO3，
单元测试用 `cargo test` 原生跑（不经过 Python），只有 `namecat-py` 碰边界。
这比 C++/pybind11 方案多一层结构，但换来：借用检查覆盖全部核心路径、
核心 crate 可给其他宿主（CLI、服务端）复用、编译增量更小。

### 2.2 接入方式决策

| 方案 | 结论 | 理由 |
|---|---|---|
| **PyO3 + maturin（选定）** | ✅ | Rust→Python 事实标准；`#[pyclass]/#[pyfunction]` 直接复刻现有签名；Python 3.13 支持成熟（PyO3 ≥ 0.22） |
| C ABI cdylib + ctypes | ✗ | 手工封送字符串/结构体，胶水量 ≈ PyO3 方案的 3 倍 |
| PyO3 + abi3（稳定 ABI） | 备选 | 想一个 `.pyd` 兼容多 Python 小版本时启用；默认按 cp313 精确编译即可 |
| 常驻子进程 + JSON | ✗ | UI 每次预览有 IPC 开销；无崩溃隔离刚需 |

### 2.3 迁移策略：strangler（绞杀式）

`engine/` 包名与模块名**不变**，每个 `.py` 逐步退化为一行 re-export
（**实施结果**：这层转发壳只作为迁移期的过渡，2.0.1 已全部删除，调用方直接
`from namecat_py.<模块> import …`）：

```python
# engine/rules.py（过渡终态）
from namecat_py.rules import *   # noqa: F401,F403
```

- 任何时刻程序都可运行：未替换的模块继续走 Python 实现。
- 适配层 / UI / CLI **零改动**（`from engine import rules as R` 照旧）。
- 每替换一个模块就跑一次全量自检，行为漂移当场定位。
- `namecat-py` 的子模块名刻意与 Python 模块名一一对应（rules/plan/scan/…），
  `dir()` 对比脚本保证公开符号无缺漏（自检项之一）。

---

## 3. 模块映射表

| Python 现状 | 行数 | Rust 落点（namecat-core） | 依赖替代 | 难点 |
|---|---|---|---|---|
| `rules.py` | 899 | `rules.rs` | re → **fancy-regex**；hashlib → md5/sha1/sha256/crc32fast | case/title 语义、替换串解析（`\1`/`{name}` 自定义语法） |
| `common.py` | 756 | `fsops.rs` | os/shutil → std::fs + std::path（OsString） | 两阶段改名+临时名回滚事务、日志文件名 8 位目录指纹 |
| `llm.py` | 838 | `llm.rs` | urllib → **仍用宿主 urllib**（实施修正，见下） | `parse_suggestions` 的脏 JSON 容错、分块批量 |
| `provider.py` | 726 | `provider.rs` | json → serde_json；urllib → **仍用宿主 urllib**（实施修正） | 三协议请求构造、`effective_config` 合并优先级、环境变量兜底 |
| `template.py` | 456 | `template.rs` | re + datetime → fancy-regex + chrono | 选择器语法 `{name:1,3}`、未知变量渲染成空串 |
| `scan.py` | 415 | `scan.rs` | os.walk → walkdir 或手写递归 | 隐藏属性（Windows API）、magic 头探测、LOG_DIR_SKIP 小写比对表 |
| `plan.py` | 413 | `plan.rs` | 纯逻辑 | 编号 scope 发号、`with_number_rule` 覆盖语义 |
| `context.py` | 340 | `context.rs` | 纯逻辑 | NameContext 字段对齐 |
| `zhconv.py` | 303 | `zhconv.rs` + 数据 | 映射表 → `phf` 静态哈希表或编译期 include | 表数据原样搬运（机械劳动） |
| `secrets.py` | 182 | `secrets.rs` | ctypes DPAPI → **windows** crate（CryptProtectData） | DATA_BLOB 生命周期 → Drop 守卫 |
| `__init__.py` | 174 | 两分 | 路径/常量助手可留 Python；状态码进 core | `backup_dir()` 写权限探测逻辑逐行对齐 |
| `ops.py` | 145 | `ops.rs` | 组合层 | `Result.success` 显式字段（v1.5.0 修过的坑，勿退化） |
| `presets.py` | 136 | `presets.rs` | serde_json | 脏 JSON 兜底（extras 非 dict、plan 是数字等） |
| `cli.py` | 510 | **留 Python**（推荐） | argparse 现成 | 另提供可选原生 CLI（clap），复用 core |

**实施修正（2026-11-05，P4）**：`llm/provider` 的**字节出口**没有换成
reqwest，而是把传输抽成 `core::http::Transport`，由绑定层 `py_net` 仍调 urllib。
原因：系统代理（注册表 + 环境变量两套口径）、回环绕过代理、TLS 证书这三件事
换实现就等于换行为，而 `verify/test_edges.py` 的 H5 是拿 opener **对象身份**
断言的。决策逻辑（端点、请求体、请求头、回复抽取、错误文案）全部在 Rust，
只有"发字节"留在宿主。详见 `docs/HANDOFF.md` P4 小节。

范围裁剪选项（省 token）：`llm/provider/secrets`（≈1,740 行）无性能诉求，
可整体留 Python，Rust 只做规则/扫描/计划/执行/撤销（≈3,700 行 Python →
约 5-6k 行 Rust）。**默认按全引擎 Rust 化设计**，裁剪只是预算开关。

---

## 4. 接口设计

### 4.1 原则

- **签名 1:1 复刻现有函数名与参数**（`make_rule`、`apply_chain`、`build`、
  `scan`、`apply`、`undo`……），适配层只换 import 来源。
- `namecat-py` 子模块镜像现有结构：`namecat_py.rules` / `namecat_py.plan` / …
- 字符串边界统一 `String`（UTF-8）↔ Python `str`；文件系统路径内部用
  `OsString`/`PathBuf`，只在规则计算的文本面上转成 `String`（见 §5.3）。

### 4.2 类型映射

| Python | namecat-core | PyO3 边界 | 说明 |
|---|---|---|---|
| `str` | `String`（文本）/ `OsString`（路径） | 自动 | 路径绝不走有损 UTF-8 转换 |
| `list[Tuple[str,str]]` | `Vec<(String,String)>` | 自动 | 改名对、日志 pairs |
| `dict` | `serde_json::Value` | pythonize 或自定义 caster | JSON schema 兼容的关键 |
| `Rule`（dataclass） | `pub struct Rule`（值语义 + serde） | `#[pyclass]` | 字段 `kind/a/b/c/id/extras` |
| `Plan` / `ScanResult` | `struct` + `Vec<Item>` | `#[pyclass]` | `pairs()/changing()/by_status()` 方法照搬 |
| `Callable`（progress） | `Py<PyAny>` 回调 | 调用时 `Python::with_gil` | 进度回调（执行/撤销） |
| `LLMClient` 等可变对象 | `Arc<...>` 内核 | `#[pyclass]` 包一层 | 允许 Python 持有跨调用状态 |

### 4.3 错误模型

- core 内部：`#[derive(thiserror::Error)] pub enum Error { ... }` +
  `Result<T, Error>`（全面用 `?`，不 panic）。
- 跨边界：`impl From<Error> for PyErr`，统一映射为 `EngineError`
  （PyO3 里注册的 `Exception` 子类），message 与现实现异常文案对齐。
- 能救的错误照常救（脏 JSON → 空字典/转文本兜底、非法正则 → 原样返回），
  这些是 `test_edges.py` 里有断言的既有语义。
- 任何 `panic` 都不得越过边界：`catch_unwind` 包绑定层入口，转成
  `EngineError`，CI 加一条"异常路径"自检。

### 4.4 公开符号面（适配层实际用到的，按组）

```
root    : APP_NAME, APP_VER, STATUS_TEXT, appdata_dir, backup_dir,
          ST_* , LOG_SCHEMA, UNDO_SCHEMA, 目录/环境变量常量
rules   : Rule, make_rule, normalize, describe_chain, rules_to_json,
          rules_from_json, parse_cli_rule, number_rule, number_options,
          RULE_KIND_IDS/TEXT/NOTE/PARAM_HINT, MAX_RANDOM
scan    : ScanOptions, ScanResult, scan, sort_items, natural_key,
          KIND_*, IMAGE_EXTS
plan    : Plan, PlanOptions, build, describe_plan, SCOPES/SCOPE_*
ops     : Result, preview, apply, export_csv
common  : validate_items, execute, undo, list_logs, load_log, log_pairs,
          log_dir, find_temp_leftovers, RenameStep
template: render, REF_KEYS, SELECTOR_KINDS/TEXT, MAX_WIDTH
presets : Preset, BUILTIN, builtin_list, list_presets, load, save, delete, preset_dir
provider: ProviderConfig, PROVIDER_PRESETS, PROTOCOLS, PROTOCOL_TEXT, STATUS,
          apply_preset, effective_config, effective_provider_config,
          save_config, config_path, test_connection, list_models,
          models_url_for, secrets
llm     : LLMConfig, LLMClient, make_client, SuggestResult, STYLES,
          read_instruction, write_instruction, ensure_instruction_file,
          instruction_path, DEFAULT_INSTRUCTION, INSTRUCTION_ENV
context : NameContext
```

绑定层完成后用脚本对比 `dir()` 输出，保证无缺漏。

---

## 5. 关键技术决策

### 5.1 语言与工具链

- Rust stable（MSVC 工具链，与 PySide6/Qt 的 Windows 目标一致）。
- `cargo workspace` 双 crate（§2.1）；构建入口 `maturin build --release`。
- 依赖锁定：`Cargo.lock` 入库，保证可复现构建。
- Python 3.13 + PyO3 ≥ 0.22（支持 3.13 的 free-threading 前的常规 GIL 模式即可）。
  **实施修正（2026-11-05）**：本机唯一解释器是 **Python 3.15.1**，实际用
  **PyO3 0.29.3**（0.29.x 才正式支持 3.15），产物 `namecat_py.cp315-win_amd64.pyd`。
  maturin 必须 `--interpreter` 钉死解释器，否则会按 PATH 上另一份 3.13 编译。

### 5.2 正则引擎选型（本方案最大的语义坑）

| 引擎 | 回溯/回引用/环视 | 与 Python `re` 语义距离 | 结论 |
|---|---|---|---|
| `regex` crate | ❌ 线性时间，无回引用、无环视 | 远——用户写的 `\1`、`(?=...)` 会直接报错 | 不选 |
| **`fancy-regex`** | ✅ 回溯式，支持回引用与环视 | 近——按 PCRE 语义，最接近 Python `re` | **选定** |

- 改名工具的正则模式来自用户输入，**不能悄悄改变语法能力**；
  `fancy-regex` 慢一点无所谓（输入是文件名字符串，不是大文本）。
- 替换串不是 `re.sub` 直通：`rules.py:regex` 自己解析 `\1` 与 `{name}`
  混合引用，这段解析器**逐行照抄**，不许"顺手改设计"。
- 非法正则 → 原样返回不抛异常（`RULE_NOTE` 明文承诺），`fancy-regex`
  的 `Regex::new` Err 要映射成这个行为。
- 对齐用例清单（进 `test_edges.py` 扩展）：`\1` 组引用、`{name}` 模板引用、
  混合引用、大小写不敏感的 Unicode 折叠、空匹配、嵌套组、非法模式。

### 5.3 文本与 Unicode 语义（高风险项）

- **对齐对象是现有行为，不是"正确性"**：
  - `case: title/sentence/capitalize`：Python `str.title()` 把 `don't` 变
    `Don'T` 的细节也要复现（自研 title_case，不用 `unicode-title` 凑）。
  - `upper/lower`：Python 全 Unicode 映射（ß→SS 这类），Rust
    `str::to_uppercase/to_lowercase` 同为全 Unicode，差异用对齐用例钉住。
- `translit`：西里尔/希腊/带音标拉丁 → ASCII 映射表原样搬运为静态表。
- `zhconv`：逐字映射表原样搬运（phf 静态哈希）；表外字符原样保留。
- `slug / clean_filename / pad_numbers / filename_clean`：纯算法照抄，
  注意 Windows 保留名（CON/PRN…）与尾随点/空格规则逐条对齐。
- **路径与文本分界**：scan/ops 全程 `OsString`/`PathBuf`（不丢非 UTF-8
  文件名）；规则计算面（名字主干/扩展名拆分后）才转 `String`。
  这是 Rust 比 Python 更严谨的地方，但要小心"Python 版遇到非 UTF-8 名字
  的实际行为"——现实现依赖 Python str（本就是 Unicode），非 UTF-8 名在
  Windows 上罕见，按现状对齐即可。
- `str` 边界宽度语义：Python len() 按码点，补零/截取/`move_chars` 都按
  码点计数——Rust 用 `chars()` 计数，不用 `len()`（字节）或字形簇。

### 5.4 内存、并发与"C++ 特性"对应物

原评估关注的模板/智能指针/RAII，Rust 的对应与用法：

| C++ 概念 | Rust 对应 | 本项目用法 |
|---|---|---|
| 模板 | 泛型 + trait（单态化） | 克制：`Result<T>`、`Option<T>`、少量容器工具；**不做宏元编程** |
| `std::variant` | `enum` + `match` | 28 种规则的参数/分发就用普通 enum，穷举 match 顺带编译期查漏 |
| `std::expected` | `Result<T, Error>` | 全面使用，替代异常路径 |
| 智能指针 | `Box` / `Rc` / `Arc` | `Box` 拥有权转移（LLMClient 等）；`Arc<Mutex<ProviderStatus>>` 跨线程共享状态（LLM 状态气泡数据） |
| RAII | `Drop` | DPAPI `DATA_BLOB` 释放、撤销日志临时写入、改名回滚守卫（`TempGuard`）、进度回调锁 |
| 虚函数 | `dyn Trait` | `LLMClient` 协议抽象（三种协议一个接口），仅此一处 |
| 手动 new/delete | 所有权 + 借用 | 无 GC、无泄漏；这是选 Rust 的核心理由之一 |

线程模型：Python 侧 `workers/` 已把网络调用放后台线程；Rust 侧
`reqwest::blocking` 直接在该线程跑，绑定层用 `py.allow_threads(|| ...)`
释放 GIL，扫描/hash/正则管道全程不占 GIL——顺带解决 Python 版
大目录扫描卡 UI 的问题。跨线程状态只有一处（`ProviderStatus` 耗时均值），
`Arc<Mutex<..>>` 足够，不引入 channels/async。

### 5.5 外围依赖决策

- **HTTP**：`reqwest::blocking`（自带 TLS）；不开 tokio——引擎只有
  测试连接 / 拉模型列表 / LLM 对话三处网络调用，全在后台线程。
- **JSON**：serde + serde_json；`#[serde(deny_unknown_fields)]` **不用**，
  读取侧必须容忍多余字段（前向兼容），写出侧字段名与现实现逐个一致
  （undo log schema 5、预设格式、llm_credential.dat 除外——那是 DPAPI 密文）。
- **DPAPI**：`windows` crate（官方绑定）`CryptProtectData/CryptUnprotectData`，
  `Drop` 包 `LocalFree`；失败回落路径（弱加密兜底）按现状保留。
- **hash**：`md5` + `sha1` + `sha2` + `crc32fast`（`hash` 规则与文件校验）。
- **chrono**：模板里的 `{date}` 与日志时间戳。
- 不引入：tokio、rayon（并行收益小，先串行对齐行为）、anyhow（core 用
  thiserror 显式枚举，绑定层才用得到动态错误）。

---

## 6. 测试策略

三层，成本从低到高：

1. **cargo test（新增，白盒）**：core 内每个模块的单元测试，尤其文本语义
   （title/case/translit/zhconv/替换解析）用表驱动测试钉行为。不经过 Python，
   迭代最快。
2. **现有 Python 自检（回归主力，白盒）**：`verify/run_tests.py` /
   `test_edges.py` 的 410+190 断言**只改 import 来源**（`engine.rules` →
   `engine.rules`（壳）→ namecat_py），逻辑零改动。这是行为对齐的最终裁判。
   `NAMECAT_LOG_DIR` 沙箱机制照常工作（环境变量路径在 core 里读）。
3. **CLI 黑盒（兜底）**：`cli.py` 留 Python 调绑定；另编可选原生 CLI
   （clap），用同一套 Python 测试 subprocess 驱动，覆盖"无 Python 场景"
   （给未来独立发行留口子）。

对齐专项清单（每项独立用例组）：regex 引擎差异（§5.2）、title/case 语义
（§5.3）、脏 JSON 兜底、`--step 0` 静默回落为 1、模板未知变量渲染成空串、
`Result.success` 显式判定（steps 空 ≠ 成功）、日志新旧格式段数识别。

---

## 7. 构建与打包

- 开发循环：`maturin develop`（装进当前 venv）→ 跑自检。
- 发布构建：`maturin build --release` 产出 `namecat_py-*.cp313-win_amd64.pyd`。
- `build_exe.py` / `NameCat.spec` 调整：
  - `.pyd` 放入 `_vendor/`（与 PySide6 同级），PyInstaller `hiddenimports` +
    `binaries` 声明；
  - MSVC 运行库（vcruntime140.dll 等）按 PyInstaller 惯例带上；
  - 依赖 `Qt6Core.dll` 不新增（PySide6 自带；core 用不到 Qt，这是纯 Rust）。
- CI 闸门：build → `cargo test` → `python verify/run_tests.py` → 打包冒烟。
- 回退方案：strangler 壳允许任何时刻把某模块的 re-export 改回 Python 实现
  （git revert 单个文件），不必整包回滚。

---

## 8. 迁移路线图

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 脚手架 | cargo workspace + PyO3 hello + maturin 打通 + `dir()` 对比脚本 | 现有自检全绿（壳全走 Python） |
| P1 文本核心 | `rules.rs` + `template.rs` + `zhconv/translit` 数据表 | 两模块换壳，600 断言全绿 |
| P2 扫描计划 | `scan.rs` + `plan.rs` + `context.rs` | 四模块换壳，全绿 |
| P3 执行撤销 | `fsops.rs` + `ops.rs`（回滚事务、日志指纹、裁剪） | 全绿 + 手工撤销演练 |
| P4 网络密钥 | `llm.rs` + `provider.rs` + `secrets.rs`（reqwest + DPAPI） | 全绿 + 真实 LLM 连接测试 |
| P5 收尾 | `presets.rs`、可选原生 CLI、打包脚本、性能基准、文档 | 发布候选；大目录基准对比数据入 TEST_REPORT |

每阶段工作量均以"能跑全量自检"为界，禁止跨阶段堆进度。

---

## 9. Token / 工作量估算

| 项 | 估算 | 依据 |
|---|---|---|
| 现有 engine 生产代码（ex-cli） | 5.8k 行 / ~250KB ≈ 8-10 万 tokens（通读） | 混排中文注释 ≈ 2.5-3.5 字节/token |
| Rust 成品（core + py shim） | **8-11k 行** | ×1.5-1.8：无头文件省行，enum/match/错误处理/借用标注费行，大致抵消 C++ 的 h/cpp 拆分开销 |
| **最终代码产出** | **15-25 万 tokens** | Rust 均 14-18 tokens/行（中文注释同密度） |
| cargo test + 对齐用例组 | +2-4 万 | 表驱动测试性价比高 |
| **全项目实际总消耗** | **约 50-100 万 tokens** | 读旧码 + 阶段化迭代（P0-P5）+ 借用检查/对齐修正轮次 |

与 C++ 方案同量级。差异因素：Rust 编译器反馈强，"编译通过"质量高于 C++，
但借用/生命周期返工轮次略多；正则选型（fancy-regex）替用户省掉一批
语义回归。若执行 §3 的范围裁剪（llm/provider/secrets 留 Python），
总量可压到 **35-70 万**。

### 影响 token 消耗的因素

1. **行为对齐强度（最大项）**：410+190 断言全绿为验收线时迭代最多；
   接受"核心路径对齐"可省 30-40%。
2. **正则语义对齐**：fancy-regex 已最近，但替换串解析器仍须逐行照抄。
3. **测试策略**：cargo test + 现有断言复用 = 最省；重写测试最贵。
4. **注释语言与密度**：现有中文 docstring 重，Rust 侧保持同密度则 token 不降。
5. **宏的诱惑**：`macro_rules!` 批量生成规则分支可省行数但费调试 token，
   能用 enum+match 就不用宏。
6. **范围裁剪**：llm/provider/secrets 留 Python（无性能诉求）直接砍 1/3。
7. **打包链路调试**：PyInstaller + .pyd + MSVC 运行库的组合坑是隐性开销，
   预留一轮迭代预算。
8. **Windows 专属面**（DPAPI/隐藏属性/保留名）：`windows` crate 文档好，
   属省钱项；但每个 API 的安全封装都要自己写对。

---

## 10. 风险与对策

| 风险 | 等级 | 对策 |
|---|---|---|
| 正则语义漂移（用户模式在新引擎报错/不同结果） | 高 | fancy-regex + §5.2 对齐用例组；非法正则保持"原样返回" |
| title/case/translit 细节不一致 | 高 | 表驱动对齐测试钉住 Python 现行为（含"不正确"的细节） |
| 路径非 UTF-8 / 宽字符边界 | 中 | 全程 OsString，文本面才转 String；对齐 Python str 语义按码点计数 |
| JSON schema 漂移导致旧日志/预设读不出 | 中 | 读取侧容忍多余字段；`_split_log_name` 新旧格式段数识别保留；样例文件回归 |
| PyO3/PyInstaller 打包坑 | 中 | P0 阶段先打通打包冒烟，不留到最后 |
| panic 跨边界 | 低 | `catch_unwind` 包入口 + CI 异常路径自检 |
| 借用检查导致进度低于预期 | 低 | 阶段化交付；核心数据结构以值语义 + Vec 为主，少引生命周期参数 |
| 性能收益不及预期 | 低 | P5 基准（10k 文件扫描/改名/hash）对比 Python 版，数据入 TEST_REPORT |

---

## 11. 附录

### 11.1 PyO3 绑定草案

```rust
// crates/namecat-py/src/lib.rs
use pyo3::prelude::*;

#[pymodule]
fn namecat_py(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<py_rule::PyRule>()?;
    m.add_class::<py_plan::PyPlan>()?;
    m.add_function(wrap_pyfunction!(py_rules::make_rule, m)?)?;
    // ...子模块 rules/plan/scan/ops/... 与 Python 模块名一一对应
    Ok(())
}

// crates/namecat-py/src/py_rules.rs
#[pyfunction]
#[pyo3(signature = (kind, a="", b="", c="", **kw))]
fn make_rule(kind: String, a: String, b: String, c: String,
             kw: Option<&Bound<'_, PyDict>>) -> PyResult<PyRule> {
    let rule = namecat_core::rules::make_rule(&kind, &a, &b, &c, extras)
        .map_err(Error::into_pyerr)?;
    Ok(PyRule(rule))
}
```

### 11.2 core 侧错误与守卫草案

```rust
#[derive(thiserror::Error, Debug)]
pub enum Error {
    #[error("无效文件名: {0}")] InvalidName(String),
    #[error("冲突: {0}")] Conflict(String),
    #[error("IO: {0}")] Io(#[from] std::io::Error),
    #[error("LLM: {0}")] Llm(String),
}

/// 改名回滚守卫：任一步失败，Drop 时把已改的临时名还原
pub struct TempGuard { moved: Vec<(PathBuf, PathBuf)> }
impl Drop for TempGuard {
    fn drop(&mut self) {
        for (src, tmp) in self.moved.iter().rev() {
            let _ = std::fs::rename(tmp, src);
        }
    }
}
```

### 11.3 与 C++ 方案的差异速览

| 维度 | C++（前稿） | Rust（本稿） |
|---|---|---|
| 内存安全 | 靠约定（RAII/智能指针纪律） | 编译器保证，无 GC |
| 规则分发 | std::variant + visit | enum + match（穷举检查） |
| 错误处理 | 异常或 expected，纪律驱动 | Result 全覆盖，panic 不出边界 |
| 正则 | QRegularExpression（PCRE2）语义近 | fancy-regex 语义近，需显式选型 |
| 基础库 | Qt6Core（复用现有 DLL） | 纯 Rust 依赖（serde/reqwest/windows） |
| 构建 | CMake + MSVC + pybind11 | cargo + maturin + PyO3 |
| 纯产出 tokens | 15-25 万 | 15-25 万（同量级） |
| 项目总消耗 | 50-100 万 | 50-100 万（裁剪后 35-70 万） |
