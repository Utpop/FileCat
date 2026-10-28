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

    def common_args(sp):
        sp.add_argument("root", nargs="?", default="", help="要处理的目录")
        sp.add_argument("--only", default="file", choices=sorted(ONLY_CHOICES),
                        help="处理对象：file/dir/image/video/media/all（默认 file）")
        sp.add_argument("--recursive", "-r", action="store_true", help="递归子目录")
        sp.add_argument("--hidden", action="store_true", help="包含隐藏项")
        sp.add_argument("--only-match", default="", help="名字含该文本才处理")
        sp.add_argument("--exclude-match", default="", help="名字含该文本就跳过")
        sp.add_argument("--sort", default="name_asc",
                        choices=[k for k, _t in SORT_MODES], help="排序方式")
        sp.add_argument("--verify", action="store_true", help="校验文件头（图片视频）")
        sp.add_argument("--apply", action="store_true", help="真的改名（默认只预览）")
        sp.add_argument("--dry-run", action="store_true", help="显式预览")
        sp.add_argument("--csv", default="", help="把预览结果导出到 CSV")
        sp.add_argument("--limit", type=int, default=60,
                        help="预览显示条数，0 = 全部（默认 60）")
        sp.add_argument("--quiet", "-q", action="store_true", help="不显示进度")
        sp.add_argument("--start", type=int, default=1,
                        help="编号起始值（链里有 number 规则时以规则上的为准）")
        sp.add_argument("--width", type=int, default=0,
                        help="编号补零宽度（同上，规则写了补零就用规则的）")
        sp.add_argument("--step", type=int, default=1,
                        help="编号步长（同上，规则写了步长就用规则的）")
        sp.add_argument("--scope", default=P.SCOPE_GLOBAL, choices=P.SCOPES,
                        help="编号分组方式")
        sp.add_argument("--no-split-ext", action="store_true",
                        help="扩展名也参与规则链（默认扩展名不动）")
        sp.add_argument("--preset", default="", help="从预设文件读取规则与选项")
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

    pr = sub.add_parser("preview", help="只看结果（等价于不带 --apply 的 rename）")
    common_args(pr)
    pr.add_argument("--rule", action="append", default=[])

    un = sub.add_parser("undo", help="按日志撤销")
    un.add_argument("root", nargs="?", default="",
                    help="目标目录（只找这个目录的日志；不给则用最新一份）")
    un.add_argument("--log", default="", help="指定日志文件")
    un.add_argument("--list", action="store_true", help="只列日志")
    un.add_argument("--quiet", "-q", action="store_true")

    sub.add_parser("rules", help="列出全部规则类型")
    sub.add_parser("presets", help="列出内置与已保存的预设")
    return p


def _make_scan_options(args) -> ScanOptions:
    kinds = ONLY_CHOICES.get(args.only, (KIND_FILE,))
    return ScanOptions(
        root=args.root, kinds=kinds, recursive=args.recursive,
        include_hidden=args.hidden, only_match=args.only_match,
        exclude_match=args.exclude_match, verify_content=args.verify,
        sort_mode=args.sort)


def _make_plan_options(args) -> P.PlanOptions:
    return P.PlanOptions(start=args.start, width=args.width, step=args.step,
                         scope=args.scope, split_ext=not args.no_split_ext,
                         rename_dirs=args.only in ("dir", "all"),
                         dry_run=not args.apply)


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


def _collect_rules(args) -> list:
    out = []
    if getattr(args, "preset", ""):
        preset = PS.load(args.preset)
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
    rules = _collect_rules(args)
    if not rules:
        print("没有规则。用 --rule 指定，或先看 `rules` 子命令的说明。")
        return 2
    sc = _make_scan_options(args)
    po = _make_plan_options(args)
    if args.dry_run:
        po.dry_run = True

    print("规则链：%s" % R.describe_chain(rules))
    print("对象：%s  排序：%s  递归：%s" % (
        args.only, args.sort, "是" if args.recursive else "否"))

    prog = _progress(args.quiet)
    client = _make_client(args) if any(r.kind == "llm" for r in rules) else None
    res = ops.scan_and_plan(args.root, rules, sc, po, prog, client)
    print(P.describe_plan(res, args.limit))
    print("汇总：%s" % res.summary())
    for w in res.warnings:
        print("提示：%s" % w)

    if args.csv:
        ops.export_csv(res, args.csv)
        print("已导出：%s" % args.csv)

    if not args.apply:
        print("（预览模式，未改动任何文件；确认无误后加 --apply）")
        return 0

    # 把已经算好的计划直接交给 apply：不再重算，避免 LLM 请求翻倍、
    # 也避免 randomize 这类规则导致「预览名」与「落盘名」不一致
    out = ops.apply(res.items, rules, po, root=args.root,
                    kind=args.only, progress=prog, client=client, plan=res)
    print(out.message)
    for step in out.failed:
        print("失败：%s -> %s：%s" % (step.old, step.new, step.error))
    if out.log_path:
        print("撤销日志：%s" % out.log_path)
    return 0 if out.ok else 1


def cmd_undo(args) -> int:
    logs = []
    if args.log:
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
            print("%s  %s  %s  共 %d 项"
                  % (path, data.get("time", ""), data.get("root", ""),
                     len(data.get("pairs") or [])))
        return 0
    latest = logs[-1]
    print("撤销：%s" % latest)
    res = ops.undo(latest, _progress(args.quiet))
    print(res.message)
    return 0 if res.ok else 1


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
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    if args.cmd in ("rename", "preview"):
        return cmd_rename(args)
    if args.cmd == "undo":
        return cmd_undo(args)
    if args.cmd == "rules":
        return cmd_rules(args)
    if args.cmd == "presets":
        return cmd_presets(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
