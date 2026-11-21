"""第七步面板的持久化模型审核任务；与编排共用模型审核实现。"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID

from app.domain.errors import DomainError
from app.domain.traffic import TrafficReviewProgress
from app.workflows.deps import WorkflowDeps
from app.workflows.nodes_steps_6_8 import review_traffic_report

logger = logging.getLogger(__name__)


class TrafficAutoReviewService:
    def __init__(self, deps: WorkflowDeps) -> None:
        self.deps = deps
        self.traffic = deps.traffic
        self.repository = deps.repository
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="traffic-review")

    def _save(self, progress: TrafficReviewProgress) -> None:
        with self.repository.connect() as conn:
            conn.execute(
                "INSERT INTO traffic_review_jobs (analysis_id,run_id,status,document) "
                "VALUES(?,?,?,?) ON CONFLICT(analysis_id) DO UPDATE "
                "SET status=excluded.status,document=excluded.document",
                (
                    str(progress.analysis_id),
                    str(progress.run_id),
                    progress.status,
                    progress.model_dump_json(),
                ),
            )

    def _workflow_running(self, run: UUID, analysis: UUID) -> bool:
        with self.repository.connect() as conn:
            rows = conn.execute(
                "SELECT state,view FROM workflow_runs WHERE run_id=? "
                "AND status IN ('queued','running','waiting_external')",
                (str(run),),
            ).fetchall()
        for state, view in rows:
            facts, projection = json.loads(state), json.loads(view)
            if (
                facts.get("traffic_id") == str(analysis)
                and projection.get("current_node") == "traffic_review"
            ):
                return True
        return False

    def get(self, project: UUID, run: UUID, analysis: UUID) -> TrafficReviewProgress:
        # 先校验项目/运行归属；绝不能仅凭任务 ID 读取其他项目的结果。
        report = self.traffic.get(project, run, analysis)
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM traffic_review_jobs WHERE analysis_id=? AND run_id=?",
                (str(analysis), str(run)),
            ).fetchone()
        progress = (
            TrafficReviewProgress.model_validate_json(row[0])
            if row
            else TrafficReviewProgress(
                analysis_id=analysis, project_id=project, run_id=run, updated_at=report.created_at
            )
        )
        progress.total, progress.required = report.total, report.required
        progress.reviewed, progress.version = report.reviewed, report.version
        progress.complete, progress.stale = report.complete, report.stale
        if report.stale:
            progress.status = "failed"
            progress.error_code = "TRAFFIC_INPUT_STALE"
            progress.error = "上游数据已变化，请重新生成流量筛选。"
        elif report.complete:
            progress.status, progress.error_code, progress.error = "succeeded", "", ""
        elif self._workflow_running(run, analysis):
            progress.status, progress.error_code, progress.error = "running", "", ""
        elif progress.status == "succeeded":
            # 人工把已审核词改回待核对后，不能沿用旧任务的成功状态。
            progress.status = "idle"
        return progress

    def start(self, project: UUID, run: UUID, analysis: UUID) -> TrafficReviewProgress:
        self.traffic.get(project, run, analysis)
        with self.repository.connect() as conn:
            # 同一筛选报告的启动、人工保存、模型保存共用行锁，双击不会重复收费。
            conn.execute(
                "SELECT id FROM traffic_analyses WHERE id=? AND run_id=? FOR UPDATE",
                (str(analysis), str(run)),
            )
            progress = self.get(project, run, analysis)
            if progress.stale:
                raise DomainError(409, "TRAFFIC_INPUT_STALE", progress.error)
            if progress.complete or progress.status in ("queued", "running"):
                return progress
            progress.attempt += 1
            progress.status, progress.error_code, progress.error = "queued", "", ""
            progress.updated_at = datetime.now(UTC)
            conn.execute(
                "INSERT INTO traffic_review_jobs (analysis_id,run_id,status,document) "
                "VALUES(?,?,?,?) ON CONFLICT(analysis_id) DO UPDATE "
                "SET status=excluded.status,document=excluded.document",
                (str(analysis), str(run), progress.status, progress.model_dump_json()),
            )
        self.executor.submit(self._execute, project, run, analysis, progress.attempt)
        return progress

    def _execute(self, project: UUID, run: UUID, analysis: UUID, attempt: int) -> None:
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM traffic_review_jobs WHERE analysis_id=? FOR UPDATE",
                (str(analysis),),
            ).fetchone()
            if row is None:
                return
            job = TrafficReviewProgress.model_validate_json(row[0])
            if job.attempt != attempt or job.status != "queued":
                return
            job.status, job.updated_at = "running", datetime.now(UTC)
            conn.execute(
                "UPDATE traffic_review_jobs SET status=?,document=? WHERE analysis_id=?",
                (job.status, job.model_dump_json(), str(analysis)),
            )
        try:
            report = self.traffic.get(project, run, analysis)
            if report.stale:
                raise DomainError(409, "TRAFFIC_INPUT_STALE", "上游数据已变化，请重新生成。")
            if not report.complete:
                review_traffic_report(self.deps, project, run, report)
            current = self.traffic.get(project, run, analysis)
            if not current.complete:
                raise DomainError(
                    409, "TRAFFIC_REVIEW_INCOMPLETE", "仍有未审核词，请续审剩余关键词。"
                )
            job.status, job.error_code, job.error = "succeeded", "", ""
        except DomainError as error:
            job.status, job.error_code, job.error = "failed", error.code, error.message
        except Exception:
            logger.exception("Traffic model review failed for %s", analysis)
            job.status, job.error_code = "failed", "TRAFFIC_REVIEW_FAILED"
            job.error = "模型审核异常；已保存结果保留，可重试剩余词。"
        job.updated_at = datetime.now(UTC)
        self._save(job)

    def recover(self) -> None:
        # 重启后不盲目重新发送可能已计费请求；保留断点并提供明确重试入口。
        with self.repository.connect() as conn:
            rows = conn.execute(
                "SELECT document FROM traffic_review_jobs WHERE status IN ('queued','running')"
            ).fetchall()
        for row in rows:
            job = TrafficReviewProgress.model_validate_json(row[0])
            job.status, job.error_code = "failed", "TRAFFIC_REVIEW_INTERRUPTED"
            job.error = "服务重启，审核已暂停；已保存结果保留，请重试未审核词。"
            job.updated_at = datetime.now(UTC)
            self._save(job)

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
