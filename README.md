# NameCat

Windows 批量重命名工具。扫描文件 / 文件夹 / 图片 / 视频，用一条可排序的规则链算出新名字，**先预览、再落盘、全程可撤销**。

PySide6 桌面界面 + 可脚本化命令行，共用同一套引擎，行为完全一致。

![工作台](docs/screenshots/app1.png)

`v1.5.1` · Windows · Python 3.10+ · 界面基于 PySide6 6.10

---

## 三条底线

1. **预览显示的名字就是落盘的名字** —— 执行时直接用已算好的计划，绝不重算（LLM 不会被请求两遍，随机规则不会跑偏）。
2. **冲突项跳过、绝不覆盖** —— 批内重名、盘上占用、非法名字在校验阶段就标出来，勾一下就能排除。
3. **每一步都可撤销** —— 临时名中转处理链式改名与父子同时改名，任一步失败自动回滚；执行后写撤销日志，可整批还原。

## 功能

**扫描**
- 对象四类：文件 / 文件夹 / 图片 / 视频，可混合；图片、视频支持文件头魔数校验（扩展名骗不了它）
- 名称与扩展名过滤、只含 / 排除关键词；递归深度控制、隐藏项开关
- 排序十档：自然序 / 时间 / 大小 / 路径 / 倒序，**先排序再编号**
- 直接拖入文件与文件夹，或用工具栏的「选择…」精确指定（选一个文件夹当根目录，或选多个文件当「只要这几个」）

**规则链**
- **27 种规则**，自上而下依次作用（顺序即语义），可启停、拖动排序、复制
- 每条规则可用「作用范围」限定到名字的某一段（某字符之前 / 之后、区间、第 N 次出现）
- 六个分组：增删 / 切取 / 修饰 / 编号与序列 / 模板与规范化 / 智能
- 编号（起始值 / 步长 / 补零 / 编号范围 / 倒序 / 插入位置）全在「编号」一条里，链上没有第二个编号入口
- 「音视频文件序列化」把整名换成四位序号（每个文件夹各自从 0001 起、扩展名不变）
- 模板变量 `{n}` `{name}` `{ext}` `{date}` `{parent}` …，两种写法（`{var}` 与 `$Var`）都认

**校验与落盘**
- 两遍校验：名字语法（非法字符 / 保留名 / 尾点尾空格 / 超长）→ 批内重名 + 盘上占用
- 状态五档：待重命名 / 名称未变 / 冲突 / 无效 / 已跳过，原因写在行里
- 临时名中转：`A→B、B→C` 这类链式改名与父子目录同时改名都能正确落地
- 撤销日志统一存 `<程序目录>\Name_backup\`，**不往你的数据目录里塞垃圾**

**预设**
- 内置 4 个常用预设，自定义预设存为 JSON（不含路径与密钥），一键应用 / 导入导出

**LLM 智能精简（可选）**
- 把 `IMG_20240601_拷贝(2)` 这类名字精简成可读短语；离线 / 没配密钥时自动跳过，其余规则照常工作

**命令行**
- 全部能力可脚本化；默认**只预览**，加 `--apply` 才落盘

## 界面

| 工作台 | 预设 |
|---|---|
| ![工作台](docs/screenshots/app1.png) | ![预设](docs/screenshots/app2.png) |
| **执行记录** | **供应商配置** |
| ![执行记录](docs/screenshots/app3.png) | ![供应商配置](docs/screenshots/app4.png) |

四个页面：**工作台**（扫描 → 规则链 → 预览 → 执行）、**预设**、**执行记录**（查看与撤销）、**供应商配置**（LLM 接入）。

工作台右上角的 ⤢ 把预览表放大到整页（`Esc` 还原）；规则多时列表在卡内滚动。

### 快捷键

| 键 | 动作 |
|---|---|
| `F5` | 重新扫描 |
| `Ctrl+Enter` | 按当前规则生成预览 |
| `Ctrl+1…4` | 切换四个页面 |
| `空格` / 双击 | 勾选 / 取消勾选预览行 |
| `Delete` | 删除选中的规则 |

## 快速开始

```bat
NameCat.vbs        :: 双击启动（无控制台，推荐）
namecat.bat        :: 带控制台启动，报错可直接看到
python main.py     :: 等价写法
```

需要 **Python 3.10+** 与界面依赖：

```bat
py -3 -m pip install -r requirements.txt
```

- 界面需要 PySide6（6.10+）；**纯命令行不需要装任何依赖**（引擎是纯标准库）。
- 界面字体使用**系统自带 UI 字体**（Windows 的 Microsoft YaHei UI 等），不随包分发字体文件。
- 双击没反应时看 `%TEMP%\NameCat_startup_error.log`；用 `namecat.bat` 启动可直接看到 traceback。

## 命令行

```bat
:: 预览（默认不落盘）
python main.py preview D:\照片 --rule "template={n}" --width 3 --only image --recursive

:: 真正改名（写撤销日志）
python main.py rename D:\照片 --rule "case=lower" --rule "remove-after=-" --apply

:: 撤销、列出规则、列出预设
python main.py undo D:\照片
python main.py rules
python main.py presets
```

- `--only`：`file / dir / image / video / media / all`
- `--rule 名称=参数1=参数2`，可重复多条按顺序生效；`--rule "case=lower;sel=after;sep=-"` 限定作用范围
- 默认**只预览**，加 `--apply` 才落盘；`--csv` 导出预览结果；`--preset` 直接用预设文件
- 撤销失败会返回非 0 退出码（并写明是哪一项没还原），方便脚本判断

## 工作流与安全设计

```
扫描 ──► 编号 ──► 规则链算名 ──► 两遍校验 ──► 预览 ──► 执行 ──► 撤销日志
```

- 冲突项默认**跳过、不覆盖**；执行采用临时名迁移 + 失败回滚。
- 撤销按日志逐条还原（**父目录先还原**，多层嵌套也能完整复原）；目标已存在时跳过而不是覆盖。
- 扫描自动跳过 `Name_backup` 与旧版 `_rename_logs` 目录，不会把日志当文件改名。
- 每个目录保留最近 20 份撤销日志（总量上限 200），程序目录不可写时自动退回 `%APPDATA%\NameCat\Name_backup`。

## LLM 智能精简（可选）

在规则链里加一条「LLM 智能精简」即可，接入方式在「供应商配置」页配置：

- **供应商**：协议（OpenAI Chat Completions / OpenAI Responses / Anthropic Messages）、预设（自定义、OpenAI、DeepSeek、Xiaomi MiMo、OpenCode Go、本地 Ollama、离线替身）、基础 URL、API Key
- **模型**：手动添加，或一键「获取模型列表」从接口拉一份存到本地
- **高级**：模型列表 URL、自定义请求头（随每次请求发出）
- **测试连接**：真发一个请求验证地址 / 密钥 / 模型，报耗时与失败原因（401 / 404 / 连不上都说人话）

密钥**不回显**，单独存 `%APPDATA%\NameCat\llm_credential.dat`（Windows 用 DPAPI 加密，只有当前账号解得开）；地址、模型等非敏感项存 `llm_provider.json`。输入框留空表示沿用已保存的密钥。

整理要求在 `%APPDATA%\NameCat\llm_instruction.txt` 里自由编辑（同页右侧）。左下角的状态气泡显示真实连接状态与耗时；测试连接与拉模型列表都在后台线程跑，不卡界面。

命令行也认环境变量（界面保存过配置时以界面为准，命令行参数优先级最高）：

```bat
set NAMECAT_LLM_API_KEY=sk-...
set NAMECAT_LLM_BASE_URL=https://api.deepseek.com
set NAMECAT_LLM_MODEL=deepseek-chat
```

也认 `OPENAI_API_KEY` / `DEEPSEEK_API_KEY`。

## 数据与隐私

| 位置 | 内容 |
|---|---|
| `%APPDATA%\NameCat\` | 界面偏好、预设（**不含任何密钥**） |
| `%APPDATA%\NameCat\llm_provider.json` | LLM 地址 / 模型 / 请求头 |
| `%APPDATA%\NameCat\llm_credential.dat` | LLM 密钥（DPAPI 加密，不进仓库、不进日志） |
| `<程序目录>\Name_backup\rename_*.json` | 撤销日志（文件名带 8 位目录指纹，便于分开查） |

预设与撤销日志都不含密钥；日志里的路径是本机真实路径，`Name_backup/` 已列入 `.gitignore`，绝不入库。

## 开发

```
main.py                 入口：无参数开 GUI，其余走命令行
ui/                     桌面界面（PySide6）
  base.py                 设计令牌、字体、图标、基础控件
  window.py               顶栏 / 侧栏 / 页面栈 / 状态栏
  pages/                  工作台 / 预设 / 执行记录 / 供应商配置
adapters/               界面 ⇄ 引擎的唯一通道（FormState ⇄ 引擎参数）
engine/                 重命名引擎（纯标准库，无界面依赖）
  scan.py plan.py rules.py template.py context.py llm.py
  provider.py secrets.py
  common.py ops.py presets.py cli.py
workers/                后台任务（QRunnable + 线程池，信号回主线程）
verify/                 自检：run_tests.py（403 断言）、ui_smoke.py（界面冒烟）
docs/                   设计文档、设计稿与界面截图
```

**分层约定**：`ui/` 只调 `adapters.engine_adapter`，不直接碰 `engine/`；`engine/` 是纯逻辑、零 Qt 依赖，所以命令行与界面行为永远一致。改界面前先读 `docs/GUI_DESIGN_GUIDE.md`。

自检（改动后必跑）：

```bat
py -3 verify\run_tests.py     :: 引擎 + 端到端 + 回归（403 断言）
py -3 verify\ui_smoke.py      :: 界面交互冒烟（离屏，不需要显示器）
```

## 更新记录

### 1.5.1

- 工具栏的「选择目录」与「扫描对象…」合并成一个 **「选择…」**：选一个文件夹当扫描根目录，选多个文件 / 文件夹就是「只要这几个」（与拖放完全同一套逻辑）
- 修正 README 里两条并不存在的快捷键（`Ctrl+O` / `Ctrl+S`）

### 1.5.0

- **版本号统一**为 1.5.0，界面 / 命令行 / 预设元数据 / 撤销日志全部一致
- **修复**：扫描不再把 `Name_backup` 里的撤销日志当成改名对象
- **修复**：撤销失败现在会正确报错（`main.py undo` 返回非 0），不再误报成功
- **修复**：手改坏的预设 JSON 不再抛异常，能读回来的规则照常使用
- **字体**：移除随包字体，改用系统自带 UI 字体；小字号不再加粗，密集汉字更清晰
- **LLM 供应商配置**：三种协议、七个预设、DPAPI 密钥保管、后台测连通

---

NameCat 1.5.1 · 界面基于 PySide6 6.10 · 设计稿见 `docs/design-preview/boards.png`
