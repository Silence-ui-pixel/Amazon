"""流量筛选快照和追加式人工审核，复用清洗来源与失效判断。"""

import csv
import hashlib
import io
import json
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import cast
from uuid import UUID, uuid4

from app.db.repository import Repository
from app.domain.aba import AbaRow
from app.domain.cleaning import CleaningReport, Phrase, Source
from app.domain.errors import DomainError
from app.domain.third_party import ImportRow
from app.domain.traffic import (
    BatchTrafficReview,
    MetricInput,
    Metrics,
    Reference,
    ReviewRecord,
    RuleSet,
    SourceMetricRow,
    TrafficPage,
    TrafficReport,
    TrafficRequest,
    TrafficReview,
    TrafficRow,
    evaluate,
    relevance,
)
from app.services.cleaning import CleaningService


def raw_metrics(raw: object, rank: int) -> MetricInput:
    """按原协议的 1/2/3 位置与百分数取证，避免 float 运算及产品缺位压缩。"""
    item = cast(dict[str, object], raw) if isinstance(raw, dict) else {}

    def share(position: int, field: str) -> Decimal | None:
        asin = item.get(f"st-top-asin-{position}")
        if not isinstance(asin, str) or not asin.strip() or asin.strip() == "-":
            return None
        value = item.get(f"st-top-asin-{field}-{position}")
        try:
            number = Decimal(str(value).strip().removesuffix("%")) / 100
        except InvalidOperation:
            return None
        return number if number.is_finite() and 0 <= number <= 1 else None

    return MetricInput(
        rank=rank,
        click=share(1, "click-share"),
        conversion=share(1, "conversion-share"),
        conversion2=share(2, "conversion-share"),
        conversion3=share(3, "conversion-share"),
    )


def aba_row_metrics(row: AbaRow, raw: object, rules: RuleSet) -> Metrics:
    """优先读取第四步快照；旧行或规则升级时按原始来源行回退计算。"""
    if row.traffic_metrics is not None and row.traffic_metrics.rule_id == rules.id:
        return row.traffic_metrics.metrics.model_copy(deep=True)
    return evaluate(raw_metrics(raw, row.search_frequency_rank), rules)


def import_row_metrics(row: ImportRow, rules: RuleSet) -> Metrics:
    """优先读取第五步快照；旧导入行继续使用相同确定性规则。"""
    if row.traffic_metrics is not None and row.traffic_metrics.rule_id == rules.id:
        return row.traffic_metrics.metrics.model_copy(deep=True)
    return evaluate(
        MetricInput(
            rank=row.weekly_rank,
            click=row.top1_click_share,
            conversion=row.top1_conversion_share,
            conversion2=row.top2_conversion_share,
            conversion3=row.top3_conversion_share,
        ),
        rules,
    )


def phrase_metrics(phrase: Phrase, aba_ids: set[UUID], rules: RuleSet) -> Metrics:
    # aba_ids 来自该清洗快照的所选周来源；卖家精灵同名词的排名只进入参考区。
    records: dict[str, Metrics] = {}
    for evidence in phrase.evidence:
        if evidence.source_id not in aba_ids:
            continue
        payload = json.loads(evidence.payload)
        row = AbaRow.model_validate(payload["parsed"])
        metrics = aba_row_metrics(row, payload.get("raw"), rules)
        # 冲突身份必须保留原始前三位分布与 ASIN；只比较派生 Metrics 会把
        # “总和相同、位置不同”的两条周记录误合并为一条。
        signature = row.model_dump_json(exclude={"keyword", "traffic_metrics"}) + raw_metrics(
            payload.get("raw"), row.search_frequency_rank
        ).model_dump_json()
        records[signature] = metrics
    # 多个查询命中相同原始指标可去重；存在冲突时留缺失，不擅自取最好排名。
    if len(records) != 1:
        return Metrics(missing=["同周 ABA 记录冲突" if records else "无所选周 ABA 原始记录"])
    return next(iter(records.values())).model_copy(deep=True)


def reference_records(
    phrase: Phrase, seller_ids: set[UUID], sources: dict[UUID, Source]
) -> list[Reference]:
    """三方词表原值；同名来源只记一次，不择优、不汇总，也不参与指标计算。"""
    found: dict[tuple[object, ...], Reference] = {}
    for evidence in phrase.evidence:
        if evidence.source_id not in seller_ids:
            continue
        source = sources[evidence.source_id]
        row = ImportRow.model_validate_json(evidence.payload)
        key = (
            source.direction,
            source.query,
            source.period_start,
            source.period_end,
            source.period_kind,
            source.collected_at,
            row.weekly_rank,
            row.monthly_searches,
        )
        found.setdefault(
            key,
            Reference(
                direction=source.direction,
                query=source.query,
                period_start=source.period_start,
                period_end=source.period_end,
                period_kind=source.period_kind,
                collected_at=source.collected_at,
                weekly_rank=row.weekly_rank,
                monthly_searches=row.monthly_searches,
            ),
        )
    return sorted(
        found.values(),
        key=lambda item: (
            -(
                item.period_end or (item.collected_at.date() if item.collected_at else date.min)
            ).toordinal(),
            item.direction,
            item.query,
        ),
    )


def source_metric_rows(
    phrase: Phrase, sources: dict[UUID, Source], rules: RuleSet
) -> list[SourceMetricRow]:
    """每条来源证据独立计算；同名词不去重，指标不跨表拼接。"""
    result: list[SourceMetricRow] = []
    for evidence in phrase.evidence:
        source = sources.get(evidence.source_id)
        if source is None:
            continue
        if source.provider == "aba":
            payload = json.loads(evidence.payload)
            row = AbaRow.model_validate(payload["parsed"])
            metrics = aba_row_metrics(row, payload.get("raw"), rules)
        else:
            row = ImportRow.model_validate_json(evidence.payload)
            metrics = import_row_metrics(row, rules)
        result.append(
            SourceMetricRow(
                source_id=source.id,
                provider=source.provider,
                direction=source.direction,
                query=source.query,
                source_row=evidence.source_row,
                original=evidence.original,
                period_start=source.period_start,
                period_end=source.period_end,
                period_kind=source.period_kind,
                collected_at=source.collected_at,
                metrics=metrics,
            )
        )
    return result


def merge_keyword_rows(rows: list[Phrase]) -> list[Phrase]:
    """第七步按关键词审核；第六步保留的重复汇总行在这里重新聚合证据。"""
    merged: dict[str, Phrase] = {}
    for row in rows:
        target = merged.setdefault(row.keyword, Phrase(keyword=row.keyword, evidence=[]))
        target.evidence.extend(row.evidence)
    return list(merged.values())


class TrafficService:
    def __init__(self, repository: Repository, cleaning: CleaningService) -> None:
        self.repository, self.cleaning = repository, cleaning

    def _input(self, project: UUID, run: UUID, request: TrafficRequest) -> CleaningReport:
        report = self.cleaning.get(project, run, request.cleaning_id)
        if report.stale or report.review is None or report.version != request.cleaning_version:
            raise DomainError(409, "TRAFFIC_INPUT_STALE", "请先完成第六步审核，并使用当前清洗版本")
        return report

    def create(self, project: UUID, run: UUID, request: TrafficRequest) -> TrafficReport:
        cleaning = self._input(project, run, request)
        rules = RuleSet()
        fingerprint = hashlib.sha256(
            (request.model_dump_json() + rules.model_dump_json()).encode()
        ).hexdigest()
        phrases = merge_keyword_rows(
            self.cleaning.page(
                project, run, cleaning.id, "phrases", 0, cleaning.phrase_count
            ).phrases
        )
        exclusions = (
            {e.keyword: e.reason for e in cleaning.review.exclusions} if cleaning.review else {}
        )
        aba_ids = {s.id for s in cleaning.sources if s.provider == "aba"}
        seller_ids = {s.id for s in cleaning.sources if s.provider == "sellersprite"}
        sources = {s.id: s for s in cleaning.sources}
        rows = [
            TrafficRow(
                phrase=p,
                metrics=phrase_metrics(p, aba_ids, rules),
                cleaning_exclusion=exclusions.get(p.keyword, ""),
                references=reference_records(p, seller_ids, sources),
                source_metrics=source_metric_rows(p, sources, rules),
            )
            for p in phrases
        ]
        report = TrafficReport(
            id=uuid4(),
            project_id=project,
            run_id=run,
            created_at=datetime.now(UTC),
            request=request,
            rules=rules,
            total=len(rows),
            required=sum(not r.cleaning_exclusion for r in rows),
        )
        with self.repository.connect() as conn:
            self._input(project, run, request)
            conn.execute(
                "INSERT INTO rule_sets (id, document) VALUES(?,?) ON CONFLICT (id) DO NOTHING", (rules.id, rules.model_dump_json())
            )
            stored_rule_row = conn.execute(
                "SELECT document FROM rule_sets WHERE id=?", (rules.id,)
            ).fetchone()
            assert stored_rule_row is not None
            stored_rule = cast(str, stored_rule_row[0])
            if stored_rule != rules.model_dump_json():
                raise DomainError(
                    409, "TRAFFIC_RULE_CONFLICT", "规则版本已存在且内容不一致，请升级规则版本"
                )
            conn.execute(
                "INSERT INTO traffic_analyses (id, run_id, fingerprint, document, rows) VALUES(?,?,?,?,?) ON CONFLICT (run_id, fingerprint) DO NOTHING",
                (
                    str(report.id),
                    str(run),
                    fingerprint,
                    report.model_dump_json(),
                    json.dumps([r.model_dump(mode="json") for r in rows], ensure_ascii=False),
                ),
            )
            key_row = conn.execute(
                "SELECT id FROM traffic_analyses WHERE run_id=? AND fingerprint=?",
                (str(run), fingerprint),
            ).fetchone()
        assert key_row is not None
        key = cast(str, key_row[0])
        return self.get(project, run, UUID(key))

    def _stored(
        self, project: UUID, run: UUID, key: UUID
    ) -> tuple[TrafficReport, list[TrafficRow], list[ReviewRecord]]:
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            stored = conn.execute(
                "SELECT document,rows FROM traffic_analyses WHERE id=? AND run_id=?",
                (str(key), str(run)),
            ).fetchone()
            audits = conn.execute(
                "SELECT document FROM traffic_reviews WHERE analysis_id=? ORDER BY version",
                (str(key),),
            ).fetchall()
        if not stored:
            raise DomainError(404, "TRAFFIC_NOT_FOUND", "流量筛选不存在")
        return (
            TrafficReport.model_validate_json(stored[0]),
            [TrafficRow.model_validate(r) for r in json.loads(stored[1])],
            [ReviewRecord.model_validate_json(r[0]) for r in audits],
        )

    def get(self, project: UUID, run: UUID, key: UUID) -> TrafficReport:
        report, rows, reviews = self._stored(project, run, key)
        latest = {r.data.keyword: r for r in reviews}
        report.version = reviews[-1].version if reviews else 0
        report.reviewed = sum(
            not r.cleaning_exclusion
            and r.phrase.keyword in latest
            and latest[r.phrase.keyword].data.decision != "defer"
            for r in rows
        )
        try:
            self._input(project, run, report.request)
        except DomainError as error:
            if error.status_code != 409:
                raise
            report.stale = True
        report.complete = not report.stale and report.reviewed == report.required
        return report

    def history(self, project: UUID, run: UUID) -> list[TrafficReport]:
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            keys = conn.execute(
                "SELECT id FROM traffic_analyses WHERE run_id=? ORDER BY seq DESC LIMIT 50",
                (str(run),),
            ).fetchall()
        return [self.get(project, run, UUID(k[0])) for k in keys]

    def page(self, project: UUID, run: UUID, key: UUID, offset: int, limit: int) -> TrafficPage:
        _, rows, reviews = self._stored(project, run, key)
        latest = {r.data.keyword: r for r in reviews}
        for row in rows[offset : offset + limit]:
            row.review = latest.get(row.phrase.keyword)
        return TrafficPage(
            total=len(rows), offset=offset, limit=limit, items=rows[offset : offset + limit]
        )

    def reviews(self, project: UUID, run: UUID, key: UUID) -> list[ReviewRecord]:
        return self._stored(project, run, key)[2]

    def review(self, project: UUID, run: UUID, key: UUID, data: TrafficReview) -> TrafficReport:
        report, rows, _ = self._stored(project, run, key)
        row = next((r for r in rows if r.phrase.keyword == data.keyword), None)
        if (
            row is None
            or row.cleaning_exclusion
            or any(not v.strip() for v in (data.reason, data.evidence_note, data.reviewer))
        ):
            raise DomainError(
                422, "TRAFFIC_REVIEW_INVALID", "请选择未排除的原词，填写审核人、证据及理由"
            )
        record = ReviewRecord(
            version=0,
            created_at=datetime.now(UTC),
            data=data,
            relevance=relevance(data, report.rules),
        )
        with self.repository.connect() as conn:
            conn.execute("SELECT id FROM traffic_analyses WHERE id=? AND run_id=? FOR UPDATE", (str(key), str(run)))
            self._input(project, run, report.request)
            version_row = conn.execute(
                "SELECT COALESCE(MAX(version),0) FROM traffic_reviews WHERE analysis_id=?",
                (str(key),),
            ).fetchone()
            assert version_row is not None
            version = cast(int, version_row[0])
            if version != data.expected_version:
                raise DomainError(409, "TRAFFIC_VERSION_CONFLICT", "审核已更新，请刷新后再提交")
            record.version = version + 1
            conn.execute(
                "INSERT INTO traffic_reviews VALUES(?,?,?)",
                (str(key), record.version, record.model_dump_json()),
            )
        return self.get(project, run, key)

    def review_automatic_batch(
        self, project: UUID, run: UUID, key: UUID, decisions: list[TrafficReview]
    ) -> TrafficReport:
        """原子保存模型批次；并发或重放时保留已完成审核，不使用调用前的旧版本。"""
        with self.repository.connect() as conn:
            conn.execute("SELECT id FROM traffic_analyses WHERE id=? AND run_id=? FOR UPDATE", (str(key), str(run)))
            report, rows, audits = self._stored(project, run, key)
            self._input(project, run, report.request)
            latest = {audit.data.keyword: audit for audit in audits}
            available = {row.phrase.keyword for row in rows if not row.cleaning_exclusion}
            version = audits[-1].version if audits else 0
            for data in decisions:
                if data.keyword not in available or data.reviewer != "自动编排":
                    raise DomainError(422, "TRAFFIC_REVIEW_INVALID", "自动审核须使用当前未排除原词")
                previous = latest.get(data.keyword)
                if previous is not None and previous.data.decision != "defer":
                    continue
                current = data.model_copy(update={"expected_version": version})
                record = ReviewRecord(
                    version=version + 1,
                    created_at=datetime.now(UTC),
                    data=current,
                    relevance=relevance(current, report.rules),
                )
                conn.execute(
                    "INSERT INTO traffic_reviews VALUES(?,?,?)",
                    (str(key), record.version, record.model_dump_json()),
                )
                version = record.version
                latest[data.keyword] = record
        return self.get(project, run, key)

    def export(self, project: UUID, run: UUID, key: UUID) -> str:
        report = self.get(project, run, key)
        cleaning = self.cleaning.get(project, run, report.request.cleaning_id)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            [
                "keyword",
                "metrics",
                "cleaning_exclusion",
                "review",
                "sources",
                "evidence",
                "rules",
                "cleaning_id",
                "cleaning_version",
                "review_version",
                "stale",
            ]
        )
        for row in self.page(project, run, key, 0, report.total).items:
            values = [
                row.phrase.keyword,
                row.metrics.model_dump_json(),
                row.cleaning_exclusion,
                row.review.model_dump_json() if row.review else "",
                json.dumps(
                    [s.model_dump(mode="json") for s in cleaning.sources], ensure_ascii=False
                ),
                row.phrase.model_dump_json(),
                report.rules.model_dump_json(),
                str(cleaning.id),
                str(report.request.cleaning_version),
                str(report.version),
                str(report.stale),
            ]
            writer.writerow(
                ["'" + v if v.lstrip().startswith(("=", "+", "-", "@")) else v for v in values]
            )
        return output.getvalue()

    def batch_review(
        self, project: UUID, run: UUID, key: UUID, data: BatchTrafficReview
    ) -> TrafficReport:
        with self.repository.connect() as conn:
            conn.execute("SELECT id FROM traffic_analyses WHERE id=? AND run_id=? FOR UPDATE", (str(key), str(run)))
            report, rows, reviews = self._stored(project, run, key)
            self._input(project, run, report.request)
            version = max((r.version for r in reviews), default=0)
            if version != data.expected_version:
                raise DomainError(409, "TRAFFIC_VERSION_CONFLICT", "审核已更新，请刷新后再提交")
            allowed = {r.phrase.keyword: r for r in rows if not r.cleaning_exclusion}
            if not set(data.keywords) <= allowed.keys():
                raise DomainError(
                    422,
                    "TRAFFIC_REVIEW_INVALID",
                    "批量审核包含不存在或已清洗排除的关键词，整批未保存",
                )
            latest = {r.data.keyword: r for r in reviews}
            for index, keyword in enumerate(data.keywords, 1):
                old = latest.get(keyword)
                evidence = old.data.evidence_note if old else ""
                note = f"{evidence}\n批量核对：{data.evidence_note}".strip()
                if len(note) > 2000:
                    raise DomainError(
                        422, "TRAFFIC_REVIEW_INVALID", "已有证据过长，请改为逐词审核；整批未保存"
                    )
                review = TrafficReview(
                    expected_version=version + index - 1,
                    keyword=keyword,
                    decision=data.decision,
                    priority=data.priority,
                    reason=data.reason,
                    reviewer=data.reviewer,
                    evidence_note=note,
                    style_similar=old.data.style_similar if old else None,
                    price_delta_ratio=old.data.price_delta_ratio if old else None,
                    first_half_result_share=old.data.first_half_result_share if old else None,
                )
                record = ReviewRecord(
                    version=version + index,
                    created_at=datetime.now(UTC),
                    data=review,
                    relevance=relevance(review, report.rules),
                )
                conn.execute(
                    "INSERT INTO traffic_reviews VALUES(?,?,?)",
                    (str(key), record.version, record.model_dump_json()),
                )
        return self.get(project, run, key)
