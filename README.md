# NameCat

**Windows 批量重命名工具**：扫描文件 / 文件夹 / 图片 / 视频，用一条可排序的规则链算出新名字，预览校验后落盘，全程可撤销。PySide6 桌面界面 + 可脚本化命令行，共用同一套引擎。

![工作台](docs/screenshots/app1.png)

## 功能

- **扫描**：文件 / 文件夹 / 图片 / 视频（图片、视频可选文件头魔数校验，防止扩展名骗人）；名称与扩展名过滤、自然序或按时间大小排序、递归深度控制；支持直接拖入文件与「扫描对象…」精确指定。
- **规则链**：27 种规则，自上而下依次作用（顺序即语义），可启停、排序、复制；每条规则可用「作用范围」限定到名字的某一段。分组：增删 / 切取 / 修饰 / 编号与序列 / 模板与规范化 / 智能。
- **编号**：起始值 / 步长 / 补零宽度，编号范围四档（整批连续、每文件夹、每类型、文件夹+类型）。
- **预览先行**：所有新名字先算好再动磁盘；两遍校验（名字语法 + 批内重名 + 盘上占用），冲突与无效项一目了然，勾选即排除。
- **安全落盘**：临时名中转处理链式改名与父子同时改名，任一步失败自动回滚；每次执行写撤销日志（保留最近 20 条），可整批撤销。
- **预设**：内置 4 个常用预设，自定义预设存为 JSON（不含路径与密钥），一键应用。
- **LLM 智能精简（可选）**：把 `IMG_20240601_拷贝(2)` 这类名字精简成可读短语；支持 OpenAI 兼容接口 / 本地 Ollama / 离线替身，离线时自动跳过。
- **命令行**：全部能力都可脚本化，界面与命令行共用同一引擎，行为完全一致。

## 界面

| 工作台 | 预设 |
|---|---|
| ![工作台](docs/screenshots/app1.png) | ![预设](docs/screenshots/app2.png) |
| **执行记录** | **LLM 设置** |
| ![执行记录](docs/screenshots/app3.png) | ![LLM 设置](docs/screenshots/app4.png) |

工作台右上角的 ⤢ 按钮可把预览表放大到整页（Esc 还原）；规则多时列表在卡内滚动。

## 运行

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
- 字体 HarmonyOS Sans SC 随仓库携带，无需安装。
- 双击没反应时看 `%TEMP%\NameCat_startup_error.log`；再用 `namecat.bat` 启动可直接看到 traceback。

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

- `--only` 支持 `file / dir / image / video / media / all`
- 规则写法 `--rule 名称=参数1=参数2`，可重复多条按顺序生效；`--rule "case=lower;sel=after;sep=-"` 可限定作用范围
- 默认**只预览**，加 `--apply` 才落盘；`--csv` 导出预览结果；`--preset` 直接使用预设文件

## 工作流与安全设计

```
扫描 ──► 编号 ──► 规则链算名 ──► 两遍校验 ──► 预览 ──► 执行 ──► 撤销日志
```

- 预览显示的名字就是落盘的名字：执行时直接使用已算好的计划，**绝不重算**。
- 冲突项默认**跳过、不覆盖**；执行采用临时名迁移 + 失败回滚。
- 撤销日志保存在 `<目标目录>\_rename_logs\`，按日志逐条还原；目标已存在时跳过而不是覆盖。
- 状态五档：待重命名 / 名称未变 / 冲突 / 无效 / 已跳过。

## 快捷键

| 键 | 动作 |
|---|---|
| `F5` | 重新扫描 |
| `Ctrl+Enter` | 按当前规则生成预览 |
| `Ctrl+O` | 选择目录 |
| `Ctrl+S` | 保存为预设 |
| `Ctrl+1…4` | 切换四个页面 |
| `空格` / 双击 | 勾选 / 取消勾选预览行 |
| `Delete` | 删除选中的规则 |

## LLM 智能精简（可选）

在规则链里加一条「LLM 智能精简」即可。密钥**只从环境变量读取**，界面不回显、不落盘：

```bat
set NAMECAT_LLM_API_KEY=sk-...
set NAMECAT_LLM_BASE_URL=https://api.deepseek.com
set NAMECAT_LLM_MODEL=deepseek-chat
```

也认 `OPENAI_API_KEY` / `DEEPSEEK_API_KEY`。整理要求可在 `%APPDATA%\NameCat\llm_instruction.txt` 里自由编辑（「LLM 设置」页或「打开配置文件」），没配密钥时该规则自动跳过、其余规则照常工作。

## 数据与隐私

- 界面偏好与预设：`%APPDATA%\NameCat\`（不保存任何 API Key）。
- 撤销日志：`<目标目录>\_rename_logs\rename_*.json`。
- 扫描会自动跳过 `_rename_logs` 目录，避免把日志当文件改名。

## 代码结构

```
main.py                 入口：无参数开 GUI，其余走命令行
ui/                     桌面界面（PySide6）
  base.py                 设计令牌、字体、图标、基础控件
  window.py               顶栏 / 侧栏 / 页面栈 / 状态栏
  pages/                  工作台 / 预设 / 执行记录 / LLM 设置
  fonts/                  HarmonyOS Sans SC（可变字体）
adapters/               界面 ⇄ 引擎的唯一通道（FormState ⇄ 引擎参数）
engine/                 重命名引擎（纯标准库，无界面依赖）
  scan.py plan.py rules.py template.py context.py llm.py
  common.py ops.py presets.py cli.py
workers/                后台任务（QRunnable + 线程池，信号回主线程）
verify/                 自检：run_tests.py（引擎 315 断言）、ui_smoke.py（界面交互冒烟）
docs/                   设计文档、设计稿与界面截图
```

**分层约定**：`ui/` 只调 `adapters.engine_adapter`，不直接碰 `engine/`；`engine/` 是纯逻辑、零 Qt 依赖，所以命令行与界面行为永远一致。改界面前先读 `docs/GUI_DESIGN_GUIDE.md`（功能层级、数据契约、状态机与踩坑记录）。

自检（改动后必跑）：

```bat
py -3 verify\run_tests.py     :: 引擎 + 端到端 + 回归
py -3 verify\ui_smoke.py      :: 界面交互冒烟（离屏，不需要显示器）
```

---

NameCat 1.2.0 · 界面基于 PySide6 6.10 · 设计稿见 `docs/design-preview/boards.png`
