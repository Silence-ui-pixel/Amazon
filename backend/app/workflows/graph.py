"""图装配：节点线性串联，自动节点带失败/重做条件边。"""

from collections.abc import Callable
from typing import cast

from app.execution_control import check_workflow_cancelled
from app.workflows.browser_operations import REMOTE_NODE_SOURCES, wrap_browser_operation
from app.workflows.checkpointer import Checkpointer
from app.workflows.deps import WorkflowDeps
from app.workflows.nodes import build_nodes
from app.workflows.nodes_common import Node
from app.workflows.protocols import CompiledWorkflow
from app.workflows.state import NODE_KEYS, WorkflowState

# LangGraph 的 graph 子包未随主包发布类型标记，pyright 会提示缺少 stub。
from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]


def _router(current: str, next_node: str) -> Callable[[WorkflowState], str]:
    """成功进下一节点；上游失效回指定节点；失败且无路可退则终止。"""

    def route(state: WorkflowState) -> str:
        # redo_from 用于上游变化或节点主动重做；同节点已失败时停止，避免无限自循环。
        target = state["redo_from"]
        if target and (target != current or not state["failed_node"]):
            return target
        if state["failed_node"]:
            return END
        return next_node

    return route


def _guard_node(node: Node) -> Node:
    def guarded(state: WorkflowState) -> dict[str, object]:
        check_workflow_cancelled()
        return node(state)

    return guarded


def build_graph(deps: WorkflowDeps, saver: Checkpointer) -> CompiledWorkflow:
    nodes = build_nodes(deps)
    builder = StateGraph(WorkflowState)
    for key in NODE_KEYS:
        node = nodes[key]
        if deps.browser_operations is not None and key in REMOTE_NODE_SOURCES:
            node = wrap_browser_operation(key, node, deps.browser_operations)
        # add_node 的重载含 CachePolicy[Unknown]，严格模式视为部分未知；
        # 节点签名本身由 Node 类型保证，这里只收敛第三方泛型。
        builder.add_node(key, _guard_node(node))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
    builder.add_edge(START, NODE_KEYS[0])
    # 人工门在自动模式下也会失败，因此所有节点统一走条件边。
    for index, key in enumerate(NODE_KEYS):
        next_node = NODE_KEYS[index + 1] if index + 1 < len(NODE_KEYS) else END
        builder.add_conditional_edges(key, _router(key, next_node))
    # LangGraph 的 compile 签名含未绑定泛型，pyright 视为部分未知；
    # 这里一次性收敛为本地 Protocol，节点与 API 不再接触第三方泛型。
    compiled = builder.compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]
    return cast(CompiledWorkflow, compiled)
