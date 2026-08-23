"""Bounded dependency-aware logical worker executor."""
from __future__ import annotations
import heapq
import os
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Callable, Mapping
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

    def execute(self, graph: TaskGraph, *, completed: Mapping[str, TaskResult] | None = None,
                event_sink: Callable[[str, dict], None] | None = None,
                task_completed: Callable[[TaskResult], None] | None = None) -> ExecutionResult:
        tasks = {t.task_id: t for t in graph.tasks}
        results = dict(completed or {})
        if not set(results) <= set(tasks) or any(r.status != "completed" for r in results.values()):
            raise ValueError("resume results are incompatible with task graph")
        remaining = set(tasks) - set(results)
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
                        remaining.remove(task_id)
                        if event_sink: event_sink("task_ready", {"task_id": task_id})
                        if event_sink: event_sink("task_started", {"task_id": task_id})
                        running[pool.submit(run, task_id)] = task_id
                if not running:
                    if remaining: raise ValueError("task graph is cyclic or has missing dependencies")
                    break
                done, _ = wait(running, return_when=FIRST_COMPLETED, timeout=.05)
                for future in done:
                    task_id = running.pop(future)
                    result = future.result()
                    results[task_id] = result
                    if event_sink:
                        event_sink("task_completed" if result.status == "completed" else "task_failed",
                                   {"task_id": task_id, "status": result.status,
                                    **({"code": result.error.get("code", "TASK_FAILED")} if result.error else {})})
                    if result.status == "completed" and task_completed:
                        task_completed(result)
            return ExecutionResult(results, peak)
        except CancellationRequested:
            for future in running: future.cancel()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
