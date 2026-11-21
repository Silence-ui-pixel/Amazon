"""图状态与节点清单；只保存 id、版本和状态，不保存业务文档。"""

from datetime import UTC, datetime
from typing import Annotated, Literal, TypedDict, cast

GRAPH_VERSION = "1"

NodeState = Literal["pending", "running", "done", "failed", "skipped"]

# 键即图节点名；展示标签只在前端/视图层使用。
NODE_SEQUENCE: tuple[tuple[str, str], ...] = (
    ("ensure_input", "校验输入"),
    ("identify_product", "商品识别"),
    ("product_confirmation", "商品确认"),
    ("generate_roots", "生成词根"),
    ("root_selection", "确认词根"),
    ("week", "选择报告周"),
    ("collect_core_report", "采集 ABA 周报"),
    ("core_terms", "核心大词"),
    ("competitor_discovery", "发现竞对"),
    ("competitor_selection", "确认竞对"),
    ("submit_sources", "提交拓词"),
    ("wait_sources", "等待采集中"),
    ("third_party", "三方词表"),
    ("create_cleaning", "生成清洗"),
    ("cleaning_review", "清洗审核"),
    ("create_traffic", "生成流量筛选"),
    ("traffic_review", "流量审核"),
    ("create_library", "生成词库"),
    ("library_publish", "发布"),
)
NODE_LABELS: dict[str, str] = dict(NODE_SEQUENCE)
NODE_KEYS: tuple[str, ...] = tuple(key for key, _ in NODE_SEQUENCE)


class NodeStatus(TypedDict):
    status: NodeState
    detail: str
    updated_at: str


def merge_maps[MapValue](
    left: dict[str, MapValue], right: dict[str, MapValue]
) -> dict[str, MapValue]:
    """节点状态与错误按 key 合并，节点只需返回自己那一条。"""
    return {**left, **right}


class WorkflowState(TypedDict):
    workflow_id: str
    run_id: str
    project_id: str
    graph_version: str
    revision: int
    node_status: Annotated[dict[str, NodeStatus], merge_maps]
    # 阻塞性错误：失败时记录节点与信息，成功执行任一节点时清空。
    failed_node: str | None
    error: str
    # 产物指针：只存 id / 版本，业务文档仍留在各自的表里。
    product_confirmation_version: int | None
    root_selection_id: str | None
    aba_report_id: str | None
    core_analysis_id: str | None
    hot_search_core_analysis_id: str | None
    hot_search_core_analysis_status: str | None
    hot_search_collection_id: str | None
    core_rule_version: str
    core_selection_id: str | None
    competitors_id: str | None
    expansion_forward_id: str | None
    expansion_reverse_id: str | None
    third_party_ids: list[str]
    cleaning_id: str | None
    cleaning_version: int | None
    cleaning_model_error: str
    traffic_id: str | None
    traffic_version: int | None
    library_id: str | None
    publication_version: int | None
    # 参数
    aba_week_end: str | None
    seller_start: str | None
    seller_end: str | None
    options: dict[str, object]
    redo_from: str | None
    attempts: Annotated[dict[str, int], merge_maps]
    browser_operation_ids: Annotated[dict[str, str], merge_maps]
    # 该任务全程使用的访问者浏览器设备；恢复/重试都读这一个值，绝不重新挑选。
    browser_device_id: str | None


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def initial_state(
    *,
    workflow_id: str,
    run_id: str,
    project_id: str,
    options: dict[str, object],
    browser_device_id: str | None = None,
) -> WorkflowState:
    """新实例的初始状态；所有字段都显式给出，便于类型检查。"""
    stamp = now_iso()
    return cast(
        WorkflowState,
        {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "project_id": project_id,
            "graph_version": GRAPH_VERSION,
            "revision": 0,
            "node_status": {
                key: {"status": "pending", "detail": "", "updated_at": stamp} for key in NODE_KEYS
            },
            "failed_node": None,
            "error": "",
            "product_confirmation_version": None,
            "root_selection_id": None,
            "aba_report_id": None,
            "core_analysis_id": None,
            "hot_search_core_analysis_id": None,
            "hot_search_core_analysis_status": None,
            "hot_search_collection_id": None,
            "core_rule_version": "",
            "core_selection_id": None,
            "competitors_id": None,
            "expansion_forward_id": None,
            "expansion_reverse_id": None,
            "third_party_ids": [],
            "cleaning_id": None,
            "cleaning_version": None,
            "cleaning_model_error": "",
            "traffic_id": None,
            "traffic_version": None,
            "library_id": None,
            "publication_version": None,
            "aba_week_end": None,
            "seller_start": None,
            "seller_end": None,
            "options": options,
            "redo_from": None,
            "attempts": {},
            "browser_operation_ids": {},
            "browser_device_id": browser_device_id,
        },
    )


def record(
    state: WorkflowState,
    node: str,
    status: NodeState,
    detail: str = "",
    **fields: object,
) -> dict[str, object]:
    """节点返回的状态更新：推进 revision、记录状态，并清空上一轮阻塞错误。"""
    return {
        "revision": state["revision"] + 1,
        "node_status": {node: {"status": status, "detail": detail, "updated_at": now_iso()}},
        "failed_node": None,
        "error": "",
        "redo_from": None,
        "attempts": {node: 0},
        **fields,
    }
