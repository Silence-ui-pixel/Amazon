"""Bounded worker pool with one ordered queue per visitor browser device."""

import os
from collections import deque
from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from contextvars import copy_context
from threading import Lock
from typing import Any

from app.adapters.remote_browser import current_browser_device_id


def browser_task_workers() -> int:
    try:
        configured = int(os.getenv("KEYWORD_BROWSER_TASK_WORKERS", "4"))
    except ValueError:
        configured = 4
    return max(1, min(configured, 8))


class BrowserTaskExecutor(Executor):
    """Independent computers overlap; one computer keeps its ordered task lane.

    Unbound/local-browser work shares one lane. ContextVars retain each task's
    device and operation IDs even when a worker serves another user next.
    """

    def __init__(self, *, thread_name_prefix: str, max_workers: int | None = None) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=browser_task_workers() if max_workers is None else max_workers,
            thread_name_prefix=thread_name_prefix,
        )
        self._lock = Lock()
        self._queues: dict[str, deque[tuple[Future[Any], Callable[[], Any]]]] = {}
        self._closed = False

    def submit[**P, R](self, fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> Future[R]:
        device = current_browser_device_id()
        context = copy_context()
        future: Future[R] = Future()

        def invoke() -> R:
            return context.run(fn, *args, **kwargs)

        with self._lock:
            if self._closed:
                raise RuntimeError("cannot schedule new futures after shutdown")
            queue = self._queues.get(device)
            if queue is None:
                self._queues[device] = deque([(future, invoke)])
                self._pool.submit(self._drain, device)
            else:
                queue.append((future, invoke))
        return future

    def _drain(self, device: str) -> None:
        while True:
            with self._lock:
                queue = self._queues[device]
                if not queue:
                    del self._queues[device]
                    return
                future, invoke = queue.popleft()
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = invoke()
            except BaseException as error:
                future.set_exception(error)
            else:
                future.set_result(result)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._lock:
            self._closed = True
            pending = (
                [future for queue in self._queues.values() for future, _ in queue]
                if cancel_futures
                else []
            )
        # Future callbacks can submit work, so cancel outside the queue lock.
        for future in pending:
            future.cancel()
        # Drainers must run to release/cancel their user-facing Futures.
        self._pool.shutdown(wait=wait)
