"""汇总全部节点；图装配只依赖这里的入口。"""

from app.workflows.deps import WorkflowDeps
from app.workflows.nodes_common import Node
from app.workflows.nodes_steps_1_2 import build_nodes_1_2
from app.workflows.nodes_steps_3_5 import build_nodes_3_5
from app.workflows.nodes_steps_6_8 import build_nodes_6_8


def build_nodes(deps: WorkflowDeps) -> dict[str, Node]:
    nodes: dict[str, Node] = {}
    nodes.update(build_nodes_1_2(deps))
    nodes.update(build_nodes_3_5(deps))
    nodes.update(build_nodes_6_8(deps))
    return nodes
