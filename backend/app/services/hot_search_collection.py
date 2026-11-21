"""多商品词按 25 万排名边界采集；每个商品词失败可单独恢复。"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from app.db.repository import Repository
from app.domain.aba import AbaQueryReport, AbaReport
from app.domain.errors import DomainError
from app.domain.hot_search_collection import HotSearchCollectionJob, HotSearchTermTask
from app.services.aba import AbaService

RANK_LIMIT = 250000
MAX_ROWS = 10000


def latest_completed_week(now: datetime) -> datetime:
    yesterday = now.astimezone(UTC).date() - timedelta(days=1)
    day = yesterday - timedelta(days=(yesterday.weekday() - 5) % 7)
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC)


class HotSearchCollectionService:
    def __init__(self, repository: Repository, aba: AbaService) -> None:
        self.repository = repository
        self.aba = aba

    def _save(self, job: HotSearchCollectionJob, reports: list[AbaQueryReport]) -> None:
        job.updated_at = datetime.now(UTC)
        raw = [{"query": report.query, "rows": report.raw_rows} for report in reports]
        with self.repository.connect() as conn:
            conn.execute(
                "UPDATE hot_search_collection_jobs SET document=?,raw_document=?,updated_at=? "
                "WHERE id=?",
                (
                    job.model_dump_json(),
                    json.dumps(raw, ensure_ascii=False),
                    job.updated_at.isoformat(),
                    str(job.id),
                ),
            )

    def _load_reports(self, job_id: UUID) -> list[AbaQueryReport]:
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT raw_document FROM hot_search_collection_jobs WHERE id=?", (str(job_id),)
            ).fetchone()
        if not row:
            return []
        data = json.loads(row[0])
        reports: list[AbaQueryReport] = []
        for entry in data:
            if "report" not in entry:
                continue
            report = AbaQueryReport.model_validate(entry["report"])
            report.raw_rows = entry.get("rows", [])
            reports.append(report)
        return reports

    def _save_reports(self, job: HotSearchCollectionJob, reports: list[AbaQueryReport]) -> None:
        job.updated_at = datetime.now(UTC)
        raw = [
            {
                "query": report.query,
                "report": report.model_dump(mode="json"),
                "rows": report.raw_rows,
            }
            for report in reports
        ]
        with self.repository.connect() as conn:
            conn.execute(
                "UPDATE hot_search_collection_jobs SET document=?,raw_document=?,updated_at=? "
                "WHERE id=?",
                (
                    job.model_dump_json(),
                    json.dumps(raw, ensure_ascii=False),
                    job.updated_at.isoformat(),
                    str(job.id),
                ),
            )

    def start(
        self,
        project: UUID,
        run: UUID,
        *,
        now: datetime | None = None,
    ) -> HotSearchCollectionJob:
        terms, context = self.aba.product_term_context(project, run)
        clock = now or datetime.now(UTC)
        week_end = latest_completed_week(clock).date()
        snapshot = json.dumps(
            {
                "context": context,
                "terms": terms,
                "week_end": week_end.isoformat(),
                "rank_limit": RANK_LIMIT,
                "rule_version": "hot-search-core-v2",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        fingerprint = hashlib.sha256(snapshot.encode()).hexdigest()
        with self.repository.connect() as conn:
            saved = conn.execute(
                "SELECT document FROM hot_search_collection_jobs WHERE run_id=? AND fingerprint=?",
                (str(run), fingerprint),
            ).fetchone()
            if saved:
                return HotSearchCollectionJob.model_validate_json(saved[0])
            job = HotSearchCollectionJob(
                id=uuid4(),
                project_id=project,
                run_id=run,
                fingerprint=fingerprint,
                context_hash=context,
                week_start=week_end - timedelta(days=6),
                week_end=week_end,
                tasks=[HotSearchTermTask(product_term=term, updated_at=clock) for term in terms],
                created_at=clock,
                updated_at=clock,
            )
            conn.execute(
                "INSERT INTO hot_search_collection_jobs "
                "(id,run_id,fingerprint,created_at,updated_at,document,raw_document) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    str(job.id),
                    str(run),
                    fingerprint,
                    clock.isoformat(),
                    clock.isoformat(),
                    job.model_dump_json(),
                    "[]",
                ),
            )
        return job

    def get(self, project: UUID, run: UUID, job_id: UUID) -> HotSearchCollectionJob:
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM hot_search_collection_jobs WHERE id=? AND run_id=?",
                (str(job_id), str(run)),
            ).fetchone()
        if not row:
            raise DomainError(404, "HOT_SEARCH_COLLECTION_NOT_FOUND", "未找到热门搜索词采集任务")
        return self._export_state(HotSearchCollectionJob.model_validate_json(row[0]))

    def _export_state(self, job):
        exports = getattr(self.aba, 'source_exports', None)
        if exports:
            items = exports.store.list(job.project_id, job.run_id)
            for task in job.tasks:
                item = next((i for i in items if i['owner'] == str(self._report_id(job)) and i['query'] == task.product_term and i['kind'] == 'step2'), None)
                if item:
                    task.export_status = item['status']
                    task.export_error_code = item.get('error_code', '')
                    task.export_error = item.get('error', '')
        return job

    def _report_id(self, job):
        # 提前为本任务确定来源ID，使每个词一采集完成就能并行下载；
        # report_id 仍只在全部候选完成后发布，不改变原有工作流就绪判断。
        return job.report_id or uuid5(NAMESPACE_URL, f'keyword-hot-search-report:{job.id}')

    def _queue_export(self, job, query, report):
        exports = getattr(self.aba, 'source_exports', None)
        if exports:
            exports.enqueue(job.project_id, job.run_id, 'step2', self._report_id(job), query,
                {'query': query, 'week_end': job.week_end.isoformat(), 'query_mode': 'product_term',
                 'max_rows': MAX_ROWS, 'rank_limit': RANK_LIMIT}, report=report)

    def latest(self, project: UUID, run: UUID) -> HotSearchCollectionJob:
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM hot_search_collection_jobs WHERE run_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (str(run),),
            ).fetchone()
        if not row:
            raise DomainError(404, "HOT_SEARCH_COLLECTION_NOT_FOUND", "未找到热门搜索词采集任务")
        return self._export_state(HotSearchCollectionJob.model_validate_json(row[0]))

    def _collect_task(
        self,
        job: HotSearchCollectionJob,
        task: HotSearchTermTask,
        reports: list[AbaQueryReport],
    ) -> None:
        task.status = "collecting"
        task.export_status = "queued"
        task.error_code = ""
        task.error = ""
        task.last_output = "正在连接紫鸟并读取热门搜索词"
        self._save_reports(job, reports)

        def progress(report: AbaQueryReport) -> None:
            task.page = report.pages
            task.candidate_count = len(report.rows)
            task.last_output = f"第 {report.pages} 页已保存，累计 {len(report.rows)} 条 25 万内候选"
            self._save_reports(job, reports)

        try:
            result = self.aba.client.fetch(
                task.product_term,
                job.week_end.isoformat(),
                max_rows=MAX_ROWS,
                query_mode="product_term",
                rank_limit=RANK_LIMIT,
                on_progress=progress,
            )
            if result.truncated or result.stop_reason not in ("complete", "rank_limit"):
                raise DomainError(
                    502,
                    "ABA_COLLECTION_INCOMPLETE",
                    "热门搜索词未可靠到达 25 万排名边界，请重试该商品词",
                )
            reports[:] = [item for item in reports if item.query != task.product_term]
            reports.append(result)
            order = {item.product_term: index for index, item in enumerate(job.tasks)}
            reports.sort(key=lambda item: order[item.query])
            task.status = "completed"
            task.export_status = "queued"
            task.page = result.pages
            task.candidate_count = len(result.rows)
            task.last_output = f"已到达 25 万排名边界，共 {len(result.rows)} 条候选"
            self._queue_export(job, task.product_term, result)
        except DomainError as error:
            task.status = "failed"
            task.export_status = "download_failed"
            task.export_error_code = error.code
            task.export_error = error.message
            task.error_code = error.code
            task.error = error.message
            task.last_output = "候选采集失败"
        task.updated_at = datetime.now(UTC)
        self._save_reports(job, reports)

    def _publish_report(self, job: HotSearchCollectionJob, reports: list[AbaQueryReport]) -> UUID:
        report = AbaReport(
            id=self._report_id(job),
            project_id=job.project_id,
            run_id=job.run_id,
            week_start=job.week_start,
            week_end=job.week_end,
            fetched_at=datetime.now(UTC),
            roots_snapshot=[task.product_term for task in job.tasks],
            query_terms_snapshot=[task.product_term for task in job.tasks],
            rule_version="hot-search-core-v2",
            query_mode="product_term",
            max_rows=MAX_ROWS,
            reports=reports,
        )
        raw = [{"query": item.query, "rows": item.raw_rows} for item in reports]
        with self.repository.connect() as conn:
            conn.execute(
                "INSERT INTO aba_reports "
                "(id,run_id,fingerprint,context_hash,created_at,document,raw_document) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING",
                (
                    str(report.id),
                    str(job.run_id),
                    job.fingerprint,
                    job.context_hash,
                    report.fetched_at.isoformat(),
                    report.model_dump_json(),
                    json.dumps(raw, ensure_ascii=False),
                ),
            )
        for query in report.reports:
            self._queue_export(job, query.query, query)
        return report.id

    def execute(self, project: UUID, run: UUID, job_id: UUID) -> HotSearchCollectionJob:
        job = self.get(project, run, job_id)
        reports = self._load_reports(job.id)
        for task in job.tasks:
            if task.status == "completed":
                continue
            self._collect_task(job, task, reports)
        if all(task.status == "completed" for task in job.tasks):
            job.status = "completed"
            if job.report_id is None:
                job.report_id = self._publish_report(job, reports)
        elif any(task.status == "failed" for task in job.tasks):
            job.status = "failed"
        else:
            job.status = "collecting"
        self._save_reports(job, reports)
        return job

    def workbook(self, project: UUID, run: UUID, job_id: UUID) -> bytes:
        from app.adapters.hot_search_workbook import build_workbook

        job = self.get(project, run, job_id)
        return build_workbook(job, self._load_reports(job.id))

    def retry(
        self, project: UUID, run: UUID, job_id: UUID, product_term: str
    ) -> HotSearchCollectionJob:
        job = self.get(project, run, job_id)
        task = next((item for item in job.tasks if item.product_term == product_term), None)
        if task is None:
            raise DomainError(404, "HOT_SEARCH_TERM_NOT_FOUND", "采集任务中没有该商品词")
        if task.status != "failed":
            return job
        task.status = "queued"
        task.retries += 1
        reports = self._load_reports(job.id)
        self._collect_task(job, task, reports)
        if all(item.status == "completed" for item in job.tasks):
            job.status = "completed"
            if job.report_id is None:
                job.report_id = self._publish_report(job, reports)
        else:
            job.status = "failed"
        self._save_reports(job, reports)
        return job
