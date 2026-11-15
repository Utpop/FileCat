# AGENT.MD — FileCat 修改指南

> 面向后续维护者 / Agent 的项目地图与改动手册。
> 依据 2026-11-14 对当前工作树的静态审查写成（含 `git log` 至 `8e66c73`），只记录实读代码与文档，审查过程未改动源码、未运行测试与构建。
> **仓库根**：`C:\Users\LocalAcc\WorkBuddy\NameCAT\NameCAT\`（外层同名目录只是工作区容器，`.git` 在内层）。本文所有路径都相对仓库根。
> **当前版本 3.0.0**（`engine/__init__.py:APP_VER` 与 `Cargo.toml:[workspace.package].version` 两处一致）；程序名 FileCat；四个页面：文件重命名 / 文件提取 / 执行记录 / 供应商配置（预设改为页内弹窗）。§11 记录文件提取引擎与 GUI 集成。

---

## 0. 开工前必读

| 约束 | 内容 |
|---|---|
| 交流 | 中文，结论先行，给数据支撑；迭代节奏快，多给对照 / 截图。 |
| 测试 | **非明确要求勿主动跑测试套件**（`docs/HANDOFF.md:31,457`）。写完代码给关键路径验证即可；下面命令清单仅作发版 / 验收参考。 |
| 提交 | 未明确要求勿 `git commit` / `git push`（House Rules）。 |
| 改动范围 | 优先可逆、最小改动，原文件与生成物分开；`FileCat.spec`、`_vendor/`、`target/`、`dist/`、`gui_state.json` 都是生成物或运行态。 |
| 文档可信度 | **实跑行为 > 当前源码 > `docs/HANDOFF.md` > `README.md` > `docs/TEST_REPORT.md` > `docs/DESIGN_RUST_ENGINE.md` / `docs/GUI_DESIGN_GUIDE.md` > 代码注释**。后几项存在过时段落，见 §6.2。 |

先读这四份再动手：`README.md`（工程结构 + 用户视角）、`docs/HANDOFF.md`（Rust 化交接，P1–P5 实现要点）、`docs/GUI_DESIGN_GUIDE.md`（界面红线，注意过时项）、本文 §5 速查表。

---

## 1. 目录地图

| 路径 | 职责 | 改动后的连带影响 |
|---|---|---|
| `main.py` | 唯一入口：无参数 / `--gui` / `gui` 开 GUI，其余转 `engine.cli.main()`；插 `_vendor` 进 `sys.path`；缺 `.pyd` 时给人话提示 | 新增子命令需同时动 `engine/cli.py` |
| `ui/app.py` | GUI 启动：vendor 路径、DPI、QApplication、系统字体、MainWindow；`_fatal()` 写 `%TEMP%/FileCat_startup_error.log` 并弹系统框 | 窗口程序无控制台，排障靠这份日志 |
| `ui/window.py` | 主窗口骨架：顶栏 + 侧栏 204px + `QStackedWidget` 四页 + 状态栏；全局 QSS；`NAV`、`_wire()`、`_goto()`、跨页契约、偏好、快捷键（F5 / Ctrl+Enter / Ctrl+1…4） | 加页面要同步 `NAV`、stack、`_wire`、`_goto`、快捷键 |
| `ui/base.py` | 设计令牌唯一来源：`C` 色板、`F()` 字体、`ICONS`；`Card/Button/Check/Badge/Chip/EmptyState/PageHead/Select/Strip/Clickable/LlmBubble/NavButton`；`vbox/hbox/grid_row/scroll_area/fit_scroll_content` | 样式改动优先落这里；`fit_scroll_content()` 是固定高度卡片内列表的必备补刀 |
| `ui/pages/workspace.py` | **最核心**，工作台 2189 行：工具栏、规则链、属性面板、自绘预览表、摘要条、扫描状态机、流程编排、勾选 / 导出 / 执行 / 预设 | 见 §3、§4、§5 |
| `ui/pages/presets.py` / `history.py` / `llm_settings.py` | 预设对话框（页内弹出）、执行记录 + 撤销（两类日志）、LLM 供应商配置 | 三者都经 `adapters` 取数；`history.py` 复用 `workspace.Item` |
| `ui/prefs.py` | 界面偏好白名单（`last_root`、`deleted_builtin`），`FORBIDDEN` 过滤密钥类键名 | 新增偏好键须同步 `DEFAULTS` |
| `adapters/engine_adapter.py` | **界面 ⇄ 引擎唯一通道**：`FormState`、`PlanBundle`、`scan_only/build_plan/apply_plan`、勾选排除、规则元数据（`KIND_FIELDS/DEFAULTS/KIND_FLAGS/RULE_GROUPS`）、状态文案、预设、LLM、日志 | 新增规则类型或引擎参数，这里是必改点 |
| `workers/task_workers.py` | `BaseTask(QRunnable)` + `TaskRunner(QThreadPool)`；`done/failed/finished/progress` 信号回主线程；后台线程禁止碰 QWidget | 新增后台任务加一个 `xxx_task()` 工厂即可 |
| `engine/__init__.py` | 常量与路径助手：`APP_VER`（**版本唯一来源之一**）、状态码、`STATUS_TEXT`、`LOG_SCHEMA=5`、`UNDO_SCHEMA=3`、`appdata_dir()`、`backup_dir()`、旧版 NameKit 数据迁移 | 发版必改；日志目录策略也在这里 |
| `engine/cli.py` | 命令行全部逻辑：`rename/preview/undo/leftovers/rules/presets`，`--apply` 才落盘，`preview` 拒收 `--apply`；GBK 符号降级 `_SafeStream` | CLI 参数改动同步 README |
| `engine/llm_resolve.py` | LLM 规则「整批问一次模型」的 Python 桥，由 Rust `plan` 回调；离线 / 无客户端时原样放行并写提示 | 网络动作只在这一层 |
| `crates/namecat-core/` | 纯 Rust 引擎（22 个 `pub mod`），零 PyO3，可独立 `cargo test` | 改这里必须重编 `.pyd` |
| `crates/namecat-py/` | PyO3 绑定层：`lib.rs` 挂 12 个子模块 + 根常量，`shim.rs` 处理 `sys.modules`/`__all__`/异常映射，其余 `py_*.rs` 做类型转换 | 公开符号变更要过 `rust_check.py` |
| `rust_build.py` | 版本校验 → `maturin build` → wheel 解包装 `_vendor/` → import 冒烟；`--debug` / `--check` | Rust 改动后必跑 |
| `build_exe.py` | PyInstaller 单文件打包：三道闸 + 生成 `FileCat.spec` + 瘦身清单 `DROP_BINARIES` + 打包后「改名→撤销」自检 | **改 spec 模板请改这里**，spec 每次覆盖 |
| `verify/` | `run_tests.py`(410 断言) / `test_edges.py`(190+16 探针) / `rust_check.py`(12 模块 193 符号) / `ui_smoke.py`(离屏全流程) / `bench.py` / `llm_save_check.py` / `make_icon.py` / `engine_symbols.json`(只读基准) | 新增引擎公开符号同步 `engine_symbols.json` |
| `tools/` | `gen_zhconv.py` 生成 `zhconv_data.rs`；`gen_path_cases.py` 生成 `path_cases.json`（417 组 ntpath 对拍语料） | 生成物勿手改，用 `--check` 校验 |
| `docs/` | 设计稿、交接、测试报告、截图 | 见 §6.2 过时清单 |
| `assets/` | `app_icon.png` / `.ico`，打包按 `assets;assets` 原样带入 | 图标源改动重跑 `verify/make_icon.py` |
| `_vendor/` | 内联 PySide6 6.10.3 + `namecat_py.cp315-win_amd64.pyd`，`.gitignore` 忽略 | 首次运行需 `pip install -r requirements.txt -t _vendor` + `rust_build.py` |
| `Name_backup/` | 撤销日志（含真实路径），忽略入库 | 数据安全资产，勿清理用户数据 |

---

## 2. 分层与依赖方向

```
main.py ──► ui.app ──► ui.window ──► ui/pages/* ──► adapters/engine_adapter.py ──► namecat_py (.pyd, Rust)
   │                                                          ▲
   └──► engine/cli.py ──► namecat_py ────────────────────────┘
                    engine/llm_resolve.py ◄── Rust plan 的 llm 规则回调

ui/pages/* ──► workers/task_workers.py (QRunnable + 信号) ──► adapters ──► 引擎
```

硬规则：

1. `ui/` 只 import `adapters.engine_adapter`（记作 `A`）与 `ui.base`；业务算法禁止在界面里再写一份。界面直接 import 的只有 `namecat_py.rules as R`（`workspace.py:24`，取 `R.Rule` 类型标注）这类数据类，别继续扩大。
2. `engine/` 禁止出现 Qt；`adapters/` 是唯一通道，`FormState` / `PlanBundle` 是纯数据，方便预设序列化与测试。
3. 后台线程只跑引擎函数；`done` 回调里才更新 UI（`workers/task_workers.py:51-60`）。
4. **预览即承诺**：算名只做一次，落盘复用 `bundle.plan`（`engine_adapter.py:281-288`，`py_ops.rs:143-173`）。重算会让 LLM 请求两次、随机数跑偏。
5. **统计唯一来源** `PlanBundle.stats()`；界面自己数数会与引擎状态脱节。
6. 2.0.1 已删掉 `engine/*.py` 的 12 个转发壳，现只剩 `__init__.py` / `cli.py` / `llm_resolve.py`；调用方一律 `from namecat_py.<模块> import …`。

---

## 3. 关键数据结构与状态机

### 3.1 `FormState`（`adapters/engine_adapter.py:86-209`）

界面全部可调参数的纯数据：`root / only / recursive / include_hidden / only_match / exclude_match / include_exts / exclude_exts / verify_content / sort_mode / max_depth / selected / strict_selected`，加计划参数 `start / step / width / scope / reverse / split_ext / rename_dirs`。

- `scan_options()` → `ScanOptions`；`plan_options(dry_run)` → `PlanOptions`。
- `scan_dict()` / `plan_dict()` 供预设落盘，**不含 root 与密钥**；`apply_scan_dict()` 会按预设的 kinds 反推 `only`，并令 `rename_dirs = only in ("dir","all")`。
- 拖放多个对象时 `selected` 存绝对路径元组，`strict_selected` 处理跨盘（无公共父目录时只收选中项）。

### 3.2 `PlanBundle`（`:215-251`）

`scan` / `plan` / `state` 三字段；`has_scan`、`has_plan`、`items()`（有计划用 `plan.items`，否则 `scan.items`）、`stats()`。
工作台三段状态由此推出：**未扫描 → 只扫描（`plan=None`，新名称列显示「—」）→ 已算名**。

### 3.3 `WorkspacePage` 核心字段（`ui/pages/workspace.py:804-814`）

| 字段 | 语义 |
|---|---|
| `state` / `rules` / `selected_id` | 表单、规则链、当前选中规则 |
| `bundle` | 当前 `PlanBundle`；根目录变化时置 `None` |
| `stale` | 配置改动后预览过期；`_mark_stale()` 在**已有 plan** 时才置位 |
| `_busy` / `runner` | 忙状态（`_busy_set()` 统一收口）、`TaskRunner(max_threads=2)` |
| `_plan_after_scan` | 扫描完成后由 `_task_finished()` 经 `QTimer.singleShot(0, generate_plan)` 自动算名 |

按钮可用性收口在 `_refresh_summary()`（`:1450-1475`）：`btn_apply = has_plan and stats.ok and not stale and not _busy`；`btn_preview` 文案随 `has_plan` / `has_scan` 变；`btn_all/btn_none` 随 `has_scan`。

### 3.4 勾选排除（引擎的真实 skip 语义）

`A.set_excluded_one()` / `set_excluded()` / `clear_exclusions()`（`engine_adapter.py:295-363`）在 `Item.__dict__` 上存 `_excluded` / `_status_before` / `_note_before`，把状态改成 `ST_SKIP`、备注改成「按勾选排除」，还原时恢复原状态（真实冲突不会被洗白）。`build_plan()` 算名前先 `clear_exclusions()`，因为两阶段复用同一批 `Item` 对象。

`PreviewTable.refresh_tones_at()` 刷新单行状态列 + 说明列（`workspace.py:599-617`）；`refresh_tones()` 是批量路径。被排除的行同时置灰 `_dim`。

### 3.5 预览表是**自绘控件**，别退回控件树

`PreviewTable`（`:494-791`）：`set_rows()` 一次喂入 7 段文本 + 颜色 + 勾选状态；`paintEvent` 只画 `visibleRegion()` 覆盖的行；`_cols()` **直接读表头 `phead` 的 8 个 QLabel 几何**保证对齐；`RowHandle` / `_HandleCheck` 是给老调用点与自检脚本保留的轻量句柄（`_row_widgets()`）。

2.1.0 性能治理：9000 行建表 259 s → 52 ms，单行勾选 ≈ 1 ms（`docs/TEST_REPORT.md §8.5`）。新增列要同时改 `cols`（`:1015`）、`_preview_rows()`（`:1044`）、`paintEvent` 的列索引、`_tooltip_at()` 的范围。

### 3.6 扫描栏状态机

`_scan_status_idle / busy / done / message`（`:1519-1577`）+ 旋转图标 timer + 5 秒淡出动画 `_scan_fade_start()`。页内横幅已全部退场，`_notice_msg()` 是消息统一入口；专注模式（`_toggle_focus()`）下警告 / 错误会先还原布局。

### 3.7 参数变化后的刷新策略（容易漏）

| 用户动作 | 处理 |
|---|---|
| 规则增删改 / 参数 / 扫描范围 / 排序 | `_mark_stale()` → 只标过期，等用户点「预览变更」 |
| 排序方式换档 | `_on_sort_changed()` 立即 `scan(plan_after=True)`（先排序再编号） |
| 高级筛选「应用」 | `scan()` 重扫 |
| 根目录编辑 | `_on_root_changed()` → `bundle = None` + 清空预览 |
| 拖入路径 / 选择目录 | `add_paths()` → 去重 + 抽样 stat + commonpath → `scan()` |
| 应用预设 | `apply_preset()` 保住 root、同步所有控件，再 `scan(plan_after=True)` |

---

## 4. 端到端数据流（带行号）

```
scan()            workspace.py:1952  ── W.scan_task(A.scan_only) ──► namecat_py.scan
_scan_done()      :1966  bundle=scan-only, stale=False, _refresh_preview(), 绿勾状态
_task_finished()  :1978  _plan_after_scan → QTimer.singleShot(0, generate_plan)
generate_plan()   :1984  ── W.build_plan_task(A.build_plan) ──► namecat_py.plan.build
_plan_done()      :1999  bundle 换成带 plan 的新包，刷新预览 / 摘要 / LLM 气泡
勾选             :2044  A.set_excluded_one → refresh_tones_at → _after_check_change()
confirm_apply()   :2094  确认框 ── W.apply_task(A.apply_plan) ──► namecat_py.ops.apply
                          （复用 bundle.plan，绝不重算）
_apply_done()     :2117  报结果 + stale=True + _refresh_preview()/_refresh_summary()
撤销              ui/pages/history.py:259 → W.undo_task(A.undo) → namecat_py.common.undo
```

`py_ops.rs:143-285` 落盘后写撤销日志（含 `exec` 段真实成败，schema 5），失败信息写进 `message`；`py_common.rs:387-479` 的 `execute_impl()` 只挑 `status == ok` 且 `new_name` 真的变了的项，交给 `rename_core::execute()` 做临时名中转与整批回滚。日志统一存 `<程序目录>\Name_backup\`，`NAMECAT_LOG_DIR` 可覆盖（验证脚本靠它隔离）。

---

## 5. 修改任务速查表（Cookbook）

| 想改什么 | 动哪些文件 | 重编 Rust？ | 注意 / 建议验证 |
|---|---|---|---|
| 配色 / 圆角 / 字号 / 控件外观 | `ui/base.py`（`C`、`F()`、`ICONS`、QSS 片段）、`ui/window.py` 的 `QSS` | 否 | QSS 里只用 `/* */` 注释；`% C` 格式化，别写成 `#` 行注释 |
| 工作台布局 / 交互 | `ui/pages/workspace.py` | 否 | 固定高度卡片内列表刷新后调 `fit_scroll_content()`；编辑规则会重建列表，记得保住滚动位置 |
| 加一个页面 | `ui/pages/<name>.py` + `window.py` 的 `NAV`/`stack`/`_wire`/`_goto` | 否 | `status_message` 信号接状态栏；`_goto` 里的 reload 分支 |
| 预览表加列 | `workspace.py`：`cols`、`_preview_rows`、`paintEvent`、`_tooltip_at`；必要时 `ui/base.py:grid_row` | 否 | 表头与自绘列几何同源，勿另算一套 |
| 加一条规则类型 | `crates/namecat-core/src/rules.rs` → `py_rules.rs` 导出 → `engine_adapter.py` 的 `KIND_FIELDS` / `KIND_FLAGS` / `RULE_GROUPS` / `DEFAULTS` / `rule_problems()` / `rule_advisories()`；必要时 CLI 别名、内置预设 | **是** | 同步 `verify/engine_symbols.json`（若新增公开符号）、`README.md` 规则数、`ui/pages/workspace.py` 的属性渲染分支 |
| 规则属性面板字段 | `workspace.py:_refresh_props / _number_props / _flag_props / _advisory_label` | 否 | 编号规则的 `extras.*` 字段走 `A.get_field/set_field`；「锚点字符」写的是 `rule.charset` |
| 扫描过滤 / 排序档 | `core/scan.rs` + `py_scan.rs` + `FormState.scan_options/apply_scan_dict` + `AdvancedDialog` + `engine/cli.py` 的 `--sort` | **是** | 排序影响发号顺序；`SORT_MODES` 十档，预设里显式存的不受默认值变更影响 |
| 算名 / 校验语义 | `core/plan.rs`、`validate.rs`、`names.rs` + `py_plan.rs` | **是** | 行为对齐现有实现（含 Python `str.title()` 怪癖），非法正则原样返回 |
| 落盘 / 回滚 / 残留 | `core/rename.rs` + `py_common.rs` + `py_ops.rs` | **是** | 禁止 `std::fs::rename`（Windows 会覆盖目标）；用 `MoveFileExW(flags=0)`；失败整批回滚 |
| 撤销日志 / 执行记录 | `core/logs.rs` + `py_common.rs` + `ui/pages/history.py` | **是**（Rust 侧） | `LOG_SCHEMA=5` / `UNDO_SCHEMA=3`，旧日志照读；一份坏日志只跳过它 |
| LLM 接入 / 供应商配置 | `core/llm.rs` / `provider.rs` + `py_llm.rs` / `py_provider.rs` / `py_net.rs` + `engine/llm_resolve.py` + `ui/pages/llm_settings.py` + `engine_adapter.py` 的 `llm_*` | 视层次 | 密钥永不回显、单独 DPAPI 存储；回环地址绕过代理；测试 / 拉模型列表走后台线程 |
| 命令行参数 | `engine/cli.py`（`build_parser()`、`cmd_*`） | 否 | `preview` 拒收 `--apply`；输出经 `_SafeStream` 降级，新增装饰符号补 `_SYM_FALLBACK` |
| 工作台消息 / 状态槽 | `workspace.py:_notice_msg` + `_scan_status_*` | 否 | 横幅已退场，别再加独立提示条 |
| 版本号 | `engine/__init__.py:APP_VER` **与** `Cargo.toml:[workspace.package].version` 两处同改；`README.md` 页眉 / 更新记录随动 | **是** | `rust_build.py` 校验两处一致；`namecat_py.APP_VER` 取自 Cargo，`pyproject.toml` 是 dynamic version |
| 打包 / 瘦身 | `build_exe.py`（`DROP_BINARIES`、`write_spec()`、`verify_exe()`） | 否（但需先有 `.pyd`） | `FileCat.spec` 每次覆盖；`--keep-opengl` / `--zip` / `--no-verify` / `--allow-stale-engine` |
| 启动器 | `FileCat.bat`（ASCII）、`FileCat.vbs`（**UTF-16 LE + BOM**） | 否 | VBS `sh.Run cmd, 1, False`，写 0 会以 SW_HIDE 启动并藏掉主窗口；`cscript //nologo FileCat.vbs --print-launch` 排障 |

**是否重编的判断**：只改 `ui/`、`adapters/`、`engine/*.py`、`workers/` → 源码即时生效，发 exe 时重新 `build_exe.py` 即可。改 `crates/` 任何 `.rs` / `.toml` → `cargo test` → `py -3 rust_build.py` → `rust_check.py` → 行为自检 → 重新打包。

---

## 6. 审查发现项与状态

### 6.1 执行重命名后预览未及时刷新 —— 已在 2.1.1 修复

证据链（全部为当前代码事实）：

1. `confirm_apply()`（`workspace.py:2094-2115`）把**执行前**的 `self.bundle` 交给 `W.apply_task`；`A.apply_plan()`（`engine_adapter.py:281-288`）直接把 `bundle.plan` 传给 `namecat_py.ops.apply`，并要求「绝不重算」。
2. `py_ops.rs:143-285` 与 `py_common.rs:387-479` 落盘时只构造 `PyRenameStep`（`old/new/ok/error`）+ 撤销日志，**没有把新路径 / 新名字写回 `Item`**，返回的 `PyOpsResult` 里 `plan` 仍是原对象。
3. `_apply_done()`（`workspace.py:2117-2131`）只做：`_notice_msg` → `status_message` → `stale = True` → `_refresh_preview()` → `_refresh_summary()`。而 `_refresh_preview()`（`:1478-1495`）读的是 `self.bundle.items()` —— 仍是旧 `name` / `path` / `new_name`。
   → 磁盘上名字已变，界面表格还停留在执行前那一批数据；`stale=True` 只把「执行重命名」按钮禁用并让摘要写「配置已改动，预览已过期」，**不会主动重扫**。
4. 更隐蔽的一点：`generate_plan()`（`:1984-1998`）只检查 `bundle.has_scan`，**没有检查 `stale`**。用户执行完直接点「重新生成预览」时，会拿旧 `Item` 重算名字，预览与磁盘实际文件名脱节；后续若再执行会撞 `WinError 183`。
5. `verify/ui_smoke.py:193-203` 第 9/10 步只断言目录列表与日志条数，**没有断言工作台预览内容**，所以这个现象目前没有自检覆盖。

2.1.1 已按「磁盘状态优先」收口（只改 `ui/`，不动 Rust 引擎）：

- `WorkspacePage` 增加 `_scan_outdated`（清单与磁盘脱节）、`_rescan_after_apply`（待重扫）、`_refresh_is_apply`（本次扫描属于落盘刷新）、`_apply_result_message/tone`（重扫后把执行结果贴回状态槽）。`stale` 继续只表示「规则改过、清单还能复用」，两种过期语义不再混用。
- `_apply_done()` 标记清单失效，但不立刻重扫；`_task_finished()` 在线程池彻底退出后经 `_schedule_apply_refresh()` 排队 `scan()`。只扫清单、不自动再套规则，用户看到的是刚落盘的名字。重扫失败保留 `_scan_outdated`，用户点「预览变更」会再次触发重扫。
- `generate_plan()` / `request_preview()` / `confirm_apply()` 在 `_scan_outdated` 时一律先重扫或禁用，`btn_csv` 同步禁用，堵住「拿旧 Item 重算 / 再落盘撞 WinError 183」这条路径。
- 「执行记录」页撤销完成后通过 `workspace.mark_scan_outdated()` 走同一套刷新；工作台没选目录或目录已不存在时只标记旧清单失效，不弹「还没有选择文件夹」。
- `verify/ui_smoke.py` 第 9/10 步补了断言：执行与撤销完成后 `bundle.items()` 的名字集合必须与磁盘一致，且 `_scan_outdated` 归零。

### 6.2 文档 / 注释过时（读的时候别被带偏）

| 位置 | 过时内容 | 实际 |
|---|---|---|
| `crates/namecat-py/src/lib.rs:21-26` | 「`engine/rules.py` 退化成一行 re-export」「`engine/*.py` 只剩一行 re-export」 | 2.0.1 已删转发壳；`engine/` 只有 `__init__.py` / `cli.py` / `llm_resolve.py`；`adapters` / `cli` 直接 import `namecat_py` |
| `docs/GUI_DESIGN_GUIDE.md` §2.2 / §4.1 / §13 | 列 `verify/gui_smoke.py`、`gui_shots.py`、`check_icons.py` | 三个旧界面脚本已删（memory 2026-10-27），现用 `verify/ui_smoke.py` |
| 同上 §2 F6.6 / §6.6 | 「schema 4」「最多 20 份」 | `LOG_SCHEMA=5`（`engine/__init__.py:164`）；每目录 20 份 + 总量 200 |
| 同上「令牌唯一来源 `theme.py` / QSS `{{token}}`」 | — | 实际是 `ui/base.py` 的 `C` 字典 + `QSS % C`（`ui/window.py:34-85`） |
| `docs/HANDOFF.md:17` | 「版本唯一来源…当前 1.6.3」 | 当前 `APP_VER = 3.0.0` |
| `README.md` 旧页脚 | 「NameCat 2.0.1」 | 页眉已是 3.0.0 |
| `docs/DESIGN_RUST_ENGINE.md` | HTTP 用 `reqwest::blocking` | 实际字节出口留在 `py_net.rs` 的 urllib（回环绕过代理） |

---

## 7. 命令清单（**用户偏好：非明确要求勿主动跑测试套件**）

### 日常开发 / 源码运行

```bat
py -3 -m pip install -r requirements.txt -t _vendor   :: 仅首次，PySide6>=6.10,<6.11
py -3 rust_build.py                                   :: 首次 / 改 crates 后，装 .pyd 进 _vendor
python main.py                                        :: GUI
FileCat.bat                                           :: 带控制台启动（报错可见）
FileCat.vbs                                           :: 无控制台启动（UTF-16）
python main.py preview D:\照片 --rule "template={n}" --width 3
python main.py rename  D:\照片 --rule "case=lower" --apply
python main.py undo D:\照片
python main.py leftovers D:\照片
```

### Rust / 引擎改动后

```bat
cargo test --release -p namecat-core
py -3 rust_build.py                       :: 或 --debug（快编）/ --check（只校版本）
py -3 verify/rust_check.py --fail-on-any :: 12 模块 193 符号 + 根常量
```

### 验收 / 发版（明确要求时执行，建议 `set PYTHONPATH=_vendor`）

```bat
py -3 verify/run_tests.py                 :: 410 断言
py -3 verify/test_edges.py                :: 190 断言 + 16 缺陷探针
py -3 verify/ui_smoke.py                  :: 离屏 QT_QPA_PLATFORM=offscreen
py -3 verify/llm_save_check.py            :: 供应商配置保存链路
py -3 verify/bench.py                     :: 2000 文件 7 轮中位数
py -3 tools/gen_zhconv.py --python-file <旧版 zhconv.py> --check
py -3 tools/gen_path_cases.py --check
py -3 build_exe.py                        :: 产出 dist/FileCat.exe（本版 Release 直发 EXE）
py -3 build_exe.py --zip                  :: 额外压 zip
```

`build_exe.py` 三道闸：`.pyd` 存在且唯一 / ABI 含 `cp%d%d` 且匹配当前解释器 / `.pyd` mtime ≥ `crates/` 最新源码；`--allow-stale-engine` 只把第三条降为告警。

### 发版顺序

改 `engine/__init__.py:APP_VER` + `Cargo.toml` 版本 → `rust_build.py` → 上述验收 → `build_exe.py` → commit + `git tag -a vX.Y.Z` → push → Release 传 `dist/FileCat.exe`。

---

## 8. 硬约束与历史坑（按类分组）

**界面 / Qt**
- QSS 里只用 `/* */`，`#` 行注释会吞掉其后全部规则。
- PySide6 禁止 `widget.mousePressEvent = fn` 实例覆盖（C++ 按类分发虚函数），要子类化 `Clickable`。
- 固定高度卡片里的列表刷新后必须 `fit_scroll_content()`；规则列表编辑会重建，记住并恢复滚动位置。
- 自适应列设 `QSizePolicy.Ignored`；别反复 `deleteLater` + 重建行（离屏叠字）。
- 预览表保持自绘 + 只画可见行，勿退回「一行 13 个 QWidget」。
- Qt 原生对话框在 Windows 上做不到文件 + 文件夹混选；「只要这几个文件」走拖放（`DropCard`）。
- 改 CRLF 文件先 `replace("\r\n", "\n")` 再写回，否则字面替换静默落空。

**数据安全 / 行为**
- `MoveFileExW(flags=0)`；禁止 `std::fs::rename`（Windows 会覆盖已存在目标）。
- 冲突项跳过、绝不覆盖；失败整批回滚，父目录优先还原。
- 撤销日志写不进去时必须在结果里说明「本次改名无法撤销」。
- `ops.Result.success` 显式给，撤销成败别靠 steps 推断。
- 脏 JSON / 坏预设 / 坏日志一律「能救的救、救不了的跳过」，勿让整页崩。
- 这些是**设计内行为**，勿顺手「修正」：`--step 0` 回落 1；模板未知变量渲染成空串；非法正则原样返回；`str.title()` 的怪癖。
- 扫描跳过日志目录用小写比对表 `LOG_DIR_SKIP`。
- `normcase` 必须走 `LCMapStringEx`（İ / ǅ / 希腊词尾 sigma 与 Rust `to_lowercase` 不同）。

**Rust / PyO3**
- 子模块必须 `shim::new_submodule()` 注册进 `sys.modules`；`__all__` 必须 `set_all()` 显式声明并逐个 `hasattr` 校验。
- `secrets` 必须排在 `provider` 之前挂载，否则报 `'namecat_py' is not a package`。
- `extras` 保持活 dict；`normalize()` 返回同对象；`Item` 跨线程用 `Mutex`。
- 错误映射：`ValueError` / `OSError` / `EngineError`；panic 用 `catch_unwind` 兜住不过边界。
- `plan_one` 里禁用 `?`；切片按码点防越界；`ProviderStatus.model` setter 别撞名。
- 正则依赖 `fancy-regex`（回引用 / 环视），换成 `regex` crate 会破坏用户正则。

**构建 / 启动 / 打包**
- Python 3.15 + `cp315`；`maturin` 必须 `--interpreter sys.executable` 钉死，PATH 上另挂 3.13 会编出 cp313 导致 ImportError。
- `_vendor/` 里同名 `.pyd` 只留一份，多份时 `build_exe.py` 直接退出。
- PyInstaller 必须在 `binaries=[(PYD, ".")]` 显式带 `.pyd`，仅 `pathex` 会让 12 个子导入判 missing、exe 启动即崩。
- 窗口程序无控制台：启动异常看 `%TEMP%/FileCat_startup_error.log`；单文件 exe 需要 `%TEMP%` 可写。
- VBS 是 UTF-16 LE + BOM；`sh.Run cmd, 1, False`，写 0 藏主窗口。
- GBK 终端：`engine/cli.py` 的 `_SafeStream` + `_SYM_FALLBACK` 负责降级，新增装饰符号记得补表。
- 版本两处不一致或 `.pyd` 比 `crates/` 旧 → 脚本直接退出。

---

## 9. 数据位置与隐私

| 位置 | 内容 |
|---|---|
| `%APPDATA%\FileCat\gui.json` | 界面偏好（`last_root` 等），白名单过滤密钥类键 |
| `%APPDATA%\FileCat\llm_provider.json` | LLM 地址 / 模型 / 请求头（无密钥） |
| `%APPDATA%\FileCat\llm_credential.dat` | DPAPI 加密的密钥，界面永不回显，留空 = 沿用已保存 |
| `%APPDATA%\FileCat\llm_instruction.txt` | 用户可编辑的整理要求，`#` 开头是注释 |
| `<程序目录>\Name_backup\` | 撤销日志（含真实路径），`NAMECAT_LOG_DIR` 可覆盖，每目录 20 份 / 总量 200 |
| `gui_state.json` / `_vendor/` / `target/` / `build/` / `dist/` / `*.spec` / `Name_backup/` | 全部 `.gitignore` 忽略；`Cargo.lock` 要入库 |

预设与撤销日志都不含密钥；GUI 指南里「预设导入导出 / 拖动排序」属于已删除功能，别照着加回来。

---

## 10. 维护本文档

改动以下任一项时同步更新本文对应章节：分层方向（§2）、`FormState` / `PlanBundle` 字段或状态机（§3）、端到端流程链路（§4）、新增规则 / 子模块 / 页面（§1、§5）、构建链与版本号来源（§5、§7）、§6.1 预览刷新问题后续若再调整自动重扫策略，同步更新修法说明与 `verify/ui_smoke.py` 的断言位置。

---

## 11. FileCat 文件批量提取引擎（2026-11-15 新增）

> 程序名由 `NameCat` 改为 **FileCat**（`engine/__init__.py:APP_NAME` 与 `crates/namecat-core/src/consts.rs` 常量同改；`rust_check.py` 根常量比对仍逐条相等）。原重命名业务逻辑、接口、数据格式零改动。

### 11.1 架构与职责边界

| 层 | 路径 | 职责 |
|---|---|---|
| 新 Rust Core | `crates/namecat-extract-core/` | 纯 Rust，零 PyO3 / Qt / GUI 依赖。扫描、深度、排除、两种搬运方式、冲突策略、模拟/执行/停止、备份、空目录清理、固定日志、撤销、CSV。可 `cargo test -p namecat-extract-core` 独立跑 |
| 原 Rust Core | `crates/namecat-core/` | 仅改名/规则链/模板/LLM/预设；**不因提取功能改造** |
| PyO3 绑定 | `crates/namecat-py/src/py_extract.rs` → `namecat_py.extract` 子模块 | 唯一新增的第 13 个子模块；JSON↔Python dict 互转，`py.detach` 释放 GIL 跑长任务，进度回调 `Python::attach` 回主线程，停止标志按 task_id 存 Rust 全局表。不复用 `PyItem` / `FormState` / `PlanBundle` |
| 共用 GUI | PySide6 主窗口（后续接入 `ui/pages/extract.py`） | 复用 `ui/base.py` 令牌、`workers/task_workers.py` 线程池、唯一一份 `.pyd`、同一套 `build_exe.py` |
| 独立 Adapter | `adapters/extract_adapter.py`（后续接入） | 界面 ⇄ 提取引擎唯一通道，与 `engine_adapter.py` 平行、互不 import |

**隔离证据**：`rust_check.py` 的 12 模块 / 193 符号期望集（`engine_symbols.json`，只读）不变；`extract` 单独用 `EXTRACT_SYMBOLS`（19 项）核对。`namecat-extract-core` 不依赖 `namecat-core`，两者只并列挂在同一个 workspace 下，编译产物仍只有 `_vendor/namecat_py.cp315-win_amd64.pyd`。

### 11.2 新 Core 模块

| 文件 | 内容 |
|---|---|
| `model.rs` | `ExtractOptions` / `ExtractMode{Flatten,OneLevel}` / `ConflictPolicy{Rename,Skip,Overwrite}` / `FileStatus`（9 档）/ `PlanItem` / `ExtractPlan` / `ExecResult` / `RollbackLog`（`SCHEMA=1`）/ `StopFlag` / `Progress` trait / `Error` |
| `fs.rs` | `\\?\` 长路径、重解析点识别（`GetFileAttributesW`）、`canonical_plain()` 剥 `\\?\` 前缀保证路径一致、`MoveFileExW(flags=MOVEFILE_COPY_ALLOWED\|WRITE_THROUGH)` 非覆盖移动、覆盖备份、空目录删除 |
| `scan.rs` | 一次遍历拿快照：`depth` 以目标根为基准、`max_depth=0` 不限、目录名/扩展名排除（大小写与空格归一）、跳过 `Extract/Rollback_Log` 内部目录、不跟随 symlink/Junction、路径字典序确定顺序 |
| `plan.rs` | `flatten`（全部进根）与 `one_level`（只上移一级，基于快照不重算）；`unique_name()` 生成 `name (2).ext` 保留扩展名并二次验证冲突；`occupied` 集合保证多源同目标的确定性 |
| `execute.rs` | `execute(dry_run)`：模拟零写入；真实执行前 `prepare_log` 必须成功否则拒绝；逐项重新校验源/目标；覆盖前先备份；`StopFlag` 在文件之间检查、未处理项标 `Stopped` 不标成功；`ThrottledProgress` 40ms/25 项节流；每 25 项日志检查点；最深优先清理空目录 |
| `rollback.rs` | 固定目录、原子写（`.tmp` + rename）、坏日志跳过、撤销安全检查（mtime/存在性/原位置冲突）、覆盖先还原新文件再恢复备份、先重建目录再放文件、`Active/Partial/Undone` 状态、绝不自动删除有效日志 |
| `csv.rs` | UTF-8 BOM + RFC4180 转义 + CRLF，含文件名/来源目录/原始路径/目标路径/大小/状态/错误/改名/覆盖/备份 |

### 11.3 PyO3 接口（`namecat_py.extract`）

`SCHEMA` / `INTERNAL_DIR_NAME` / `ROLLBACK_DIR_NAME` / `BACKUP_SUBDIR` / `MODES` / `POLICIES` / `STATUSES` 常量；函数：

| 函数 | 语义 |
|---|---|
| `program_root_hint()` | 源码 = `engine/__init__.py` 上一级；`sys.frozen` = `sys.executable` 所在目录；**绝不取 cwd / `_MEIPASS` / `%APPDATA%`** |
| `rollback_dir(program_root)` | 返回固定 `Extract/Rollback_Log` 绝对路径 |
| `parse_list(text, dotted)` | 逗号/分号分隔，忽略大小写与首尾空格，扩展名自动补点 |
| `build_plan(options, progress=None)` | 只读扫描 + 计划（dict） |
| `execute(options, plan, dry_run=False, task_id="", progress=None)` | 模拟/真实执行，返回 `ExecResult` dict |
| `stop_task(task_id)` | 置停止标志（当前文件完整结束） |
| `undo_latest(program_root, progress=None)` / `undo_path(log_path, ...)` | 撤销，返回 succeeded/failed/skipped/restored_dirs/errors/complete |
| `list_logs(program_root)` / `read_log(path)` | 日志摘要与全文 |
| `export_csv(path, items)` / `plan_stats(plan)` | CSV / 统计 |

错误映射：`Invalid/Path/Log/Json` → `ValueError`，`Io` → `OSError`；`shim::guard` 兜住 panic。

### 11.4 扫描 → 规划 → 执行 → 撤销 流程

```
build_plan(options)
  └─ scan() 一次遍历快照（深度/排除/内部目录隔离/重解析点）
  └─ plan_from_scanned() 算 target + 冲突策略（occupied 确定性）
execute(options, plan, dry_run)
  ├─ dry_run: 只改内存状态，零文件系统写入
  └─ real: prepare_log（目录 + 初始原子日志）→ 逐项重校验 → 覆盖先备份
           → MoveFileExW 非覆盖 → 检查点日志 → 空目录最深优先清理
           → finalize_log 原子替换
undo_latest(program_root)
  └─ 读最新 Active/Partial → 先重建被清理目录 → 逐项校验 mtime/存在/原位置
     → 覆盖项：新文件回原位 + 备份恢复目标 → 写回状态（失败保留日志与备份）
```

### 11.5 固定撤销日志路径与备份策略

```
<程序根目录>\Extract\Rollback_Log\
    rollback_<时间戳_pid_序号>.json     schema=1，临时文件 + 原子 rename 写入
    .write_probe                        试写探针，失败则在搬文件前拒绝执行
    backups\<op_id>_<序号>_<原名>       覆盖前原目标文件备份，绝不覆盖已有备份
```

- 日志含 `operation_id / time / root / mode / conflict / result / state / entries[] / cleaned_dirs[] / errors[]`；`entries` 记录 source/target/size/status/mtime/renamed/overwritten/backup。
- 撤销失败或跳过时状态改 `Partial`，**日志与备份全部保留**；成功也只是标 `Undone`，不删记录。
- 与重命名引擎的 `Name_backup` 完全隔离：不同目录、不同 schema、不同清理逻辑。
- 扫描时 `Extract/Rollback_Log`（含 backups）作为内部数据目录排除；用户把程序根目录当扫描目标同样生效。

### 11.6 构建与验证命令

```bat
cargo test --workspace                       :: 112 原 core + 10 新单元 + 14 集成断言
py -3 rust_build.py --debug                  :: 装 .pyd 进 _vendor（冒烟已加 extract 子模块检查）
py -3 verify/rust_check.py --fail-on-any    :: 193 原符号 + 19 个 extract 独立符号
py -3 verify/extract_smoke.py               :: PyO3 端到端 31 断言（计划/模拟/执行/日志/CSV/撤销）
PYTHONPATH="_vendor;." py -3 verify/run_tests.py   :: 410 断言原功能回归
PYTHONPATH="_vendor;." py -3 verify/test_edges.py  :: 190 断言 + 16 缺陷探针
py -3 build_exe.py                          :: 单文件 EXE；stale 检查用 crates/**/*.rs 通配，自动覆盖新 crate
```

`build_exe.py` 三道闸（`.pyd` 唯一 / ABI 匹配 / 比 `crates/` 新）不变；`rust_build.py` 的 import 冒烟新增 `namecat_py.extract` 与 `build_plan`/`undo_latest` 可用性断言。程序名改为 FileCat 后 `.spec` / `.exe` / zip 名自动跟随 `APP_NAME`。

### 11.7 GUI 集成（2026-11-15 完成）

**设计契约**：与其它四页同一套 `ui/base.py` 设计令牌（`C` 色板 / `F()` 字体 / `Card` / `Button` / `Check` / `Chip` / `NoticeBar` / `PageHead` / `Strip` / `EmptyState`），不引入新色值、渐变、大圆角或第二套控件；页面骨架照工作台：`PageHead` + 工具 `Card` + 选项 `Card` + 清单 `Card` + 底部状态 `Card`。清单是主区域，占剩余高度。

| 文件 | 职责 |
|---|---|
| `adapters/extract_adapter.py` | 界面 ⇄ 提取引擎唯一通道。`ExtractFormState` / `ExtractBundle`（纯数据）、`build_plan/execute/undo_latest/export_csv`、`status_text/filter_items/relative_dir` 展示辅助。零 Qt、不 import `engine_adapter` |
| `ui/pages/extract.py` | 5 段布局：① 页头（打开目录 / 撤销日志 / 撤销上次）② 目标文件夹（路径可编辑 + 浏览 + 扫描预览）③ 选项（搬运方式 / 扫描深度 / 同名处理 / 清理空文件夹 / 排除目录 / 排除扩展名）④ 清单卡（文本筛选 / 全部折叠 / 全部展开 / 导出 CSV / 按目标目录分组的树 / 7 列：文件名·来源目录·原位置·目标位置·大小·状态·说明）⑤ 底部（进度条 + 6 个状态胶囊 + 模拟运行 / 停止 / 开始提取） |
| `workers/task_workers.py` | 新增 `extract_plan_task / extract_execute_task / extract_undo_task / extract_logs_task / extract_csv_task`，复用 `BaseTask` + `TaskRunner` + Qt 信号回主线程 |
| `ui/window.py` | NAV 扩到 5 页：0 **文件重命名** / 1 **文件提取** / 2 预设 / 3 执行记录 / 4 供应商配置；`stack`、`_wire`（5 页都接 `status_message`）、`_goto` 分支、气泡 `goto_llm_page` → 4、`load_preset_requested` → 2、`Ctrl+1…5`、帮助文案、`closeEvent` 的 shutdown 列表同步 |

**状态与控件启用收口在 `_refresh_ui()`**：未扫描 → 只有「扫描预览」可用；有清单 → 模拟 / 开始 / CSV / 折叠展开可用；忙 → 全部禁用、只留「停止」；撤销后旧清单置空、提示重扫。覆盖策略首次执行弹 `QMessageBox` 确认，确认位写进 `options.overwrite_confirmed`。

**3.0.0 默认值（用户指定）**：`FormState.recursive = True`（文件重命名「包含子文件夹」默认打开）、`ExtractFormState.clean_empty_dirs = True`（文件提取「清理空文件夹」默认打开）。预设里显式存的 `recursive` 仍由 `apply_scan_dict()` 覆盖默认值；`apply_preset()` 会同步勾选框（`workspace.py:2260`）。

**文件重命名页工具栏进度条（3.0.0）**：`_toolbar()` 第二行「高级筛选」与右侧「已扫描 N 项」胶囊之间新增 `progress_wrap`（细 `QProgressBar` + 计数标签）。布局上 `progress_wrap` 是**唯一** stretch 项，承担原来 `spacer()` 把胶囊推到最右的职责；进度条固定 220px、内部再 `addStretch(1)`，容器只在忙时 `show()` 子控件、闲时 `hide()`，不与胶囊抢空间。生命周期收口在 `_busy_set()`：`_progress_reset()` / `_progress_hide()`；`_on_progress(cur,total,text)` 接 `BaseTask.signals.progress`（扫描无总数 → 不确定模式，算名/执行有总数 → 确定进度 + `cur/total`）。`scan / generate_plan / confirm_apply / 执行后自动重扫` 四类任务都连了 progress 信号。`verify/ui_smoke.py` 第 6b 步断言进度条与胶囊不重叠、胶囊始终可见、忙显闲隐。**新增 stretch 项时别让进度条和 `spacer()` 各占一个 stretch**——两者会平分剩余宽度，窄窗口下挤压胶囊。

**清单渲染**：`QTreeWidget` 按 `target_dir` 分组，批量 `setUpdatesEnabled(False)` 插入，`setUniformRowHeights(True)`；筛选只过滤内存中的 dict，不重新扫描。分组节点可全部折叠 / 展开；状态列用文字 + 语义色（不只靠颜色），tooltip 给全路径。

**表头对齐（修复）**：初版用了手绘 `Strip` + 固定宽度 `Label` 做表头，与 `QTreeWidget` 内容列各有几何（卡片 14px 内边距、树缩进），列标题与数据错位。现改为**原生 `QHeaderView`**（表头与内容共用同一套列几何），QSS 染成 `C["s1"]` 风格，`setStretchLastSection(True)`；`verify/extract_ui_smoke.py` 加了「表头与内容列对齐」断言（`header().sectionSize(i) == columnWidth(i)`）防回归。新增列时只改 `COLS` / `COL_W`，不要再引入第二套表头。

### 11.8 数据目录改名（2026-11-15 完成）

- `engine/__init__.py` 与 `crates/namecat-core/src/consts.rs` 的 `DATA_DIR_NAME` = `FileCat`，`LEGACY_DATA_DIRS` = `("NameCat", "NameKit")`。`migrate_legacy_appdata()` 会把旧目录里缺失的文件补到新目录（只复制、不删除），老用户配置不丢。
- `%APPDATA%` 实际路径：`...\AppData\Roaming\FileCat\gui.json`（已实测）；`rust_check.py` 的 17 个根常量比对两边同改，仍逐条相等。
- README / AGENT.MD 里当前事实的 `%APPDATA%\NameCat` 全部改为 `FileCat`；历史更新记录与「过时清单」引文保留原文。

### 11.9 验证命令（补充）

```bat
py -3 verify/extract_smoke.py          :: PyO3 端到端 31 断言
PYTHONPATH="_vendor;." py -3 verify/extract_ui_smoke.py  :: GUI 离屏 12 组交互（5 页共存/扫描/筛选/折叠/模拟/执行/CSV/撤销/快捷键）
PYTHONPATH="_vendor;." py -3 verify/ui_smoke.py          :: 原 GUI 全流程回归（页面索引已同步到 3/4）
```

### 11.10 已知限制

- `py.detach` 下进度回调的 Python 异常被吞（只影响提示，不影响文件安全）。
- 非 Windows 分支仅保留可编译语义（`MoveFileExW` 是 Windows 专属），项目仍只支持 Windows。
- 清单树用 `QTreeWidget` 承载分组/折叠；上万行时插入成本高于工作台的自绘表，但扫描与执行本身已在后台线程且 Rust 侧节流进度，界面不会卡死。
- 未跑 `build_exe.py` 整包（只在 `--check` 与 stale 覆盖层面验证）；`.pyd` 已就位，打包链路零改动。


### 11.11 3.0.0 界面统一收口（2026-11-15）

**状态反馈统一（不再两套逻辑）**：`ui/base.py` 新增公共 `StatusSlot(QWidget)`，把原先只长在 `WorkspacePage` 里的四态状态机（idle 灰字 / busy 蓝字+旋转图标 / done 绿勾+5 秒淡出 / message 四种语气色且停留）整体搬进来，`count_provider` 由调用方注入条目数，控件不认识 bundle。`WorkspacePage` 删掉重复的 ~160 行实现，保留 `_scan_status_*` 方法名**委托**给 `StatusSlot`（20 处调用点一行没改）；`ExtractPage` 删掉独立 `NoticeBar` 横幅，在工具栏第二行右侧挂同一个 `StatusSlot`。两页反馈方式、颜色、淡出、省略/tooltip 规则完全一致。`verify/extract_ui_smoke.py` 断言 `type(ep.status) is type(page_ws.scan_status)` 且 `not hasattr(ep, "notice")`。

**「执行记录」同时显示两类日志**：`workers/task_workers.py` 新增 `history_all_task(root)`，分别调 `A.logs()`（重命名 / `Name_backup`）与 `EA.list_logs()`（文件提取 / `Extract/Rollback_Log`），归并成同构行并按时间倒序；目录筛选对两类都生效。`ui/pages/history.py` 加「类型」徽章列（文件重命名 / 文件提取，不靠颜色单独表意）、`_result()` 按类型解析 result/state、撤销按钮按 `record_type` 分发到 `W.undo_task` 或 `W.history_extract_undo_task`，撤销后同时让工作台与提取页清单失效。`verify/history_merge_smoke.py` 真跑两类操作并断言同页显示、分类撤销、互相隔离（17 项全通过）。

**预设页 → 独立对话框**：`ui/pages/presets.py` 的 `PresetsPage` 改为 `PresetDialog(QDialog)`（选择 / 编辑 / 删除 / 恢复 / 打开目录；编辑可改名与说明，内置预设编辑后另存为自定义，不改动内置数据）。主窗口删掉预设页，`NAV` 变 4 页（0 文件重命名 / 1 文件提取 / 2 执行记录 / 3 供应商配置），`_goto` 分支、气泡跳转 → 3、`Ctrl+1…4`、帮助文案同步；「加载预设」按钮 `load_preset_requested` 连到 `MainWindow.open_preset_dialog()`，对话框 `applied` 信号回调 `apply_preset()`。`save_preset()` 不再 reload 页面。`verify/ui_smoke.py` 改为构造 `PresetDialog` 并断言信号连到弹窗。

**扫描对象下拉（最终按用户要求回退）**：恢复原样式——无前置标签、宽 148、`ONLY_LABELS` 回到「仅文件 / 仅文件夹 / 仅图片 / 仅视频 / 图片 + 视频」原文案；`ONLY_ORDER` 去掉 `all`，界面不再提供「文件 + 文件夹」同时扫描；默认 `FormState.only = "media"`（图片 + 视频）。`ONLY_LABELS["all"]` 与 `ONLY_CHOICES["all"]`（CLI `--only all`）保留以支持命令行和旧预设读取；`apply_scan_dict()` 反推命中 `all` 时回落到默认档 `media`，避免显示一个下拉里不存在的原始代号。`verify/ui_smoke.py` 第 4a 步钉住这些约束。
