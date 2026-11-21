"""ABA 采集用例：从第一步选择读取词根，按任务快照保存周报与私有原始证据。"""

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from app.adapters.aba import AbaProvider
from app.db.repository import Repository
from app.domain.aba import AbaQueryReport, AbaReport, AbaRequest
from app.domain.errors import DomainError
from app.domain.roots import RootCandidates, RootSelection


def fetch_product_term_reports(
    provider: AbaProvider,
    queries: Sequence[str],
    requested_week: date,
    max_rows: int,
) -> tuple[date, list[AbaQueryReport], str]:
    """最新周尚未发布时只回退一周；权限、登录和限流错误原样抛出。"""

    week = requested_week
    reports: list[AbaQueryReport] = []
    try:
        for query in queries:
            reports.append(
                provider.fetch(
                    query,
                    week.isoformat(),
                    max_rows=max_rows,
                    query_mode="product_term",
                )
            )
        return week, reports, ""
    except DomainError as error:
        if error.code != "ABA_UPSTREAM_ERROR" or reports:
            raise
    fallback = week - timedelta(days=7)
    reports = [
        provider.fetch(
            query,
            fallback.isoformat(),
            max_rows=max_rows,
            query_mode="product_term",
        )
        for query in queries
    ]
    return (
        fallback,
        reports,
        f"ABA 最新周 {week.isoformat()} 尚未发布，已使用最近可用周 {fallback.isoformat()}。",
    )


class AbaService:
    def __init__(self, repository: Repository, client: AbaProvider) -> None:
        self.repository = repository
        self.client = client

    def _context(
        self, project_id: UUID, run_id: UUID, *, product_term: bool = False
    ) -> tuple[list[str], str]:
        self.repository.get_run(project_id, run_id)
        with self.repository.connect() as conn:
            saved = conn.execute(
                "SELECT document FROM root_selections WHERE run_id=?", (str(run_id),)
            ).fetchone()
            candidates = conn.execute(
                "SELECT document FROM root_candidates WHERE run_id=? AND status='succeeded'",
                (str(run_id),),
            ).fetchone()
            product = conn.execute(
                "SELECT document FROM product_understandings WHERE run_id=?", (str(run_id),)
            ).fetchone()
        if not saved or not candidates:
            raise DomainError(409, "ROOT_SELECTION_REQUIRED", "请先完成第一步并保存词根")
        selection = RootSelection.model_validate_json(saved[0])
        options = RootCandidates.model_validate_json(candidates[0])
        if product_term and options.rule_version != "product-term-v1":
            raise DomainError(409, "PRODUCT_TERM_CANDIDATES_REQUIRED", "请先确认新规则的商品词")
        mapping = {item.id: item.text for item in options.candidates}
        if not selection.candidate_ids or not all(
            key in mapping for key in selection.candidate_ids
        ):
            raise DomainError(409, "ROOT_SELECTION_STALE", "词根选择已过期，请重新确认")
        roots = [mapping[key] for key in selection.candidate_ids]
        context = json.dumps(
            {
                "roots": roots,
                "candidate_ids": sorted(str(key) for key in selection.candidate_ids),
                "product": product[0] if product else None,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return roots, hashlib.sha256(context.encode()).hexdigest()

    def product_term_context(self, project_id: UUID, run_id: UUID) -> tuple[list[str], str]:
        """Return the confirmed product terms and their immutable collection context."""

        return self._context(project_id, run_id, product_term=True)

    def collect(self, project_id: UUID, run_id: UUID, data: AbaRequest) -> AbaReport:
        run = self.repository.get_run(project_id, run_id)
        if run.input.marketplace != "US":
            raise DomainError(422, "ABA_SITE_UNSUPPORTED", "本次迁入只验证美国站 ABA")
        week_end = data.week_end
        product_term = data.query_mode == "product_term"
        if product_term:
            today = datetime.now(UTC).date()
            # 周六当天尚未结束；从昨天回溯到最近一个周六。
            yesterday = today - timedelta(days=1)
            latest_week = yesterday - timedelta(days=(yesterday.weekday() - 5) % 7)
            if week_end is not None and week_end != latest_week:
                raise DomainError(422, "ABA_WEEK_INVALID", "商品词模式只能查询最近已结束周六的周报")
            week_end = latest_week
        if week_end is None:
            raise DomainError(422, "ABA_WEEK_INVALID", "必须提供报告周结束日")
        roots, context = self._context(project_id, run_id, product_term=product_term)
        queries = roots
        if data.query_mode == "asin":
            if not run.input.asin:
                raise DomainError(409, "ABA_ASIN_REQUIRED", "此任务未提供 ASIN，无法反向查询")
            queries = [run.input.asin]
        resolved_request = data.model_copy(update={"week_end": week_end})
        key = hashlib.sha256(
            (context + resolved_request.model_dump_json() + str(run_id)).encode()
        ).hexdigest()
        with self.repository.connect() as conn:
            cached = conn.execute(
                "SELECT document FROM aba_reports WHERE run_id=? AND fingerprint=? ORDER BY created_at DESC LIMIT 1",
                (str(run_id), key),
            ).fetchone()
        if cached:
            result = AbaReport.model_validate_json(cached[0])
            if datetime.now(UTC) - result.fetched_at < timedelta(days=7) and all(
                report.stop_reason in ("complete", "row_limit") for report in result.reports
            ):
                return result
        # 网络期间不持有数据库事务；任何一路失败都不写入成功周报。
        publication_warning = ""
        if product_term:
            week_end, reports, publication_warning = fetch_product_term_reports(
                self.client, queries, week_end, data.max_rows
            )
            # 自动回退后，保存/复用键必须反映实际报告周，不能把上一周数据标成最新周。
            resolved_request = data.model_copy(update={"week_end": week_end})
            key = hashlib.sha256(
                (context + resolved_request.model_dump_json() + str(run_id)).encode()
            ).hexdigest()
        else:
            reports = [
                self.client.fetch(
                    query, week_end.isoformat(), max_rows=data.max_rows, query_mode=data.query_mode
                )
                for query in queries
            ]
        # 防止采集过程中人工切换词根，旧请求不能代表新输入的结果。
        if self._context(project_id, run_id, product_term=product_term)[1] != context:
            raise DomainError(409, "ABA_INPUT_CHANGED", "采集期间词根或商品信息变化，请重新采集")
        result = AbaReport(
            id=uuid4(),
            project_id=project_id,
            run_id=run_id,
            week_start=week_end - timedelta(days=6),
            week_end=week_end,
            fetched_at=datetime.now(UTC),
            roots_snapshot=roots,
            query_terms_snapshot=queries if product_term else [],
            rule_version="hot-search-core-v1" if product_term else "legacy-root-v1",
            query_mode=data.query_mode,
            max_rows=data.max_rows,
            reports=reports,
            warnings=(
                ([publication_warning] if publication_warning else [])
                + (
                    ["部分查询达到采集上限或返回不完整，请查看各路 truncated 和 stop_reason。"]
                    if any(report.truncated for report in reports)
                    else []
                )
            ),
        )
        raw = [{"query": report.query, "rows": report.raw_rows} for report in reports]
        with self.repository.connect() as conn:
            conn.execute(
                "INSERT INTO aba_reports (id,run_id,fingerprint,context_hash,created_at,document,raw_document) VALUES (?,?,?,?,?,?,?)",
                (
                    str(result.id),
                    str(run_id),
                    key,
                    context,
                    result.fetched_at.isoformat(),
                    result.model_dump_json(),
                    json.dumps(raw, ensure_ascii=False),
                ),
            )
        exports = getattr(self, 'source_exports', None)
        if exports:
            for query in result.reports:
                exports.enqueue(project_id, run_id, 'step2', result.id, query.query,
                    {'query': query.query, 'week_end': result.week_end.isoformat(),
                     'query_mode': result.query_mode, 'max_rows': result.max_rows}, report=query)
        return result

    def get(self, project_id: UUID, run_id: UUID, report_id: UUID) -> AbaReport:
        self.repository.get_run(project_id, run_id)
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT context_hash,document FROM aba_reports WHERE id=? AND run_id=?",
                (str(report_id), str(run_id)),
            ).fetchone()
        if not row:
            raise DomainError(404, "ABA_REPORT_NOT_FOUND", "此任务下未找到 ABA 周报")
        result = AbaReport.model_validate_json(row[1])
        try:
            result.stale = (
                self._context(project_id, run_id, product_term=result.query_mode == "product_term")[
                    1
                ]
                != row[0]
            )
        except DomainError as error:
            if error.code not in (
                "ROOT_SELECTION_REQUIRED",
                "ROOT_SELECTION_STALE",
                "PRODUCT_TERM_CANDIDATES_REQUIRED",
            ):
                raise
            result.stale = True
        return result

    def history(self, project_id: UUID, run_id: UUID, offset: int, limit: int) -> list[AbaReport]:
        self.repository.get_run(project_id, run_id)
        with self.repository.connect() as conn:
            ids = conn.execute(
                "SELECT id FROM aba_reports WHERE run_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (str(run_id), limit, offset),
            ).fetchall()
        return [self.get(project_id, run_id, UUID(row[0])) for row in ids]
