"""显式异步 ABA 拓词；失败、截断与来源证据均持久化。"""

import csv
import hashlib
import io
import json
import logging
from datetime import UTC, date, datetime, timedelta
from threading import Lock
from typing import cast
from uuid import UUID, uuid4

from app.adapters.aba import AbaProvider, normalize_row
from app.adapters.aba_expansion_workbook import build_query_workbook, query_filename
from app.adapters.browser_tasks import BrowserTaskExecutor
from app.adapters.remote_browser import submit_with_browser_operation_context
from app.db.repository import Repository
from app.domain.aba import AbaQueryReport
from app.domain.aba_expansion import (
    Direction,
    ExpansionEvidence,
    ExpansionKeyword,
    ExpansionPage,
    ExpansionQuery,
    ExpansionReport,
    ExpansionRequest,
    ExpansionWorkspace,
)
from app.domain.errors import DomainError
from app.domain.text import phrase_key
from app.domain.traffic import RuleSet
from app.services.competitors import CompetitorService
from app.services.core_terms import CoreService

logger = logging.getLogger(__name__)

PARTIAL_FINISH_WARNING = (
    "部分查询失败或达到采集边界，当前结果不代表穷尽；请查看各查询原因后显式重试。"
)
PROTOCOL_VERSION = "aba-expansion-v2-batch-reverse"


def _ensure_current_traffic_metrics(report: AbaQueryReport) -> AbaQueryReport:
    """在服务器保存边界补齐旧 Agent 未返回的当前指标快照。"""
    current_rule = RuleSet().id
    rows = []
    for index, row in enumerate(report.rows):
        if row.traffic_metrics is not None and row.traffic_metrics.rule_id == current_rule:
            rows.append(row)
            continue
        if index >= len(report.raw_rows):
            rows.append(row)
            continue
        try:
            normalized = normalize_row(report.raw_rows[index])
        except DomainError:
            rows.append(row)
            continue
        # 只接受同位置、同关键词、同排名的原始证据，避免错位时写入错误指标。
        if (
            phrase_key(normalized.keyword) != phrase_key(row.keyword)
            or normalized.search_frequency_rank != row.search_frequency_rank
        ):
            rows.append(row)
            continue
        rows.append(row.model_copy(update={"traffic_metrics": normalized.traffic_metrics}))
    upgraded = report.model_copy(update={"rows": rows})
    upgraded.raw_rows = list(report.raw_rows)
    return upgraded


class ExpansionService:
    def __init__(
        self,
        repository: Repository,
        client: AbaProvider,
        core: CoreService,
        competitors: CompetitorService,
    ) -> None:
        self.repository, self.client, self.core, self.competitors = (
            repository,
            client,
            core,
            competitors,
        )
        self.executor = BrowserTaskExecutor(thread_name_prefix="aba-expansion")
        self.lock = Lock()

    def recover(self) -> None:
        with self.repository.connect() as conn:
            for key, document in conn.execute("SELECT id,document FROM aba_expansions").fetchall():
                report = ExpansionReport.model_validate_json(document)
                if report.status == "running":
                    report.status = "interrupted"
                    for query in report.queries:
                        if query.status in ("pending", "running"):
                            query.status = "interrupted"
                    report.warnings.append("服务曾中断，请显式重新采集；刷新不会自动请求。")
                    conn.execute(
                        "UPDATE aba_expansions SET document=? WHERE id=?",
                        (report.model_dump_json(), key),
                    )

    def close(self) -> None:
        self.executor.shutdown(wait=True)

    def context(
        self, project: UUID, run: UUID, direction: Direction
    ) -> tuple[UUID, UUID | None, list[str]]:
        self.repository.get_run(project, run)
        try:
            core = self.core.selection(project, run)
        except DomainError as error:
            if error.status_code != 404:
                raise
            raise DomainError(409, "CORE_SELECTION_REQUIRED", "请先确认第二步核心大词") from None
        if core.stale:
            raise DomainError(409, "CORE_SELECTION_STALE", "核心大词已过期，请重新确认")
        if direction == "forward":
            return core.id, None, list(dict.fromkeys(item.keyword for item in core.items))
        try:
            competitors = self.competitors.get(project, run)
        except DomainError as error:
            if error.status_code != 404:
                raise
            raise DomainError(409, "COMPETITORS_REQUIRED", "请先确认第三步竞对") from None
        if competitors.stale or not competitors.confirmed_at:
            raise DomainError(409, "COMPETITORS_REQUIRED", "请先保存当前第三步竞对选择")
        asins = list(
            dict.fromkeys(item.asin for item in competitors.items if item.decision == "include")
        )
        if not 1 <= len(asins) <= 100:
            raise DomainError(409, "COMPETITORS_REQUIRED", "反向查询需要 1 至 100 个已保留竞对")
        return core.id, competitors.id, asins

    def get(self, project: UUID, run: UUID, key: UUID) -> ExpansionReport:
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM aba_expansions WHERE id=? AND run_id=?", (str(key), str(run))
            ).fetchone()
        if not row:
            raise DomainError(404, "EXPANSION_NOT_FOUND", "未找到此任务的拓词记录")
        report = ExpansionReport.model_validate_json(row[0])
        try:
            core, competitors, _ = self.context(project, run, report.direction)
            report.stale = (
                core != report.core_selection_id
                or competitors != report.competitor_id
                or report.protocol_version != PROTOCOL_VERSION
            )
        except DomainError as error:
            if error.status_code != 409:
                raise
            report.stale = True
        return report

    def collect(self, project: UUID, run: UUID, data: ExpansionRequest) -> ExpansionReport:
        if self.repository.get_run(project, run).input.marketplace != "US":
            raise DomainError(422, "ABA_SITE_UNSUPPORTED", "当前适配器仅支持美国站 ABA")
        if data.query_mode == "product_term":
            raise DomainError(422, "ABA_QUERY_MODE_INVALID", "第四步拓词不支持商品词查询模式")
        week_end = data.week_end
        if week_end is None:
            raise DomainError(422, "ABA_WEEK_INVALID", "拓词必须指定已结束报告周的周六")
        core, competitors, queries = self.context(project, run, data.direction)
        if not queries:
            raise DomainError(409, "CORE_SELECTION_REQUIRED", "没有已确认的核心词")
        report_queries = [",".join(queries)] if data.direction == "reverse" else queries
        fingerprint = hashlib.sha256(
            json.dumps(
                [
                    str(run),
                    str(core),
                    str(competitors),
                    data.direction,
                    week_end.isoformat(),
                    data.max_rows,
                    report_queries,
                    PROTOCOL_VERSION,
                ]
            ).encode()
        ).hexdigest()
        with self.lock, self.repository.connect() as conn:
            row = conn.execute(
                "SELECT id FROM aba_expansions WHERE run_id=? AND fingerprint=? ORDER BY created_at DESC LIMIT 1",
                (str(run), fingerprint),
            ).fetchone()
            if row:
                old = self.get(project, run, UUID(row[0]))
                if not old.stale and (
                    old.status == "running"
                    or old.status == "succeeded"
                    and datetime.now(UTC) - old.created_at < timedelta(days=7)
                ):
                    return old
            report = ExpansionReport(
                id=uuid4(),
                project_id=project,
                run_id=run,
                core_selection_id=core,
                competitor_id=competitors,
                direction=data.direction,
                week_start=week_end - timedelta(days=6),
                week_end=week_end,
                created_at=datetime.now(UTC),
                max_rows=data.max_rows,
                protocol_version=PROTOCOL_VERSION,
                queries=[ExpansionQuery(query=query, status="pending") for query in report_queries],
            )
            conn.execute(
                "INSERT INTO aba_expansions (id, run_id, fingerprint, created_at, document, results) VALUES (?,?,?,?,?,?)",
                (
                    str(report.id),
                    str(run),
                    fingerprint,
                    report.created_at.isoformat(),
                    report.model_dump_json(),
                    "[]",
                ),
            )
        submit_with_browser_operation_context(
            self.executor, self._execute, report.model_copy(deep=True)
        )
        return report

    def _save(self, report: ExpansionReport, results: list[dict[str, object]]) -> None:
        with self.repository.connect() as conn:
            conn.execute(
                "UPDATE aba_expansions SET document=?,results=? WHERE id=?",
                (report.model_dump_json(), json.dumps(results, ensure_ascii=False), str(report.id)),
            )

    def _execute(self, report: ExpansionReport) -> None:
        results: list[dict[str, object]] = []
        try:
            for summary in report.queries:
                summary.status = "running"
                self._save(report, results)
                current = self.context(report.project_id, report.run_id, report.direction)
                if current[:2] != (report.core_selection_id, report.competitor_id):
                    raise DomainError(
                        409,
                        "EXPANSION_CONTEXT_CHANGED",
                        "采集期间上游选择变化，请按当前选择重新采集",
                    )
                try:
                    index = len(results)

                    def progress(
                        result: AbaQueryReport, target: ExpansionQuery = summary, slot: int = index
                    ) -> None:
                        result = _ensure_current_traffic_metrics(result)
                        target.total, target.fetched, target.pages = (
                            result.total,
                            result.fetched,
                            result.pages,
                        )
                        target.truncated, target.stop_reason = (
                            result.truncated,
                            result.stop_reason,
                        )
                        document: dict[str, object] = {
                            **result.model_dump(),
                            "raw_rows": result.raw_rows,
                        }
                        if len(results) == slot:
                            results.append(document)
                        else:
                            results[slot] = document
                        self._save(report, results)

                    result = self.client.fetch(
                        summary.query,
                        report.week_end.isoformat(),
                        max_rows=report.max_rows,
                        query_mode="root" if report.direction == "forward" else "asin",
                        on_progress=progress,
                    )
                    progress(result)
                    summary.status = "succeeded"
                    exports = getattr(self, 'source_exports', None)
                    if exports:
                        exports.enqueue(report.project_id, report.run_id, 'aba_' + report.direction, report.id, summary.query,
                            {'query': summary.query, 'week_end': report.week_end.isoformat(),
                             'query_mode': 'root' if report.direction == 'forward' else 'asin',
                             'max_rows': report.max_rows}, report=result)
                except DomainError as error:
                    summary.error_code, summary.error = error.code, error.message
                    summary.status = "failed"
                self._save(report, results)
            good = sum(query.error_code is None for query in report.queries)
            if not good:
                report.status = (
                    "partial" if any(query.fetched for query in report.queries) else "failed"
                )
            elif good < len(report.queries) or any(query.truncated for query in report.queries):
                report.status = "partial"
            else:
                report.status = "succeeded"
            if report.status != "succeeded":
                report.warnings.append(PARTIAL_FINISH_WARNING)
            if self.get(report.project_id, report.run_id, report.id).stale:
                report.stale = True
                report.status = "partial"
                report.warnings.append("采集期间上游选择发生变化，结果仅保留为历史证据。")
        except DomainError as error:
            report.status = "partial" if results else "failed"
            report.stale = True
            report.warnings.append(error.message)
        except Exception:
            logger.exception("ABA expansion worker failed")
            report.status = "partial" if results else "failed"
            report.warnings.append("采集意外中断，请读取已保存进度后显式重试。")
        finally:
            for query in report.queries:
                if query.status in ("pending", "running"):
                    query.status = "interrupted"
            report.finished_at = datetime.now(UTC)
            self._save(report, results)

    def keywords(self, report: ExpansionReport) -> list[ExpansionKeyword]:
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT results FROM aba_expansions WHERE id=? AND run_id=?",
                (str(report.id), str(report.run_id)),
            ).fetchone()
        grouped: dict[str, list[ExpansionEvidence]] = {}
        raw = cast(list[dict[str, object]], json.loads(row[0])) if row else []
        for document in raw:
            query = AbaQueryReport.model_validate(
                {key: value for key, value in document.items() if key != "raw_rows"}
            )
            for item in query.rows:
                key = phrase_key(item.keyword)
                # 指标快照属于采集行持久化数据，不是第四步工作区证据契约字段。
                evidence = ExpansionEvidence(
                    query=query.query, **item.model_dump(exclude={"traffic_metrics"})
                )
                if evidence not in grouped.setdefault(key, []):
                    grouped[key].append(evidence)
        items: list[ExpansionKeyword] = []
        for keyword, evidence in grouped.items():
            ranks = {item.search_frequency_rank for item in evidence}
            items.append(
                ExpansionKeyword(
                    keyword=keyword,
                    search_frequency_rank=next(iter(ranks)) if len(ranks) == 1 else None,
                    rank_conflict=len(ranks) > 1,
                    evidence=evidence,
                )
            )
        return sorted(items, key=lambda item: (item.search_frequency_rank or 2**63, item.keyword))

    def query_workbook(
        self, project: UUID, run: UUID, key: UUID, query_index: int
    ) -> tuple[str, bytes]:
        """导出一次完整持久化 ABA 查询；正向逐词、反向为联合 ASIN。"""
        report = self.get(project, run, key)
        if query_index < 0 or query_index >= len(report.queries):
            raise DomainError(404, "ABA_QUERY_EXPORT_NOT_FOUND", "未找到该 ABA 查询结果")
        summary = report.queries[query_index]
        if summary.error_code:
            raise DomainError(
                409,
                "ABA_QUERY_EXPORT_FAILED",
                f"该 ABA 查询采集失败：{summary.error or summary.error_code}",
            )
        legacy_complete = summary.status == "legacy" and (
            report.status == "succeeded"
            or (
                report.status == "partial"
                and PARTIAL_FINISH_WARNING in report.warnings
            )
        )
        if summary.status != "succeeded" and not legacy_complete:
            raise DomainError(
                409,
                "ABA_QUERY_EXPORT_INCOMPLETE",
                "该 ABA 查询尚未完成采集，请等待完成或重试后再下载",
            )
        with self.repository.connect() as conn:
            stored = conn.execute(
                "SELECT results FROM aba_expansions WHERE id=? AND run_id=?",
                (str(key), str(run)),
            ).fetchone()
        documents = cast(list[dict[str, object]], json.loads(stored[0])) if stored else []
        document = next(
            (item for item in documents if item.get("query") == summary.query), None
        )
        if document is None:
            raise DomainError(409, "ABA_QUERY_EXPORT_EMPTY", "该 ABA 查询尚无可下载数据")
        query = AbaQueryReport.model_validate(
            {field: value for field, value in document.items() if field != "raw_rows"}
        )
        query.raw_rows = cast(list[dict[str, object]], document.get("raw_rows", []))
        if not query.rows:
            raise DomainError(409, "ABA_QUERY_EXPORT_EMPTY", "该 ABA 查询结果为空")
        if query.query != summary.query:
            raise DomainError(409, "ABA_QUERY_EXPORT_MISMATCH", "持久化结果与 ABA 查询索引不一致")
        return (
            query_filename(query.query, direction=report.direction),
            build_query_workbook(
                query,
                report.week_start,
                report.week_end,
                direction=report.direction,
            ),
        )

    def workspace(self, project: UUID, run: UUID, week: date | None) -> ExpansionWorkspace:
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM aba_expansions WHERE run_id=? ORDER BY created_at DESC", (str(run),)
            ).fetchall()
        reports = [self.get(project, run, UUID(row[0])) for row in rows]
        relevant = [
            item for item in reports if not item.stale and (week is None or item.week_end == week)
        ]
        target_week = week or (relevant[0].week_end if relevant else None)
        latest: dict[Direction, ExpansionReport] = {}
        for item in relevant:
            if item.week_end == target_week:
                latest.setdefault(item.direction, item)
        forward, reverse = latest.get("forward"), latest.get("reverse")
        f = {item.keyword for item in self.keywords(forward)} if forward else set[str]()
        r = {item.keyword for item in self.keywords(reverse)} if reverse else set[str]()
        complete = bool(forward and reverse and forward.status == reverse.status == "succeeded")
        return ExpansionWorkspace(
            reports=reports[:50],
            complete=complete,
            comparison={"both": len(f & r), "forward_only": len(f - r), "reverse_only": len(r - f)},
        )

    def page(self, project: UUID, run: UUID, key: UUID, offset: int, limit: int) -> ExpansionPage:
        report = self.get(project, run, key)
        items = self.keywords(report)
        opposite = next(
            (
                item
                for item in self.workspace(project, run, report.week_end).reports
                if item.direction != report.direction
                and item.week_end == report.week_end
                and not item.stale
            ),
            None,
        )
        other = (
            {item.keyword for item in self.keywords(opposite)}
            if opposite and not report.stale
            else set[str]()
        )
        for item in items:
            item.also_in_other_direction = item.keyword in other
        return ExpansionPage(
            total=len(items), offset=offset, limit=limit, items=items[offset : offset + limit]
        )

    def export(self, project: UUID, run: UUID, key: UUID) -> str:
        report = self.get(project, run, key)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(
            [
                "Keyword",
                "Search Frequency Rank",
                "Direction",
                "Query",
                "Week Start",
                "Week End",
                "Top ASIN",
                "Click Share",
                "Conversion Share",
                "Status",
                "Stale",
                "Rank Conflict",
            ]
        )

        def safe(value: str) -> str:
            return (
                "'" + value
                if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n"))
                else value
            )

        for item in self.keywords(report):
            for evidence in item.evidence:
                for product in evidence.products or [None]:
                    writer.writerow(
                        [
                            safe(evidence.keyword),
                            evidence.search_frequency_rank,
                            report.direction,
                            safe(evidence.query),
                            report.week_start,
                            report.week_end,
                            safe(product.asin) if product else "",
                            product.click_share if product else "",
                            product.conversion_share if product else "",
                            report.status,
                            report.stale,
                            item.rank_conflict,
                        ]
                    )
        return "\ufeff" + stream.getvalue()
