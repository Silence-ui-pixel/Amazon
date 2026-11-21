"""Cooperative workflow cancellation at durable operation boundaries."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_CANCEL_CHECK: ContextVar[Callable[[], bool] | None] = ContextVar(
    "workflow_cancel_check", default=None
)


class WorkflowCancelled(BaseException):
    """Exit retry/error handlers while preserving finished external results."""


@contextmanager
def workflow_execution_context(check: Callable[[], bool]) -> Iterator[None]:
    token = _CANCEL_CHECK.set(check)
    try:
        yield
    finally:
        _CANCEL_CHECK.reset(token)


def check_workflow_cancelled() -> None:
    check = _CANCEL_CHECK.get()
    if check is not None and check():
        raise WorkflowCancelled()
