# NameCat 代码结构说明（改代码前先读这一篇）

面向「日后要改这个程序的人」的结构地图：**每个文件负责什么、数据怎么流动、改某个功能要动哪几处**。
不写用户手册，只写代码结构。

- 程序根目录：`NameKit\`
- 启动：`NameCat.vbs`（无控制台）/ `namecat.bat`（保留控制台看报错）/ `python main.py`
- 规模：46 个 `.py`，约 12900 行（不含 `_vendor`）
- 工作流是**两阶段**的：先扫出文件清单，再按规则算新名字（见第 3 节）

> **2026-10 界面改版**：`ui/` 已整体重写为新的桌面界面（设计稿见 `docs/GUI_DESIGN_GUIDE.md`），
> 旧界面（`main_window.py` + `pages/widgets/dialogs` 那套）在 git 历史 `b17b217` 里，需要时取回。
> 新界面的分层是：`ui/base.py`（令牌/字体/图标/基础控件）→ `ui/pages/*`（四个页面，直接调
> `adapters.engine_adapter`）→ `ui/window.py`（窗口骨架与跨页连线）→ `ui/app.py`（启动）。
> 引擎（`engine/`）与适配层（`adapters/`）没有改，第 3 节的数据流照旧成立。

---

## 1. 一句话架构

```
main.py
  ├─ 无参数 / --gui  ──►  ui/app.py  ──►  ui/window.py  ──►  4 个页面
  └─ 其它参数       ──►  engine/cli.py  （命令行，共用同一引擎）
```

**唯一硬规则：`ui/` 不许直接碰 `engine/`。** 界面要的一切都经过 `adapters/engine_adapter.py` 转一手；
`engine/` 是纯逻辑（不 import Qt），命令行和界面共用它，所以两边行为永远一致。

```
ui/  ──►  adapters/engine_adapter.py  ──►  engine/  （纯逻辑，无界面依赖）
                    ▲
           workers/task_workers.py  （把引擎调用丢到后台线程，信号回主线程）
```

---

## 2. 目录与文件清单

### 2.1 根目录

| 文件 | 行 | 职责 |
|---|---|---|
| `main.py` | 55 | 入口分流：`_wants_gui(argv)` 决定开界面还是转交 `engine.cli.main()` |
| `README.md` | — | 本文档 |
| `namecat.bat` / `NameCat.vbs` | — | 启动脚本（自己找 Python，项目内 `_vendor\python` 优先） |
| `gui_state.json` | — | 界面偏好的**兜底**落盘位置；首选写 `%APPDATA%\NameCat\gui.json`（**不存 API Key**，见 `ui/settings.py` 的 `FORBIDDEN`） |
| `_vendor\` | — | 内联依赖（PySide6），`main.py`/`ui/app.py` 启动时插到 `sys.path` 最前 |
| `assets\` | — | `app_icon.png` / `app_icon.ico`（窗口与任务栏图标） |
| `ui\styles\light.qss` | 12.8 KB | 样式表，`{{token}}` 占位由 `theme.tokens()` 替换 |

### 2.2 `engine/` —— 纯逻辑层（无 PySide6 依赖）

| 文件 | 行 | 职责 | 主要符号 |
|---|---|---|---|
| `__init__.py` | 109 | 全局常量、数据目录、状态码 | `APP_NAME` `APP_VER` `ST_OK/SAME/CONFLICT/ERROR/SKIP` `STATUS_TEXT` `LOG_SCHEMA` `appdata_dir()` |
| `scan.py` | 405 | 扫描、过滤、排序 | `ScanOptions`(167) `ScanResult`(199) `scan()`(213) `sort_items()`(376) `nkey()` `kind_of()` |
| `plan.py` | 362 | 编号 → 算名 → 校验成计划 | `PlanOptions`(46) `Plan`(63) `assign_numbers()`(103) `compute_names()`(139) `finalize()`(247) `build()`(335) |
| `rules.py` | 737 | 规则表与规则链执行 | `RULE_KINDS`(43) `Rule`(131) `transform()`(369) `apply_rule()`(561) `apply_chain()`(596) |
| `context.py` | 340 | 模板变量上下文 | `NameContext`(61) `Item`(270) `split_ext()` |
| `template.py` | 443 | 模板渲染 + 选择器 + 旧语法兼容 | `render()`(327) `select()`(84) `expand_refs()`(412) `SELECTOR_KINDS` |
| `llm.py` | 701 | 大模型精简名字 | `LLMConfig`(171) `LLMClient`(329) `make_client()`(630) `sanitize_name()`(243) |
| `common.py` | 510 | 校验、两遍冲突检测、落盘、日志、撤销 | `validate_items()`(105) `execute()`(232) `save_log()`(407) `undo()`(473) `ValidateReport`(73) |
| `ops.py` | 129 | 高层动作（预览/落盘/撤销/导出），界面与命令行都只调这里 | `preview()` `apply()`(45) `undo()` `export_csv()` `scan_and_plan()`(123) |
| `presets.py` | 125 | 预设存取（JSON 在 `%APPDATA%\NameCat\presets`） | `Preset`(33) `save/load/list_presets` `BUILTIN`(102) |
| `cli.py` | 321 | 命令行子命令 | `ONLY_CHOICES`(32) `build_parser()`(66) `cmd_rename()`(196) `main()`(302) |

### 2.3 `adapters/` —— 翻译层（界面 ⇄ 引擎的唯一通道）

| 文件 | 行 | 职责 |
|---|---|---|
| `engine_adapter.py` | 843 | `FormState`(86) 表单状态 ⇄ `ScanOptions`/`PlanOptions`；`PlanBundle`(212)；**两阶段** `scan_only()`(250) / `build_plan()`(259) / `build()`(272)；`apply_plan()`(277)；`set_excluded()`(291) / `clear_exclusions()`(323)；状态文案 `status_text`；规则字段表 `KIND_FIELDS`/`RULE_GROUPS`；预设/日志/LLM/CSV 的界面侧封装 |

关键点：界面只碰 `FormState` 和 `PlanBundle` 两个对象，**不自己拼 ScanOptions**。
`engine_adapter.py` 第 364–470 行是一整段**纯数据表**（`KIND_FIELDS`(364) 规则字段、`KIND_FLAGS`(425)、
`RULE_GROUPS`(438)、`COMMON_RULES`(449)、`DEFAULTS`(451)），加规则类型时改那里，不用读逻辑代码。

### 2.4 `ui/` —— 界面层（2026-10 重写）

| 文件 | 行 | 职责 |
|---|---|---|
| `app.py` | 58 | 启动：vendor 路径 → DPI 策略（PassThrough）→ QApplication → 加载字体 → 主窗口 |
| `window.py` | 250 | 顶栏 / 侧栏 / 页面栈 / 状态栏；跨页契约 `llm_overrides()`、`workspace_root()`；快捷键；偏好落盘 |
| `base.py` | 560 | 设计令牌（颜色/圆角/字号）、HarmonyOS 字体加载、24×24 内联 SVG 图标、基础控件（Card/Button/Check/Badge/Chip/NoticeBar/Select/PageHead/NavButton…） |
| `prefs.py` | 45 | `%APPDATA%\NameCat\gui.json` 白名单读写（不存密钥） |
| `fonts/HarmonyOS_Sans_SC.ttf` | — | 可变字体（wght 100–900），随包加载 |

`ui/pages/`

| 文件 | 职责 |
|---|---|
| `workspace.py` | 工作台：工具栏 / 规则链 / 规则属性 / 预览表 / 摘要条；扫描、算名、执行、CSV、勾选排除、预设应用 |
| `presets.py` | 预设：内置 + 自定义列表，应用 / 删除 / 打开目录 |
| `history.py` | 执行记录：读 `_rename_logs`、撤销、目录切换 |
| `llm_settings.py` | LLM：生效配置、环境变量来源、要求文件编辑与保存 |

> 页面只调 `adapters.engine_adapter`；后台任务走 `workers.task_workers`（扫描/算名/执行/日志/撤销）。

### 2.5 `workers/` 与 `verify/`

| 文件 | 行 | 职责 |
|---|---|---|
| `workers/task_workers.py` | 122 | `BaseTask`(28) 把引擎调用包成 QRunnable，异常 → `failed` 信号；`TaskRunner`(63) 线程池；任务工厂 `scan_task`(93) `build_plan_task`(98) `scan_and_plan_task`(103) `apply_task` `undo_task` `logs_task` `leftovers_task` |
| `verify/run_tests.py` | 1187 | 无界面验证套件：规则单测、魔数、兼容、LLM、端到端、回归（当前 **315 条断言**全通过，约 1.3s） |
| `verify/ui_smoke.py` | 210 | **界面交互级冒烟**（离屏）：27 种规则逐条添加、三个弹窗、扫描→算名→执行→撤销、预设、LLM，异常即退出码 1 |
| `verify/make_icon.py` | 189 | 把一张方形图加工成 PNG + ICO |

---

## 3. 数据流（最重要的一张图）

界面上是**两阶段**：第 ①步可以单独跑（此时还没有任何规则），第 ②③④步在点「生成预览」时才跑。
好处是「先扫出文件清单、看清楚要处理什么，再决定加什么规则」。

```
① 扫描     FormState ──scan_options()──► ScanOptions ──scan()──► ScanResult(items=[Item…], skipped)
                                                                      │
              ┌──── 第一阶段到此为止：表格显示原名、状态列「已扫描」、新名称列「—」
              │      此时还没有任何规则也完全可以扫描（build 的硬卡子已拆掉）
              │      拖放 / 「扫描对象…」都是往 FormState.selected 里加路径，然后重跑这一步
              ▼
② 编号     PlanOptions ──assign_numbers()──► 每项填 index/total/num/width
                                                                      │
③ 算名     rules ──apply_chain()──► 每项 new_name（空规则链 = 保持原名）
                                                                      │
④ 校验     finalize() ──validate_items()──► 每项 status/note + ValidateReport（两遍：批内重名 / 盘上占用）
                                                                      │
⑤ 成计划   Plan(items, report, warnings)──► 表格显示（PreviewTable 只读它）
                                                                      │
⑥ 落盘     OPS.apply(..., plan=plan) ──execute()──► save_log() → _rename_logs\rename_*.json
```

对应代码（三个入口，`build` 只是把前两个串起来）：

```python
# adapters/engine_adapter.py:250  第一阶段：只扫描，不需要规则
def scan_only(state, progress=None) -> PlanBundle:
    return PlanBundle(scan=scan(state.scan_options(), progress), plan=None, state=state)

# adapters/engine_adapter.py:259  第二阶段：复用已扫出的 Item，绝不重扫文件系统
def build_plan(bundle, rules, progress=None, client=None) -> PlanBundle:
    items = list(bundle.scan.items)
    clear_exclusions(items)          # 上一轮勾选排除的痕迹不能带进新计划
    plan = P.build(items, list(rules or []), bundle.state.plan_options(True),
                   progress, client)
    return PlanBundle(scan=bundle.scan, plan=plan, state=bundle.state)

# adapters/engine_adapter.py:272  一步走完（命令行与旧路径仍可用）
def build(state, rules, progress=None, client=None) -> PlanBundle:
    return build_plan(scan_only(state, progress), rules, progress, client)
```

**`apply` 必须传 `plan=`，绝不能在落盘时重算** —— 重算会让 LLM 规则请求两次、`randomize` 两次结果不同
（预览显示的名字和真正落盘的名字不一致）。`ops.py:45` 的 docstring 写了这个理由。

### 3.1 核心数据结构

| 结构 | 位置 | 说明 |
|---|---|---|
| `FormState` | `engine_adapter.py:86` | 界面表单的唯一真相：扫描字段 + 计划字段 + `selected`（指定条目）。`scan_dict()/plan_dict()` ⇄ 预设 JSON |
| `ScanOptions` | `scan.py:167` | 引擎扫描参数（`selected: Optional[set]` 为 `nkey()` 集合；语义见 3.3） |
| `Item` | `context.py:270` | 单个对象。`__slots__` **含 `__dict__`**，可挂临时字段（勾选排除就靠这个） |
| `PlanOptions` | `plan.py:46` | 编号与作用域（`scope` 四档：global/folder/kind/folder_kind） |
| `Plan` | `plan.py:63` | `items` + `report` + `warnings`，`pairs()/changing()/by_status()/summary()` |
| `PlanBundle` | `engine_adapter.py:212` | `scan` + `plan` + `state` 三元组；`has_scan` / `has_plan` / `items()` / `stats()`（无计划时各状态为 0，只给 total） |
| `Rule` | `rules.py:131` | `kind/a/b/c/enabled/id/selector/charset/occurrence/skip_ext/keep_sep/extras` |

### 3.2 三阶段界面状态（`PlanBundle` 的两种非空形态）

| 阶段 | `bundle` | 表格 | 底部主按钮 | CSV/执行 |
|---|---|---|---|---|
| 未扫描 | `None` | 空状态「拖入文件或文件夹开始」 | 「预览变更」 | 灰 |
| 只扫描 | `has_scan`，`plan is None` | 原名 + 新名称列显示 `—` + 状态列「已扫描」 | **「生成预览」** | 灰 |
| 已算名 | `has_plan` | 真实新名字与状态 | 「重新生成预览」 | 按 `stats()["ok"]` 决定 |

`_refresh_summary()`(522) 是这套状态的总闸；`_apply_selection()`(727)、`export_csv()`(744)、
`confirm_apply()`(765) 都必须先判 `bundle.has_plan`——它们的 `bundle is None` 分支只管「还没扫描」，
不能拿来当「扫描了但没计划」。

### 3.3 `ScanOptions.selected` 的语义（拖放与「扫描对象…」共用）

- 一般情况：**「只要这些」**。选中的目录代表它**里面的整棵子树**（目录自身不出现）。
- 选中项在 root 之下但更深：会自动下探到它所在目录，**不受「包含子文件夹」和深度上限约束**
  （否则关掉递归后用户明确拖进来的东西会被静默丢弃）。
- 选中项在 root 之外（跨盘符拖入或 `add_paths()` 收窄了 root）：语义放宽成
  **「root 里全部 + 这些」**——这时用户意图明显是「往已有列表里再加几个」，不是「只要这几个」。
  没有这条，往扫好的列表里拖一个外部文件，原有内容会一起消失。
- `WorkspacePage._pick_items()` 把 root 收窄到所选条目的公共父目录；拖放同样走这条路径，
  保证 `ScanOptions.selected` 是唯一的过滤入口。

### 3.4 状态码语义（贯穿全程序）

| 码 | 文案 | 谁能设置 |
|---|---|---|
| `ok` | 待重命名 | `validate_items`：语法通过且目标可用 |
| `same` | 名称未变 | 算完名与原名相同 |
| `conflict` | 冲突 | 批内重名 / 目标已存在 |
| `error` | 无效 | 非法文件名、目录不存在 |
| `skip` | 已跳过 | 用户取消勾选（`set_excluded`）、或有冲突时选「跳过」 |

`validate_items()` 第一件事就是**跳过 `status == ST_SKIP` 的项并保留其 note**——这是「取消勾选不动真实状态」的实现机制。

---

## 4. 工作台 `ui/pages/workspace.py`

三段状态由 `PlanBundle` 决定，界面不自己推导：

| 阶段 | `bundle` | 表格 | 主按钮 | CSV / 执行 |
|---|---|---|---|---|
| 未扫描 | `None` | 空状态 | 预览变更（会先扫描） | 灰 |
| 只扫描 | `has_scan` | 原名 + 新名称列 `—` + 状态「已扫描」 | 生成预览 | 灰 |
| 已算名 | `has_plan` | 真实新名与状态 | 重新生成预览 | 按 `stats()["ok"]` |

主要方法：`scan()` → `_scan_done()`、`generate_plan()` → `_plan_done()`、`confirm_apply()` →
`_apply_done()`、`_refresh_rules()` / `_refresh_props()`（规则链与属性面板）、
`_refresh_preview()` / `_refresh_summary()`（表与统计）、`_mark_stale()`（配置改动后置灰执行）。

约定：勾选排除走 `A.set_excluded()`（引擎的 skip 状态），界面不另存一份排除清单；
统计数字一律来自 `bundle.stats()`。

## 5. 常见改造的「该动哪里」

### 加一种新规则类型
1. `engine/rules.py:43` `RULE_KINDS` 加一项 `(kind, 中文名, {a/b/c 提示}, 说明)`
2. `engine/rules.py:369` `transform()` 加分支（真正干活的地方）
3. `adapters/engine_adapter.py` `KIND_FIELDS` 加字段表（决定属性面板长什么样），必要时 `RULE_GROUPS`
4. 若涉及新变量/语法 → `engine/context.py` `variables()` 与 `engine/template.py`

### 加一个扫描选项
1. `engine/scan.py:167` `ScanOptions` 加字段（带默认值，`selected=None` 的语义别破坏）
2. `engine/scan.py:271` `take()` 里插过滤（顺序：扩展名 → 正则 → 类型 → 隐藏 → stat → 魔数 → selected）
3. `adapters/engine_adapter.py` `FormState` 加字段 + `scan_options()` 传下去 + `scan_dict/apply_scan_dict`
4. `ui/pages/workspace.py` 的 `_toolbar()` 加控件，并在 `_collect()` 里写回 `FormState`

### 加一个拖放目标
`ui/widgets/drop_area.py` 的 `PathDrop` 是**事件过滤器**，一个实例可以挂多个控件：

```python
self.drop = PathDrop(self)          # 挂在页面（parent 决定生命周期）
self.drop.attach(self.preview_box)  # 整张卡片
self.drop.attach(self.table)        # 表格本身也要挂，子控件会先吃掉事件
self.drop.paths_dropped.connect(self._drop_paths)   # list[str]
self.drop.drag_active.connect(self._on_drag_active) # bool → 高亮
```

注意两点：①挂父容器时**子控件也要挂**，否则鼠标在表格上时事件被子控件吞掉；
②高亮用动态属性 `setProperty("dropActive", True)` + `style().unpolish/polish`，
样式在 `ui/styles/light.qss` 的 `QFrame#Card[dropActive="true"]` 里。

### 加一个界面页面
1. `ui/pages/xxx.py` 写 `QWidget` 页面（数据走 `adapters.engine_adapter`）
2. `ui/window.py` 的 `NAV` 加一项（顺序即 Ctrl+1..N 顺序）
3. `MainWindow.__init__` 里 `self.stack.addWidget(...)`、`_wire()` 里连信号

### 改视觉
只改 `ui/base.py` 顶部的 `C`（颜色）/ `R_*`（圆角）/ `F()`（字号字重）与 `ICONS`（图标路径）。
图标是内联 SVG，按色渲染，任意 DPI 下都清晰。

---

## 6. 两个容易踩的坑（都是改这两块功能时才会遇到）

### 6.1 两阶段共用同一批 `Item` 对象
`build_plan()` 不是重新扫描，而是直接吃 `bundle.scan.items` 那批对象。这意味着：

- **勾选排除的痕迹会跨阶段残留**。`set_excluded()` 把 `status` 改成 `skip` 并挂 `_status_before`；
  下次算名时 `validate_items()` 一上来就跳过 `skip` 项，于是「上一轮取消勾选的项」在新计划里会**永远算不出来**。
  所以 `build_plan()` 第一步必须 `clear_exclusions(items)`（`engine_adapter.py:323`）。改动这里时别把这一步删掉。
- **同一批 `Item` 被 `P.build()` 就地改写**（`new_name`/`status`/`note`/`index`/`num`/`_new_dir`）。
  想让「同一个扫描结果按两套规则各算一次」，必须各自 `copy.deepcopy` 或重新 `scan_only()`。
- **`scan_only()` 之后改扫描选项必须重扫**。`bundle.state` 是扫描那一刻的 `FormState` 快照引用，
  改了 root/筛选条件却只调 `generate_plan()`，用的还是旧清单。

### 6.2 阶段切换的时序
界面里「扫描 → 自动算名」是**串起来的两段异步任务**，容易写出竞态：

- `QRunnable.run()` 里 `finished` 信号是在**线程还没归还线程池**时发出的，
  在 `finished` 槽里立刻 `runner.submit(下一个任务)` 会让 `busy()` 仍为真。
  正确做法见 `_scan_finished()`(636) → `QTimer.singleShot(0, self._continue_plan_after_scan)`：
  等一拍，让事件循环把线程池状态刷新后再提交。
- `_drop_paths()`(504) 里的 `_plan_after_scan` 标志只在「已有规则」时为真——
  没有规则时拖入只扫描、不白跑一次算名（算出来也全是「名称未变」）。
- 测试里判断「这一轮跑完了」不能只等 `bundle is not None`：旧 bundle 还在时条件立刻成立。
  用例开头要先把 `bundle` 清空（`verify/ui_smoke.py` 就是在新建窗口后直接扫描）。

---

## 7. 验证怎么跑（改完必跑）

```bat
:: 引擎 + 端到端 + 回归（无界面，最快）
py -3 verify\run_tests.py

:: 界面交互冒烟（离屏，不需要显示器），改界面后必跑
py -3 verify\ui_smoke.py
```

`ui_smoke.py` 会把界面上每个可点的地方走一遍：27 种规则逐条添加、规则选择弹窗、
高级筛选、编号选项、扫描 → 算名 → 勾选 → 导出 CSV → 执行 → 撤销、预设应用/保存/删除、
LLM 页刷新。任何一步抛异常都会打印堆栈并以退出码 1 结束——接手改界面时，先跑它。

两个脚本都在临时目录造沙盒，不碰真实文件。

**双击 `NameCat.vbs` 没反应时**，按这个顺序查：

1. `%TEMP%\NameCat_startup_error.log`：启动异常会写在这里，并同时弹出消息框；
2. 文件为空 → 多半是启动方式把窗口藏了：`NameCat.vbs` 里 `sh.Run cmd, 1, False`
   的第二个参数必须是 **1**。写 0 会以 `SW_HIDE` 启动，PySide6 主窗口即使调
   `show()/showNormal()` 也不会出现（实测：样式 0 时 `MainWindowHandle` 恒为 0）；
3. 仍无头绪就用 `namecat.bat` 启动，控制台会直接打印 traceback。

本机可用解释器：`py -3` → Python 3.14（`where python` 只有 Microsoft Store 的占位 exe，别直接用）。
控制台输出中文乱码时先 `$env:PYTHONIOENCODING='utf-8'`。

当前基线：`run_tests.py` **315/315**。
改动后数字变了就顺手更新这里和第 2.5 节。

---

## 8. 几条不成文但必须守的约定

1. **不重算**：落盘一律用 `plan=` 传现成计划（`ops.py:45`）。
2. **`Item` 可以挂临时字段**：`set_excluded` 用 `it.__dict__["_excluded"]`，改校验逻辑别把这个覆盖掉。
3. **界面文案不推导数字**：统计数字一律来自 `PlanBundle.stats()` / `Plan.report`，界面不自己数。
4. **不存密钥**：`ui/settings.py:38` `FORBIDDEN` 命中即丢弃；API Key 只从环境变量读。
5. **数据目录是 `%APPDATA%\NameCat`**，旧目录 `NameKit` 只做单向补齐迁移（`engine/__init__.py:47`）。
6. **日志目录 `_rename_logs` 要跳过扫描**（`scan.py:92` `LOG_DIR_NAMES`），否则会把日志当文件改名。
7. **Qt 命名**：覆写 Qt 方法用 `# noqa: N802` 标注（`sizeHint`/`paintEvent` 之类）。
8. **两阶段的状态判断一律走 `bundle.has_scan` / `bundle.has_plan`**，不要写 `bundle is None` 之外的分支；
   新增任何依赖计划的按钮/动作，都先想清楚它在「只扫描」阶段该是什么样。
9. **拖放收进来的路径要落进 `FormState.selected`**，别在界面层另外维护一份清单——
   引擎的 `ScanOptions.selected` 是唯一过滤入口，两套清单一定会打架（`WorkspacePage._pick_items()` 就是为此存在的）。
