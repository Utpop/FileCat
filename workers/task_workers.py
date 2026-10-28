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
        except Exception as exc:          # 引擎异常要带回界面，而不是静默
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
