"""LangGraph 对象的最小协议；第三方泛型差异不影响节点与 API 的类型。"""

from collections.abc import Iterator
from typing import Literal, Protocol

from langchain_core.runnables import RunnableConfig
from langgraph.types import Durability


class InterruptView(Protocol):
    @property
    def value(self) -> object: ...


class TaskView(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def interrupts(self) -> tuple[InterruptView, ...]: ...


class SnapshotView(Protocol):
    @property
    def values(self) -> dict[str, object]: ...

    @property
    def next(self) -> tuple[str, ...]: ...

    @property
    def tasks(self) -> tuple[TaskView, ...]: ...


class CompiledWorkflow(Protocol):
    def invoke(
        self, value: object, config: RunnableConfig, *, durability: Durability | None = None
    ) -> dict[str, object]: ...

    def stream(
        self,
        value: object,
        config: RunnableConfig,
        *,
        stream_mode: Literal["updates"],
        durability: Durability | None = None,
    ) -> Iterator[dict[str, object]]: ...

    def get_state(self, config: RunnableConfig) -> SnapshotView: ...

    def update_state(
        self, config: RunnableConfig, values: dict[str, object], as_node: str | None = None
    ) -> RunnableConfig: ...
