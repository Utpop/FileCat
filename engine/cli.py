# -*- coding: utf-8 -*-
"""
engine.cli —— 命令行入口（不依赖界面，可脚本化 / 可批处理）。

设计要点（合并两边的命令行习惯）：
    · 默认只预览，加 --apply 才真的改名（BatchRenamer 的老规矩，最安全）
    · --rule 支持 `名称=参数1=参数2` 与 BatchRenamer 的 `名称=参数` 写法
    · 文件、文件夹、图片视频都在同一个命令里，用 --only 切换对象

示例：
    python -m engine.cli rename D:\\照片 --rule "template={n}" --only image --apply
    python -m engine.cli rename D:\\项目 --rule "remove-after=-" --only dir --rule "lower"
    python -m engine.cli undo D:\\照片
    python -m engine.cli rules
"""

from __future__ import annotations

import argparse
import os
import sys

from . import APP_NAME, APP_VER, STATUS_TEXT, ST_SKIP
from . import common as C
from . import ops
from . import plan as P
from . import presets as PS
from . import rules as R
from .scan import (KIND_DIR, KIND_FILE, KIND_IMAGE, KIND_VIDEO, SORT_MODES,
                   ScanOptions, scan)

ONLY_CHOICES = {
    "file": (KIND_FILE,),
    "dir": (KIND_DIR,),
    "image": (KIND_IMAGE,),
    "video": (KIND_VIDEO,),
    "media": (KIND_IMAGE, KIND_VIDEO),
    "all": (KIND_FILE, KIND_DIR, KIND_IMAGE, KIND_VIDEO),
}


# ---- 控制台输出：先降级再写，宁可变丑也不要崩 ----
#
# Windows 中文终端默认 cp936 / GBK，装不下 U+2713（✓）、U+2192（→）这类
# 装饰符号，而 plan.describe_plan 正是用它们当状态标记 —— 直接 print 会以
# UnicodeEncodeError 崩掉整个命令，且崩溃点离真正的原因很远（报错里只会看到
# 某个 py 行的 \u2713，看不出是"终端装不下"）。中文汉字 GBK 都表示得了，
# 所以正文不受影响，只有这些符号需要替身。
_SYM_FALLBACK = (
    ("✓", "OK"), ("✗", "X"), ("✔", "OK"), ("✘", "X"),
    ("×", "x"), ("→", "->"), ("←", "<-"), ("…", "..."),
    ("—", "--"), ("–", "-"), ("─", "-"), ("·", "."),
    ("【", "["), ("】", "]"), ("、", ","),
)

# ASCII 兜底顺序：这些是可降级符号集之外的备用（箭头等在上面已覆盖）
_ENC_CACHE: dict = {}


def _console_encoding() -> str:
    stream = sys.__stdout__ or sys.__stderr__
    return getattr(stream, "encoding", None) or "utf-8"


def _fit_text(text: str) -> str:
    """把一段待输出文本降级到控制台能表示的形态。"""
    enc = _ENC_CACHE.get("enc") or _console_encoding()
    try:
        text.encode(enc)
        return text
    except (UnicodeEncodeError, LookupError):
        pass
    for src, dst in _SYM_FALLBACK:
        if src in text:
            text = text.replace(src, dst)
    try:
        text.encode(enc)
        return text
    except (UnicodeEncodeError, LookupError):
        # 还有装不下的（罕见：希腊字母、扩展汉字等）：按字符替换成 ?
        return text.encode(enc, "replace").decode(enc, "replace")


class _SafeStream:
    """包在 stdout / stderr 外面的一层壳，其余属性原样透传。"""

    def __init__(self, base):
        self._base = base

    def __getattr__(self, name):
        return getattr(self._base, name)

    def write(self, text):
        if isinstance(text, bytes):
            return self._base.write(text)
        return self._base.write(_fit_text(str(text)))

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        return self._base.flush()


def install_safe_output() -> None:
    """命令行启动时调一次：装好降级输出。重复调用安全。"""
    _ENC_CACHE["enc"] = _console_encoding()
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if isinstance(stream, _SafeStream):
            continue
        if stream is None:
            # 窗口化子系统从无控制台的方式启动（双击 GUI exe 却带了命令行参数）：
            # 这时 print 会是 AttributeError，宁可悄悄丢掉也不崩。
            import io
            setattr(sys, name, io.StringIO())
            continue
        try:
            stream.reconfigure(errors="replace")   # 兜底：别处绕过壳也不崩
        except Exception:
            pass
        setattr(sys, name, _SafeStream(stream))


def _progress(quiet: bool):
    """统一的进度回调。扫描阶段只传一个目录名，执行阶段传三参数。"""
    if quiet:
        return None
    state = {"last": ""}

    def report(*args):
        if len(args) == 1:
            text, cur, total = str(args[0]), 0, 0
        else:
            cur, total, text = args[0], args[1], args[2]
        if text == state["last"]:
            return
        state["last"] = text
        if total:
            sys.stderr.write("\r%s (%d/%d)" % (text, cur, total))
            if cur >= total:
                sys.stderr.write("\n")
        else:
            sys.stderr.write("\r扫描：%s" % text)
        sys.stderr.flush()
    return report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="namecat",
        description="%s %s —— 合并版批量重命名（文件 / 文件夹 / 图片视频）" % (APP_NAME, APP_VER))
    sub = p.add_subparsers(dest="cmd")

    def common_args(sp, allow_apply=True):
        # 受预设影响的参数一律 default=None：只有**显式写了**才覆盖预设里的值，
        # 否则 `--preset 照片序号（四位）` 会拿命令行默认值（scope=global / width=0）
        # 把预设的编号选项盖掉 —— 同一条预设在界面出 0001、在命令行出 1。
        sp.add_argument("root", nargs="?", default="", help="要处理的目录")
        sp.add_argument("--only", default=None, choices=sorted(ONLY_CHOICES),
                        help="处理对象：file/dir/image/video/media/all（默认 file，预设可改）")
        sp.add_argument("--recursive", "-r", action="store_true", default=None,
                        help="递归子目录（预设可改）")
        sp.add_argument("--hidden", action="store_true", default=None, help="包含隐藏项")
        sp.add_argument("--only-match", default=None, help="名字含该文本才处理")
        sp.add_argument("--exclude-match", default=None, help="名字含该文本就跳过")
        sp.add_argument("--sort", default=None,
                        choices=[k for k, _t in SORT_MODES],
                        help="排序方式（默认 name_asc，预设可改）")
        sp.add_argument("--verify", action="store_true", default=None,
                        help="校验文件头（图片视频）")
        if allow_apply:
            sp.add_argument("--apply", action="store_true", help="真的改名（默认只预览）")
        sp.add_argument("--dry-run", action="store_true", help="显式预览")
        sp.add_argument("--csv", default="", help="把预览结果导出到 CSV")
        sp.add_argument("--limit", type=int, default=60,
                        help="预览显示条数，0 = 全部（默认 60）")
        sp.add_argument("--quiet", "-q", action="store_true", help="不显示进度")
        sp.add_argument("--start", type=int, default=None,
                        help="编号起始值（链里有 number 规则时以规则上的为准）")
        sp.add_argument("--width", type=int, default=None,
                        help="编号补零宽度（同上，规则写了补零就用规则的）")
        sp.add_argument("--step", type=int, default=None,
                        help="编号步长（同上，规则写了步长就用规则的）")
        sp.add_argument("--scope", default=None, choices=P.SCOPES,
                        help="编号分组方式（预设可改）")
        sp.add_argument("--no-split-ext", action="store_true", default=None,
                        help="扩展名也参与规则链（默认扩展名不动）")
        sp.add_argument("--preset", default="", help="从预设文件读取规则与扫描 / 编号选项")
        sp.add_argument("--llm-instruction", default="",
                        help="换一个「给 LLM 的要求」文件（默认 %%APPDATA%%\\NameCat\\llm_instruction.txt）")
        sp.add_argument("--llm-edit", action="store_true",
                        help="打开/生成要求文件后退出，供你直接编辑")
        sp.add_argument("--llm-provider", default="",
                        help="openai / ollama / echo（默认读环境变量）")
        sp.add_argument("--llm-model", default="", help="模型名，如 deepseek-chat")
        sp.add_argument("--llm-batch", type=int, default=0, help="一次请求带多少条（默认 20）")

    rn = sub.add_parser("rename", help="按规则改名")
    common_args(rn)
    rn.add_argument("--rule", action="append", default=[],
                    help="规则，格式 名称=参数1=参数2（可重复，按顺序生效）")

    pr = sub.add_parser("preview", help="只看结果，绝不落盘（等价于不带 --apply 的 rename）")
    common_args(pr, allow_apply=False)     # 预览子命令不提供 --apply，避免"只想看"却改了文件
    pr.add_argument("--rule", action="append", default=[])

    un = sub.add_parser("undo", help="按日志撤销")
    un.add_argument("root", nargs="?", default="",
                    help="目标目录（只找这个目录的日志；不给则用最新一份）")
    un.add_argument("--log", default="", help="指定日志文件")
    un.add_argument("--list", action="store_true", help="只列日志")
    un.add_argument("--quiet", "-q", action="store_true")

    lf = sub.add_parser("leftovers", help="列出执行被中断后遗留的临时名文件")
    lf.add_argument("root", nargs="?", default="", help="要检查的目录")

    sub.add_parser("rules", help="列出全部规则类型")
    sub.add_parser("presets", help="列出内置与已保存的预设")
    return p


def _preset_of(args):
    """读 `--preset` 指定的预设；没给返回 None。

    预设里的 **规则 + 扫描选项 + 编号选项** 都要生效（与界面「应用预设」同口径），
    命令行显式给出的参数再逐项覆盖它。
    """
    path = str(getattr(args, "preset", "") or "")
    if not path:
        return None
    try:
        return PS.load(path)
    except (OSError, ValueError) as exc:
        raise SystemExit("预设读取失败：%s（%s）" % (path, exc))


def _make_scan_options(args, preset=None) -> ScanOptions:
    """扫描选项：预设打底，命令行显式参数覆盖。"""
    sc = preset.scan_options(root=args.root) if preset is not None \
        else ScanOptions(root=args.root)
    sc.root = args.root
    if args.only:
        sc.kinds = ONLY_CHOICES[args.only]
    if args.recursive is not None:
        sc.recursive = bool(args.recursive)
    if args.hidden is not None:
        sc.include_hidden = bool(args.hidden)
    if args.only_match is not None:
        sc.only_match = args.only_match
    if args.exclude_match is not None:
        sc.exclude_match = args.exclude_match
    if args.sort:
        sc.sort_mode = args.sort
    if args.verify is not None:
        sc.verify_content = bool(args.verify)
    return sc


def _make_plan_options(args, preset=None) -> P.PlanOptions:
    """编号选项：预设打底，命令行显式参数覆盖（规则上的发号参数仍然最高优先）。"""
    applying = bool(getattr(args, "apply", False))
    po = preset.plan_options(dry_run=not applying) if preset is not None \
        else P.PlanOptions(dry_run=not applying)
    if args.start is not None:
        po.start = args.start
    if args.step is not None:
        po.step = args.step
    if args.width is not None:
        po.width = args.width
    if args.scope:
        po.scope = args.scope
    if args.no_split_ext:
        po.split_ext = False
    if args.only:
        po.rename_dirs = args.only in ("dir", "all")
    if args.dry_run:
        po.dry_run = True
    return po


def _make_client(args):
    """按命令行 + 环境变量造 LLM 客户端；没配 key 时返回不可用客户端。

    界面里保存过的供应商配置同样生效（界面与命令行共用同一套配置），
    命令行参数优先级最高。
    """
    from . import llm as L
    from . import provider as PRV
    cfg = PRV.effective_config()
    prov = (getattr(args, "llm_provider", "") or "").strip()
    if prov:
        cfg.provider = prov
    model = (getattr(args, "llm_model", "") or "").strip()
    if model:
        cfg.model = model
    batch = int(getattr(args, "llm_batch", 0) or 0)
    if batch > 0:
        cfg.batch = batch
    return L.make_client(cfg)


def cmd_llm_instruction(args) -> int:
    """`--llm-edit`：把要求文件准备好并打印路径，使用者直接改文本即可。"""
    from . import llm as L
    custom = getattr(args, "llm_instruction", "")
    path = L.ensure_instruction_file(custom)
    print("要求文件：%s" % path)
    if not os.path.exists(path):
        print("（注意：这个位置现在写不进去，改了也不会保存。）")
        print("换个可写位置：--llm-instruction <文件路径>，"
              "或用环境变量 %s" % L.INSTRUCTION_ENV)
    print()
    print(L.read_instruction(custom))
    print()
    print("（改这个文件即可；改动下次运行生效。以 # 开头的行是注释，不会发给模型。）")
    return 0


def _collect_rules(args, preset=None) -> list:
    out = []
    if preset is not None:
        out.extend(preset.rules)
    for spec in getattr(args, "rule", []) or []:
        try:
            out.append(R.parse_cli_rule(spec))
        except ValueError as exc:
            raise SystemExit("规则解析失败：%s（%s）" % (spec, exc))
    # 命令行 --llm-instruction 对 llm 规则生效（规则自己写了要求时以规则为准）
    instr_file = getattr(args, "llm_instruction", "")
    if instr_file:
        from . import llm as L
        body = L.read_instruction(instr_file)
        for r in out:
            if r.kind == "llm" and not str((r.extras or {}).get("instruction") or "").strip():
                r.extras["instruction"] = body
    return out


def cmd_rename(args) -> int:
    if getattr(args, "llm_edit", False):
        return cmd_llm_instruction(args)
    if not args.root:
        print("请给出要处理的目录")
        return 2
    if not os.path.isdir(args.root):
        print("目录不存在：%s" % args.root)
        return 2
    preset = _preset_of(args)
    rules = _collect_rules(args, preset)
    if not rules:
        print("没有规则。用 --rule 指定，或先看 `rules` 子命令的说明。")
        return 2
    sc = _make_scan_options(args, preset)
    po = _make_plan_options(args, preset)
    applying = bool(getattr(args, "apply", False))

    print("规则链：%s" % R.describe_chain(rules))
    print("对象：%s  排序：%s  递归：%s" % (
        args.only or "（预设）", sc.sort_mode, "是" if sc.recursive else "否"))

    prog = _progress(args.quiet)
    client = _make_client(args) if any(r.kind == "llm" for r in rules) else None
    res = ops.scan_and_plan(args.root, rules, sc, po, prog, client)
    print(P.describe_plan(res, args.limit))
    print("汇总：%s" % res.summary())
    for w in res.warnings:
        print("提示：%s" % w)

    if args.csv:
        try:
            ops.export_csv(res, args.csv)
        except OSError as exc:
            # 目录不存在/没权限是常见手误，不该把整个命令崩掉
            print("导出失败：%s（%s）" % (args.csv, exc))
            return 1
        print("已导出：%s" % args.csv)

    if not applying:
        print("（预览模式，未改动任何文件；确认无误后加 --apply）")
        return 0

    # 把已经算好的计划直接交给 apply：不再重算，避免 LLM 请求翻倍、
    # 也避免 randomize 这类规则导致「预览名」与「落盘名」不一致
    out = ops.apply(res.items, rules, po, root=args.root,
                    kind=args.only or "mixed", progress=prog, client=client, plan=res)
    print(out.message)
    for step in out.failed:
        print("失败：%s -> %s：%s" % (step.old, step.new, step.error))
    if out.log_path:
        print("撤销日志：%s" % out.log_path)
    return 0 if out.ok else 1


def cmd_undo(args) -> int:
    logs = []
    if args.log:
        # 显式指定的日志：不存在就直说，别让它一路走到 open() 抛栈
        if not os.path.isfile(args.log):
            print("日志不存在：%s" % args.log)
            return 1
        logs = [args.log]
    elif args.root:
        logs = C.list_logs(args.root)
    else:
        # 不给目录 = 列出全部（所有目标目录的日志都存在同一个文件夹里）
        logs = C.list_logs("")
    if not logs:
        print("没有找到撤销日志")
        return 1
    if args.list:
        for path in logs:
            try:
                data = C.load_log(path)
            except (OSError, ValueError):
                print("%s（无法读取）" % path)
                continue
            if not isinstance(data, dict):
                print("%s（格式不认识）" % path)
                continue
            print("%s  %s  %s  共 %d 项"
                  % (path, data.get("time", ""), data.get("root", ""),
                     len(C.log_pairs(data))))
        return 0
    latest = logs[-1]
    print("撤销：%s" % latest)
    try:
        res = ops.undo(latest, _progress(args.quiet))
    except (OSError, ValueError) as exc:
        # 日志被改坏（不是 JSON / 结构不对）时也给一句人话
        print("无法读取日志：%s（%s）" % (latest, exc))
        return 1
    print(res.message)
    return 0 if res.ok else 1


def cmd_leftovers(args) -> int:
    """列出执行被中断后遗留的临时名文件（`__nk_tmp_*`）。

    这些名字里没有原名的任何线索，程序无权替用户猜，只能如实列出来。
    有残留时返回 1，方便脚本/批处理发现异常。
    """
    root = args.root or "."
    if not os.path.isdir(root):
        print("目录不存在：%s" % root)
        return 2
    found = C.find_temp_leftovers(root)
    if not found:
        print("没有临时名残留：%s" % os.path.abspath(root))
        return 0
    print("发现 %d 个临时名残留（上次执行被中断时留下的）：" % len(found))
    for path in found:
        print("  " + path)
    print()
    print("文件内容完好，但原名已无从还原（临时名里没有原名线索）；")
    print("请按修改时间/大小辨认后手工改名，撤销日志里通常也有对应记录。")
    return 1


def cmd_rules(_args) -> int:
    print(R.describe_rules_table())
    print()
    print("命令行写法：--rule 名称=参数1=参数2=参数3")
    print("  例：--rule \"remove-after=-\"  --rule \"replace=_:- \"  --rule \"template={n}\"")
    print("  可加后缀用分号设置作用范围：--rule \"case=upper;sel=after;sep=._\"")
    return 0


def cmd_presets(_args) -> int:
    print("内置预设：")
    for name in PS.builtin_list():
        p = PS.BUILTIN[name]
        print("  %-24s %s" % (name, R.describe_chain(p.rules)))
        if p.note:
            print("  %-24s %s" % ("", p.note))
    files = PS.list_presets()
    print()
    if files:
        print("已保存预设（%s）：" % PS.preset_dir())
        for f in files:
            print("  " + f)
    else:
        print("已保存预设：无（%s）" % PS.preset_dir())
    return 0


def main(argv=None) -> int:
    install_safe_output()   # GBK 终端上遇到装不下的符号先降级，别炸在这儿
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    if args.cmd in ("rename", "preview"):
        return cmd_rename(args)
    if args.cmd == "undo":
        return cmd_undo(args)
    if args.cmd == "leftovers":
        return cmd_leftovers(args)
    if args.cmd == "rules":
        return cmd_rules(args)
    if args.cmd == "presets":
        return cmd_presets(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
