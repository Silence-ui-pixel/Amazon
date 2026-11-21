"""真实八步编排联调：真实模型、ABA 会话、前台搜索与卖家精灵。

使用独立数据库目录，不修改生产任务数据；日志实时打印，便于后台运行观察。
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.domain.workflow import WorkflowView
from app.main import create_app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        default="data/e2e-live-20261027/keyword-library.sqlite3",
        help="独立数据库路径",
    )
    parser.add_argument("--asin", default="B0BGP8RBWR")
    parser.add_argument("--product-name", default="Sliding Gate Wheel Caster")
    parser.add_argument(
        "--description",
        default=(
            "Heavy duty sliding gate wheel caster, 2 inch gate roller for sliding gate "
            "systems, steel bracket, for driveway gates, indoor and outdoor use"
        ),
    )
    parser.add_argument("--project", help="复用已有项目 ID")
    parser.add_argument("--run", help="复用已有任务 ID")
    parser.add_argument("--workflow-id", help="续跑已有编排实例")
    parser.add_argument("--inline", action="store_true", help="在请求线程同步执行图")
    parser.add_argument("--minutes", type=int, default=90)
    args = parser.parse_args()

    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.minutes * 60
    with TestClient(create_app(db, workflow_inline=args.inline)) as api:
        if args.project and args.run:
            project, run = args.project, args.run
        else:
            project = api.post(
                "/api/v1/projects", json={"name": "联调 gate wheel", "marketplace": "US"}
            ).json()["id"]
            run = api.post(
                f"/api/v1/projects/{project}/pipeline-runs",
                json={
                    "marketplace": "US",
                    "asin": args.asin,
                    "product_name": args.product_name,
                    "description": args.description,
                },
                headers={"Idempotency-Key": "e2e-live-20261027"},
            ).json()["id"]
        base = f"/api/v1/projects/{project}/pipeline-runs/{run}"
        if args.workflow_id:
            view = WorkflowView.model_validate(
                api.get(base + f"/workflow/{args.workflow_id}").json()
            )
        else:
            started = api.post(base + "/workflow", json={"options": {"auto_decide": True}})
            print("start", started.status_code, started.text[:300], flush=True)
            if started.status_code != 202:
                return
            view = WorkflowView.model_validate(started.json())
        last: tuple[object, object, int] | None = None
        while time.monotonic() < deadline:
            gate = view.waiting_for.gate if view.waiting_for else None
            key = (view.status, gate, view.revision)
            if key != last:
                print("state", key, "error=", view.error, flush=True)
                for node in view.nodes:
                    if node.status != "pending":
                        print("  ", node.key, node.status, node.detail, flush=True)
                last = key
            if view.status == "failed":
                print("FAILED", view.error, flush=True)
                return
            if gate == "library_publish":
                print("REACHED_PUBLISH_GATE", flush=True)
                print(
                    "nodes",
                    json.dumps({node.key: node.status for node in view.nodes}, ensure_ascii=False),
                    flush=True,
                )
                reports = api.get(base + "/library").json()
                if reports:
                    print(
                        "library_entries",
                        len(reports[0]["entries"]),
                        "publication_version",
                        reports[0]["publication_version"],
                        flush=True,
                    )
                return
            if view.waiting_for and view.waiting_for.kind == "external":
                time.sleep(10)
                response = api.post(
                    base + f"/workflow/{view.workflow_id}/resume",
                    json={
                        "gate": view.waiting_for.gate,
                        "action": "poll",
                        "expected_revision": view.revision,
                        "payload": {},
                    },
                )
                if response.status_code != 202:
                    print("resume", response.status_code, response.text[:300], flush=True)
                    return
                view = WorkflowView.model_validate(response.json())
                continue
            if view.waiting_for and view.waiting_for.kind == "human":
                print("UNEXPECTED_HUMAN_GATE", view.waiting_for.gate, flush=True)
                return
            time.sleep(5)
            view = WorkflowView.model_validate(
                api.get(base + f"/workflow/{view.workflow_id}").json()
            )
        print("TIMEOUT", flush=True)


if __name__ == "__main__":
    main()
