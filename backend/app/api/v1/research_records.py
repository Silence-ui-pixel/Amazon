"""研究记录只读索引：跨项目保留每轮研究，不启动采集或模型。"""

import json
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query

from app.db.repository import Repository
from app.domain.errors import DomainError
from app.domain.workflow import WorkflowStatus

# 只取列表字段；词库原文、图片和模型诊断不进入列表响应。
# 每轮 run 独立列出，同一 ASIN 不合并；关联最新编排视图和最新词库摘要以便追踪重跑。
# workflow_runs 与业务库同库同 schema，无需 ATTACH；JSON 查询用 jsonb 算子按需转换。
BASE_SQL = """
WITH records AS (
 SELECT r.id, r.project_id,
 CAST(r.document AS jsonb) #>> '{input,asin}' AS asin,
 CAST(r.document AS jsonb) #>> '{input,marketplace}' AS marketplace,
 coalesce(
   (SELECT CAST(document AS jsonb) #>> '{data,product,product_name}' FROM product_confirmations
    WHERE run_id=r.id ORDER BY version DESC LIMIT 1),
   CAST(r.document AS jsonb) #>> '{input,product_name}',
   (SELECT CAST(document AS jsonb) #>> '{product,product_name}' FROM product_understandings
    WHERE run_id=r.id), '') AS product_name,
 CAST(p.document AS jsonb) #>> '{name}' AS project_name,
 CAST(r.document AS jsonb) #>> '{created_at}' AS created_at,
 coalesce(w.updated_at, CAST(r.document AS jsonb) #>> '{created_at}') AS updated_at,
 coalesce(w.status, 'input_received') AS status,
 w.workflow_id, w.view AS workflow_view,
 coalesce(CAST(w.state AS jsonb) #>> '{aba_week_end}',
          CAST(w.state AS jsonb) #>> '{options,report_week_end}',
          (SELECT CAST(document AS jsonb) #>> '{week_end}' FROM aba_reports
           WHERE run_id=r.id ORDER BY created_at DESC, seq DESC LIMIT 1)) AS report_week,
 (SELECT jsonb_build_object(
   'id', l.id,
   'publication_version', NULLIF(
      CAST(l.document AS jsonb) #>> '{publication_version}', ''
    )::integer,
   'total', jsonb_array_length(CAST(l.document AS jsonb) -> 'entries'),
   'ranked', (SELECT count(*) FROM jsonb_array_elements(CAST(l.document AS jsonb) -> 'entries') e
              WHERE (e.value #>> '{evidence,metrics,rank}')::numeric > 0))::text
  FROM keyword_library_drafts l WHERE l.run_id=r.id
  ORDER BY CAST(l.document AS jsonb) #>> '{created_at}' DESC, l.seq DESC LIMIT 1) AS library
 FROM pipeline_runs r JOIN projects p ON p.id=r.project_id
 LEFT JOIN LATERAL (
   SELECT w.* FROM workflow_runs w
   WHERE w.run_id=r.id AND w.project_id=r.project_id
   ORDER BY w.created_at DESC, w.seq DESC LIMIT 1
 ) w ON TRUE
 WHERE EXISTS(SELECT 1 FROM research_record_deletions d WHERE d.run_id=r.id) = :deleted
)
"""


def build_research_router(repository: Repository) -> APIRouter:
    router = APIRouter(tags=["research-records"])

    @router.get("/api/v1/research-records")
    def records(
        q: Annotated[str, Query(max_length=200)] = "",
        status: WorkflowStatus | Annotated[str, Query(pattern="^input_received$")] | None = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        deleted: bool = False,
    ) -> dict[str, Any]:
        filters = (
            " WHERE (strpos(lower(coalesce(asin,'')),lower(?)) > 0 "
            "OR strpos(lower(product_name),lower(?)) > 0)"
        )
        params: list[str | int] = [q.strip(), q.strip()]
        if status is not None:
            filters += " AND status=?"
            params.append(status)
        with repository.connect() as conn:
            # 独立墓碑只控制列表可见性，不删除图片、发布快照或工作流检查点。
            sql = BASE_SQL.replace(":deleted", "TRUE" if deleted else "FALSE")
            total_row = conn.execute(sql + "SELECT count(*) FROM records" + filters, params).fetchone()
            assert total_row is not None
            total = int(total_row[0])
            cursor = conn.execute(
                sql
                + "SELECT * FROM records"
                + filters
                + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
            names = list(cursor.keys())
            items: list[dict[str, Any]] = []
            for row in cursor.fetchall():
                item = dict(zip(names, row, strict=True))
                view = json.loads(item.pop("workflow_view") or "{}")
                node = view.get("current_node")
                item["current_step"] = next(
                    (n["label"] for n in view.get("nodes", []) if n["key"] == node), ""
                )
                item["error"] = view.get("error", "")
                item["library"] = json.loads(item["library"]) if item["library"] else None
                items.append(item)
        return {"total": total, "offset": offset, "limit": limit, "items": items}

    @router.delete("/api/v1/projects/{project_id}/research-records/{run_id}")
    def delete_record(project_id: UUID, run_id: UUID) -> dict[str, bool]:
        repository.get_run(project_id, run_id)
        with repository.connect() as conn:
            active = conn.execute(
                "SELECT 1 FROM workflow_runs WHERE run_id=? "
                "AND status IN ('queued','running','waiting_external') LIMIT 1",
                (str(run_id),),
            ).fetchone()
            if active:
                raise DomainError(
                    409, "RESEARCH_RUNNING", "研究正在执行或等待采集，请先终止任务再删除"
                )
            conn.execute(
                "INSERT INTO research_record_deletions (run_id, deleted_at) VALUES (?,?) "
                "ON CONFLICT (run_id) DO NOTHING",
                (str(run_id), datetime.now(UTC).isoformat()),
            )
        return {"deleted": True}

    @router.post("/api/v1/projects/{project_id}/research-records/{run_id}/restore")
    def restore_record(project_id: UUID, run_id: UUID) -> dict[str, bool]:
        repository.get_run(project_id, run_id)
        with repository.connect() as conn:
            conn.execute("DELETE FROM research_record_deletions WHERE run_id=?", (str(run_id),))
        return {"deleted": False}

    return router
