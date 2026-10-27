# NameCat GUI 设计指导文档

> **文档定位**：面向 GUI 设计 / 界面重构 / 二次开发的结构基线。
> 依据当前代码库（`engine/` 11 个模块、`adapters/`、`ui/` 4 页面 + 11 控件 + 5 对话框、`workers/`，共约 12 900 行不含 `_vendor`）逐文件核对写成，**不描述尚未实现的能力**。
>
> **怎么用**：第 2 章查「有哪些功能」，第 3–5 章查「功能长在哪一层、谁调谁」，第 7 章是**设计时的必读项**（控件可用性由状态机决定），第 8–11 章是页面与视觉规范，第 14 章是红线清单。

---

## 目录

1. [产品定位与核心概念](#1-产品定位与核心概念)
2. [功能全景（三级功能树）](#2-功能全景三级功能树)
3. [整体架构与核心分层](#3-整体架构与核心分层)
4. [模块划分与从属关系](#4-模块划分与从属关系)
5. [依赖与调用关系](#5-依赖与调用关系)
6. [数据契约](#6-数据契约)
7. [界面状态机（设计必读）](#7-界面状态机设计必读)
8. [页面与控件规范](#8-页面与控件规范)
9. [视觉规范与设计令牌](#9-视觉规范与设计令牌)
10. [布局与自适应规则](#10-布局与自适应规则)
11. [反馈与文案规范](#11-反馈与文案规范)
12. [导航与快捷键](#12-导航与快捷键)
13. [扩展指南：改功能时 GUI 要同步哪里](#13-扩展指南改功能时-gui-要同步哪里)
14. [设计红线（Checklist）](#14-设计红线checklist)
15. [附录](#15-附录)

---

## 1. 产品定位与核心概念

### 1.1 一句话

**NameCat 是本地批量重命名工具**：扫描文件 / 文件夹 / 图片 / 视频 → 用一条可排序的规则链算出新名字 → 预览并校验 → 确认后落盘 → 可整批撤销。

三个不可动摇的产品特征，决定了 GUI 的骨架：

| 特征 | 含义 | 对界面的硬要求 |
|---|---|---|
| **两阶段** | 「扫描清单」与「按规则算名」是两个独立可停的步骤 | 界面必须有「只扫描」这一中间态，且该态下主按钮改文案 |
| **预览即承诺** | 预览显示的名字必须与落盘的名字逐字一致 | 落盘不重算；界面不得自己推导数字或名字 |
| **可撤销** | 每次执行写日志，撤销按日志逆序还原 | 「执行记录」必须是独立一级页面，而不是弹窗 |

### 1.2 两阶段工作流

```
        ┌────────────── 阶段一：采集（可单独完成） ──────────────┐
拖入 / 选择目录 ──► 扫描 ──► 文件清单（原名，无新名）
                                    │
                                    ▼
        ┌────────────── 阶段二：计算（点「生成预览」） ──────────┐
                              编号 ──► 规则链算名 ──► 两遍校验
                                    │
                                    ▼
                          预览表（原名 → 新名 + 状态）
                                    │
                    勾选排除 ──► 确认框 ──► 落盘 + 写日志 ──► 可撤销
```

### 1.3 核心概念表（GUI 文案应统一使用这些词）

| 概念 | 代码载体 | 用户看到的说法 |
|---|---|---|
| 对象 / 条目 | `engine.context.Item` | 对象、条目、这一项 |
| 扫描对象 | `FormState.selected` / `ScanOptions.selected` | 「扫描对象…」、已选 N 个具体条目 |
| 规则链 | `List[Rule]` | 规则链（顺序即生效顺序） |
| 编号作用域 | `PlanOptions.scope` | 编号范围（整批 / 每文件夹 / 每类型 / 文件夹+类型） |
| 计划 | `engine.plan.Plan` | 预览、预览结果 |
| 计划包 | `PlanBundle` | 三阶段的唯一状态载体 |
| 落盘 / 执行 | `ops.apply()` → `common.execute()` | 执行重命名 |
| 撤销日志 | `<目录>\_rename_logs\rename_*.json` | 执行记录、撤销日志 |
| 预设 | `engine.presets.Preset` | 预设（规则 + 扫描/编号选项，不含路径与密钥） |

---

## 2. 功能全景（三级功能树）

### 2.1 功能域总览

```
NameCat
├─ F1 对象采集（扫描与筛选）        → 工作台 · 扫描区
├─ F2 规则编排（27 种规则 + 链）    → 工作台 · 规则链 + 规则属性
├─ F3 编号与作用域                 → 工作台 · 编号与执行选项
├─ F4 计划生成与校验               → 工作台 · 预览（引擎侧）
├─ F5 预览与选择性执行             → 工作台 · 预览表 + 摘要条
├─ F6 执行落盘（含回滚）           → 工作台 · 执行 / 进度框
├─ F7 历史与撤销                   → 执行记录页
├─ F8 预设管理                     → 预设页 + 工作台保存
├─ F9 LLM 智能精简                 → LLM 设置页 + 工作台 llm 规则
├─ F10 系统与偏好                  → 全局（顶栏 / 侧栏 / 状态栏 / 帮助）
├─ F11 命令行并行入口              → 无界面（CLI）
└─ F12 自检与验证（开发用）        → 无界面（verify/）
```

### 2.2 逐域功能点明细

#### F1 对象采集

| # | 功能点 | 实现位置 | 界面承载 |
|---|---|---|---|
| F1.1 | 选择根目录（可手输） | `ScanToolbar.root_edit` / `_browse()` | 输入框 + 「选择目录」 |
| F1.2 | 直选具体文件（“只要这些”） | `_pick_items()` → `selected` | 「扫描对象…」按钮 |
| F1.3 | 拖放加入（文件/文件夹） | `PathDrop` → `add_paths()` | 预览卡片 + 表格（拖入高亮） |
| F1.4 | 对象类型 6 档 | `ONLY_CHOICES` | 「对象类型」下拉 |
| F1.5 | 递归子目录 + 深度上限 12 | `ScanOptions.recursive/max_depth` | 勾选框 + 深度 SpinBox |
| F1.6 | 包含隐藏项（点名 + Windows 属性） | `include_hidden` | 勾选框（带 tooltip 说明） |
| F1.7 | 名称包含 / 排除文本过滤 | `only_match` / `exclude_match` | 高级筛选面板 |
| F1.8 | 仅这些扩展名 / 排除扩展名 | `include_exts` / `exclude_exts` | 高级筛选面板（逗号串） |
| F1.9 | 文件头魔数校验 | `MAGIC` / `check_file_magic()` | 「校验文件头（魔数）」 |
| F1.10 | 排序（10 档） | `SORT_MODES` | 「排序方式」下拉 |
| F1.11 | 扫描结果计数与被过滤提示 | `ScanResult.skipped` | 工具栏计数 + NoticeBar |
| F1.12 | 自动跳过 `_rename_logs` 目录 | `LOG_DIR_NAMES` | 无控件（隐式保护） |

#### F2 规则编排

| # | 功能点 | 实现位置 | 界面承载 |
|---|---|---|---|
| F2.1 | 27 种规则（增删 / 切取 / 修饰 / 编号 / 模板 / 智能） | `RULE_KINDS` | 规则选择弹窗（分组 + 搜索） |
| F2.2 | 规则链串行生效（顺序即语义） | `apply_chain()` | 可拖拽排序的列表 |
| F2.3 | 单条启停 | `Rule.enabled` | 行内勾选框（**唯一入口**） |
| F2.4 | 添加 / 删除 / 复制 / 上移下移 / 拖拽 | `move_rule()`、`clone_rule()` | 行内图标按钮 + Delete 键 |
| F2.5 | 参数动态表单（5 类控件） | `KIND_FIELDS` | 规则属性面板 |
| F2.6 | 统一选择器（6 种作用范围） | `SELECTOR_KINDS` | 属性面板下半区 |
| F2.7 | 出现次数（第 1/2/3/最后） | `Rule.occurrence` | 选择器内下拉 |
| F2.8 | 排除扩展名 / 保留分隔符（按规则类型开放） | `KIND_FLAGS` | 属性面板开关 |
| F2.9 | 即时参数校验（阻断） | `rule_problems()` | 生成预览前 NoticeBar |
| F2.10 | 参数提醒（不阻断） | `rule_warnings()` | 属性面板提示文字 |
| F2.11 | 模板编辑器（变量参考 + 实时渲染） | `TemplateEditorDialog` + `render()` | 「模板原文」右侧按钮 |
| F2.12 | 规则摘要一句话 | `Rule.describe()` | 列表行副标题 |

#### F3 编号与作用域

| # | 功能点 | 参数 |
|---|---|---|
| F3.1 | 起始值 / 步长 | `start` / `step` |
| F3.2 | 补零宽度（0 = 不补） | `width` |
| F3.3 | 编号范围 4 档 | `scope` = global / folder / kind / folder_kind |
| F3.4 | 倒序编号 | `reverse` |
| F3.5 | 扩展名是否参与规则链 | `split_ext` |
| F3.6 | 是否同时重命名文件夹 | `rename_dirs`（选「文件夹 / 全部」时自动开） |

#### F4 计划生成与校验（引擎侧，界面只展示结果）

| # | 功能点 | 实现 |
|---|---|---|
| F4.1 | 按作用域分配 `index/num/width`（文件夹深的先编号） | `assign_numbers()` |
| F4.2 | 规则链算名（空链 = 保持原名） | `compute_names()` → `apply_chain()` |
| F4.3 | 父子同时改名的路径修正 | `finalize()` |
| F4.4 | 两遍校验：语法 + 批内重名 → 盘上占用 | `validate_items()` |
| F4.5 | 链式改名不误判（A→B、B→C） | `validate_items()` 的 `leaving` 集合 |
| F4.6 | 警告汇总 | `Plan.warnings` |

#### F5 预览与选择性执行

| # | 功能点 | 实现 | 界面承载 |
|---|---|---|---|
| F5.1 | 8 列预览表（勾选/序号/类型/原名/新名/所在文件夹/状态/说明） | `PreviewModel` | 预览卡片 |
| F5.2 | 单行勾选 / 空格切换 / 双击切换 | `setData()` | 表格 |
| F5.3 | 全选 / 全不选 | `_check_all()` | 表头右侧按钮 |
| F5.4 | 右键菜单 7 项（复制新名/原名/完整路径、切换勾选、仅保留所选、全部勾选、查看该项说明） | `PreviewTable._menu()` | 右键 |
| F5.5 | 摘要 6 个统计 chip | `PlanSummaryBar` | 底部条 |
| F5.6 | 导出 CSV（8 列，UTF-8-SIG） | `ops.export_csv()` | 「导出 CSV」 |
| F5.7 | 过期提醒（配置改动后） | `stale` | 摘要条文案 |

#### F6 执行落盘

| # | 功能点 | 实现 |
|---|---|---|
| F6.1 | 执行前确认（真实统计 + 冲突项可勾「跳过不覆盖」） | `ConfirmDialog` |
| F6.2 | 三级策略：直改 / 临时名中转 / 失败回滚 | `common.execute()` |
| F6.3 | 文件夹强制走中转（父子同时改名） | 同上 |
| F6.4 | 权限/占用重试（3 次，指数退避） | `rename_with_retry()` |
| F6.5 | 进度反馈（引擎真实 current/total） | `ProgressDialog` |
| F6.6 | 写撤销日志（schema 4，最多保留 20 份） | `save_log()` |

#### F7 历史与撤销

| # | 功能点 | 实现 |
|---|---|---|
| F7.1 | 读日志列表（6 列） | `A.logs()` → `C.list_logs/load_log` |
| F7.2 | 日志详情（含 old → new 明细） | `DetailDialog` |
| F7.3 | 撤销（父项先还原、目标已存在则跳过、全成才删日志） | `C.undo()` |
| F7.4 | 临时名残留检查（`__nk_tmp_` 前缀） | `find_temp_leftovers()` |
| F7.5 | 打开日志目录 / 刷新 / 用工作台目录 | `os.startfile` + 跨页契约 |

#### F8 预设管理

| # | 功能点 | 实现 |
|---|---|---|
| F8.1 | 内置 4 个预设（照片序号四位 / 去尾部圆括号序号 / 首尾去空格 / 小写+去非法字符） | `PS.BUILTIN` |
| F8.2 | 自定义预设列表（`%APPDATA%\NameCat\presets\*.json`） | `list_presets()` |
| F8.3 | 应用（覆盖工作台规则与选项） | `apply_preset()` |
| F8.4 | 删除 / 打开预设目录 / 刷新 | `delete_preset()` |
| F8.5 | 保存为预设（工作台按钮 + Ctrl+S） | `collect_preset()` → `save_preset()` |

#### F9 LLM 智能精简

| # | 功能点 | 实现 |
|---|---|---|
| F9.1 | 作为一条规则参与规则链（`kind="llm"`） | `apply_rule("llm")` |
| F9.2 | 三项可编辑参数：整理要求 / 输出语言 / 最大长度 | `KIND_FIELDS["llm"]` |
| F9.3 | 整批一次请求（20 条/批），失败保持原名 | `LLMClient.plan()` |
| F9.4 | 客户端类型：HTTP 接口 / Ollama / 离线替身(Echo) / 不可用 | `make_client()` |
| F9.5 | 配置明细展示（8 项，密钥只显示「已配置（已遮蔽）」） | `llm_settings.reload()` |
| F9.6 | 环境变量来源（9 组，只显示变量名 + 是否已设置） | `llm_env_sources()` |
| F9.7 | 本次运行覆盖参数（provider / model / batch） | `overrides()` → 工作台 `_client()` |
| F9.8 | 要求文件读写 / 按默认创建 / 用系统编辑器打开 | `llm_read/write/ensure_instruction()` |
| F9.9 | 命名风格 5 档（short/natural/kebab/snake/camel） | `LLM.STYLES`（当前仅引擎侧，界面未开放） |

#### F10 系统与偏好

| # | 功能点 | 实现 |
|---|---|---|
| F10.1 | 主题（单套浅色令牌 + QSS 占位替换） | `theme.qss()` |
| F10.2 | 窗口基准尺寸与最小尺寸 | `theme.WINDOW` |
| F10.3 | 侧栏折叠 / 展开 | `set_nav_collapsed()` |
| F10.4 | 分栏比例记忆（规则区 : 预览区） | `prefs["split_rules2"/"split_editor2"]` |
| F10.5 | 上次目录恢复 | `prefs["last_root"]` |
| F10.6 | 帮助对话框（快捷键 + 各数据目录真实路径） | `show_help()` |
| F10.7 | 偏好落盘（白名单 + 禁存密钥） | `ui/settings.py` |
| F10.8 | 旧数据目录单向迁移（NameKit → NameCat） | `migrate_legacy_appdata()` |

#### F11 / F12（无界面）

| 域 | 内容 |
|---|---|
| F11 CLI | `rename` / `preview` / `undo` / `rules` / `presets`；默认只预览，`--apply` 才落盘；`--rule 名称=参数=参数`；`--limit` / `--csv` / `--scope` / `--llm-*` |
| F12 自检 | `verify/run_tests.py`（引擎 + 端到端）、`verify/gui_smoke.py`（离屏 GUI）、`verify/check_icons.py`（图标名）、`verify/gui_shots.py`（四页截图） |

### 2.3 功能 ↔ 页面矩阵

| 功能域 | 工作台 | 预设 | 执行记录 | LLM 设置 | 全局 |
|---|:--:|:--:|:--:|:--:|:--:|
| F1 对象采集 | ● 主 | | | | |
| F2 规则编排 | ● 主 | ○ 应用落位 | | ○ llm 参数 | |
| F3 编号与作用域 | ● 主 | ○ 随预设 | | | |
| F4 计划生成 | ● 主 | | | | |
| F5 预览与选择 | ● 主 | | | | |
| F6 执行落盘 | ● 主 | | ○ 结果回溯 | | |
| F7 历史与撤销 | | | ● 主 | | |
| F8 预设 | ○ 存 / 载 | ● 主 | | | |
| F9 LLM | ○ 规则使用 | | | ● 主 | |
| F10 系统偏好 | | | | ○ 覆盖参数 | ● 主 |

● 主承载　○ 参与

---

## 3. 整体架构与核心分层

### 3.1 六层结构

```
┌──────────────────────────────────────────────────────────────────────┐
│ L0 入口层        main.py ──► ui/app.py（GUI） │ engine/cli.py（CLI）  │
├──────────────────────────────────────────────────────────────────────┤
│ L1 表现层  ui/                                                        │
│    main_window（顶栏 + 侧栏 + 页面栈 + 状态栏）                        │
│    pages/    workspace │ presets │ history │ llm_settings            │
│    widgets/  scan_toolbar rule_list rule_editor plan_options          │
│              preview_table plan_summary drop_area common             │
│    dialogs/  rule_picker template_editor common_dialogs               │
│    styles/   theme（令牌）+ light.qss     icons（44 个内联 SVG）       │
│    settings  偏好白名单落盘                                           │
├──────────────────────────────────────────────────────────────────────┤
│ L2 适配层  adapters/engine_adapter.py  ← 界面与引擎的唯一通道          │
│    FormState ⇄ ScanOptions/PlanOptions/Rule                          │
│    PlanBundle（scan + plan + state）· 状态文案 · 预设/日志/LLM 封装    │
├──────────────────────────────────────────────────────────────────────┤
│ L3 并发层  workers/task_workers.py                                    │
│    BaseTask(QRunnable) + TaskRunner(QThreadPool)                     │
│    progress / done / failed / finished 四类信号回主线程                │
├──────────────────────────────────────────────────────────────────────┤
│ L4 引擎层  engine/（纯逻辑，零 Qt 依赖）                               │
│    ops 高层动作 │ scan 采集 │ plan 编号+算名 │ rules 规则链            │
│    template 模板/选择器 │ context 变量表 │ llm 大模型                  │
│    common 校验/落盘/日志/撤销 │ presets 预设 │ cli 命令行              │
├──────────────────────────────────────────────────────────────────────┤
│ L5 资源层  文件系统 + %APPDATA%\NameCat + _vendor(PySide6) + assets    │
└──────────────────────────────────────────────────────────────────────┘
```

### 3.2 唯一硬规则

> **`ui/` 不许直接 `import engine`。**

界面需要的一切都必须经过 `adapters.engine_adapter` 转一手。目的：引擎是命令行与界面共用的，两边行为必须永远一致；界面里一旦出现第二份「怎么算名字/怎么判冲突」的逻辑，两个入口就会走岔。

> **`engine/` 不许 `import PySide6`。**

`engine/` 里任何模块都不能出现 Qt，否则 CLI 与无界面测试都跑不起来。

（实际代码中存在 6 处**已知例外**：`ui/settings.py` 取 `DATA_DIR_NAME/LEGACY_DATA_DIRS`；`ui/pages/workspace.py`、`ui/widgets/rule_list.py`、`ui/widgets/rule_editor.py`、`ui/dialogs/rule_picker.py` 取 `engine.rules`（构造/归一化 `Rule`、读 `RULE_KINDS`）；`ui/dialogs/template_editor.py` 取 `engine.template`（`REF_KEYS` 等常量）。它们只读常量与纯数据结构，不参与任何算法；**新增代码应优先走 `A.new_rule()` / `A.clone_rule()` / `A.*` 这类适配函数，不要扩大例外面**。）

### 3.3 分层职责与依赖许可

| 层 | 目录 | 允许依赖 | 明确禁止 |
|---|---|---|---|
| L0 入口 | `main.py`、`ui/app.py` | 一切 | 承载业务逻辑 |
| L1 表现 | `ui/**` | L1 内部、L2 适配层、`ui/icons`、`ui/styles` | 直接 import `engine.*`、直接读写文件系统改名、自己算统计 |
| L2 适配 | `adapters/` | L4 引擎、标准库 | import Qt、实现重命名算法 |
| L3 并发 | `workers/` | L2 适配层、QtCore | 在 `run()` 里碰任何 QWidget |
| L4 引擎 | `engine/**` | 标准库 + 同层模块 | import Qt、读界面状态 |
| L5 资源 | 文件系统 / APPDATA | — | 被 L4 之外的层直接拼路径 |

### 3.4 双入口，同一引擎

```
python main.py            ──┐
python main.py --gui      ──┼──► ui/app.py ──► ui/main_window ──► pages
python main.py gui        ──┘                                        │
                                                                     ▼
python main.py preview …  ──► engine/cli.py ──────────────► engine/ops.py ──► engine/*
python main.py rename  …  ──► engine/cli.py ──────────────────────────────┘
python main.py undo    …  ──► engine/cli.py ──► engine/common.undo
```

- `main.py` 的 `_wants_gui(argv)`：**无参数 / `--gui` / `-g` / `gui`** 才开界面，其余全部转交 `engine.cli.main()`。
- GUI 与 CLI 的**唯一差异**：GUI 把扫描与算名拆成两次可停任务（`scan_only` → `build_plan`），CLI 一次走完（`build` / `scan_and_plan`）。

---

## 4. 模块划分与从属关系

### 4.1 目录树与单文件职责

```
NameCAT/
├─ main.py                    55   入口分流（GUI / CLI）
├─ namecat.bat / NameCat.vbs       启动脚本（保留 / 无控制台）
├─ gui_state.json                  偏好兜底落盘位置
├─ requirements.txt
├─ _vendor/                        内联 PySide6（启动时插到 sys.path 最前）
├─ assets/                         app_icon.png / .ico
│
├─ engine/                         纯逻辑层（无 Qt）
│  ├─ __init__.py            109   常量、数据目录、迁移、5 个状态码
│  ├─ scan.py                405   扫描/过滤/排序 → Item 列表
│  ├─ plan.py                362   编号 → 算名 → 校验 → Plan
│  ├─ rules.py               737   27 种规则 + 规则链执行
│  ├─ template.py            443   模板渲染、选择器、旧语法兼容
│  ├─ context.py             340   模板变量表 NameContext、Item
│  ├─ llm.py                 701   大模型精简（配置/客户端/净化）
│  ├─ common.py              510   名字校验、两遍冲突、落盘、日志、撤销
│  ├─ ops.py                 129   高层动作：preview/apply/undo/export_csv
│  ├─ presets.py             125   预设存取 + 4 个内置预设
│  └─ cli.py                 321   命令行子命令
│
├─ adapters/
│  └─ engine_adapter.py      843   FormState/PlanBundle + 全部界面侧封装
│
├─ workers/
│  └─ task_workers.py        122   QRunnable 包装 + 线程池 + 7 个任务工厂
│
├─ ui/                             表现层
│  ├─ app.py                  70   QApplication / 主题 / 主窗口
│  ├─ main_window.py         370   四页导航、跨页契约、偏好、快捷键
│  ├─ settings.py            121   偏好白名单 + 禁存键
│  ├─ icons.py               208   44 个内联 SVG 图标
│  ├─ styles/theme.py        112   COLORS/SIZES/FONTS/TONES/WINDOW + qss()
│  ├─ styles/light.qss      12.8KB  样式表（{{token}} 占位）
│  ├─ pages/
│  │  ├─ workspace.py        864   重命名工作台（主力页面）
│  │  ├─ presets.py          230   预设管理
│  │  ├─ history.py          410   执行记录与撤销
│  │  └─ llm_settings.py     463   LLM 配置 + 要求文件编辑
│  ├─ widgets/
│  │  ├─ common.py           420   Badge/SectionTitle/Hint/PageHeader/按钮/空状态/提示条/卡片
│  │  ├─ scan_toolbar.py     329   扫描工具栏 + 高级筛选 + add_paths
│  │  ├─ rule_list.py        235   规则链列表（拖拽排序）
│  │  ├─ rule_editor.py      389   动态规则属性面板 + 选择器编辑器
│  │  ├─ plan_options.py     178   编号与执行选项（可折叠）
│  │  ├─ preview_table.py    378   预览表（Model + View + 右键菜单）
│  │  ├─ plan_summary.py     228   统计 chip + 三按钮 + 窄窗自适应
│  │  └─ drop_area.py         93   拖放事件过滤器
│  └─ dialogs/
│     ├─ rule_picker.py      143   规则选择（分组 + 搜索）
│     ├─ template_editor.py  143   模板编辑器
│     └─ common_dialogs.py   169   确认框 / 进度框 / 详情框
│
└─ verify/                         自检（开发用，不参与运行）
   ├─ run_tests.py          1187   引擎 + 端到端 + 回归
   ├─ gui_smoke.py           578   离屏 GUI 冒烟
   ├─ gui_shots.py           120   四页渲染 PNG
   ├─ check_icons.py          74   图标名一致性
   └─ make_icon.py           189   PNG/ICO 生成
```

### 4.2 引擎层内部依赖（谁 import 谁）

```
        __init__.py（常量/数据目录）
             ▲   ▲   ▲
             │   │   └────────────── presets.py ──► plan.py, scan.py, rules.py
             │   └────────────────── common.py ──► rules.py（非法字符表）, scan.py(nkey)
             │
   context.py ◄── template.py ◄── rules.py ◄── plan.py ◄── ops.py ◄── cli.py
        ▲                            ▲            ▲          ▲
        └──────── scan.py ───────────┴────────────┴──────────┘
                                     llm.py ──► （被 rules.py 的 llm 分支调用）
```

要点：

- `context.py` 是最底层（`Item` + 变量表 + `split_ext` 单一实现），被 `scan` / `template` / `rules` 共用。
- `template.py` 只依赖 `context`，是纯函数模块。
- `rules.py` 依赖 `context` + `template` + `llm`（llm 分支）。
- `plan.py` 依赖 `rules` + `template` + `common`（校验）+ `scan`（类型常量）。
- `ops.py` 是**面向调用方的高层门面**：`preview / apply / undo / export_csv / scan_and_plan`。命令行直接调它，界面经适配层调它。

### 4.3 界面层组合关系（从属）

```
NameCatWindow (QMainWindow)
├─ AppHeader（logo / 标题 / 版本 / 帮助按钮）
├─ Sidebar（4 个 NavItem + 折叠按钮）
├─ QStackedWidget
│   ├─ WorkspacePage ◄────────── 最大从属树
│   │   ├─ PageHeader + 加载/保存预设
│   │   ├─ NoticeBar
│   │   ├─ ScanToolbar ────────► 高级筛选面板（折叠）
│   │   ├─ QSplitter(Vertical)
│   │   │   ├─ 规则区 ── QSplitter(Horizontal)
│   │   │   │            ├─ 卡片「规则链」→ RuleList + EmptyState
│   │   │   │            └─ 卡片「规则属性」→ RuleEditor(SelectorEditor + 动态字段)
│   │   │   │           └─ PlanOptionsPanel（可折叠，跨两列）
│   │   │   └─ 卡片「预览」→ 表头(全选/全不选) + PreviewTable / EmptyState
│   │   └─ PlanSummaryBar（6 chip + 摘要 + 预览/CSV/执行）
│   ├─ PresetsPage      （PageHeader + NoticeBar + 表格卡片 + Hint）
│   ├─ HistoryPage      （PageHeader + NoticeBar + 目录行 + 表格卡片 + Hint）
│   └─ LLMSettingsPage  （PageHeader + NoticeBar + 滚动区(配置双列 + 要求文件) + 保存栏）
└─ QStatusBar（左：状态消息　右：当前根目录）
```

共用元件（全部来自 `ui/widgets/common.py`）：`PageHeader`、`SectionTitle`、`Hint`、`MonoLabel`、`Badge`、`NoticeBar`、`EmptyState`、`Divider`、`card()`、`page_card()`、`table_card()`、`hline()`、`tool_button()`、`ghost_button()`、`icon_button()`、`compact_button()`、`SwitchRow`、`ToggleSwitch`、`combo_items()`、`qcolor()`。

### 4.4 适配层：界面需求 → 适配函数 → 引擎函数

| 界面要做的事 | 适配函数（`engine_adapter`） | 最终落到 |
|---|---|---|
| 只扫描 | `scan_only(state)` | `scan.scan(ScanOptions)` |
| 按规则算名 | `build_plan(bundle, rules, client)` | `plan.build()` |
| 一步到位（保留） | `build(state, rules)` | `scan` + `plan.build` |
| 执行落盘 | `apply_plan(bundle, rules, client)` | `ops.apply(..., plan=)` |
| 撤销 | `undo(log_path)` | `common.undo` |
| 读日志 | `logs(root)` | `common.list_logs/load_log/log_pairs` |
| 残留检查 | `temp_leftovers(root)` | `common.find_temp_leftovers` |
| 导出 CSV | `export_csv(plan, path)` | `ops.export_csv` |
| 新建 / 复制规则 | `new_rule(kind)` / `clone_rule(rule)` | `rules.make_rule/normalize` |
| 规则字段与开关 | `rule_fields()` / `rule_flags()` / `get_field/set_field` | `KIND_FIELDS` / `KIND_FLAGS` |
| 规则校验 | `rule_problems()` / `rule_warnings()` | 语义同 `rules` 分支 |
| 规则摘要 | `rule_summary()` / `describe_chain()` | `Rule.describe()` |
| 模板变量参考 | `template_variables()` / `render_preview()` | `NameContext.variables()` / `template.render` |
| 预设 CRUD | `builtin_presets/preset_paths/load_preset/save_preset/delete_preset` | `presets.*` |
| LLM 客户端 | `llm_config/llm_client/llm_env_sources/llm_*instruction` | `llm.*` |
| 状态文案 / 语气 | `status_text()` / `status_tone()` | `STATUS_TEXT` |
| 勾选排除 | `set_excluded` / `is_excluded` / `clear_exclusions` | 直接改 `Item.status` |
| 枚举给下拉框 | `sort_modes()` / `scope_text()` / `kinds_for()` / `ONLY_LABELS` | `SORT_MODES` / `SCOPE_TEXT` / `ONLY_CHOICES` |

> **设计含义**：界面里出现的每一个下拉选项、每一句状态文案，都应该有适配层函数作为来源。**不要在下拉框里硬编码选项**——加一种排序方式时，引擎改一张表，界面自动多一项。

---

## 5. 依赖与调用关系

### 5.1 主调用链（扫描）

```
用户点「开始扫描」/ F5 / 拖入后自动
  └─ WorkspacePage.scan()
       ├─ _collect_state(need_rules=False)      ← 只校验 root 存在，不要求规则
       ├─ W.scan_task(state)                    ← BaseTask(A.scan_only, state)
       └─ runner.submit(task)  ──► 线程池 ──► A.scan_only(state)
                                                └─ scan.scan(ScanOptions) → ScanResult(items)
       ◄── signals.done(bundle) ──► _scan_done(bundle)
             ├─ table.model_.set_items(items, plan_ready=False)
             ├─ toolbar.set_count("已扫描 N 项")
             ├─ _refresh_summary()   → 摘要各状态 0，只给 total
             └─ NoticeBar「已扫出 N 项，点「生成预览」」
       ◄── signals.finished ──► _scan_finished()
             └─ 若 _plan_after_scan：QTimer.singleShot(0, _continue_plan_after_scan)
```

### 5.2 主调用链（生成计划）

```
「生成预览」/ Ctrl+Enter / 摘要条主按钮
  └─ request_preview()  ──► 没扫过 → scan()；扫过 → generate_plan()
       ├─ _collect_state(need_rules=True)   ← 至少一条规则 + 逐条 rule_problems 校验
       ├─ _client()                          ← 只有存在 llm 规则才造客户端（跨页契约 llm_overrides）
       ├─ W.build_plan_task(bundle, rules, client)
       └─ runner.submit  ──► A.build_plan(bundle, rules, client)
                              ├─ clear_exclusions(items)      ← 清上一轮勾选痕迹
                              └─ plan.build(items, rules, PlanOptions(dry_run=True), client)
       ◄── done(bundle) ──► _plan_done(bundle)
             ├─ set_items(plan_ready=True) + set_all_checked(True) + clear_exclusions
             ├─ warnings 有则 NoticeBar(warn)
             └─ _refresh_summary()  → 真实统计 + 三按钮复位
```

### 5.3 主调用链（执行）

```
「执行重命名」
  └─ confirm_apply()
       ├─ _apply_selection()            ← 勾选 → A.set_excluded(plan, excluded)
       ├─ stats 全 0 则拦下
       ├─ ConfirmDialog（真实统计；有冲突/无效时显示「跳过不覆盖」）
       └─ _run_apply()
            ├─ ProgressDialog（模态、不可关窗、说明「不提供强制中断」）
            ├─ W.apply_task(bundle, rules, client)
            └─ A.apply_plan → ops.apply(items, rules, opts(dry_run=False), plan=bundle.plan)
                              └─ common.execute() → save_log() → <root>\_rename_logs\
            ◄── done(result) ──► _apply_done()
                  ├─ ProgressDialog.finish(标题, 明细 + 撤销日志路径 + 失败项)
                  ├─ NoticeBar(success / error)
                  └─ stale = True（磁盘已变，旧预览必然过期）
```

### 5.4 信号连接表（谁发 → 谁接）

| 发送方 | 信号 | 接收方 |
|---|---|---|
| `ScanToolbar` | `scan_requested` | `WorkspacePage.scan` |
| `ScanToolbar` | `only_changed(str)` | `_on_only_changed`（自动开「重命名文件夹」） |
| `ScanToolbar` | `state_changed` | `_on_state_changed` → 回写 `FormState` → 标脏 |
| `RuleList` | `edit/remove/clone/toggle/order_changed` | 对应 `_` 方法 |
| `RuleList` | `currentRowChanged` | `_current_rule_changed` → 刷新属性面板 |
| `RuleEditor` | `changed` | `_on_rule_edited` → 刷新摘要 → 标脏 |
| `RuleEditor` | `open_template` | `_open_template_editor` |
| `PlanOptionsPanel` | `changed` | `_on_state_changed` |
| `PreviewModel` | `check_changed` | `_on_check_changed` → `_apply_selection` |
| `PlanSummaryBar` | `preview_requested / csv_requested / apply_requested` | `_on_preview_requested` / `export_csv` / `confirm_apply` |
| `PathDrop` | `paths_dropped(list)` / `drag_active(bool)` | `_drop_paths` / `_on_drag_active` |
| 所有页面 | `status_message(str)` | `NameCatWindow.show_status` |
| `PresetsPage` | `apply_requested(Preset)` | `NameCatWindow._apply_preset` → 工作台 + 切页 |
| `WorkspacePage` | `load_preset_requested` | `_goto("presets")` |
| `WorkspacePage` | `save_preset_requested` | `NameCatWindow.save_preset`（Ctrl+S 同路径） |
| `BaseTask.signals` | `progress / done / failed / finished` | 页面对应槽 |

### 5.5 跨页契约（主窗口提供，页面只按名字取）

| 契约 | 调用方 | 返回 |
|---|---|---|
| `llm_overrides()` → `(provider, model, batch)` | 工作台 `_client()` | 空串 / 0 表示沿用环境变量 |
| `workspace_root()` | 执行记录页「用工作台目录」 | 工作台当前 `state.root` |

> GUI 重构时这两个契约必须保留等价物：**LLM 配置只在一处编辑、目录只有一个真相**。

### 5.6 线程模型

| 事实 | 设计含义 |
|---|---|
| 每个页面自带 `TaskRunner`（`max_threads=2`） | 同一页面同一时刻只跑一件事；按钮在 `busy()` 时置灰 |
| `BaseTask.run()` 只调引擎函数，异常转 `failed(str)` | 界面永远能拿到可显示的错误文本，不会静默 |
| `finished` 在线程归还线程池**之前**发出 | 「扫描后自动算名」必须 `QTimer.singleShot(0, …)` 延后提交 |
| **不提供取消** | 落盘/回滚没有安全中断点 → 界面不画取消按钮，改为明确说明 |
| 关窗前 `runner.wait(3000)` | 避免回调已销毁的控件 |

---

## 6. 数据契约

### 6.1 `FormState`（界面唯一真相，`adapters/engine_adapter.py`）

| 分组 | 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| 扫描 | `root` | str | `""` | 根目录 |
| | `only` | str | `file` | `all/file/dir/image/video/media` |
| | `recursive` | bool | False | 递归 |
| | `include_hidden` | bool | False | 含隐藏 |
| | `only_match` / `exclude_match` | str | `""` | 名称包含 / 排除 |
| | `include_exts` / `exclude_exts` | str | `""` | 逗号串，`jpg, .png` |
| | `verify_content` | bool | False | 魔数校验 |
| | `sort_mode` | str | `name_asc` | 10 档 |
| | `max_depth` | int | 12 | 1–12 夹紧 |
| | `selected` | tuple[str] | `()` | 「扫描对象」绝对路径 |
| 计划 | `start` / `step` / `width` | int | 1 / 1 / 0 | 编号 |
| | `scope` | str | `global` | 4 档 |
| | `reverse` | bool | False | 倒序 |
| | `split_ext` | bool | True | 扩展名参与规则链 |
| | `rename_dirs` | bool | False | 同时改文件夹名 |

序列化：`scan_dict()` / `plan_dict()` → 预设 JSON；`apply_scan_dict()` / `apply_plan_dict()` ← 预设。

### 6.2 `PlanBundle`（一次扫描/计划的答案包）

| 属性 | 未扫描 | 只扫描 | 已算名 |
|---|:--:|:--:|:--:|
| `has_scan` | ✗ | ✓ | ✓ |
| `has_plan` | ✗ | ✗ | ✓ |
| `items()` | `[]` | 扫描项（`status` 全是默认 `ok`） | 计划项（真实状态） |
| `stats()` | 全 0 | 仅 `total` 有值，其余 0 | 5 个状态 + total 全真实 |

> **这是 GUI 最重要的一条数据约束**：`stats()` 在「只扫描」阶段故意不给各状态数字（没算过不能假装有）。界面不得用 `len(items)` 去补状态数。

### 6.3 状态码（贯穿全程序，颜色与文案都由它派生）

| 码 | 文案 | 语气 `status_tone()` | 谁能设置 |
|---|---|---|---|
| `ok` | 待重命名 | info | 校验通过且目标可用 |
| `same` | 名称未变 | muted | 算完名与原名相同 |
| `conflict` | 冲突 | warn | 批内重名 / 目标已存在 |
| `error` | 无效 | error | 非法文件名、目录不存在 |
| `skip` | 已跳过 | muted | 用户取消勾选 / 冲突时选「跳过」 |

摘要 chip 的语气略有区别（`plan_summary.py`）：`ok`=success（绿，表示“要动手的量”）、`skip`=info（蓝）、`same`=muted、`conflict`=warn、`error`=error。**stat chip 的 tone 与表格 status 的 tone 不要求一致，但必须都来自 `theme.TONES`。**

### 6.4 `Rule` 结构

```
id        稳定标识（预设、撤销日志、列表选中都靠它）
kind      27 种之一
a / b / c 三段参数（含义由 kind 决定）
enabled   启停
selector  text/after/before/from/until/range
charset   分隔符或区间表达式
occurrence 1/2/3/-1（最后）
skip_ext  排除扩展名
keep_sep  切分时保留分隔符
extras    llm 的 instruction / lang / max_len 等
```

**27 种规则表**（GUI 的属性面板按 `KIND_FIELDS` 自动生成，不要为每种规则手写页面）：

| 分组 | kind | 中文名 | 参数 a / b / c |
|---|---|---|---|
| 增删 | `insert` | 插入文字 | 位置(prefix/suffix/before/after) / 锚点 / 文字 |
| | `delete` | 删除文字 | 要删的文字 |
| | `remove_chars` | 删除指定字符集 | 字符集合 |
| | `replace` | 文本替换 | 查找 / 替换为 |
| | `regex` | 正则替换 | 正则 / 替换为（`\1` 或 `{1}`） |
| 切取 | `keep_after` | 只保留某字符之后 | 分隔符 |
| | `keep_before` | 只保留某字符之前 | 分隔符 |
| | `remove_after` | 删除某字符之后 | 分隔符（可保留分隔符） |
| | `remove_before` | 删除某字符之前 | 分隔符（可保留分隔符） |
| 修饰 | `prefix` | 添加前缀 | 前缀 |
| | `suffix` | 添加后缀 | 后缀（文件加在扩展名前） |
| | `case` | 大小写 | lower/upper/title/sentence/capitalize |
| | `trim` | 去首尾空格与尾点 | 字符集（默认空白） |
| | `collapse` | 合并重复字符 | 字符（如空格） |
| 编号与序列 | `number` | 编号 | 起始 / 补零宽度 / 位置 |
| | `serialize` | 序列化编号 | 起始 / 位数 / 位置 |
| | `pad_numbers` | 数字补零 | 宽度 / 填充字符 |
| | `randomize` | 随机化 | 长度 / 字符集 / 位置 |
| | `date_insert` | 插入日期 | 日期风格 / — / 位置 |
| 模板与规范化 | `template` | 模板 | 模板原文（打开模板编辑器） |
| | `slug` | 规范化 URL 安全名 | 分隔符 |
| | `translit` | 音译转拉丁 | — |
| | `filename_clean` | Windows 文件名清理 | — / 非法字符替换成 |
| | `move` | 移动字符位置 | 起点 / 取几个 / 插到第几位 |
| | `hash` | 追加校验值 | 算法 / 取前 N 位 / 位置 |
| | `extension` | 扩展名 | 新扩展名 / remove·lower·upper |
| 智能 | `llm` | LLM 智能精简 | 整理要求 / 语言 / 最大长度 |

**每种规则开放哪些通用开关**（`KIND_FLAGS`，属性面板据此显隐）：

| 规则 | selector | skip_ext | occurrence | keep_sep |
|---|:--:|:--:|:--:|:--:|
| 默认（大多数） | ✓ | ✓ | ✓ | |
| `template` / `llm` | ✓ | | ✓ | |
| `number` / `serialize` | | ✓ | ✓ | |
| `remove_after` / `remove_before` | ✓ | ✓ | ✓ | ✓ |
| `extension` | | | | |

### 6.5 枚举表（界面下拉选项的唯一来源）

| 枚举 | 数量 | 取值 |
|---|---|---|
| 对象类型 `ONLY_CHOICES` | 6 | 文件+文件夹 / 仅文件 / 仅文件夹 / 仅图片 / 仅视频(图片+视频) / 全部 |
| 排序 `SORT_MODES` | 10 | 名称自然序/倒序、修改时间新旧、创建时间新旧、大小、路径、不改顺序 |
| 选择器 `SELECTOR_KINDS` | 6 | 整个名字 / 某字符之后 / 之前 / 从某字符起 / 到某字符止 / 字符区间 |
| 编号范围 `SCOPES` | 4 | 整批连续 / 每文件夹 / 每类型 / 每文件夹每类型 |
| 规则分组 `RULE_GROUPS` | 6 | 增删 / 切取 / 修饰 / 编号与序列 / 模板与规范化 / 智能 |
| 常用规则 `COMMON_RULES` | 7 | template, replace, regex, case, number, trim, llm |
| 预设（内置） | 4 | 照片序号（四位）/ 去掉末尾圆括号序号 / 首尾去空格并规范空格 / 改为小写并去非法字符 |
| LLM 客户端 | 4 | openai(HTTP) / ollama / echo(离线替身) / unavailable |
| LLM 命名风格 | 5 | short / natural / kebab / snake / camel（引擎已支持，界面未开放） |

### 6.6 文件与目录约定

| 位置 | 内容 | 谁写 |
|---|---|---|
| `%APPDATA%\NameCat\gui.json` | 界面偏好（白名单） | `ui/settings.py` |
| `<程序目录>\gui_state.json` | 偏好兜底 | 同上 |
| `%APPDATA%\NameCat\presets\*.json` | 用户预设 | `engine/presets.py` |
| `%APPDATA%\NameCat\llm_instruction.txt` | 交给模型的整理要求 | `engine/llm.py` |
| `<目标目录>\_rename_logs\rename_*.json` | 撤销日志（schema 4，最多 20 份） | `engine/common.save_log` |
| 临时名 `__nk_tmp_*` | 中转改名残留（可检查） | `engine/common.execute` |
| `%APPDATA%\NameKit\*` | 旧数据，**只读**并单向补齐 | `migrate_legacy_appdata()` |

偏好白名单（`settings.DEFAULTS`）：`last_root / llm_provider / llm_model / llm_batch / llm_instruction_file / nav_collapsed / split_rules / split_editor`。
禁存键（`FORBIDDEN`，命中即丢弃）：`api_key / apikey / key / token / secret / password`。

---

## 7. 界面状态机（设计必读）

### 7.1 三阶段

```
        ┌──────────┐  扫描成功   ┌──────────┐  生成预览   ┌──────────┐
        │  未扫描   │ ─────────► │  只扫描   │ ─────────► │  已算名   │
        └──────────┘             └──────────┘             └──────────┘
             ▲                        │  ▲                    │
             │  改 root/选项需重扫      │  │ 配置改动（stale）   │
             └────────────────────────┘  └────────────────────┘
                                    执行成功后 → 回「已算名但 stale」
```

### 7.2 控件可用性矩阵（实现时必须逐格对齐）

| 控件 / 区域 | 未扫描 | 只扫描 | 已算名 | 已算名但 stale | 执行中 |
|---|:--:|:--:|:--:|:--:|:--:|
| 预览表格 | 隐藏（空状态） | 显示（新名称列 `—`） | 显示 | 显示 | 显示 |
| 空状态文案 | 「拖入文件或文件夹开始」 | — | — | — | — |
| 全选 / 全不选 | 灰 | 可点 | 可点 | 可点 | 灰 |
| 主按钮文案 | 预览变更 | **生成预览** | 重新生成预览 | 重新生成预览 | 预览变更/不变 |
| 主按钮可点 | ✓（会先扫描） | ✓ | ✓ | ✓ | ✗ |
| 导出 CSV | 灰 | **灰** | 按现状可点 | 灰 | 灰 |
| 执行重命名 | 灰 | **灰** | 有 `ok` 项且非 stale 才可点 | 灰 | 灰 |
| 顶部「开始扫描」 | 可点 | 可点 | 可点 | 可点 | 「扫描中…」 |
| 勾选排除是否生效 | — | 只记在表格里，不落 `status` | 生效（`set_excluded`） | 生效 | 生效 |
| 摘要 chip | 全 0 | 仅「对象总数」有值 | 5 状态 + 总数 | 同上 | 冻结 |
| 摘要文案 | 还没有扫描 | 已扫描 N 项，尚未应用规则 | `Plan.summary()` | 「配置已改动，预览已过期 — 请重新生成」 | 保持 |

> 建议：把这 5 列做成设计稿里的 5 个画板，逐格标注按钮状态——这是本项目**最容易画错**的地方。

### 7.3 迁移触发表

| 从 → 到 | 触发 | 代码 |
|---|---|---|
| 未扫描 → 只扫描 | 扫描成功 | `_scan_done` |
| 只扫描 → 已算名 | 生成计划成功 | `_plan_done` |
| 已算名 → 已算名(stale) | 任何配置改动：加/删/改/排序/启停规则、扫描选项、编号选项 | `_mark_stale()` |
| 已算名 → 只扫描 | 重新扫描 | `_scan_done` |
| 已算名 → 已算名(stale) | 执行成功 | `_apply_done`（磁盘变了） |
| 任意 → 未扫描 | 切换目录 / 应用预设（会清空 `state` 与 `bundle` 相关数据） | `apply_preset()` |
| 任意 → 失败提示 | 任务异常 | `_task_failed()` |

### 7.4 stale 的精确语义

- 只有**已经生成过计划**时，配置改动才算「预览过期」；只扫描阶段改规则只是「还没算」，**不能**标脏。
- stale 时：表格内容保留（旧计划仍可见，供参考），但 CSV 与执行置灰，摘要条给出重算提示。

### 7.5 并发与忙碌

- `runner.busy()` 时：`scan()` / `generate_plan()` 直接返回；拖放被拒绝并提示「正在处理上一个任务」。
- 忙碌期间按钮置灰，**但不显示不确定进度条绕过真实进度**：有 `total` 才显示百分比，否则不定态。
- 不提供「取消」。

---

## 8. 页面与控件规范

### 8.1 全局框架

| 区域 | 尺寸 | 内容 | 交互 |
|---|---|---|---|
| 顶栏 `AppHeader` | 高 56 | 应用图标 24 + 「NameCat」+ 版本号 + 右侧「帮助」 | 帮助打开 `DetailDialog` |
| 侧栏 `Sidebar` | 宽 204（折叠 56） | 4 个 `NavItem`（可勾选、互斥）+ 底部「收起」 | 点击切换页面；折叠后只留图标 + tooltip |
| 页面栈 `QStackedWidget` | 自适应 | 4 页 | Ctrl+1…4 |
| 状态栏 | 系统默认 | 左：状态消息（默认「就绪」）；右：当前根目录 | 只读 |

页面切换时的**数据刷新契约**：进「预设」→ `reload()`；进「执行记录」→ 若尚无目录则取 `workspace_root()`；进「LLM」→ `reload()`。

### 8.2 工作台（`WorkspacePage`，5 段竖排）

```
PageHeader「重命名工作台」            [加载预设] [保存为预设]
NoticeBar（可隐藏，四语气）
ScanToolbar
  ├ 行1：根目录输入 ──────────────── [选择目录] [扫描对象…] [开始扫描●]
  ├ 行2：对象类型 ▾  包含子文件夹  包含隐藏项  ▸高级筛选   已扫描 N 项
  └ 高级面板（折叠）：名称包含 / 名称排除 / 仅这些扩展名 / 排除扩展名
                     排序方式 ▾ / 递归深度 / 校验文件头 + 图片与视频扩展名提示
QSplitter(垂直, 比例 0.50，可拖)
  ├ 规则区（最小高 140，内部横向对半分）
  │   ├ 卡片「规则链 N 条」 [添加规则]
  │   │    RuleList（行：勾选 · 序号 · 名称+「已停用」· 参数摘要 · 上/下/复制/删除 · 拖拽把手）
  │   │    或 EmptyState「还没有规则」
  │   └ 卡片「规则属性 · 第 N 条/共 M 条」
  │        RuleEditor（动态字段 + 选择器 + 提示）
  │   └ PlanOptionsPanel（折叠，跨两列；尾部提示「规则自上而下依次作用」）
  └ 卡片「预览 · N 项 · 已勾选 M 项」           [全选] [全不选]
       PreviewTable（8 列，可多选，右键菜单）
       或 EmptyState「拖入文件或文件夹开始」
PlanSummaryBar（固定在底部）
  [对象总数|待重命名|名称未变|冲突|无效|已跳过]  摘要文字  [预览变更][导出 CSV][执行重命名●]
```

**关键交互约定**

| 交互 | 行为 |
|---|---|
| 拖放 | 拖入时预览卡片描边高亮（动态属性 `dropActive`）；松手加入「扫描对象」并自动扫描；**已有规则时顺带算名** |
| 双击表格行 | 切换该行勾选 |
| 空格 | 切换当前行勾选 |
| 规则行勾选 | 只就地刷新该行（不重建列表，否则会销毁正在发信号的行） |
| 切换类型为「文件夹/全部」 | 自动勾上「同时重命名文件夹」 |
| 未选目录就点扫描 | NoticeBar(warn)「还没有选择文件夹」 |
| 规则参数不全 | NoticeBar(error) 逐条列出「规则名：第一个问题」 |
| 扫描被过滤 | NoticeBar(info)「有 N 个对象被过滤掉」 |

### 8.3 预设页

- 结构：`PageHeader`（刷新 / 打开预设目录）+ `NoticeBar` + 表格卡片 + `Hint`。
- 表格 5 列：名称（加粗）/ 来源（内置·自定义·读取失败）/ 规则数 / 说明 / 操作（应用 + 非内置才有删除）。
- 双击行 = 应用。
- 空状态：「没有找到预设 / 在工作台里配好规则后点「保存为预设」」。
- 单个坏文件不拖垮整页：来源列显示「读取失败」，说明列给出异常。

### 8.4 执行记录页

- 结构：`PageHeader`（刷新 / 检查临时残留 / 打开日志目录）+ `NoticeBar` + 目录行（`选择目录` / `用工作台目录`）+ 表格卡片 + `Hint`。
- 表格 6 列：执行时间 / 任务摘要 / 对象数量 / 执行结果（按 summary 着色）/ 日志状态（可撤销·版本未知·无可还原项）/ 操作（查看 + 撤销）。
- 「撤销」在无可还原项时置灰；撤销确认框用 `danger=True`。
- 撤销结果：成功会删日志；失败**保留**日志并列出未完成项。

### 8.5 LLM 设置页

- 结构：`PageHeader`（重新读取 / 打开要求文件）+ `NoticeBar` + 滚动区 + **固定保存栏**（在任何尺寸下都能点到保存）。
- 滚动区内：双列
  - 左列一张卡：状态徽标（HTTP 接口 / 本地模型 / 离线替身 / 不可用）+ 客户端说明 + 不可用原因 + 分隔线 + 「本次运行的覆盖参数」（客户端 ▾ / 每批条数 / 模型名）。
  - 右列两张卡：「生效配置明细」8 项（密钥显示「已配置（已遮蔽）」）+「配置来源（只读取环境变量）」9 项（变量名 + 已设置/未设置徽标，含义放 tooltip）+ 一行取值顺序提示。
- 全宽一张卡：「整理要求文件」路径只读框 + 「按默认要求创建」+ 多行编辑器（等宽字体）+ 注释规则提示。
- 保存栏：左「有未保存的修改 / 与磁盘一致」，右「放弃修改」+「保存要求」（主色）。

> 三条硬规则必须体现在设计上：① 配置只读环境变量，页面**不提供**「记住密钥」；② 密钥任何位置都不回显明文；③ `EchoClient` 必须明确标为「离线替身」，不得伪装成真模型。

### 8.6 对话框清单

| 对话框 | 用途 | 关键点 |
|---|---|---|
| `RulePickerDialog` | 添加规则 | 搜索框 + 左侧分组 + 列表；双击即选；清单来自 `RULE_KINDS` |
| `TemplateEditorDialog` | 编辑模板 | 左编辑 / 右变量参考 / 底部实时渲染结果；变量与示例来自真实实现 |
| `ConfirmDialog` | 危险操作确认 | 真实统计正文；`danger` 主色；可选「遇到目标已存在时跳过（不覆盖）」 |
| `ProgressDialog` | 执行/撤销进度 | 模态、不可关窗；有 total 才显示确定进度；说明「不提供强制中断」；完成后按钮才可用 |
| `DetailDialog` | 帮助 / 日志详情 / 残留列表 / 错误明细 | 等宽只读文本 + 复制 + 关闭；最小 680×480 |

### 8.7 复用元件速查（`ui/widgets/common.py`）

| 元件 | 语义 | 用法 |
|---|---|---|
| `PageHeader(title, subtitle)` | 页面标题区 | 每页顶部，`add_action()` 挂右侧按钮 |
| `SectionTitle` | 区块小标题 | 卡片内、字段标签 |
| `Hint` | 灰色说明文字 | 表单下方、页面底部约束说明 |
| `Badge(text, tone)` | 状态徽标 | 五语气；颜色不是唯一信息载体（文字必在） |
| `NoticeBar` | 页内提示条 | `show_notice(标题, tone, 明细)`，可关闭，同一页只有一个 |
| `EmptyState(标题, 说明, 图标)` | 空态 | 表格/列表无数据时替换 |
| `Divider` | 分隔线 | 卡片内分组 |
| `card()` / `page_card(title)` | 卡片容器（带内嵌标题版） | 工作台三个区、LLM 卡片 |
| `table_card()` | 只装表格的卡（无内嵌标题，表格填满） | 预设、执行记录 |
| `hline()` | 水平细分隔 | 卡片内分组 |
| `tool_button(text, icon, primary)` / `ghost_button` / `icon_button` / `compact_button` | 主按钮 / 次按钮 / 图标按钮 / 小按钮 | 层级固定：一屏只有一个主按钮 |
| `SwitchRow(title, on_text, off_text, checked, hint)` / `ToggleSwitch` | 两态开关 | 属性面板的布尔项 |
| `combo_items(combo, pairs, current)` | 给下拉框批量填 (value, label) | 所有枚举下拉 |
| `qcolor(hex)` | 令牌色 → QColor（给表格着色用） | 状态列、结果列 |

---

## 9. 视觉规范与设计令牌

**令牌唯一来源：`ui/styles/theme.py`；QSS 里用 `{{token}}` 占位；控件代码里不允许写死颜色。**

### 9.1 颜色

| 语义 | 令牌 | 值 |
|---|---|---|
| 背景 | `bg` / `bg_subtle` / `bg_hover` / `bg_selected` / `bg_pressed` | `#FFFFFF` / `#FAFAFA` / `#F5F5F5` / `#F0F0F0` / `#EBEBEB` |
| 文字 | `text` / `text_secondary` / `text_tertiary` | `#171717` / `#737373` / `#A3A3A3` |
| 描边 | `border` / `border_strong` | `#E5E5E5` / `#D4D4D4` |
| 主色 | `primary` / `primary_text` / `primary_hover` | `#171717` / `#FFFFFF` / `#2A2A2A` |
| 语义 | `success` / `warning` / `error` / `info` | `#16A34A` / `#D97706` / `#DC2626` / `#2563EB` |
| 语义浅底 | `success_bg` / `warning_bg` / `error_bg` / `info_bg` | `#F0FDF4` / `#FFFBEB` / `#FEF2F2` / `#EFF6FF` |
| 焦点 | `focus` | `#171717` |

### 9.2 语义语气 `TONES`（(浅底, 前景) 二元组）

| tone | 浅底 | 前景 | 用在哪 |
|---|---|---|---|
| `info` | `#EFF6FF` | `#2563EB` | 状态「待重命名」、已跳过 chip、信息提示 |
| `muted` | `#F5F5F5` | `#737373` | 名称未变、无数据、中性徽标 |
| `warn` | `#FFFBEB` | `#B45309` | 冲突、离线替身、规则参数提醒 |
| `error` | `#FEF2F2` | `#DC2626` | 无效、失败 |
| `success` | `#F0FDF4` | `#15803D` | 执行完成、已设置、待重命名 chip |

### 9.3 尺寸 / 字体 / 窗口

| 令牌 | 值 |
|---|---|
| `radius` / `radius_sm` | 6px / 4px |
| `btn_h` / `input_h` / `row_h` | 34px / 36px / 40px |
| 字体 | `"Segoe UI", "Microsoft YaHei UI", "Noto Sans CJK SC", sans-serif` |
| 等宽 | `"Cascadia Mono", "Consolas", "JetBrains Mono", monospace` |
| 字号 | 正文 13px / 小字 12px / 页面标题 22px |
| 窗口 | 1440×900，最小 1100×700；顶栏 56；侧栏 204；页面边距 20；间隔 16 |

### 9.4 图标表（44 个，内联 SVG，`icons.icon(name, color, size)`）

| 类别 | 名称 |
|---|---|
| 导航 | `workspace` `preset` `history` `spark` `app` |
| 扫描/文件 | `folder` `scan` `file` `image` `video` `sort` `list` `clock` `eye` `eye_off` |
| 规则操作 | `plus` `trash` `copy` `up` `down` `grip` `edit` `template` `check_square` `square` |
| 状态 | `ok` `warn` `error` `info` `skip` `shield` |
| 动作 | `play` `undo` `csv` `save` `open` `refresh` `search` `close` `link` `settings` `help` |
| 导航箭头 | `chevron_down` `chevron_right` |

新增图标：往 `_PATHS` 加 path（24×24 网格、1.5 描边、`stroke=currentColor`），然后跑 `verify/check_icons.py` 校验名字是否都存在。

### 9.5 QSS 约定

- 只用 `{{token}}` 占位，不在 QSS 里写死色值。
- 动态状态用 Qt 动态属性 + `unpolish/polish`，例如拖放高亮：`QFrame#Card[dropActive="true"]`。
- QSS 文件缺失不阻断启动（退化为系统默认样式 + stderr 警告）。

---

## 10. 布局与自适应规则

### 10.1 基准与下限

以 1440×900 设计，**1100×700 必须完全可用**（这是最小尺寸，不是“大概能开”）。设计稿至少要交 1440 与 1100 两档。

### 10.2 工作台纵向分配

| 规则 | 说明 |
|---|---|
| 规则区 : 预览区 = 0.50（初始） | `_split_ratio`，`showEvent/resizeEvent` 时 `_balance_split()` 重新分配 |
| 预览区硬下限 170px | 卡片内边距 22 + 表头 30 + 约 3 行表格 |
| 规则区下限 140px | 窗口变小时**先牺牲规则区**，预览表格不许变成一条缝 |
| 用户拖过分隔条 | 记住比例（`_split_manual`），此后不再自动分配 |
| 规则链 / 属性面板装进滚动区 | 内容再长也不顶住分栏 |

### 10.3 窄窗口降级策略（逐项都要在设计里体现）

| 区域 | 空间不足时 |
|---|---|
| 侧栏 | 折叠到 56px，只留图标（可手动收起，状态持久化） |
| 底部摘要条 | 先隐藏摘要文字（tooltip 保留全文），**右侧三个按钮永远完整** |
| 摘要数字 | 6 个 chip 固定最小宽 58，等距居中 |
| 规则属性面板 | 字段竖排（标签在上、控件在下），不允许标签跑位 |
| LLM 页 | 整体进滚动区；**保存栏固定在滚动区外** |
| 页面副标题 | 允许压缩折行，不顶大最小宽度 |
| 长路径 | 用只读单行框而不是 QLabel（避免整串不可断词撑宽页面）+ tooltip 给全 |

### 10.4 表格列宽策略

| 表格 | 列 | 策略 |
|---|---|---|
| 预览表 | 勾选 34 / 序号 56 / 类型 74 / 状态 104 | Fixed |
| | 原名称 / 新名称 / 说明 | Stretch（三等分剩余空间） |
| | 所在文件夹 200 | Interactive |
| | 行高 34；无网格；无交替色；整行选中；可多选 | — |
| 预设表 | 名称 260 / 规则数 220 / 说明 Stretch / 操作 Fixed（按实际按钮宽度量取 +20） | — |
| 执行记录表 | 时间·数量·日志状态 ResizeToContents / 摘要 Interactive 260 / 结果 Stretch / 操作 ResizeToContents；行高 42 | — |

---

## 11. 反馈与文案规范

### 11.1 四条反馈通道，各司其职

| 通道 | 用在哪 | 位置 | 是否阻断 |
|---|---|---|---|
| **状态栏** | 每次动作的一句话结果（「已扫描 128 项」） | 窗口底部左侧 | 否 |
| **NoticeBar** | 与当前页面上下文相关的提示/错误/成功 | 页内标题下方 | 否（可关闭） |
| **对话框** | 需要用户决策（确认/撤销）或必须读完的明细（错误、日志） | 居中模态 | 是 |
| **进度框** | 长任务（执行、撤销） | 居中模态 | 是 |

四级不可混用：**能用状态栏说清的不要弹框；需要决策的不能用状态栏。**

### 11.2 语气选择

| 情况 | tone |
|---|---|
| 操作成功（已保存、已导出、执行完成） | `success` |
| 中性信息（已扫出 N 项、被过滤 N 个） | `info` |
| 用户可修正的问题（没选目录、参数不全、预览过期） | `warn` |
| 无法继续（目录不存在、导出异常、任务失败） | `error` |

### 11.3 数字与文案的来源规则

1. **统计数字一律来自 `PlanBundle.stats()` / `Plan.report` / `Plan.warnings`**，界面不自己数。
2. **新名字一律来自引擎**，界面不拼、不裁剪、不美化。
3. **下拉选项、规则清单、变量清单**全部从适配层的表里取。
4. 异常必须如实显示类型与消息：`"%s: %s" % (type(exc).__name__, exc)`，不吞异常、不写「未知错误」。
5. 空状态必须给出**下一步动作**（「拖入文件或文件夹开始」而不是「暂无数据」）。

### 11.4 错误处理

| 场景 | 表现 |
|---|---|
| 引擎抛异常（后台任务） | `failed(str)` → NoticeBar(error) + 状态栏 + 进度框 finish(失败) |
| 单条日志读不了 | 该行标「读取失败」，其余照常 |
| 单个预设坏文件 | 该行标「读取失败」+ 异常文本 |
| 目录不存在 | NoticeBar(error) 显示具体路径 |
| 版本未知的日志 | 「日志状态」列显「版本未知」，撤销置灰 |

---

## 12. 导航与快捷键

| 键 | 动作 | 作用域 |
|---|---|---|
| `Ctrl+1` … `Ctrl+4` | 切到第 N 页 | 全局 |
| `Ctrl+O` | 切到工作台并打开目录选择 | 全局 |
| `Ctrl+S` | 保存为预设 | 全局 |
| `Ctrl+Enter` | 切到工作台并按当前配置预览（没扫就扫，扫过就算名） | 全局 |
| `F5` | 工作台重新扫描 | 工作台 |
| `Delete`（含 `Backspace`） | 删除选中的规则 | 规则列表 |
| `空格` | 切换当前预览行勾选 | 预览表 |

侧栏 `NavItem` 的 tooltip 统一为「标题 — 说明（Ctrl+N）」，折叠态只显示标题 tooltip。

---

## 13. 扩展指南：改功能时 GUI 要同步哪里

| 要加的东西 | 引擎侧 | 适配层 | 界面侧 |
|---|---|---|---|
| **新规则类型** | `RULE_KINDS` 加一项 + `transform()` 加分支 | `KIND_FIELDS` 加字段表（决定表单长什么样）、必要时 `RULE_GROUPS` / `DEFAULTS` / `KIND_FLAGS` | 通常**零改动**（选择弹窗与属性面板自动长出） |
| **新扫描选项** | `ScanOptions` 加字段 + `scan()` 过滤链插位置 | `FormState` 加字段 + `scan_options()` 传递 + `scan_dict/apply_scan_dict` | `ScanToolbar` 加控件 + `load_state/save_state` 双向映射 |
| **新编号 / 计划选项** | `PlanOptions` 加字段 + `assign_numbers/compute_names` 用上 | `FormState` 加字段 + `plan_options()` + `plan_dict/apply_plan_dict` | `PlanOptionsPanel` 加控件 + 两个映射方法 |
| **新状态码** | `__init__.py` 的 `STATUS_TEXT` + 校验逻辑 | `status_text/status_tone` + `STATUS_ORDER` | 预览表颜色、摘要 chip、日志结果着色三处 |
| **新页面** | — | 需要的数据先加适配函数 | 新建 `QWidget` → `NAV_ITEMS` 加项（顺序即 Ctrl+N）→ `_build_ui` 入栈 → `_wire` 连线 |
| **新拖放目标** | — | — | `PathDrop(self)` 后 `attach()` 父容器**与**子控件（子控件会先吃掉事件） |
| **改视觉** | — | — | 只改 `theme.py` 的 `COLORS/SIZES/FONTS/TONES/WINDOW` 与 `light.qss` |
| **新图标** | — | — | `icons._PATHS` 加 path → 跑 `check_icons.py` |

**改完必跑**：

```bat
py -3 verify\run_tests.py      :: 引擎 + 端到端 + 回归
py -3 verify\gui_smoke.py      :: 离屏 GUI 冒烟（不需要显示器）
py -3 verify\check_icons.py    :: 图标名一致性
py -3 verify\gui_shots.py      :: 四页渲染 PNG，人工看布局
```

---

## 14. 设计红线（Checklist）

评审设计稿 / 写代码时逐条打勾：

**架构**

- [ ] 界面没有直接 `import engine.*`（一律走 `adapters.engine_adapter`）
- [ ] `engine/` 里没有出现任何 Qt
- [ ] 后台线程只调引擎函数，不碰 QWidget
- [ ] 新增的下拉选项/枚举来自适配层的表，不是界面硬编码

**流程**

- [ ] 「未扫描 / 只扫描 / 已算名 / stale」四态逐一画过，控件可用性对得上第 7.2 节
- [ ] 「只扫描」阶段主按钮文案是「生成预览」，且 CSV / 执行是灰的
- [ ] 执行用的是已有 `plan`，不重算（否则 LLM 请求翻倍、randomize 结果不一致）
- [ ] 执行成功后预览被标为过期
- [ ] 撤销/残留检查有独立入口，不藏在弹窗里

**数据**

- [ ] 统计数字全部来自 `stats()` / `report`，界面不自己数
- [ ] 新名字全部来自引擎，界面不加工
- [ ] 勾选排除用引擎的 `skip` 状态，不另造一套「排除清单」
- [ ] 拖入的路径落进 `FormState.selected`，界面不维护第二份清单

**体验**

- [ ] 每个空状态都给出了下一步动作
- [ ] 长任务有真实进度；没有伪造的取消按钮
- [ ] 错误显示异常类型 + 消息，不写「未知错误」
- [ ] 1100×700 下：三按钮完整、摘要可读（或收进 tooltip）、保存栏可见
- [ ] 危险操作用 `danger` 主色 + 真实统计

**安全与隐私**

- [ ] 任何界面都不回显 API Key 明文，不提供「记住密钥」
- [ ] 偏好落盘只走 `DEFAULTS` 白名单，`FORBIDDEN` 命中即丢
- [ ] `EchoClient` 一律标为「离线替身」
- [ ] 预设不含路径、不含密钥

---

## 15. 附录

### 附录 A 术语与文案对照

| 别写 | 应该写 |
|---|---|
| 文件列表 | 扫描结果 / 对象清单 |
| 规则设置 | 规则链 / 规则属性 |
| 开始改名 | 执行重命名 |
| 回滚 | 撤销（走日志）/ 已回滚（执行内失败自动还原，属引擎内部行为） |
| 缓存 / 临时数据 | 预览（plan）/ 临时名残留 |
| 报错 | 具体原因（含类型与消息） |

### 附录 B 关键函数索引

| 层 | 函数 | 作用 |
|---|---|---|
| 适配 | `scan_only(state)` | 第一阶段：只扫描 |
| | `build_plan(bundle, rules, client)` | 第二阶段：算名 + 校验 |
| | `apply_plan(bundle, rules, client)` | 落盘（复用 plan） |
| | `set_excluded / clear_exclusions` | 勾选排除（借引擎 skip 语义） |
| | `new_rule / clone_rule / move_rule` | 规则对象操作 |
| | `rule_problems / rule_warnings` | 就地校验 |
| | `PlanBundle.stats()` | 唯一统计来源 |
| 引擎 | `scan.scan()` | 采集 |
| | `plan.assign_numbers / compute_names / finalize / build` | 编号 → 算名 → 校验 |
| | `rules.apply_rule / apply_chain / transform` | 单条/整链执行 |
| | `common.validate_items / execute / save_log / undo` | 校验、落盘、日志、撤销 |
| | `ops.preview / apply / undo / export_csv / scan_and_plan` | 高层门面 |
| | `llm.make_client / sanitize_name / build_prompt` | 智能精简 |
| 界面 | `WorkspacePage.scan / generate_plan / confirm_apply / _refresh_summary` | 工作台主流程 |
| | `PlanSummaryBar.set_state(has_plan, can_apply, stale, has_scan)` | 三态总闸 |
| | `NameCatWindow._goto / llm_overrides / workspace_root` | 导航与跨页契约 |

### 附录 C 现状基线（设计验收参考）

| 项 | 现状 |
|---|---|
| 页面 | 4 页（工作台 / 预设 / 执行记录 / LLM 设置） |
| 规则类型 | 27 种，6 个分组 |
| 扫描选项 | 12 项（含 6 档类型、10 档排序） |
| 计划选项 | 7 项（含 4 档编号范围） |
| 预览表 | 8 列，右键 7 项 |
| 摘要 chip | 6 个 |
| 图标 | 44 个内联 SVG |
| 设计令牌 | 颜色 22 / 尺寸 5 / 字体 5 组 / 语气 5 / 窗口 8 |
| 主题 | 单套浅色（`Vercel Minimalism` 基调），QSS 12.8KB |
| 窗口 | 1440×900，最小 1100×700 |

---

*本文档与代码同步维护：改动功能层级、页面结构或设计令牌时，请同时更新对应章节与附录 C 的基线数字。*
