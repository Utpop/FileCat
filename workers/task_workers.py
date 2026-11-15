# -*- coding: utf-8 -*-
"""
workers.task_workers —— 后台任务的封装（扫描/计划、执行、撤销、日志读取）。

规则：
    · 后台线程里只跑引擎函数，绝不碰任何 QWidget；
    · 进度、结果、异常全部通过 Qt 信号回到主线程；
    · 不提供「强制取消」——引擎的落盘与回滚没有安全中断点，
      界面上写清楚这一点，而不是伪造一个取消按钮。
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from adapters import engine_adapter as A


class _Signals(QObject):
    progress = Signal(int, int, str)     # 当前, 总数, 文本
    done = Signal(object)                # 结果
    failed = Signal(str)                 # 异常文本
    finished = Signal()                  # 无论成败都会发出


class BaseTask(QRunnable):
    """把任意可调用封装成一个可跑在 QThreadPool 上的任务。"""

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._done = False
        self.signals = _Signals()
        self.setAutoDelete(True)

    # ---- 给引擎用的进度回调：单参（扫描）与三参（执行）都接 ----
    def _progress(self, *args):
        if len(args) == 1:
            self.signals.progress.emit(0, 0, str(args[0]))
        elif len(args) >= 3:
            cur, total, text = args[0], args[1], args[2]
            try:
                self.signals.progress.emit(int(cur), int(total), str(text))
            except (TypeError, ValueError):
                self.signals.progress.emit(0, 0, str(text))

    def run(self):                        # noqa: D401 - QRunnable 接口
        try:
            self._kwargs.setdefault("progress", self._progress)
            out = self._fn(*self._args, **self._kwargs)
        except (SystemExit, KeyboardInterrupt):
            # 让解释器退出/中断按原语义传播，别吞掉
            raise
        except BaseException as exc:      # noqa: BLE001 - PyO3 PanicException 继承 BaseException
            # PyO3 的 PanicException 派生自 PyBaseException，普通 `except Exception`
            # 抓不到；漏掉它的话 failed 不发、界面永远停在 busy。
            self.signals.failed.emit("%s: %s" % (type(exc).__name__, exc))
        else:
            self.signals.done.emit(out)
        finally:
            self.signals.finished.emit()


class TaskRunner:
    """统一的线程池入口，避免各处自己 new QThreadPool。"""

    def __init__(self, parent=None, max_threads: int = 2):
        self.pool = QThreadPool(parent)
        self.pool.setMaxThreadCount(max(1, max_threads))
        self._alive: List[BaseTask] = []

    def submit(self, task: BaseTask) -> BaseTask:
        self._alive = [t for t in self._alive if not getattr(t, "_done", False)]
        self._alive.append(task)
        task.signals.finished.connect(lambda: self._forget(task))
        self.pool.start(task)
        return task

    def _forget(self, task: BaseTask) -> None:
        task._done = True
        self._alive = [t for t in self._alive if t is not task]

    def wait(self, msec: int = 3000) -> bool:
        return self.pool.waitForDone(msec)

    def busy(self) -> bool:
        return self.pool.activeThreadCount() > 0


# --------------------------------------------------------------------------- #
# 具体任务的构造函数：参数全部走适配层，界面只负责传表单状态
# --------------------------------------------------------------------------- #

def scan_task(state) -> BaseTask:
    """只扫描，不碰规则（第一阶段）。"""
    return BaseTask(A.scan_only, state)


def build_plan_task(bundle, rules, client) -> BaseTask:
    """在已扫出的清单上算名 + 校验（第二阶段，不重新扫描文件系统）。"""
    return BaseTask(A.build_plan, bundle, list(rules or []), client=client)


def scan_and_plan_task(state, rules, client) -> BaseTask:
    """扫描 + 算名 + 校验，一次走完（保留给「一步到位」的调用方）。"""
    return BaseTask(A.build, state, list(rules or []), client=client)


def apply_task(bundle, rules, client) -> BaseTask:
    """落盘（复用已算好的计划，绝不重算）。"""
    return BaseTask(A.apply_plan, bundle, list(rules or []), client=client)


def undo_task(log_path) -> BaseTask:
    return BaseTask(A.undo, log_path)


def logs_task(root) -> BaseTask:
    return BaseTask(lambda progress=None: A.logs(root))


def leftovers_task(root) -> BaseTask:
    return BaseTask(lambda progress=None: A.temp_leftovers(root))


def llm_test_task(cfg) -> BaseTask:
    """测供应商连通性（真发一个请求，必须放后台线程）。"""
    return BaseTask(lambda progress=None: A.llm_test_connection(cfg))


def llm_fetch_models_task(cfg) -> BaseTask:
    """拉取模型列表（联网，必须放后台线程）。"""
    return BaseTask(lambda progress=None: A.llm_fetch_models(cfg))


# --------------------------------------------------------------------------- #
# FileCat 文件提取引擎的任务（独立 Adapter，与上面原重命名任务平行）
# --------------------------------------------------------------------------- #

def extract_plan_task(state) -> BaseTask:
    """扫描 + 生成提取计划（后台线程，只读文件系统）。"""
    from adapters import extract_adapter as EA
    return BaseTask(EA.build_plan, state)


def extract_execute_task(bundle, dry_run: bool, task_id: str) -> BaseTask:
    """模拟 / 实际执行（后台线程；Rust 侧会释放 GIL，进度经信号回主线程）。"""
    from adapters import extract_adapter as EA
    return BaseTask(EA.execute, bundle, dry_run, task_id)


def extract_undo_task(log_path: str = "") -> BaseTask:
    """撤销最新一次提取；给了日志路径就撤销指定那份。"""
    from adapters import extract_adapter as EA
    if log_path:
        return BaseTask(EA.undo_path, log_path)
    return BaseTask(EA.undo_latest)


def extract_logs_task() -> BaseTask:
    from adapters import extract_adapter as EA
    return BaseTask(lambda progress=None: EA.list_logs())


def extract_csv_task(path: str, items) -> BaseTask:
    from adapters import extract_adapter as EA
    return BaseTask(lambda progress=None: EA.export_csv(path, list(items or [])))


def history_all_task(root: str) -> BaseTask:
    """「执行记录」页：同时读取重命名与文件提取两类日志，归并成同构的行。

    两类日志放在不同目录、不同 schema、由不同引擎写，界面在这里统一读，
    但各自仍走自己的 Adapter（不互相 import）。
    """
    from adapters import engine_adapter as A
    from adapters import extract_adapter as EA

    def _read(progress=None):
        rows = []
        for r in A.logs(root or ""):
            r = dict(r)
            r["record_type"] = "rename"
            rows.append(r)
        import os
        wanted = os.path.normcase(os.path.abspath(root)) if root else ""
        for r in EA.list_logs():
            if wanted and os.path.normcase(os.path.abspath(str(r.get("root") or ""))) != wanted:
                continue  # 目录筛选对两类日志同样生效
            r = dict(r)
            r["record_type"] = "extract"
            # 与重命名日志对齐的字段：pairs 用日志里的 entries 数量占位，
            # 撤销按钮按 record_type 分发，不需要真的 pairs 内容。
            r["pairs"] = [None] * int(r.get("count") or 0)
            r["summary"] = {}
            r["exec"] = {}
            rows.append(r)
        rows.sort(key=lambda x: str(x.get("time") or ""), reverse=True)
        return rows

    return BaseTask(_read)


def history_extract_undo_task(log_path: str = "") -> BaseTask:
    """撤销某一份文件提取日志（走提取 Adapter，与重命名撤销隔离）。"""
    from adapters import extract_adapter as EA
    if log_path:
        return BaseTask(EA.undo_path, log_path)
    return BaseTask(EA.undo_latest)
