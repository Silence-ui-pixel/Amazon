"""业务异常到编排路由的映射；只决定"回到哪一步"，不改写业务判断。"""

from langgraph.types import interrupt

from app.domain.errors import DomainError
from app.workflows.browser_operations import REMOTE_NODE_SOURCES
from app.workflows.state import WorkflowState, record

# stale / 缺失类错误回到对应的门；未知错误直接终止，等待人工处理。
REDO_TARGETS: dict[str, str] = {
    "PRODUCT_UNDERSTANDING_REQUIRED": "product_confirmation",
    "INSUFFICIENT_PRODUCT_EVIDENCE": "product_confirmation",
    "ROOT_SELECTION_REQUIRED": "root_selection",
    "ROOT_SELECTION_STALE": "root_selection",
    "CORE_REPORT_STALE": "root_selection",
    "CORE_RANK_CONFLICT": "root_selection",
    "CORE_SELECTION_REQUIRED": "core_terms",
    "CORE_SELECTION_STALE": "core_terms",
    "CORE_ANALYSIS_NOT_CURRENT": "core_terms",
    "COMPETITORS_REQUIRED": "competitor_selection",
    "COMPETITORS_STALE": "competitor_discovery",
    "COMPETITOR_CONTEXT_CHANGED": "competitor_discovery",
    "EXPANSION_CONTEXT_CHANGED": "competitor_selection",
    "CLEANING_SOURCES_INCOMPLETE": "third_party",
    "CLEANING_STALE": "cleaning_review",
    "TRAFFIC_INPUT_STALE": "cleaning_review",
    "LIBRARY_SOURCE_STALE": "traffic_review",
    "WORKFLOW_WEEK_REQUIRED": "week",
    "WORKFLOW_CLEANING_MISSING": "cleaning_review",
    "WORKFLOW_TRAFFIC_MISSING": "traffic_review",
}


def redo_target(code: str) -> str | None:
    return REDO_TARGETS.get(code)


MAX_ATTEMPTS = 3

AGENT_NETWORK_ERRORS = frozenset(
    {
        "AGENT_UNAVAILABLE",
        "AGENT_CHANNEL_OFFLINE",
        "AGENT_INVALID_RESPONSE",
        "AGENT_HTTP_ERROR",
        "AGENT_BROWSER_ERROR",
        "AGENT_FILE_MISSING",
        "AGENT_SESSION_NOT_FOUND",
    }
)
AGENT_ATTENTION_ERRORS = frozenset(
    {
        "SEARCH_ACCESS_CHECK",
        "SELLERSPRITE_LOGIN_REQUIRED",
        "SELLERSPRITE_AUTH_REQUIRED",
        "ABA_AUTH_REQUIRED",
        "ZINIAO_STORE_REQUIRED",
    }
)


def failure(state: WorkflowState, node: str, error: DomainError) -> dict[str, object]:
    """记录阻塞错误；有映射时回到该门，重试超限或未知错误则终止。"""
    if error.code in AGENT_NETWORK_ERRORS or error.code in AGENT_ATTENTION_ERRORS:
        attention = error.code in AGENT_ATTENTION_ERRORS
        payload = {
            "kind": "external",
            "gate": node,
            "title": "需要在紫鸟电脑处理后继续" if attention else "等待紫鸟采集端恢复",
            "instructions": (
                (
                    "请在紫鸟电脑完成登录，并在三方词表面板重试失败的采集任务；成功后从当前步骤继续。"
                    if node == "third_party"
                    else "请在紫鸟电脑完成登录、验证码或店铺选择，然后点击从当前步骤继续。"
                )
                if attention
                else "系统会在采集端恢复在线后自动继续一次；也可以手动重新检测。"
            ),
            "actions": ["continue"] if attention else ["poll", "continue"],
            "refs": {
                "source": REMOTE_NODE_SOURCES.get(node, "browser_agent"),
                "error_code": error.code,
                "attention_required": "1" if attention else "0",
                "auto_resume": "0" if attention else "1",
                # 店铺缺失时页面据此弹出店铺选择框（多店铺无法自动判断）。
                "needs_store_choice": "1" if error.code == "ZINIAO_STORE_REQUIRED" else "0",
            },
            "ready_to_resume": not attention,
        }
        # 只中断一次：恢复后把节点交回路由重跑，真正重新尝试浏览器调用。
        # 不能在这里 while 循环 interrupt——LangGraph 恢复时会把同一任务内的
        # interrupt 按序号重放，第二次调用直接返回，节点会空转成死循环，
        # 并且一直占住这台设备的任务通道（表现为所有操作都卡住、状态不再推进）。
        interrupt(payload)
        return record(state, node, "running", "采集端已恢复，重试当前步骤", redo_from=node)
    detail = f"{error.code}: {error.message}"
    attempts = state["attempts"].get(node, 0) + 1
    target = redo_target(error.code) if attempts < MAX_ATTEMPTS else None
    return record(
        state,
        node,
        "failed",
        detail,
        failed_node=node,
        error=detail,
        redo_from=target,
        attempts={node: attempts},
    )
