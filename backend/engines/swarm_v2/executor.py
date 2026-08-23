"""Bounded dependency-aware logical worker executor."""
from __future__ import annotations
import heapq
import os
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Callable
from backend.runtime import CancellationRequested
from .contracts import TaskGraph
from .worker import GenericWorker, TaskResult

@dataclass(frozen=True)
class ExecutionResult:
    tasks: dict[str, TaskResult]
    max_active_workers: int

class BoundedTaskExecutor:
    def __init__(self, *, worker_factory: Callable[[], GenericWorker], max_active_workers: int,
                 cancellation_checker: Callable[[], bool] | None = None):
        if max_active_workers < 1:
            raise ValueError("max_active_workers must be positive")
        self._worker_factory, self._limit, self._cancelled = worker_factory, max_active_workers, cancellation_checker

    @staticmethod
    def configured_limit(env: dict[str, str] | None = None) -> int:
        raw = (env or os.environ).get("MILO_SWARM_MAX_ACTIVE_WORKERS", "4")
        try:
            value = int(raw)
        except ValueError:
            raise ValueError("MILO_SWARM_MAX_ACTIVE_WORKERS must be an integer") from None
        if not 1 <= value <= 32:
            raise ValueError("MILO_SWARM_MAX_ACTIVE_WORKERS must be between 1 and 32")
        return value

    def _check_cancelled(self) -> None:
        if self._cancelled and self._cancelled():
            raise CancellationRequested("RUN_CANCELLED")

    def execute(self, graph: TaskGraph) -> ExecutionResult:
        tasks, remaining, results = {t.task_id: t for t in graph.tasks}, {t.task_id for t in graph.tasks}, {}
        running: dict[Future[TaskResult], str] = {}
        active = peak = 0
        active_lock = threading.Lock()
        def run(task_id: str) -> TaskResult:
            nonlocal active, peak
            self._check_cancelled()
            with active_lock:
                active += 1
                peak = max(peak, active)
            try:
                return self._worker_factory().execute(tasks[task_id], {d: results[d].output for d in tasks[task_id].dependencies})
            finally:
                with active_lock:
                    active -= 1
        pool = ThreadPoolExecutor(max_workers=self._limit, thread_name_prefix="swarm-worker")
        try:
            while remaining or running:
                self._check_cancelled()
                ready = []
                for task_id in tuple(remaining):
                    deps = tasks[task_id].dependencies
                    if all(dep in results for dep in deps):
                        if any(results[dep].status != "completed" for dep in deps):
                            results[task_id] = TaskResult(task_id, "blocked", error={"code": "DEPENDENCY_FAILED", "message": "a dependency failed"})
                            remaining.remove(task_id)
                        else:
                            heapq.heappush(ready, (-tasks[task_id].priority, task_id))
                while ready and len(running) < self._limit:
                    _, task_id = heapq.heappop(ready)
                    if task_id in remaining:
                        remaining.remove(task_id); running[pool.submit(run, task_id)] = task_id
                if not running:
                    if remaining: raise ValueError("task graph is cyclic or has missing dependencies")
                    break
                done, _ = wait(running, return_when=FIRST_COMPLETED, timeout=.05)
                for future in done:
                    results[running.pop(future)] = future.result()
            return ExecutionResult(results, peak)
        except CancellationRequested:
            for future in running: future.cancel()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
