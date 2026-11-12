# NameCat 全面测试报告（1.6.0 → 1.6.2）

- 被测版本：`1.6.0`（`engine/__init__.py: APP_VER`），commit `d9ded5e`
- 修复版本：`1.6.2`（报告中的缺陷已全部修复，见第 0.1 节）
- 测试日期：2026-11-01
- 环境：Windows 10/11 x64，Python 3.13.12，PySide6（离线模式 `QT_QPA_PLATFORM=offscreen`），磁盘 C: + D:（跨盘用例）
- 代码规模：`engine/` 纯逻辑 8 个模块（约 3.4k 行）、`adapters/` 1 个（1k 行）、`ui/` 5 个文件（2.8k 行）、`workers/` 1 个

> **阅读顺序**：第 0 节是修复记录（当前状态），第 1–4 节是**修复前**的缺陷分析原文
> （保留现场：位置、复现、原因都按当时的代码写）。第 7 节是修复顺序。

## 0. 结论摘要

| 级别 | 数量 | 含义 |
|---|---|---|
| **P0** | 4 | 会造成**用户可见的名字损坏 / 整批功能不可用**，建议立即修 |
| **P1** | 8 | 功能不可用、崩溃或有误导性反馈 |
| **P2** | 10 | 逻辑瑕疵、体验与健壮性问题 |
| **P3** | 5 | 测试/文档/代码卫生问题 |

- 原有自检套件 `verify/run_tests.py`：**407 条断言，406 通过 / 1 失败**（失败项是测试自身没有隔离 `%APPDATA%`，见 P3-1）；`verify/ui_smoke.py`：**全部通过**。
- 本轮新增 `verify/test_edges.py`：**153 条断言全通过**，另带 16 个"缺陷探针"（修好会自动变绿，不会永远红）。
- 正常路径的核心业务逻辑（规则链、编号、两遍校验、落盘、撤销、日志、预设、LLM 降级、CLI、界面全流程）实测**行为与文档一致**，无内存泄漏、无死循环、无递归爆栈。问题集中在**异常路径与边界输入**。

### 0.1 修复记录（第二轮：按报告顺序逐条修完）

**全部 18 个缺陷 + 5 个健壮性项已修**，回归结果：`run_tests.py` **410 / 0**、
`test_edges.py` **190 断言 0 失败、16 个缺陷探针全部转为「已修复」**、`ui_smoke.py` 全绿。

| 编号 | 修法 | 涉及文件 |
|---|---|---|
| **B3** until 拼接错 | `splice` 的 until 分支改为 `new + text[idx+1:]` | `engine/template.py` |
| **B2** 名字被删空仍落盘 | 新增 `nameless_problem()`：新名恰好只剩原扩展名 → 判「无效」；空名改判 error | `engine/common.py` |
| **B1** 回滚残留 `__nk_tmp_` | 落位失败时用 `_rollback_temps(moved)` 统一退回原名（去掉 `landed` 过滤）；失败原因不再被"已回滚"覆盖 | `engine/common.py` |
| **P2-6** 抢救无入口 | 新增 `main.py leftovers <目录>`（有残留返回 1）；界面执行失败后检查残留并在状态槽提示 | `engine/cli.py`、`ui/pages/workspace.py` |
| **B4** 跨盘 ValueError | 新增 `_rel_to()`：跨盘返回空串并跳过前缀映射；`ScanOptions.strict_selected` + `add_paths` 严格按选中项收，不再把父目录的无关条目拉进来 | `engine/plan.py`、`engine/scan.py`、`adapters/`、`ui/pages/workspace.py` |
| **B11** 预设清空目录 | `apply_preset` 保住 root，并按新预设自动重扫一次 | `ui/pages/workspace.py` |
| **B5** 撤销日志永不清理 | `undo` 幂等化：已不存在的跳过、失败项写回日志、做完即删；新增 `_rewrite_log()` | `engine/common.py` |
| **B6** 日志写不进去仍报成功 | `apply` 文案追加"本次改名无法撤销" | `engine/ops.py` |
| **B7** `undo --log` 抛栈 | 先 `isfile` 校验；读日志包 `(OSError, ValueError)`；`--list` 容忍非 dict | `engine/cli.py` |
| **B8** 一份坏日志拖垮整页 | `log_pairs` 逐项类型校验；`adapters.logs` 把解析整个包进 try | `engine/common.py`、`adapters/engine_adapter.py` |
| **B9** `--csv` 抛栈 | 包 `OSError` 并返回 1 | `engine/cli.py` |
| **B10** README 快捷键不存在 | README 与实现对齐（只留 F5 / Ctrl+Enter / Ctrl+1…4 / Esc） | `README.md` |
| **B12** LLM 页丢输入 | `reload(force=False)`：有未保存修改不覆盖 | `ui/pages/llm_settings.py` |
| **P2-1** `--step 0` | `compute_names` 里回落成 1 并写进 warnings | `engine/plan.py` |
| **P2-2** 文件夹删空报"未变" | 同 B2：改判 error「规则把名字删空了」 | `engine/common.py` |
| **P2-3** 宽度无上限 | `template.MAX_WIDTH = 64` / `MAX_RANDOM = 64`，`number/renumber/pad_numbers/randomize/{n:xx}` 全部夹紧；界面 `rule_problems` 拦超限 | `engine/template.py`、`engine/rules.py`、`adapters/` |
| **P2-4** normalize 崩 | `extras` 非 dict → 补空字典；`selector` 先转字符串 | `engine/rules.py` |
| **P2-5** 未知 kind 静默 | 记 `extras["unknown_kind"]`，`describe()` 与界面校验都会标出 | `engine/rules.py`、`adapters/` |
| **P2-7** 回环走代理 | `provider.opener_for()`：127.0.0.1 / localhost 用 `ProxyHandler({})`；`llm._urlopen()` 同口径 | `engine/provider.py`、`engine/llm.py` |
| **P2-8** selected 要 nkey | `scan()` 入口统一 `nkey()` 归一化（传原始路径也命中） | `engine/scan.py` |
| **P2-9** 文件夹列截断 | 按目录边界比前缀（`root + os.sep`） | `ui/pages/workspace.py` |
| **P2-10** 迁移跑 N 次 | `migrate_legacy_appdata` 每进程只做一次 | `engine/__init__.py` |
| **P3-1** L1 读真实 APPDATA | 用 `NAMECAT_LLM_INSTRUCTION_FILE` 指到沙箱，`finally` 还原 | `verify/run_tests.py` |
| **P3-2** 代理导致抖动 | 由 P2-7 一并解决（回环不再受系统代理影响） | `engine/provider.py` |
| **P3-3** `expand_escape` 未接线 | `render()` 开头展开 `\x41` / `\x{1F600}` / `\n\t\r` | `engine/template.py` |
| **P3-4** 重复字典键 | 删掉重复的 `KIND_FIELDS["number"]` 与 `KIND_FLAGS["extension"]` | `adapters/engine_adapter.py` |
| **P3-5** 抢救跳过目录不分大小写 | 改成小写集合比对（与 `scan.LOG_DIR_SKIP` 同口径） | `engine/common.py` |

**顺手补的测试**：`test_edges.py` 从 153 → 190 条断言，新增/改写覆盖
— until 拼接、空名与只剩扩展名、链式回滚完整性、`leftovers` 命令、跨盘扫描范围（严格/放宽对比）、
撤销幂等（缺文件 vs 冲突两种）、宽度上限、`{n:9999}` 夹紧、转义展开、回环代理策略、
未知规则留痕、界面 `rule_problems` 上限拦截。

---

## 1. P0 —— 建议立即修复

### B1 链式改名中途失败时，已落位的项被留在 `__nk_tmp_` 临时名下，并谎报"已回滚"

- **位置**：`engine/common.py:337-344`（`execute()` 的"落位失败"回滚分支）
- **严重度**：P0（文件内容没丢，但**名字变成了 `__nk_tmp_xxxxxxxx`**，且界面/命令行明确告诉用户"已回滚"）
- **复现**（10 行，`verify/test_edges.py::test_rollback` 已覆盖）：

  ```python
  # 目录里有 a.txt / b.txt / c.txt；规则让 a、b 互换名字（触发临时名中转），c → d.txt
  plan = P.finalize(P.Plan(items=its))        # 校验：3 项都可改
  open(box + r"\d.txt", "w").write("external")   # 校验之后、落盘之前，别的程序抢先建了 d.txt
  steps = C.execute(plan.changing())
  # 实测：a.txt、b.txt 变成 __nk_tmp_xxxx / __nk_tmp_yyyy（原名消失）
  #      三步的 error 全是"已回滚"，ops.apply 的文案是"已重命名 0 项，失败 3 项（已回滚）"
  ```
- **原因**：落位阶段的失败处理先把已落位的项 `rename(dst → tmp)` 退回**临时名**，随后又用
  `landed = {id(f[0]) for f in final}` **把这些项从回滚列表里排除掉**，于是它们永远停在临时名上：

  ```python
  for s2, t2, d2 in reversed(final):
      if os.path.exists(d2):
          rename_with_retry(d2, t2)          # ← 退到 tmp，不是退到原名
  landed = {id(f[0]) for f in final}
  _rollback_temps([p for p in moved if id(p[0]) not in landed])   # ← 恰好把它们排除了
  ```
- **建议**：让已落位的项也参与第二段回滚（它们此刻正停在 `tmp`，`_rollback_temps` 的 `tmp → src` 正好合适）：

  ```python
  for s2, t2, d2 in reversed(final):
      if os.path.exists(d2):
          rename_with_retry(d2, t2)
  _rollback_temps(moved)          # 去掉 landed 过滤
  ```
  或者第一步就退到原名 `rename(d2, <该 step 的 src>)`。另外建议在失败文案里区分"已回滚"与"回滚失败"，不要一律写"已回滚"。
- **连带问题**：`find_temp_leftovers()`（`common.py:382`）本来是给这种情况兜底的，但它：
  1. 没有任何入口——`workers.leftovers_task` 定义了却没有人调用，界面与命令行都点不到（P2-6）；
  2. 跳过日志目录时用的是大小写敏感比对（`common.py:391`），与 `scan.LOG_DIR_SKIP` 的小写表不一致。

### B2 规则把名字主干删空时，会落盘成一个没有名字的隐藏文件（`.txt`）

- **位置**：`engine/common.py:38-64`（`name_problem`）+ `engine/plan.py:185`
- **复现**：

  ```python
  # 规则：delete = "abc"，对象：abc.txt
  plan = P.build([Item(p, "abc.txt", False)], [R.make_rule("delete", "abc")], P.PlanOptions())
  # 实测：status="ok"，new_name=".txt" —— 真的落盘成功，文件名只剩扩展名
  ```
  GUI 里同样：加一条「删除文字 / abc」的规则，预览显示"待重命名 → .txt"，点执行就改掉了。
- **原因**：`split_ext("abc.txt")` → `("abc", ".txt")`，删空主干后 `stem + ext = ".txt"`；
  `name_problem(".txt", is_dir=False)` 对点开头的名字走 `name[1:].split(".")[0]` → `"txt"`，
  既不为空、也不是保留名、不结尾点空格，于是判"合法"。**没有任何一处校验"文件的主干不能为空"**。
- **影响**：改名后文件在资源管理器里"看起来消失了"（隐藏文件），只能靠撤销找回；批量时会把整个目录的文件都变成 `.jpg / .txt` 并因同名而冲突。
- **建议**：在 `name_problem` 里补两条（对文件/文件夹都成立）：

  ```python
  stem, ext = context.split_ext(name) if not is_dir else (name, "")
  if not is_dir and not stem.strip():
      return "名字不能为空（只剩扩展名）"
  ```
  同时把 `validate_items` 里 `if not new: status=SAME / "规则未改变名称"`（`common.py:140-148`）改成
  `ST_ERROR + "规则把名字删空了"`——现在文件夹被删空时报"名称未变"，用户以为规则没生效（P2-2）。

### B3 作用范围选「到某字符止（含）」（`until`）时，改名结果必然错乱

- **位置**：`engine/template.py:125`（`splice()` 的 `until` 分支）
- **复现**：

  ```python
  rule = make_rule("case", "upper"); rule.selector, rule.charset = "until", "-"
  R.apply_rule(rule, "show-2026-10-19")   # 实测 "show-SHOW-"（尾部整段丢失、头部重复）
                                          # 期望 "SHOW-2026-10-19"
  ```
  GUI：规则属性 →「作用范围」下拉里就有"到某字符止（含）"，选了之后 `prefix/delete/case/...` 全部产出这种串；
  CLI：`--rule "case=upper;sel=until;sep=-"` 等价。
- **原因**：`until` 的 `select` 取的是**含分隔符的头部**，`splice` 却用了 `after` 的公式：

  ```python
  if mode == "after":
      return text[: idx + 1] + new     # after：头部原样 + 新尾部 ✅
  if mode == "until":
      return text[: idx + 1] + new     # until：头部原样 + 新头部 ❌
  ```
- **建议**：`until` 应为"新片段 + 分隔符之后"：`return new + text[idx + 1:]`。
  顺手补一条回归：`splice("show-2026", "until", "-", 1, "★") == "★2026"`。

### B4 「跨盘符拖入」只要有文件夹参与改名，整批预览直接抛 `ValueError`

- **位置**：`engine/plan.py:338-348`（`_common_root`）+ `plan.py:282/301`（`finalize` 里的 `os.path.relpath`）
- **复现**（本机 C: + D:，`verify/test_edges.py::test_cross_drive` 用假盘符 Q:/Z: 也能稳定复现）：

  ```
  1. 同时选中 C:\A（文件夹）与 D:\B（文件夹）拖进预览卡  ← README/scan.py 明确声明支持
  2. 加一条「前缀」规则，对象选「文件 + 文件夹」
  3. 生成预览 → 状态槽报「操作失败：ValueError path is on mount 'd:', start on mount 'C:'」
  ```
- **原因**：`_common_root()` 在 `os.path.commonpath` 抛 `ValueError`（跨盘）时退回 `dirs[0]`，
  但 `finalize()` 之后仍对**每一项**调用 `os.path.relpath(it.path, root)`；跨盘时该函数必抛 `ValueError`，
  且只在 `dir_map` 非空（有文件夹被改名）时才会走到这段，所以"只拖文件"时反而正常 —— 更难定位。
- **建议**：按盘符分组后再算相对路径（`os.path.splitdrive` 作为分组键），或对 `relpath` 逐个 `try/except ValueError: continue`（该项不做父目录前缀映射）。跨盘对象之间的父子改名本就不存在，跳过是最省事且正确的做法。
- **另一处同源问题**：`WorkspacePage.add_paths`（`ui/pages/workspace.py:1405-1411`）在 `commonpath` 抛错时把 root 退回 `dirname(items[0])`，于是"只要这两个文件夹"被悄悄放大成"扫它们的父目录"。实测拖入 `_probe\crossA` + `D:\B` 后，`_probe` 下 14 个无关条目全部进了待改名列表。建议跨盘时不要把 root 退回父目录，而是明确提示"跨盘需要分两批处理"。

---

## 2. P1 —— 功能不可用 / 崩溃 / 误导性反馈

### B5 撤销只成功一半后，日志永远删不掉，之后每次撤销都报"未完成"

- **位置**：`engine/common.py:597-634`（`undo()`，只在 `if not errors` 时删日志）
- **复现**：

  ```
  日志里有 2 对：a→a_new（存在）、b→b_new（b_new 已被用户删掉）
  第 1 次撤销：还原了 a，报 1 条"找不到 b_new"，日志保留        ← 合理
  第 2 次撤销：报 2 条错误（a_new 也没了 + b_new 也没了），日志仍保留 ← 永远出不去
  ```
  界面「执行记录」里该行永远在，点撤销永远提示"撤销有 N 处未完成"；命令行 `undo` 永远返回 1。
- **原因**：`undo` 没有记录"哪几条已经还原成功"，失败就整份日志保留；下次再跑时已还原的条目变成"找不到源文件"，错误只增不减。
- **建议**：成功后把**失败项写回日志**（或删掉日志另存 `.residual.json`），并把"找不到 `new`"从错误降级为"跳过（已不存在）"；这样重复撤销幂等，日志也能自然收敛消失。

### B6 撤销日志写不进去时，仍然报告"已重命名 N 项"成功

- **位置**：`engine/ops.py:71-85`
- **复现**：把 `NAMECAT_LOG_DIR` 指到一个**同名文件**（或程序目录不可写且 `%APPDATA%` 也写不进），执行改名：
  ```
  实测：Result.message = "已重命名 1 项"，ok=True，log_path="" —— 文件确实改了，但完全没有可撤销记录
  ```
  而执行前的确认框写着"执行后写入撤销日志，可在「执行记录」页撤销"。
- **原因**：`save_log` 失败时静默返回 `""`，`apply` 不检查。
- **建议**：`log_path` 为空时把提示改成"已重命名 N 项，但撤销日志没能写入（本次无法撤销）"，并让 `Result` 带一个 `warn` 字段供界面用警告语气显示。

### B7 `main.py undo --log <不存在/损坏>` 直接抛 traceback

- **位置**：`engine/cli.py:339-366` → `engine/ops.py:99` → `engine/common.py:583/588`
- **复现**（三种都会崩）：

  ```bat
  python main.py undo --log C:\no\such.json          → FileNotFoundError
  python main.py undo --log bad.json                  → json.JSONDecodeError（文件内容不是 JSON）
  python main.py undo --log pairs.json                → AttributeError（"pairs": [1,2]）
  ```
  退出码 1，但输出是完整堆栈，与 README "撤销失败会返回非 0 退出码（并写明是哪一项没还原）"不符。
- **原因**：`cmd_undo` 不校验 `--log` 是否存在；`load_log` / `log_pairs` 不做容错。
- **建议**：`cmd_undo` 里先 `os.path.isfile` 判断并给出人话；`load_log` 包一层 `(OSError, ValueError)`；
  `log_pairs` 改为 `if not isinstance(p, dict): continue`。

### B8 一份结构异常的日志，会让整个「执行记录」列表读取失败

- **位置**：`engine/common.py:588-594`（`log_pairs`）+ `adapters/engine_adapter.py:749-754`
- **复现**：`Name_backup` 里放一份 `{"pairs": [1,2]}` 的 json，再打开「执行记录」：
  ```
  实测 adapters.logs("") 抛 AttributeError: 'int' object has no attribute 'get'
  界面表现：整页"读取日志失败"，本来完好的其它日志也一条都看不到
  ```
- **原因**：适配层把 `load_log` 包在 `try/except (OSError, ValueError)` 里，但 `log_pairs(data)` 在 try **外面**；且 `data` 本身是数组/标量时 `data.get` 也会炸。
- **建议**：`log_pairs` 加类型判断；适配层把 `log_pairs` 也放进同一个 try，坏的那份记一行"无法读取"而不是整页失败（`cli.cmd_undo --list` 已经是这么做的，可以直接对齐）。

### B9 `--csv` 指向不可写路径时抛 traceback

- **位置**：`engine/cli.py:319-321` → `engine/ops.py:108-128`
- **复现**：`python main.py preview D:\x --rule "template={n}" --csv C:\no\dir\out.csv` → `FileNotFoundError` 堆栈。
- **建议**：`cmd_rename` 包 `try/except OSError`，打印"导出失败：…（预览结果仍然可用）"并返回 1。

### B10 README 宣传的两个快捷键并不存在

- **位置**：`README.md:66-73` vs `ui/window.py:312-326`、`ui/pages/workspace.py:799-803`
- **现象**：README 写着「`空格` / 双击 勾选 / 取消勾选预览行」「`Delete` 删除选中的规则」，
  但全项目 `Key_Space` / `Key_Delete` / `mouseDoubleClick` **一个都没有**（`grep` 为空），实际只有 F5 / Ctrl+Enter / Ctrl+1…4 / Esc。
- **建议**：要么补实现（`WorkspacePage.keyPressEvent` 里处理 Space/Delete，预览行加 `mouseDoubleClickEvent`），要么从 README 删掉这两行。

### B11 应用一个预设，会顺手清空当前目录和已扫结果

- **位置**：`ui/pages/workspace.py:1616-1628`（`apply_preset` 里 `self.state = A.FormState()`）
- **复现**：工作台选好目录 → 扫描 → 去「预设」页点「应用」→ 回到工作台：
  目录输入框被清空、`bundle=None`（预览表退回空状态），必须重选目录。
- **原因**：预设本来就不含根目录（`scan_dict()` 不写 root，页面文案也写着"不含根目录"），但重建 `FormState()` 把 root 一起丢了。
- **建议**：保留旧 root 与旧扫描结果，只替换规则链与扫描/编号选项：

  ```python
  root, only_old = self.state.root, self.bundle
  self.state = A.FormState(root=root)
  ...
  self.bundle = only_old if only_old else None   # 并置 stale=True 提示需重新生成
  ```

### B12 LLM 页切走再切回，会静默清空刚输入、还没保存的密钥

- **位置**：`ui/window.py:240-241`（`_goto(3)` 每次调 `page_llm.reload(False)`）+ `ui/pages/llm_settings.py:455-459`
- **复现**：供应商配置页粘贴 Key（失焦后会显示"有未保存的修改"）→ 点侧栏切到别的页 → 切回来：输入框空了、徽标回到"与磁盘一致"，**没有任何提示**。
- **建议**：`_goto` 里对 LLM 页只在 `not page._dirty` 时 reload；或者在 reload 前弹一次"有未保存修改，是否放弃"。

---

## 3. P2 —— 逻辑瑕疵与健壮性

### P2-1 `--step 0` 不回落成 1，而是一批同名（全被判冲突）

- **位置**：`engine/plan.py:146-151`（`with_number_rule`：`step=got.get("step", opts.step) or 1`）
- **说明**：这条 `or 1` 只在**链里有编号规则**时生效。`--rule "template={n}" --step 0` 走的是 `PlanOptions(step=0)`，
  `assign_numbers` 直接 `num = start + i * 0` → 所有对象拿到同一个号码 → 整批"与本批次其它 N 项同名" → 什么都改不了（好在不会误落盘）。
- **建议**：在 `assign_numbers` 或 `PlanOptions.__post_init__` 里把 `step == 0` 规整为 1，并给一句提示；与记忆里 v1.5.0 记录的"`--step 0` 静默回落为 1"保持一致。

### P2-2 文件夹被删空时报「规则未改变名称」

- **位置**：`engine/common.py:140-148`
- 与 B2 同源：`new_name == ""` 时统一塞回原名并标 `same`。对用户来说"我明明加了删除规则"和"规则没生效"是两件事，应该报"无效：规则把名字删空了"。

### P2-3 补零宽度 / 随机串长度没有上限

- **位置**：`engine/rules.py:384-392`（`pad_numbers`）、`rules.py:600-613`（`randomize`）、`rules.py:553-568` + `template.py:222-232`（`number`）
- **实测**：`b=20000000` 的编号规则一次产出 2000 万字符（0.01s，20MB 常驻）；`randomize a=20000000` 需要 **7.2s** 生成一个名字。
  1500 个对象时就是 30GB 内存 + 数小时，界面会一直"处理中"。落盘前有 255 上限兜底（结果会被判"名字过长"），所以不会写坏文件，但足以把程序拖死。
- **建议**：在 `Rule` 归一化或 `rule_problems` 里给一个上限（例如宽度 ≤ 32、随机长度 ≤ 64、号码位数 ≤ 12），超出直接判参数无效并提示。

### P2-4 `normalize()` 对程序化构造的脏 Rule 会崩

- **位置**：`engine/rules.py:248`、`rules.py:261-267`
- **复现**：`R.normalize(R.Rule(kind="llm", extras=None))` → `AttributeError: 'NoneType' object has no attribute 'get'`；
  `R.normalize(R.Rule(selector=3))` → `'int' object has no attribute 'strip'`。
  走 JSON/命令行入口时不会触发（`rules_from_json` 已经清洗过），但 `make_rule(..., extras=None)` 这类直接构造会踩到。
- **建议**：

  ```python
  rule.selector = str(rule.selector or "text").strip().lower()      # 先转文本
  ex = rule.extras if isinstance(rule.extras, dict) else {}
  rule.extras = ex
  ```

### P2-5 `make_rule("未知类型")` 静默变成"文本替换"

- **位置**：`engine/rules.py:243-244`（`if kind not in RULE_KIND_TEXT: kind = "replace"`）
- 手改过的预设里写错一个 kind，会静默多出一条 `replace(a="")` 的空规则（什么也不做），排查很久。建议保留未知 kind 并在界面标红，或至少给一条 warning。

### P2-6 临时名抢救功能没有入口

- `common.find_temp_leftovers()` / `adapters.temp_leftovers()` / `workers.leftovers_task()` 三层都写好了，**界面和命令行都没有调用点**。B1 一旦发生，用户只能手工重命名。建议在执行失败后于状态槽给一个"发现 N 个临时名文件，点此恢复"的入口。

### P2-7 本机回环地址也走系统代理，本地模型可能"连不上"

- **位置**：`engine/provider.py:458-481`（`_request` 直接用 `urllib.request.urlopen`）、`engine/llm.py:513-531`
- **实测**：本机 `HTTP_PROXY/HTTPS_PROXY=http://127.0.0.1:7370`，测 `127.0.0.1:1` 时得到的错误是代理返回的
  "网关错误（HTTP 502）upstream connect failed … os error 10061"，而不是 urllib 自己的"连不上服务器"。
  后果：① 本地 Ollama（`127.0.0.1:11434`）在有系统代理的机器上会莫名失败；② `run_tests.py` 的 F8/F9 会随代理状态时红时绿（本轮首次运行 F8 就因代理未起来而失败，第二次自动通过）。
- **建议**：对 `localhost/127.0.0.1/::1` 显式绕过代理（`build_opener(ProxyHandler({}))`），或在错误里区分"是代理报的错"。

### P2-8 `ScanOptions.selected` 名义是"绝对路径"，实际必须是 `nkey()` 归一化后的键

- **位置**：`engine/scan.py:184`（字段注释）+ `scan.py:234`、`260-273`（`hit()` 直接拿 `nkey(path)` 与集合比对）
- **实测**：直接传原始绝对路径（大小写与磁盘一致）→ 所有选中项静默不命中，只返回 `mixed_scope` 兜底的那部分（本轮 16 项变成 14 项，且是**别的目录**的内容）。
- **建议**：`scan()` 入口统一 `{nkey(p) for p in opts.selected}`，或把字段改名 `selected_keys` 并写清契约。

### P2-9 预览表"所在文件夹"列用 `startswith(root)` 截断，同前缀目录会显示错乱

- **位置**：`ui/pages/workspace.py:705-709`
- `root = D:\photo`、`folder = D:\photo2\x` → 显示成 `2\x`。建议改成 `os.path.normcase(folder) == normcase(root)` 或 `startswith(root + os.sep)` 再截。

### P2-10 `appdata_dir()` 每次调用都跑一遍旧目录迁移

- **位置**：`engine/__init__.py:47-50` + `102-138`（`migrate_legacy_appdata` 里 `os.walk` 整个 `%APPDATA%\NameKit`）
- 实测本机 200 次 `appdata_dir()` ≈ 0.10s（每次 0.5ms，纯开销）。LLM 页每次 `_refresh_state()` 都会经 `config_path()` 走到这里，界面上连续操作会累积。
- **建议**：迁移结果用模块级标志记一次（`_MIGRATED = True`），或改成首次访问时执行。

---

## 4. P3 —— 测试 / 文档 / 代码卫生

### P3-1 `run_tests.py` 的 L1 用例读真实 `%APPDATA%`，用户改过要求文件就必红

- **位置**：`verify/run_tests.py:594-595`
- `eq(L.read_instruction(), L.DEFAULT_INSTRUCTION)` 依赖"`%APPDATA%\NameCat\llm_instruction.txt` 不存在"。
  本机该文件已被用户改过，于是每次跑自检都有一条红：

  ```
  得 '不改动增删原有文件名 仅仅把日文翻译成中文 不要做多余的操作'，期望 '把文件名里没有意义的片段去掉…'
  ```
- **建议**：用例开头 `os.environ[L.INSTRUCTION_ENV] = <沙箱路径>`（文件末尾的 F11 已经在用这个套路），并在 `finally` 恢复。这样自检在任何用户机器上都稳定。

### P3-2 F8/F9 依赖"本机没有系统代理"

- 同上 P2-7。建议测试里临时清空 `HTTP(S)_PROXY` 或注入 `no_proxy=127.0.0.1`。

### P3-3 `template.expand_escape()` 定义了却从未被调用

- **位置**：`engine/template.py:184-198`
- 模块 docstring 明确写着"补齐 ReNamer 的细节能力：`\x41` / `\x{1F600}` 转义"，但 `render()` 里没有调用，
  `grep expand_escape` 全项目只有定义那一行。实测 `T.render("\\x41")` 原样返回 `'\\x41'`。
- **建议**：要么在 `render()` 出口调用一次 `expand_escape(out)`（注意 `\n` 之类会不会误伤普通文本），要么删掉这个函数与 docstring 里的承诺。

### P3-4 `KIND_FIELDS` / `KIND_FLAGS` 里 `number`、`extension` 重复定义

- **位置**：`adapters/engine_adapter.py:396` 与 `413`（`"number"` 出现两次，前者是死代码）；`449` 与 `455`（`KIND_FLAGS["extension"]` 被覆盖两次）
- Python 字典字面量后者覆盖前者，当前行为正确，但读代码的人会以为 `number` 只暴露了 `c` 一个字段。建议删掉前一处。

### P3-5 `find_temp_leftovers` 跳过日志目录用了大小写敏感比对

- **位置**：`engine/common.py:391-393`
- `if skip in dirnames` 里的 `"Name_backup"` 与 `"name_backup"` 对不上——这正是 v1.5.0 在 `scan.py` 修过的同一类坑（那边用小写表 `LOG_DIR_SKIP`）。建议复用同一张表。

---

## 5. 已验证正常的功能（覆盖矩阵）

| 模块 | 验证内容 | 结果 |
|---|---|---|
| `rules.py` 规则链 | 28 种规则逐条（GUI 全量添加 + 引擎级断言）；别名 `serialize/upper/clean/s2t/regexp/ai`；实例化/序列化/描述文本 | ✅ 除 B3（`until` 作用范围）外全部符合预期 |
| 编号 | 4 种范围（global/folder/kind/folder_kind）、起始值、步长、补零、倒序、`before/after` 锚点、`renumber` 整名换号码并保留扩展名 | ✅（P2-1 为边界） |
| 模板 | `{n}` `{n:03}` `{name}` `{stem}` `{fullname}` `{ext}` `{extdot}` `{parent}` `{index}` `{total}` `{size*}` `{mtime}` `{now}` `#Name#` `%name%` `<file.name>` `$Counter(a,b,w)` `$Date(...)` `@@n@@`；未知变量→空串；未闭合花括号原样保留 | ✅（P3-3 转义未接线） |
| 扫描 | 6 种过滤（类型/扩展名白黑名单/名字包含排除/隐藏）、10 种排序、递归深度、选中项 3 种语义、文件头校验、日志目录跳过、自然序 | ✅ |
| 两遍校验 | ok/same/conflict/error/skip 五种状态；批内重名；目标已存在；只改大小写；保留名/非法字符/结尾点空格/255 上限 | ✅ 除 B2（空主干） |
| 落盘 | 直改、临时名中转（文件互换、文件夹互换、父子同时改名）、失败回滚、只改大小写、目标不覆盖 | ✅ 除 B1（落位失败的回滚） |
| 撤销 | 完整往返、多层嵌套（父先还原）、目标已存在不覆盖、成功后删日志 | ✅ 除 B5（部分成功） |
| 日志 | 5 段新格式命名、按目录指纹隔离、每目录 20 份 + 总量 200、旧 4 段格式只读兼容、`NAMECAT_LOG_DIR` 覆盖 | ✅ |
| 预设 | 保存/读取/往返、文件名净化、脏字段兜底、内置预设增删（隐藏名单） | ✅ |
| LLM | 无 Key 时规则原样放行并给原因、模型输出净化（非法字符/保留名/截断/3 种风格）、5 种回复形状解析、Echo 离线替身、三种协议的请求体与鉴权头、401/404/连不上的人话翻译 | ✅（P2-7 代理相关） |
| 密钥 | DPAPI 加密往返、清空、`has_secret`、不写入 json/预设/日志 | ✅ |
| CLI | `rename/preview/undo/rules/presets` 退出码、预览不落盘、落盘+撤销往返、非法规则提示、GBK 符号降级 | ✅ 除 B7/B9 |
| 界面 | 4 个页面构造与全流程（加全部 28 种规则 → 扫描 → 算名 → 勾选 → CSV → 执行 → 撤销 → 预设 → LLM 页） | ✅（`ui_smoke.py` 全绿） |

### 性能基线（1500 个文件，实测）

| 阶段 | 耗时 |
|---|---|
| 扫描 | 0.04 s |
| 算名 + 两遍校验（2 条规则） | 0.78 s |
| 落盘（含写日志） | 8.0 s（≈5 ms/文件，Windows Defender 实时扫描主导） |
| 撤销 1500 项 | 5.2 s |

无内存泄漏迹象：`TaskRunner` 的线程对象在 `finished` 后从 `_alive` 摘除，QRunnable 设了 `setAutoDelete(True)`；界面列表重建时先 `setParent(None)` 再 `deleteLater`，未见控件堆积。

---

## 6. 本轮新增的自动化用例

**`verify/test_edges.py`（新增，可独立运行）**

```bat
set PYTHONPATH=_vendor
python verify\test_edges.py        :: 153 断言 + 16 个缺陷探针，退出码 0 表示"没有回归"
python verify\test_edges.py -v     :: 逐条打印
```

设计要点：

- `check()/eq()`：**期望行为**，必须通过；失败退出码 1。这一批就是上面"覆盖矩阵"里那些原本没有断言覆盖的场景。
- `defect(name, reproduces)`：**缺陷探针**。缺陷还在 → 打印"仍存在"；修好后自动变成"已修复"。所以这份文件可以直接进 CI，**不会因为已知缺陷而永远红**。
- 覆盖分组：`A` 空名/超长、`B` 落盘与回滚、`C` 跨盘、`D` 撤销与日志（含轮转、脏文件、只读目录）、`E` 扫描、`F` 编号/选择器/模板、`G` 规则归一化与预设脏数据、`H` LLM 离线层、`I` CLI 退出码、`J` 界面（离屏，无 Qt 时自动跳过）。
- 所有临时数据落在 `_verify_edges/`，日志通过 `NAMECAT_LOG_DIR` 隔离，跑完自动清理，不污染程序目录与 `%APPDATA%`。

建议把这些用例并入 `run_tests.py` 的分组（新增 `test_edges`），或在 CI 里两个脚本都跑。

---

## 7. 建议的修复顺序

1. **B3（`until` 一行公式）**、**B2（空主干校验）** —— 改动最小、影响最大。
2. **B1（回滚残留）+ P2-6（抢救入口）** —— 数据可见损坏，且已有兜底代码可用。
3. **B4（跨盘）+ B11（预设清目录）** —— 把"看起来支持但一用就废"的路径修通。
4. **B5/B6（撤销日志）**、**B7/B8/B9（日志与 CLI 容错）** —— 一组，都是"异常输入 → 堆栈/静默失败"。
5. **P2-1/P2-3（参数规整与上限）**、**P2-4/P2-5（归一化容错）**。
6. **P3 组**：测试隔离（P3-1/P3-2）建议同步做，否则自检的红绿不反映代码质量。

---

## 8. Rust 引擎迁移（1.6.3）—— 性能与打包验收

引擎 12 个模块全部由 Python 改写为 Rust（`crates/namecat-core` + PyO3 绑定
`crates/namecat-py`）。2.0.0 时 `engine/*.py` 曾保留一层"一行 re-export"的转发壳，
2.0.1 已全部删除（调用方直接 `from namecat_py.<模块> import …`）。

**行为等价性**在迁移期由三份逐字段对拍脚本把关（合计 3170 项 0 差异）。那些脚本与
参考实现是迁移期脚手架，2.0.1 一并清理；需要复核时从 `v1.6.3` 标签取回 Python 实现
即可重跑。本节只记**性能**与**发布链路**。

### 8.1 基准（2000 个文件，同一规则链，新旧对跑）

`PYTHONPATH=_vendor py -3 verify/bench.py`（脚本已入库，不再是临时脚手架）

同一批文件、同一条规则链（`replace` + `remove_chars` + 三位 `number`）、
同一个调用姿势，一边是 Rust 引擎，一边是 `_verify_tmp/ref/refengine/` 里
换壳前的 Python 实现。每个阶段 7 轮取中位数（前置 1 轮预热）。

| 阶段 | Python 引擎 | Rust 引擎 | 倍率 |
|---|---|---|---|
| 扫描（2005 项，含递归） | 50.2 ms | **30.5 ms** | **1.65×** |
| 纯算名（跑完整条规则链） | 27.1 ms | **22.4 ms** | **1.21×** |
| 算名 + 两遍校验 | 80.1 ms | 83.0 ms | 0.96×（持平） |
| 扫描 + 算名（`scan_and_plan`） | 136.9 ms | **116.1 ms** | **1.18×** |
| 落盘 2005 项 | 611 ms | 621 ms | 0.98×（持平） |
| 撤销 2005 项 | 1133 ms | 1232 ms | 0.92×（持平） |

怎么读这张表：

- **扫描是唯一显著的收益（1.65×）**：Python 版每个条目要走 `os.stat` +
  `os.scandir` 两套对象，Rust 版直接 `GetFileInformationByName`（Win11）/
  `GetFileAttributesExW` 三级回退，且排序键预计算。
- **算名 1.21×**：规则链本身是字符串处理，Rust 快，但每项要跨一次 PyO3
  边界读 `name/new_name`，边界成本吃掉了一部分。
- **校验段持平（0.96×）**：这一段不是 CPU 密集，而是**每个目标名探一次盘**
  （2005 次 `path_exists`）+ 每次比较跑一遍 `LCMapStringEx` 规范化。
  为了跟 Python 的 `str.lower()` 完全同解（`İ`/`ǅ`/`Σ` 这些大小写特例），
  `normcase` 只能用 `LCMapStringEx`，单次约 3.6 µs，比 C 里的 `.lower()` 贵。
  已经做过一次优化：源路径的 `nkey` 在建 `owner` 表时顺带存下，不再每项重算
  （98 ms → 90 ms → 稳定后 83 ms）。剩下 3 ms 属噪声与 syscall 差异。
- **落盘/撤销持平**：纯 I/O，本机 Windows Defender 实时扫描主导，两者都
  约 0.3 ms/文件；Rust 侧用的是 `MoveFileExW(flags=0)`（不覆盖目标），
  与 `os.rename` 语义一致。撤销的抖动在 ±10% 上下，与实现无关。

**结论**：这次重写的目的不是提速（引擎本来就不是瓶颈），收益集中在扫描；
其余阶段与 Python 版持平。真正拿到的是**单一实现**：界面与命令行共用同一份
逻辑，不再有"两套代码各自演化"的风险。

### 8.2 打包链路（P5）

Rust 化之后的第一个发布陷阱：应用层到处是
`from namecat_py.xxx import …`，**引擎不在包里 = 双击没反应**（窗口程序没有
控制台，`ImportError` 只会弹一个框，进程卡住）。实测确认过这个问题真实存在：
只靠 `--paths _vendor` 时，PyInstaller 的所有 TOC 里 `namecat_py` 命中数为 **0**。

`build_exe.py` 现在有四道闸：

| 闸 | 检查 | 失败时 |
|---|---|---|
| 1 | `_vendor/namecat_py*.pyd` 存在且唯一 | 直接退出，提示 `py -3 rust_build.py` |
| 2 | 扩展 ABI 与打包解释器一致（`cp315` vs 3.15） | 直接退出（编译期不报，运行期必崩） |
| 3 | `.pyd` 不比 `crates/**/*.rs` 旧 | 直接退出（`--allow-stale-engine` 可压掉） |
| 4 | `--add-binary <pyd>:.` 显式把扩展塞进包 | —— |
| 5 | 打包后自检：沙箱里 `rename → undo` 往返 | 报错退出 |

自检为什么看文件效果而不是 stdout：窗口程序默认没有控制台，`print` 被
`engine/cli.py` 的流包装器丢掉，看输出永远是"什么都没发生"。另外自检把
`TEMP`/`TMP` 也指进沙箱 —— 单文件 exe 启动时要往 `%TEMP%` 解包自身，
系统 Temp 不可写时会弹 `Could not create temporary directory!` 然后卡住，
这一条在沙箱环境里第一次跑就踩到了（表现为自检 60 秒超时）。

### 8.2.1 exe 瘦身（41.2 MB → 29.8 MB）

PyInstaller 命令行没有"排除某个二进制"的开关，只有 `--exclude-module`（管 Python
模块）。所以 `build_exe.py` 改成**自己生成 `.spec`** 再构建，在 spec 里过滤
`a.binaries`（spec 是构建产物，路径随机器变，不入库）。

剔除清单（`build_exe.py` 的 `DROP_BINARIES`，每条都写了理由）：

| 条目 | 压缩后 | 为什么能删 |
|---|---|---|
| `opengl32sw.dll` | 7.5 MB | Qt 的软件 OpenGL 兜底。QWidget 走 raster 绘制，不用 OpenGL；只有显卡驱动缺失的老机器才可能需要（`--keep-opengl` 可保留） |
| `QtNetwork.pyd` + `Qt6Network.dll` + 3 个 TLS 插件 + networkinformation | 1.4 MB | 程序里所有联网都走 Python 的 urllib，没有一处用 `QNetworkAccessManager` |
| `qdirect2d` / `qminimal` / `qoffscreen` 平台插件 | 0.5 MB | 真正需要的只有 `platforms\qwindows.dll` |
| `qtuiotouchplugin` | 46 KB | 桌面鼠标操作不用触摸板手势 |
| `qtiff` / `qicns` / `qtga` / `qwbmp` 图片格式 | 0.2 MB | PNG/BMP 由 Qt 内建；JPG/SVG/ICO/GIF/WEBP 保留 |

结果：**41.2 MB → 29.8 MB**（zip 40.9 → 29.6 MB），包内二进制 60 → 41 个。
`libssl`/`libcrypto`（Python urllib 发 HTTPS 要用）与引擎 `.pyd` 都保留。

删完必须验两件事，否则是"能打包、不能跑"：
1. **窗口真能渲染**（`opengl32sw` 没了）：拿 `user32.FindWindowW` 查窗口句柄 +
   `GetWindowRect` 读几何，实测 1454×938、可见、响应正常；
2. **改名 → 撤销**仍走通（打包后自检里已含这一条）。

### 8.3 本轮实际验收

| 项 | 结果 |
|---|---|
| `cargo test -p namecat-core` | 112 项通过 |
| 符号闸门 `verify/rust_check.py --fail-on-any` | 12/12 模块，根常量一致 |
| 对拍 `p2` / `p3` / `p4` | 1182 + 287 + 1701 项，0 差异 |
| 打包 `py -3 build_exe.py --zip` | exe **29.8 MB** + zip 29.6 MB（瘦身前 41.2 / 40.9），含引擎扩展 |
| 打包后自检（改名 → 撤销） | 通过 |
| exe 图形界面启动 | 窗口标题 `NameCat 1.6.3`，响应正常 |
| exe 命令行 `rename … --apply` | 2 项改名成功，撤销日志正常写入 |

### 8.4 2.0.1：移除 Python 兼容层

2.0.0 保留的 12 个转发壳（`engine/<模块>.py` 里只有 docstring + 一行
`from namecat_py.<模块> import *`）在 2.0.1 全部删除，调用方直接导入扩展。
改动面：

| 位置 | 改了什么 |
|---|---|
| `engine/` | 删 12 个转发壳，只剩 `__init__.py`（应用常量 / appdata）、`cli.py`、`llm_resolve.py` |
| 应用代码 | `adapters/engine_adapter.py` 11 行、`ui/pages/workspace.py` 1 行、`engine/cli.py` 10 行 |
| 自检脚本 | `verify/{run_tests,test_edges,ui_smoke,llm_save_check}.py` 共 25 行 |
| Rust 侧 | `py_ops.rs` 里 7 处 `py.import("engine.xxx")` → `namecat_py.xxx`（另外 3 处 `engine` 根包调用保留：`appdata_dir` / `backup_dir` / `llm_resolve`） |
| 闸门 | `verify/rust_check.py` 改为直接查 `namecat_py`（期望集仍是迁移前固化的 193 符号）；`--snapshot` 模式删除（已无处可取纯 Python 符号） |

同时修掉两个验证工具的问题：

- **`run_tests.py` 会删掉别人的验证资产**：它的沙箱根是 `_verify_tmp/`，收尾
  `rmtree` 整目录，把放在同一目录下的对拍脚本与参考实现一起删了。改为自己的
  `_verify_run/`，各套件（`_verify_edges/`、`_verify_gui_tmp/`、`_verify_bench/`）互不牵连。
- **对拍脚本在临时目录里**：`bench_engine.py` 随之丢失，重写为入库的 `verify/bench.py`。

验收（2.0.1，全部实测）：

| 项 | 结果 |
|---|---|
| `verify/rust_check.py --fail-on-any` | 12 个模块 / 193 个符号，根常量 17 项一致 |
| `verify/run_tests.py` | **410 断言通过 / 0 失败**（8.2 s） |
| `verify/test_edges.py` | **190 断言通过 / 0 失败**，16 个缺陷探针全部"已修复" |
| `verify/ui_smoke.py` | 全部规则 + 全流程 + 撤销 + 预设 + LLM 页，全通过 |
| CLI 往返 | `rename --apply` → `undo` 文件名正确还原 |
| 打包 | exe 29.8 MB / zip 29.6 MB，打包后自检通过 |

### 8.5 2.1.0：大目录（9000 文件）卡顿治理

用户反馈："导入大量文件和预览改名，每次要卡很久，约 9000 个图片文件"。
离屏实测（1440×900，9000 张 jpg，规则链 `replace` + 三位 `number`）定位到**三处**
互相独立的瓶颈，全部修掉：

| 环节 | 改前 | 改后 | 原因 |
|---|---|---|---|
| 建预览表（9000 行） | **259 s** | **52 ms** | 每行 13 个 QWidget×9000 ≈ 12 万个 QObject |
| 单行勾选 | 742 ms | **9 ms** | 每次点击全表遍历 + 逐行改样式表 |
| 全选 / 全不选 | 817 ms | **42 ms** | 同上，且逐控件 `setChecked` |
| 滚动一屏 | 十几至几十 ms（且越长越慢） | **3.2 ms** | 所有行都是真实控件，布局全量参与 |
| 拖入 9000 个文件（主线程） | 280 ms | **84 ms** | 逐个 `os.path.exists` + 对 9000 条路径算 `commonpath` |
| 扫描（选中 9000 个文件） | 733 ms | **282 ms** | 每个条目 `nkey()`（`GetFullPathNameW`）+ 每目录遍历选中集 |
| 扫描（选中 N 个目录） | 251 ms | **121 ms** | 与整目录扫描持平（原先多一层 O(目录数×选中数)） |

**界面侧**（`ui/pages/workspace.py`）

- 预览表从「一行一棵控件树」改成**自绘 + 只画可见行**（`PreviewTable`）：
  行数据是 7 段预计算文本，`paintEvent` 只遍历 `visibleRegion()` 覆盖的那一段，
  绘制成本与总行数无关（滚 50 屏每屏 3.2 ms）。
- 列几何**直接读表头控件的真实 geometry**，不再按公式重算 —— 自己算总差
  4~15 px，长文件名会在表头网格外面多吃一个字。现在 8 列逐像素对齐。
- `_row_widgets()` 保留方法名与 `_cb` / `_item` 属性面，但返回的是轻量句柄
  （`RowHandle`），自检脚本与老调用点不用改，也不会再建控件。
- 单行勾选走新增的 `A.set_excluded_one()`（O(1)），批量操作才走
  `A.set_excluded()`（O(n)），两者语义共用同一份排除状态存取。
- `add_paths()`（拖放入口）：去重 → **抽样**核实存在性（全量 stat 是 O(n) 次
  系统调用）→ 对**去重后的父目录**算 `commonpath`（106 ms → 3 ms）。
- `paintEvent` 补 `fillRect` 底色：控件是 `WA_OpaquePaintEvent`，不铺底会留残影。

**引擎侧**（`crates/namecat-core/src/scan.rs`）

- `selected` 语义判定不再「每个条目 × 每个选中项」：新增 `ancestor_in()`
  （逐层上溯，每层一次哈希）与 `selected_ancestors()`（祖先目录集，算一次），
  `walk_dir` 里「这个目录下面还有没有选中项」变成 O(1) 查表。
- 身处选中子树内部（`deep=true`）时**跳过 selected 过滤**：里面的一切都是要的。
- 条目路径键由「父目录键 + 名字」直接拼（`child_key()`），不再每个条目跑
  `nkey()`（`abspath` → `GetFullPathNameW`，9000 个约 100 ms）。
- 选中项命中时用现成的 `is_dir` 判类型，不再 `Path::is_dir()` 多一次 stat。

**验收**（2.1.0，全部实测）

| 项 | 结果 |
|---|---|
| `cargo test --release -p namecat-core` | 112 + 1 项通过 |
| `verify/run_tests.py` | 410 断言通过 / 0 失败 |
| `verify/test_edges.py` | 190 断言通过 / 0 失败，16 个缺陷探针全"已修复" |
| `verify/ui_smoke.py` | 全流程通过 |
| `verify/rust_check.py` | 12 模块 / 193 符号一致 |
| `selected` 语义回归 | 选文件=只要它、选目录=要整棵子树、目录+其内文件混选=子树、`strict_selected`、大小写不同路径，五种口径结果均正确 |
| 9000 文件端到端 | 拖入 84 ms → 首屏 502 ms → 生成预览 580 ms；交互全部 < 50 ms |

**口径说明**：`生成预览` 那 ~580 ms 里，引擎 `build()`（算名 + 两遍校验，
9000 项要逐个探盘）占 434 ms，界面建表只占 ~50 ms。校验段的 syscall 成本是
真实工作（Defender 实时扫描主导），不是可以靠虚拟化消掉的东西。
