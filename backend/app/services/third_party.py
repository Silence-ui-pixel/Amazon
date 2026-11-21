"""卖家精灵导出导入：预览、不可变确认、来源行与当前覆盖状态。"""

import csv
import hashlib
import io
import json
from datetime import UTC, date, datetime
from pathlib import PureWindowsPath
from typing import cast
from uuid import UUID, uuid4

from app.adapters.third_party import Table, map_rows, read_table, suggest
from app.db.repository import Repository
from app.domain.aba_expansion import Direction
from app.domain.errors import DomainError
from app.domain.text import phrase_key
from app.domain.third_party import (
    ImportConfirmation,
    ImportMetadata,
    ImportPage,
    ImportReport,
    ImportRow,
    ImportWorkspace,
)
from app.services.aba_expansion import ExpansionService


class ThirdPartyService:
    def __init__(self, repository: Repository, expansion: ExpansionService) -> None:
        self.repository, self.expansion = repository, expansion

    def get(self, project: UUID, run: UUID, key: UUID) -> ImportReport:
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM third_party_imports WHERE id=? AND run_id=?",
                (str(key), str(run)),
            ).fetchone()
        if not row:
            raise DomainError(404, "IMPORT_NOT_FOUND", "此任务下没有该导入记录")
        report = ImportReport.model_validate_json(row[0])
        try:
            core, competitors, _ = self.expansion.context(project, run, report.metadata.direction)
            report.stale = (core, competitors) != (report.core_selection_id, report.competitor_id)
        except DomainError as error:
            if error.status_code != 409:
                raise
            report.stale = True
        return report

    def preview(
        self,
        project: UUID,
        run: UUID,
        filename: str,
        data: bytes,
        metadata: ImportMetadata,
        *,
        expected_context: tuple[UUID, UUID | None] | None = None,
    ) -> ImportReport:
        current_run = self.repository.get_run(project, run)
        if metadata.marketplace != current_run.input.marketplace:
            raise DomainError(422, "IMPORT_MARKETPLACE_MISMATCH", "词表站点须与当前任务一致")
        core, competitors, queries = self.expansion.context(project, run, metadata.direction)
        if expected_context is not None and (core, competitors) != expected_context:
            raise DomainError(409, "IMPORT_CONTEXT_CHANGED", "采集期间上游已变化，请重新采集")
        if metadata.collection_method == "traffic_extension":
            available = {phrase_key(value): value for value in queries}
            selected = [available.get(phrase_key(value)) for value in metadata.source_queries]
            if any(value is None for value in selected):
                raise DomainError(
                    422,
                    "IMPORT_QUERY_INVALID",
                    "拓展流量词来源只能包含当前已保留的竞对 ASIN",
                )
            normalized = [value for value in selected if value is not None]
            metadata = metadata.model_copy(
                update={"query": ",".join(normalized), "source_queries": normalized}
            )
        else:
            query = next((q for q in queries if phrase_key(q) == phrase_key(metadata.query)), None)
            if query is None:
                raise DomainError(
                    422, "IMPORT_QUERY_INVALID", "请选择已保存的核心词或已保留竞对 ASIN"
                )
            metadata = metadata.model_copy(update={"query": query, "source_queries": [query]})
        table = read_table(filename, data)
        if metadata.source_total is not None and metadata.source_total < len(table.rows):
            raise DomainError(422, "IMPORT_TOTAL_INVALID", "来源总行数不能小于导入行数")
        digest = hashlib.sha256(data).hexdigest()
        fingerprint = hashlib.sha256(
            json.dumps(
                [str(core), str(competitors), digest, metadata.model_dump(mode="json")],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        report = ImportReport(
            id=uuid4(),
            project_id=project,
            run_id=run,
            core_selection_id=core,
            competitor_id=competitors,
            metadata=metadata,
            filename=PureWindowsPath(filename).name[:200],
            file_sha256=digest,
            created_at=datetime.now(UTC),
            imported_rows=len(table.rows),
            truncated=metadata.truncated,
            headers=table.headers,
            suggested_mapping=suggest(table.headers),
            samples=[dict(zip(table.headers, r, strict=True)) for r in table.rows[:5]],
        )
        with self.repository.connect() as conn:
            if self.expansion.context(project, run, metadata.direction)[:2] != (core, competitors):
                raise DomainError(409, "IMPORT_CONTEXT_CHANGED", "上游选择已变化，请重新上传")
            old = conn.execute(
                "SELECT id FROM third_party_imports WHERE run_id=? AND fingerprint=?",
                (str(run), fingerprint),
            ).fetchone()
            if old:
                return self.get(project, run, UUID(old[0]))
            conn.execute(
                "INSERT INTO third_party_imports (id, run_id, fingerprint, document, raw_document, results) VALUES (?,?,?,?,?,?)",
                (
                    str(report.id),
                    str(run),
                    fingerprint,
                    report.model_dump_json(),
                    json.dumps(
                        {"headers": table.headers, "rows": table.rows, "numbers": table.numbers},
                        ensure_ascii=False,
                    ),
                    "[]",
                ),
            )
            conn.execute(
                "INSERT INTO third_party_import_files VALUES (?,?)", (str(report.id), data)
            )
        return report

    def confirm(
        self, project: UUID, run: UUID, key: UUID, data: ImportConfirmation
    ) -> ImportReport:
        with self.repository.connect() as conn:
            report = self.get(project, run, key)
            if report.stale:
                raise DomainError(409, "IMPORT_STALE", "上游已变化，请按当前选择重新导入")
            if report.confirmation:
                if data != report.confirmation:
                    raise DomainError(
                        409, "IMPORT_IMMUTABLE", "已确认批次不可改映射，请重新导入新批次"
                    )
                return report
            stored = conn.execute(
                "SELECT raw_document FROM third_party_imports WHERE id=?", (str(key),)
            ).fetchone()
            assert stored is not None
            raw = json.loads(cast(str, stored[0]))
            table = Table(raw["headers"], raw["rows"], raw["numbers"])
            if "bid" in data.mapping and not report.metadata.currency:
                raise DomainError(
                    422,
                    "IMPORT_CURRENCY_REQUIRED",
                    "导入 PPC 竞价须填写原表币种，请重新上传并选择币种",
                )
            rows = map_rows(table, data.mapping, data.share_unit, currency=report.metadata.currency)
            report.status = "confirmed"
            report.confirmation = data
            report.confirmed_at = datetime.now(UTC)
            report.unique_rows = len({r.normalized_keyword for r in rows})
            report.complete = (
                report.metadata.export_complete
                and report.metadata.source_total == report.imported_rows
            )
            # 供应商单次上限导致的截断是已知口径，明确记录而不是冒充完整。
            report.truncated = report.metadata.truncated
            conn.execute(
                "UPDATE third_party_imports SET document=?,results=? WHERE id=?",
                (
                    report.model_dump_json(),
                    json.dumps([r.model_dump(mode="json") for r in rows], ensure_ascii=False),
                    str(key),
                ),
            )
        return report

    def workspace(
        self, project: UUID, run: UUID, period_kind: str | None = None
    ) -> ImportWorkspace:
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            keys = conn.execute(
                "SELECT id FROM third_party_imports WHERE run_id=? ORDER BY seq DESC", (str(run),)
            ).fetchall()
        reports = [self.get(project, run, UUID(row[0])) for row in keys]
        forward: list[str] = []
        reverse: list[str] = []
        for direction in ("forward", "reverse"):
            try:
                queries = self.expansion.context(project, run, direction)[2]
                if direction == "forward":
                    forward = queries
                else:
                    reverse = queries
            except DomainError as error:
                if error.status_code != 409:
                    raise
        # 以最近确认批次的周期评估覆盖；预览不会替换已确认批次。
        current = sorted(
            [
                r
                for r in reports
                if not r.stale
                and r.status == "confirmed"
                and (period_kind is None or r.metadata.period_kind == period_kind)
            ],
            key=lambda r: r.confirmed_at or r.created_at,
            reverse=True,
        )
        anchor = current[0].metadata if current else None
        latest: dict[tuple[str, str], ImportReport] = {}
        for report in current:
            meta = report.metadata
            if (
                anchor
                and meta.period_kind == anchor.period_kind
                and (meta.period_start, meta.period_end)
                == (
                    anchor.period_start,
                    anchor.period_end,
                )
            ):
                for query in meta.source_queries:
                    latest.setdefault((meta.direction, phrase_key(query)), report)
        covered_forward = sum(
            bool((r := latest.get(("forward", phrase_key(q)))) and (r.complete or r.truncated))
            for q in forward
        )
        covered_reverse = sum(
            bool((r := latest.get(("reverse", phrase_key(q)))) and (r.complete or r.truncated))
            for q in reverse
        )
        return ImportWorkspace(
            reports=reports[:100],
            forward_queries=forward,
            reverse_queries=reverse,
            covered_forward=covered_forward,
            covered_reverse=covered_reverse,
            complete=bool(forward and reverse)
            and covered_forward == len(forward)
            and covered_reverse == len(reverse),
        )

    def missing_queries(
        self,
        project: UUID,
        run: UUID,
        start: date | None,
        end: date | None,
        period_kind: str = "date_range",
    ) -> list[tuple[Direction, str]]:
        """所选周期内尚未确认或未完整覆盖的查询；供编排自动补齐。"""
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            keys = conn.execute(
                "SELECT id FROM third_party_imports WHERE run_id=? ORDER BY seq DESC",
                (str(run),),
            ).fetchall()
        latest: dict[tuple[Direction, str], ImportReport] = {}
        for raw in keys:
            report = self.get(project, run, UUID(cast(str, raw[0])))
            meta = report.metadata
            if report.stale or report.status != "confirmed":
                continue
            if meta.period_kind != period_kind or (meta.period_start, meta.period_end) != (
                start,
                end,
            ):
                continue
            for query in meta.source_queries:
                latest.setdefault((meta.direction, phrase_key(query)), report)
        missing: list[tuple[Direction, str]] = []
        for direction in ("forward", "reverse"):
            try:
                queries = self.expansion.context(project, run, direction)[2]
            except DomainError as error:
                if error.status_code != 409:
                    raise
                continue
            for query in queries:
                report = latest.get((direction, phrase_key(query)))
                if report is None or not (report.complete or report.truncated):
                    missing.append((direction, query))
        return missing

    def page(self, project: UUID, run: UUID, key: UUID, offset: int, limit: int) -> ImportPage:
        report = self.get(project, run, key)
        if report.status != "confirmed":
            raise DomainError(409, "IMPORT_CONFIRM_REQUIRED", "请先确认字段映射")
        with self.repository.connect() as conn:
            stored = conn.execute(
                "SELECT results FROM third_party_imports WHERE id=?", (str(key),)
            ).fetchone()
        assert stored is not None
        rows = json.loads(cast(str, stored[0]))
        return ImportPage(
            total=len(rows),
            offset=offset,
            limit=limit,
            items=[ImportRow.model_validate(r) for r in rows[offset : offset + limit]],
        )

    def export(self, project: UUID, run: UUID, key: UUID) -> bytes:
        report = self.get(project, run, key)
        rows = self.page(project, run, key, 0, report.imported_rows).items
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(
            [
                "provider",
                "direction",
                "query",
                "marketplace",
                "currency",
                "period_start",
                "period_end",
                "period_kind",
                "collected_at",
                "source_row",
                "file_sha256",
                *(
                    "'" + h if h.lstrip().startswith(("=", "+", "-", "@")) else h
                    for h in report.headers
                ),
            ]
        )
        for row in rows:
            values = [
                report.provider,
                report.metadata.direction,
                report.metadata.query,
                report.metadata.marketplace,
                report.metadata.currency or "",
                str(report.metadata.period_start or ""),
                str(report.metadata.period_end or ""),
                report.metadata.period_kind,
                report.metadata.collected_at.isoformat() if report.metadata.collected_at else "",
                str(row.row_number),
                report.file_sha256,
                *(row.raw[h] for h in report.headers),
            ]
            writer.writerow(
                ["'" + v if v.lstrip().startswith(("=", "+", "-", "@")) else v for v in values]
            )
        return stream.getvalue().encode("utf-8-sig")

    def original(self, project: UUID, run: UUID, key: UUID) -> bytes:
        self.get(project, run, key)
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT content FROM third_party_import_files WHERE report_id=?", (str(key),)
            ).fetchone()
        if not row:
            raise DomainError(404, "IMPORT_FILE_NOT_FOUND", "原始文件不存在")
        return bytes(row[0])
