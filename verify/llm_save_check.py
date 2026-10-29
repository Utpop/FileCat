# -*- coding: utf-8 -*-
"""
verify/llm_save_check.py —— 「供应商配置」页保存链路检查（离屏）。

验的是「点一下保存，到底有没有真的存下来」，不是界面长得对不对：
    · 逐字段落盘并回读（名称 / 协议 / 预设 / 地址 / 模型列表 / 请求头）
    · 密钥只进 llm_credential.dat（DPAPI），json 里一个字节都不能有
    · 留空密钥 = 沿用已保存的；切预设不清密钥
    · 重开页面能把磁盘上的东西读回界面
    · 「手动输入模型名」填了没点添加、直接保存 → 名字不能丢
    · 脏标记不能说谎（加了模型之后不许还挂着「有未保存的修改」）
    · 配置目录写不进去要报错，不能假成功
    · 脏 JSON 不能让页面崩

**全程把 %APPDATA% 指向临时目录**，绝不读写用户真实的供应商配置。

    py -3 verify/llm_save_check.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# 依赖装在 _vendor 里（与 main.py 一致），不要求调用方先设 PYTHONPATH
_VENDOR = os.path.join(ROOT, "_vendor")
if os.path.isdir(_VENDOR):
    sys.path.insert(0, _VENDOR)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 沙箱：必须在导入 engine 之前改，否则配置会写进用户真实目录
_SANDBOX = tempfile.mkdtemp(prefix="namecat_llm_check_")
os.environ["APPDATA"] = _SANDBOX
for _k in list(os.environ):
    if _k.startswith(("NAMECAT_LLM_", "NAMEKIT_LLM_", "OPENAI_", "DEEPSEEK_")):
        del os.environ[_k]

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])
from ui import base  # noqa: E402

base.FAMILY = base.load_fonts()
app.setFont(base.F(12, 400))

from adapters import engine_adapter as A  # noqa: E402
from engine import provider as PRV, secrets  # noqa: E402
from ui.pages.llm_settings import LlmPage  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((bool(cond), name, detail))
    print(("  ok   " if cond else "  FAIL ") + name + (("  | " + str(detail)) if detail else ""))


def cfg_path():
    return A.llm_provider_config_path()


def read_json():
    with open(cfg_path(), "r", encoding="utf-8") as fh:
        return json.load(fh)


def key_state(page):
    return page._state_values["API Key"].text()


print("== LLM 供应商配置：保存链路 ==")
print("沙箱配置目录：%s" % os.path.dirname(cfg_path()))

# ------------------------------------------------------------------ 1. 初始
page = LlmPage()
check("首次打开没有配置文件", not A.llm_provider_config_saved())
check("首次打开脏标记为「与磁盘一致」", page.dirty_label.text() == "与磁盘一致",
      page.dirty_label.text())
check("首次打开密钥框为空（不回显）", page.key_edit.text() == "")
check("首次打开明细里 API Key = 未填写", key_state(page) == "未填写", key_state(page))

# ------------------------------------------------------------------ 2. 填表 + 加模型
page.name_edit.setText("我的网关")
page.protocol._pick("openai_responses")
page.preset._pick("custom")
page.base_url.setText("https://gw.example.com/v1")
page.models_url.setText("https://gw.example.com/v1/models")
page.headers.setPlainText("X-Trace-Id: namecat\n# 注释行\n这不是请求头\nX-Org: utpop")
check("改表单后标记为「有未保存的修改」", page.dirty_label.text() == "有未保存的修改",
      page.dirty_label.text())
page.manual_model.setText("gpt-4o-mini")
page.add_model()
check("添加模型后磁盘出现配置文件", A.llm_provider_config_saved())
check("添加模型后脏标记回落（不假脏）", page.dirty_label.text() == "与磁盘一致",
      page.dirty_label.text())
check("添加模型不会顺手把密钥写盘", secrets.load_secret() == "")

# ------------------------------------------------------------------ 3. 填密钥并保存
page.key_edit.setText("sk-test-abcdef123456")
page._on_key_edited()
check("输入密钥后变脏", page.dirty_label.text() == "有未保存的修改")
check("输入密钥后明细为「已填写（未保存）」", key_state(page) == "已填写（未保存）",
      key_state(page))
page.base_url.setText("https://gw.example.com/v1/")
page.save()
check("保存后配置文件存在", os.path.isfile(cfg_path()), cfg_path())
check("保存后密钥文件存在", os.path.isfile(secrets.secret_path()))
check("保存后脏标记回到「与磁盘一致」", page.dirty_label.text() == "与磁盘一致")
check("保存后密钥框被清空", page.key_edit.text() == "")
check("保存后占位提示为「已保存（留空即沿用）」",
      "已保存" in page.key_edit.edit.placeholderText(),
      page.key_edit.edit.placeholderText())
check("保存后明细为「已保存（沿用）」", key_state(page) == "已保存（沿用）", key_state(page))
check("保存后徽章为「已就绪」", page.state_badge.text() == "已就绪", page.state_badge.text())

# ------------------------------------------------------------------ 4. 逐字段回读
data = read_json()
check("name 落盘", data.get("name") == "我的网关", data.get("name"))
check("protocol 落盘", data.get("protocol") == "openai_responses", data.get("protocol"))
check("preset 落盘", data.get("preset") == "custom", data.get("preset"))
check("kind 落盘（自定义不改客户端类型）", data.get("kind") == "openai", data.get("kind"))
check("base_url 落盘", data.get("base_url") == "https://gw.example.com/v1/",
      data.get("base_url"))
check("models_url 落盘", data.get("models_url") == "https://gw.example.com/v1/models")
check("model 落盘", data.get("model") == "gpt-4o-mini", data.get("model"))
check("models 落盘", data.get("models") == ["gpt-4o-mini"], data.get("models"))
check("headers 落盘（注释行与无冒号行被丢）",
      data.get("headers") == {"X-Trace-Id": "namecat", "X-Org": "utpop"},
      data.get("headers"))
check("配置文件里没有 api_key 字段", "api_key" not in data)
with open(cfg_path(), "rb") as fh:
    check("配置文件字节里没有密钥明文", b"sk-test-abcdef123456" not in fh.read())
with open(secrets.secret_path(), "rb") as fh:
    raw_sec = fh.read()
check("密钥文件是 DPAPI 格式", raw_sec.startswith(b"DPAPI1:"), raw_sec[:12])
check("密钥文件里没有明文", b"sk-test-abcdef123456" not in raw_sec)
check("密钥能解回来", secrets.load_secret() == "sk-test-abcdef123456")

# ------------------------------------------------------------------ 5. 引擎侧生效
eff = PRV.effective_config()
check("effective_config 用上新地址", eff.base_url == "https://gw.example.com/v1/")
check("effective_config 用上新模型", eff.model == "gpt-4o-mini")
check("effective_config 用上新协议", eff.protocol == "openai_responses")
check("effective_config 拿得到密钥", eff.api_key == "sk-test-abcdef123456")
check("effective_config 带上自定义请求头",
      eff.headers == {"X-Trace-Id": "namecat", "X-Org": "utpop"}, eff.headers)

# ------------------------------------------------------------------ 6. 重开页面
page2 = LlmPage()
check("重开：名称读回", page2.name_edit.text() == "我的网关")
check("重开：协议读回", page2.protocol.value() == "openai_responses", page2.protocol.value())
check("重开：预设读回", page2.preset.value() == "custom", page2.preset.value())
check("重开：地址读回", page2.base_url.text() == "https://gw.example.com/v1/")
check("重开：模型列表 URL 读回",
      page2.models_url.text() == "https://gw.example.com/v1/models")
check("重开：模型读回", page2.model.value() == "gpt-4o-mini", page2.model.value())
check("重开：请求头文本读回",
      page2.headers.toPlainText() == "X-Trace-Id: namecat\nX-Org: utpop",
      repr(page2.headers.toPlainText()))
check("重开：密钥框仍为空", page2.key_edit.text() == "")
check("重开：明细为「已保存（沿用）」", key_state(page2) == "已保存（沿用）", key_state(page2))
check("重开：配置来源为本机配置",
      page2._state_values["配置来源"].text() == "本机配置",
      page2._state_values["配置来源"].text())

# ------------------------------------------------------------------ 7. 留空 / 切预设
page2.base_url.setText("https://gw.example.com/v2")
page2.save()
check("密钥留空再保存：密钥不丢", secrets.load_secret() == "sk-test-abcdef123456")
check("密钥留空再保存：新地址生效",
      read_json().get("base_url") == "https://gw.example.com/v2")
page2.preset._pick("deepseek")
check("选预设后自动填好地址", page2.base_url.text() == "https://api.deepseek.com")
page2.save()
d2 = read_json()
check("预设落盘", d2.get("preset") == "deepseek", d2.get("preset"))
check("预设把地址带进磁盘", d2.get("base_url") == "https://api.deepseek.com")
check("预设把模型列表地址带进磁盘",
      d2.get("models_url") == "https://api.deepseek.com/models")
check("切预设不清密钥", secrets.load_secret() == "sk-test-abcdef123456")

# ------------------------------------------------------------------ 8. 手填模型名
page3 = LlmPage()
page3.manual_model.setText("typed-but-not-added")
page3.save()
d3 = read_json()
check("手填模型名不点添加直接保存 → 不丢",
      d3.get("model") == "typed-but-not-added"
      and "typed-but-not-added" in (d3.get("models") or []),
      "model=%r models=%r" % (d3.get("model"), d3.get("models")))
check("保存后手填框清空", page3.manual_model.text() == "")
check("保存后模型下拉框跟上", page3.model.value() == "typed-but-not-added",
      page3.model.value())

# ------------------------------------------------------------------ 9. 密钥焦点时序
page4 = LlmPage()
if os.path.isfile(secrets.secret_path()):
    os.remove(secrets.secret_path())
page4.base_url.setText("https://focus.example.com")
page4.show()
app.processEvents()
page4.key_edit.edit.setFocus()
app.processEvents()
QTest.keyClicks(page4.key_edit.edit, "sk-click-direct")
app.processEvents()
QTest.mouseClick(page4.btn_save, Qt.LeftButton)   # 键盘输完直接用鼠标点保存
app.processEvents()
check("键盘输入后直接点保存：密钥落盘", secrets.load_secret() == "sk-click-direct",
      repr(secrets.load_secret())[:20])
page4.key_edit.setText("sk-not-blurred")
page4._key_input = ""                              # 模拟 editingFinished 还没到
check("未失焦时也能读到刚输入的密钥",
      page4._current_config().api_key == "sk-not-blurred")

# ------------------------------------------------------------------ 10. 异常路径
_blocked = os.path.join(_SANDBOX, "blocked")
os.makedirs(_blocked, exist_ok=True)
open(os.path.join(_blocked, "afile"), "w").close()
_old = os.environ["APPDATA"]
os.environ["APPDATA"] = os.path.join(_blocked, "afile")   # 指向文件，makedirs 必失败
check("配置目录不可写：save_config 返回空串（界面据此报错）",
      A.llm_save_provider_config(A.llm_provider_config(), None) == "")
os.environ["APPDATA"] = _old

with open(cfg_path(), "w", encoding="utf-8") as fh:
    fh.write('{"name": 5, "protocol": "nope", "models": "oops",'
             ' "headers": [1, 2], "timeout": "abc", "model": "m"}')
try:
    p5 = LlmPage()
    check("脏 JSON 也能打开页面", True)
    check("非法 protocol 回落 openai_chat", p5.protocol.value() == "openai_chat",
          p5.protocol.value())
except Exception as exc:      # noqa: BLE001
    check("脏 JSON 也能打开页面", False, "%s: %s" % (type(exc).__name__, exc))

# ------------------------------------------------------------------ 11. 纯环境变量
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="namecat_llm_env_")
os.environ["NAMECAT_LLM_API_KEY"] = "sk-from-env"
os.environ["NAMECAT_LLM_BASE_URL"] = "https://env.example.com/v1"
p6 = LlmPage()
check("只有环境变量时明细说「来自环境变量」", key_state(p6) == "来自环境变量",
      key_state(p6))
check("只有环境变量时地址读进来", p6.base_url.text() == "https://env.example.com/v1",
      p6.base_url.text())
del os.environ["NAMECAT_LLM_API_KEY"]
del os.environ["NAMECAT_LLM_BASE_URL"]

# ------------------------------------------------------------------ 汇总
fails = [r for r in RESULTS if not r[0]]
print()
print("检查：通过 %d / 失败 %d" % (len(RESULTS) - len(fails), len(fails)))
for _ok, _name, _detail in fails:
    print("  - %s | %s" % (_name, _detail))
print("沙箱：%s（跑完可删）" % _SANDBOX)
sys.exit(1 if fails else 0)
