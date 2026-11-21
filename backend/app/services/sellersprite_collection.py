"""持久化浏览器采集任务；不同设备并行，同一设备有序执行。"""

from datetime import UTC, datetime
from threading import RLock
from uuid import UUID, uuid4

from app.adapters.browser_tasks import BrowserTaskExecutor
from app.adapters.remote_browser import submit_with_browser_operation_context
from app.adapters.sellersprite_browser import SellerspriteBrowserDownloader, SellerspriteDownloader
from app.adapters.third_party import read_table
from app.domain.errors import DomainError
from app.domain.sellersprite import BrowserCollectionInput, BrowserCollectionJob
from app.domain.text import phrase_key
from app.domain.third_party import ImportMetadata
from app.services.third_party import ThirdPartyService


class SellerspriteCollectionService:
    def __init__(
        self,
        imports: ThirdPartyService,
        *,
        auto_execute: bool = True,
        downloader: SellerspriteDownloader | None = None,
    ) -> None:
        self.imports = imports
        self.directory = imports.repository.storage_root / "sellersprite" / "jobs"
        self.downloader = downloader or SellerspriteBrowserDownloader(
            directory=imports.repository.storage_root / "sellersprite"
        )
        self.lock = RLock()
        self.executor = BrowserTaskExecutor(thread_name_prefix="sellersprite")
        self.auto_execute = auto_execute

    def _save(self, job: BrowserCollectionJob) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        job.updated_at = datetime.now(UTC)
        target = self.directory / f"{job.id}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(job.model_dump_json(), encoding="utf-8")
        temporary.replace(target)

    def _jobs(self) -> list[BrowserCollectionJob]:
        if not self.directory.exists():
            return []
        return [
            BrowserCollectionJob.model_validate_json(p.read_text(encoding="utf-8"))
            for p in self.directory.glob("*.json")
        ]

    def list(self, project: UUID, run: UUID) -> list[BrowserCollectionJob]:
        self.imports.repository.get_run(project, run)
        with self.lock:
            return sorted(
                [j for j in self._jobs() if (j.project_id, j.run_id) == (project, run)],
                key=lambda j: j.created_at,
                reverse=True,
            )[:100]

    def create(
        self, project: UUID, run: UUID, data: BrowserCollectionInput
    ) -> BrowserCollectionJob:
        if self.imports.repository.get_run(project, run).input.marketplace != "US":
            raise DomainError(
                422, "SELLERSPRITE_MARKETPLACE_UNSUPPORTED", "自动获取目前仅支持美国站"
            )
        core, competitor, queries = self.imports.expansion.context(project, run, data.direction)
        if data.method == "traffic_extension":
            available = {phrase_key(value): value for value in queries}
            selected = [available.get(phrase_key(value)) for value in data.queries]
            if any(value is None for value in selected):
                raise DomainError(
                    422,
                    "SELLERSPRITE_QUERY_INVALID",
                    "拓展流量词只能选择当前已保留的竞对 ASIN",
                )
            normalized = [value for value in selected if value is not None]
            data = data.model_copy(update={"query": ",".join(normalized), "queries": normalized})
        else:
            query = next((q for q in queries if phrase_key(q) == phrase_key(data.query)), None)
            if query is None:
                raise DomainError(
                    422, "SELLERSPRITE_QUERY_INVALID", "请选择已确认的核心词或保留竞品"
                )
            data = data.model_copy(update={"query": query, "queries": [query]})
        with self.lock:
            active = next(
                (
                    j
                    for j in self._jobs()
                    if (j.project_id, j.run_id) == (project, run)
                    and j.request == data
                    and j.core_selection_id == core
                    and j.competitor_id == competitor
                    and j.status in ("queued", "running")
                ),
                None,
            )
            if active:
                return active
            now = datetime.now(UTC)
            job = BrowserCollectionJob(
                id=uuid4(),
                project_id=project,
                run_id=run,
                request=data,
                core_selection_id=core,
                competitor_id=competitor,
                created_at=now,
                updated_at=now,
            )
            self._save(job)
            if self.auto_execute:
                submit_with_browser_operation_context(self.executor, self.execute, job.id)
            return job

    def execute(self, key: UUID) -> None:
        with self.lock:
            job = next(j for j in self._jobs() if j.id == key)
            job.status = "running"
            job.stage = "查询并等待卖家精灵导出（可能需要 1–5 分钟）"
            self._save(job)
        try:
            request = job.request
            if not isinstance(request, BrowserCollectionInput):
                raise DomainError(
                    409, "SELLERSPRITE_LEGACY_JOB", "历史月任务不能重跑，请创建最近30天采集"
                )
            expected = (job.core_selection_id, job.competitor_id)
            if (
                self.imports.expansion.context(job.project_id, job.run_id, request.direction)[:2]
                != expected
            ):
                raise DomainError(409, "IMPORT_CONTEXT_CHANGED", "上游已变化，请重新采集")
            directory = self.directory.parent / "downloads" / str(job.id)
            directory.mkdir(parents=True, exist_ok=True)
            name, raw, total = self.downloader.download(request, directory / "analysis.xlsx")
            # 快速接口生成件单独落盘，不冒充平台原件。
            (directory / 'analysis.xlsx').write_bytes(raw)
            table = read_table(name, raw)
            rows = len(table.rows)
            # 文件行数与页面总数交叉核验，防止拿到旧下载文件或错误查询的导出件。
            if rows > total:
                raise DomainError(
                    422,
                    "SELLERSPRITE_EXPORT_INCOMPLETE",
                    "导出件行数多于页面结果总数，未导入，请重新核对",
                )
            # 卖家精灵单次导出有上限（当前 2000 行）；少于页面总数时按截断记录，
            # 只用已导出的部分做词频，不声称覆盖全部结果。
            truncated = rows < total
            metadata = ImportMetadata(
                direction=request.direction,
                query=request.query,
                collection_method=request.method,
                source_queries=request.queries,
                variant_scope=request.variant_scope,
                marketplace="US",
                # 记录采集时间而不倒推日期边界，供应商“最近30天”口径与 ABA 周独立。
                period_kind="last_30_days",
                collected_at=datetime.now(UTC),
                source_total=total,
                export_complete=not truncated,
                truncated=truncated,
                currency="USD",
                batch_key=str(job.id),
            )
            report = self.imports.preview(
                job.project_id, job.run_id, name, raw, metadata, expected_context=expected
            )
            exports = getattr(self, 'source_exports', None)
            if exports:
                exports.enqueue(job.project_id, job.run_id, 'ss_' + request.direction, report.id, request.query,
                    request.model_dump(mode='json'), source_file=(name, raw, total, rows))
            with self.lock:
                job.report_id = report.id
                job.status = "succeeded"
                job.stage = (
                    f"已采集并导入 {report.imported_rows} / {total} 行，待确认字段映射"
                    if truncated
                    else f"已采集并导入 {report.imported_rows} 行，待确认字段映射"
                )
                self._save(job)
        except DomainError as error:
            with self.lock:
                job.status = "failed"
                job.error = error.message
                job.error_code = error.code
                job.stage = "采集失败"
                self._save(job)
        except Exception:
            # 不把供应商正文、登录数据或内部堆栈回传给页面。
            with self.lock:
                job.status = "failed"
                job.error = "卖家精灵浏览器采集失败，请检查登录、网络和页面变化后重试"
                job.stage = "采集失败"
                self._save(job)

    def recover(self) -> None:
        with self.lock:
            for job in self._jobs():
                if job.status in ("queued", "running"):
                    job.status = "failed"
                    job.stage = "任务中断"
                    job.error = "服务已重启，请检查卖家精灵导出记录后重试"
                    self._save(job)

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
