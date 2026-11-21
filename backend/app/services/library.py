"""最终库草稿、不可变发布快照与项目版本分配。"""

import csv
import hashlib
import io
import json
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any, cast, get_args
from uuid import UUID, uuid4

from pydantic import ValidationError

from app.adapters.library_template import freeze_attachment_exports
from app.db.repository import Repository
from app.db.scope import ConnectionScope
from app.domain.aba import AbaReport
from app.domain.cleaning import Category, CleaningReport
from app.domain.errors import DomainError
from app.domain.library import (
    AsinSuggestion,
    EditRecord,
    EntryEdit,
    LibraryEntry,
    LibraryReport,
    LibraryRequest,
    LibraryRules,
    PublishRequest,
    Tier,
    entry_issues,
    tier_of,
)
from app.domain.text import contains_root
from app.services.traffic import TrafficService


class LibraryService:
    def __init__(self, repository: Repository, traffic: TrafficService) -> None:
        self.repository = repository
        self.traffic = traffic

    def _fresh(self, project: UUID, run: UUID, data: LibraryRequest) -> None:
        report = self.traffic.get(project, run, data.traffic_id)
        if not report.complete or report.stale or report.version != data.traffic_version:
            raise DomainError(409, "LIBRARY_SOURCE_STALE", "流量审核未完成或已变更，请重新生成草稿")

    def _stored(self, project: UUID, run: UUID, key: UUID) -> LibraryReport:
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM keyword_library_drafts WHERE id=? AND run_id=?",
                (str(key), str(run)),
            ).fetchone()
        if row is None:
            raise DomainError(404, "NOT_FOUND", "关键词库版本不存在")
        report = LibraryReport.model_validate_json(cast(str, row[0]))
        for entry in report.entries:
            entry.issues = entry_issues(entry)
        return report

    def get(self, project: UUID, run: UUID, key: UUID) -> LibraryReport:
        report = self._stored(project, run, key)
        try:
            self._fresh(project, run, report.request)
        except DomainError as exc:
            if exc.status_code not in (404, 409):
                raise
            report.stale = True
        except ValidationError:
            # Historical drafts own immutable snapshots. If a current upstream record
            # uses an incompatible/legacy schema, keep the draft readable and mark it
            # stale instead of letting freshness validation hide the saved result.
            report.stale = True
        return report

    def history(self, project: UUID, run: UUID) -> list[LibraryReport]:
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            keys = conn.execute(
                "SELECT id FROM keyword_library_drafts WHERE run_id=? ORDER BY seq DESC",
                (str(run),),
            ).fetchall()
        return [self.get(project, run, UUID(k[0])) for k in keys]

    def template_roots(self, report: LibraryReport) -> list[str]:
        """旧词库按其固定的核心词周报取词根，不读取当前词根选择、不改写历史。"""
        if "root_source" in report.snapshots:
            source = cast(dict[str, object], json.loads(report.snapshots["root_source"]))
            return cast(list[str], source.get("roots_snapshot", []))
        core = cast(dict[str, object], json.loads(report.snapshots.get("core_selections", "{}")))
        report_id = core.get("report_id")
        if not report_id:
            return []
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM aba_reports WHERE id=? AND run_id=?",
                (str(report_id), str(report.run_id)),
            ).fetchone()
        return AbaReport.model_validate_json(row[0]).roots_snapshot if row else []

    def _attachment_snapshot(
        self, conn: ConnectionScope, run: UUID, cleaning: CleaningReport, snapshots: dict[str, str]
    ) -> str:
        """Freeze only the upstream IDs used by cleaning, including private raw evidence."""
        attachments: dict[str, object] = {
            "schema_version": 1,
            "cleaning_id": str(cleaning.id),
        }
        expansions: list[dict[str, object]] = []
        sellers: list[dict[str, object]] = []
        core_ids: list[str] = []
        competitor_ids: list[str] = []

        def state(status: str, code: str = "", reason: str = "") -> dict[str, str]:
            return {"status": status, "reason_code": code, "reason": reason}

        for source in cleaning.sources:
            table, payload = (
                ("aba_expansions", "results")
                if source.provider == "aba"
                else ("third_party_imports", "raw_document")
            )
            stored = conn.execute(
                f"SELECT document,{payload} FROM {table} WHERE id=? AND run_id=?",
                (str(source.id), str(run)),
            ).fetchone()
            item: dict[str, object] = {
                "source_id": str(source.id),
                "direction": source.direction,
                "query": source.query,
                "source": source.model_dump(mode="json"),
                "report": {},
                payload: [] if source.provider == "aba" else {},
            }
            if stored is None:
                item.update(state("missing", "source_missing", "清洗选中的来源记录不存在"))
            else:
                document, raw = json.loads(stored[0]), json.loads(stored[1])
                item.update({"report": document, payload: raw})
                for key, target in (
                    ("core_selection_id", core_ids),
                    ("competitor_id", competitor_ids),
                ):
                    value = document.get(key)
                    if value and value not in target:
                        target.append(value)
                errors = [
                    q for q in document.get("queries", []) if q.get("error_code") or q.get("error")
                ]
                if (
                    document.get("status") in ("failed", "partial", "interrupted", "running")
                    or errors
                ):
                    detail = "; ".join(
                        f"{q.get('query', '')}: {q.get('error_code') or ''} {q.get('error') or ''}"
                        for q in errors
                    )
                    item.update(
                        state(
                            "failed",
                            "query_failed",
                            detail or f"来源状态：{document.get('status')}",
                        )
                    )
                elif source.provider == "aba" and any(
                    q.get("rows") and not q.get("raw_rows") for q in raw
                ):
                    item.update(
                        state(
                            "missing",
                            "raw_rows_missing",
                            "所选 ABA 查询仅保留结构化行，原始字段不可用",
                        )
                    )
                elif not (
                    any(q.get("raw_rows") or q.get("rows") for q in raw)
                    if source.provider == "aba"
                    else raw.get("rows")
                ):
                    item.update(state("empty", "no_rows", "所选来源查询成功但没有数据行"))
                else:
                    item.update(state("available"))
            (expansions if source.provider == "aba" else sellers).append(item)
        for items, payload in ((expansions, "results"), (sellers, "raw_document")):
            for direction in ("forward", "reverse"):
                if not any(item["direction"] == direction for item in items):
                    items.append(
                        {
                            "source_id": None,
                            "direction": direction,
                            "query": "",
                            "source": {},
                            "report": {},
                            payload: [] if payload == "results" else {},
                            **state("missing", "source_not_selected", "清洗报告未选择该方向的来源"),
                        }
                    )
        attachments["aba_expansions"], attachments["sellersprite"] = expansions, sellers

        roots: list[dict[str, object]] = []
        opportunities: list[dict[str, object]] = []
        pinned_core = json.loads(snapshots.get("core_selections", "{}"))
        pinned_competitor = json.loads(snapshots.get("competitor_versions", "{}"))
        for core_id in core_ids:
            core: dict[str, Any] = pinned_core if pinned_core.get("id") == core_id else {}
            aba_id = core.get("report_id")
            aba_row = conn.execute(
                "SELECT document,raw_document FROM aba_reports WHERE id=? AND run_id=?",
                (aba_id or "", str(run)),
            ).fetchone()
            queries: list[str] = []
            if aba_row:
                report, raw_queries = json.loads(aba_row[0]), json.loads(aba_row[1])
                queries = list(
                    dict.fromkeys(
                        [q.get("query", "") for q in report.get("reports", [])]
                        + [q.get("query", "") for q in raw_queries]
                    )
                )
                for query in queries:
                    raw_query = next((q for q in raw_queries if q.get("query") == query), None)
                    rows: list[object] = raw_query.get("rows", []) if raw_query else []
                    summary: dict[str, Any] = next(
                        (q for q in report.get("reports", []) if q.get("query") == query),
                        cast(dict[str, Any], {}),
                    )
                    roots.append(
                        {
                            "source_id": aba_id,
                            "core_selection_id": core_id,
                            "query": query,
                            "report": report,
                            "query_report": summary,
                            "rows": rows,
                            **(
                                state("available")
                                if rows
                                else state(
                                    "empty" if raw_query is not None else "missing",
                                    "no_rows" if raw_query is not None else "raw_query_missing",
                                    "该词根查询没有原始数据行"
                                    if raw_query is not None
                                    else "该词根的原始查询证据不存在",
                                )
                            ),
                        }
                    )
            if not aba_row or not queries:
                roots.append(
                    {
                        "source_id": aba_id,
                        "core_selection_id": core_id,
                        "query": "",
                        "report": json.loads(aba_row[0]) if aba_row else {},
                        "query_report": {},
                        "rows": [],
                        **state(
                            "missing",
                            "aba_report_missing" if not aba_row else "queries_missing",
                            "核心词关联的 ABA 词根来源不可用",
                        ),
                    }
                )

            linked: list[dict[str, Any]] = []
            for competitor_id in competitor_ids:
                if (
                    pinned_competitor.get("id") == competitor_id
                    and pinned_competitor.get("core_selection_id") == core_id
                ):
                    linked.append(pinned_competitor)
            keywords = list(dict.fromkeys(item["keyword"] for item in core.get("items", [])))
            for keyword in keywords:
                for competitor in linked or [{}]:
                    artifacts = [
                        a
                        for a in competitor.get("poe_downloads", [])
                        if a.get("keyword") == keyword
                    ]
                    fallback = [
                        item
                        for item in competitor.get("items", [])
                        if not any(s.get("keyword") for s in item.get("sources", []))
                        or any(s.get("keyword") == keyword for s in item.get("sources", []))
                    ]
                    for artifact in artifacts or [None]:
                        item = {
                            "source_id": competitor.get("id"),
                            "core_selection_id": core_id,
                            "query": keyword,
                            "report": competitor,
                            "artifact": artifact,
                            "headers": [],
                            "preamble": "",
                            "delimiter": "",
                            "rows": fallback,
                            "row_format": "structured",
                            **state(
                                "fallback" if fallback else "missing",
                                "archive_not_recorded",
                                "该核心词未归档商机探测器 CSV 原件",
                            ),
                        }
                        if artifact:
                            try:
                                artifact_id = UUID(artifact["id"])
                                path = (
                                    self.repository.storage_root
                                    / "poe_downloads"
                                    / str(run)
                                    / f"{artifact_id}.csv"
                                )
                                content = path.read_bytes()
                                if hashlib.sha256(content).hexdigest() != artifact["sha256"]:
                                    item.update(
                                        state(
                                            "fallback" if fallback else "missing",
                                            "archive_hash_mismatch",
                                            "归档 CSV 校验值与选中版本不符",
                                        )
                                    )
                                else:
                                    encoding = (
                                        "utf-16"
                                        if content.startswith((b"\xff\xfe", b"\xfe\xff"))
                                        else "utf-8-sig"
                                    )
                                    text = content.decode(encoding)
                                    lines = text.splitlines(keepends=True)
                                    header_index = 0
                                    if lines and lines[0].startswith(
                                        ("细分市场名称:", "Niche Name:", "Niche name:")
                                    ):
                                        header_index = next(
                                            (
                                                index
                                                for index, line in enumerate(lines[:12])
                                                if re.search(
                                                    r'(?:^|[,\t;])"?ASIN"?(?:[,\t;]|$)', line
                                                )
                                            ),
                                            -1,
                                        )
                                        if header_index < 0:
                                            raise ValueError("缺少商品表头")
                                    preamble = "".join(lines[:header_index])
                                    text = "".join(lines[header_index:])
                                    dialect = csv.Sniffer().sniff(text[:8192], delimiters=",\t;")
                                    records = list(
                                        csv.reader(
                                            io.StringIO(text, newline=""),
                                            dialect=dialect,
                                            skipinitialspace=False,
                                        )
                                    )
                                    item.update(
                                        {
                                            "headers": records[0] if records else [],
                                            "rows": records[1:],
                                            "row_format": "raw",
                                            "preamble": preamble,
                                            "delimiter": dialect.delimiter,
                                        }
                                    )
                                    item.update(
                                        state("available")
                                        if len(records) > 1
                                        else state("empty", "no_rows", "归档 CSV 没有商品数据行")
                                    )
                            except FileNotFoundError:
                                item.update(
                                    state(
                                        "fallback" if fallback else "missing",
                                        "archive_missing",
                                        "所选版本的归档 CSV 文件不存在",
                                    )
                                )
                            except (OSError, UnicodeError, ValueError, csv.Error):
                                item.update(
                                    state(
                                        "fallback" if fallback else "missing",
                                        "archive_unreadable",
                                        "所选版本的归档 CSV 无法读取或解析",
                                    )
                                )
                        if not competitor:
                            item.update(
                                state(
                                    "missing",
                                    "competitor_source_missing",
                                    "清洗来源关联的竞对版本不存在",
                                )
                            )
                        opportunities.append(item)
        if not roots:
            roots.append(
                {
                    "source_id": None,
                    "core_selection_id": None,
                    "query": "",
                    "report": {},
                    "query_report": {},
                    "rows": [],
                    **state("missing", "core_source_missing", "清洗来源未关联可用的核心词版本"),
                }
            )
        if not opportunities:
            opportunities.append(
                {
                    "source_id": None,
                    "core_selection_id": None,
                    "query": "",
                    "report": {},
                    "artifact": None,
                    "headers": [],
                    "preamble": "",
                    "delimiter": "",
                    "rows": [],
                    "row_format": "structured",
                    **state(
                        "missing", "core_keywords_missing", "清洗来源未关联可用的核心大词或竞对来源"
                    ),
                }
            )
        attachments["aba_roots"], attachments["opportunity"] = roots, opportunities
        from app.adapters.source_exports import SourceExportStore
        project = cleaning.project_id
        saved = SourceExportStore(self.repository.storage_root).list(project, run) if project else []
        def attach(source, owner, query):
            source['platform_export'] = next(({k: v for k, v in i.items() if k not in ('request', 'device_id')} for i in saved if i['owner'] == str(owner) and i['query'] == query),
                {'status': 'not_recorded', 'error': '该历史来源未保存平台下载原件；下表为接口采集数据'})
        for source in roots:
            attach(source, source.get('source_id'), source.get('query', ''))
        for source in expansions:
            for result in source.get('results', []):
                attach(result, source.get('source_id'), result.get('query', ''))
        for source in sellers:
            attach(source, source.get('source_id'), source.get('query', ''))
        return json.dumps(attachments, ensure_ascii=False)

    def create(self, project: UUID, run: UUID, data: LibraryRequest) -> LibraryReport:
        with self.repository.connect() as conn:
            self._fresh(project, run, data)
            old = conn.execute(
                "SELECT document FROM keyword_library_drafts WHERE run_id=? AND traffic_id=? AND traffic_version=? AND revision_of=?",
                (str(run), str(data.traffic_id), data.traffic_version, str(data.revision_of or "")),
            ).fetchone()
            if old:
                return self._stored(
                    project, run, LibraryReport.model_validate_json(cast(str, old[0])).id
                )
            if data.revision_of:
                parent = self._stored(project, run, data.revision_of)
                if (
                    parent.publication_version is None
                    or parent.request.traffic_id != data.traffic_id
                    or parent.request.traffic_version != data.traffic_version
                ):
                    raise DomainError(
                        409, "LIBRARY_REVISION_INVALID", "请从同一审核来源的已发布版本创建修订"
                    )
                report = parent.model_copy(
                    deep=True,
                    update={
                        "id": uuid4(),
                        "created_at": datetime.now(UTC),
                        "request": data,
                        "version": 0,
                        "publication_version": None,
                        "published_at": None,
                        "publication": None,
                        "edits": [],
                    },
                )
                conn.execute(
                    "INSERT INTO keyword_library_drafts (id, run_id, project_id, traffic_id, traffic_version, document, revision_of) VALUES(?,?,?,?,?,?,?)",
                    (
                        str(report.id),
                        str(run),
                        str(project),
                        str(data.traffic_id),
                        data.traffic_version,
                        report.model_dump_json(),
                        str(data.revision_of),
                    ),
                )
                return report
            traffic = self.traffic.get(project, run, data.traffic_id)
            cleaning = self.traffic.cleaning.get(project, run, traffic.request.cleaning_id)
            aba_ids = {s.id for s in cleaning.sources if s.provider == "aba"}
            rules = LibraryRules()
            entries: list[LibraryEntry] = []
            counts: dict[Tier, Counter[str]] = {}
            for row in self.traffic.page(project, run, data.traffic_id, 0, traffic.total).items:
                if (
                    row.cleaning_exclusion
                    or row.review is None
                    or row.review.data.decision != "keep"
                ):
                    continue
                tier = tier_of(row.metrics.rank, rules)
                # 清洗模型会给出 unclassified（证据不足不猜测），这里也要留桶，
                # 否则词根命中时直接 KeyError 让建库失败。CSV 只导出前三类。
                roots: dict[str, list[str]] = {category: [] for category in get_args(Category)}
                if cleaning.review:
                    for root in cleaning.review.roots:
                        if contains_root(row.phrase.keyword, root.term):
                            roots[root.category].append(root.term)
                entries.append(
                    LibraryEntry(
                        keyword=row.phrase.keyword,
                        tier=tier,
                        roots=roots,
                        priority=row.review.data.priority,
                        evidence=row,
                    )
                )
                asins: set[str] = set()
                if tier != "unranked":
                    for evidence in row.phrase.evidence:
                        if evidence.source_id in aba_ids:
                            payload = cast(dict[str, object], json.loads(evidence.payload))
                            raw = payload.get("raw")
                            if isinstance(raw, dict):
                                raw = cast(dict[str, object], raw)
                                for i in range(1, rules.asin_positions + 1):
                                    asin = str(raw.get(f"st-top-asin-{i}", "")).strip().upper()
                                    if re.fullmatch(r"[A-Z0-9]{10}", asin):
                                        asins.add(asin)
                counts.setdefault(tier, Counter()).update(asins)
            if not entries:
                raise DomainError(422, "LIBRARY_EMPTY", "没有人工保留的关键词，无法生成空库")
            entries.sort(key=lambda e: (e.evidence.metrics.rank or 10**18, e.keyword))
            for entry in entries:
                entry.issues = entry_issues(entry)
            snapshots = {
                "traffic": traffic.model_dump_json(),
                "cleaning": cleaning.model_dump_json(),
                "traffic_reviews": json.dumps(
                    [
                        r.model_dump(mode="json")
                        for r in self.traffic.reviews(project, run, data.traffic_id)
                    ],
                    ensure_ascii=False,
                ),
                "project": self.repository.get_project(project).model_dump_json(),
                "run": self.repository.get_run(project, run).model_dump_json(),
            }
            for table in ("core_selections", "competitor_versions"):
                stored = conn.execute(
                    f"SELECT document FROM {table} WHERE run_id=? ORDER BY version DESC LIMIT 1",
                    (str(run),),
                ).fetchone()
                snapshots[table] = cast(str, stored[0]) if stored else "{}"
            snapshots["workbook_attachments"] = freeze_attachment_exports(
                self._attachment_snapshot(conn, run, cleaning, snapshots)
            )
            core_snapshot = json.loads(snapshots["core_selections"])
            root_report = conn.execute(
                "SELECT document FROM aba_reports WHERE id=? AND run_id=?",
                (core_snapshot.get("report_id", ""), str(run)),
            ).fetchone()
            if root_report:
                aba_report = AbaReport.model_validate_json(root_report[0])
                snapshots["root_source"] = json.dumps(
                    {
                        "report_id": str(aba_report.id),
                        "roots_snapshot": aba_report.roots_snapshot,
                    },
                    ensure_ascii=False,
                )
            confirmation = conn.execute(
                "SELECT document FROM product_confirmations WHERE run_id=? ORDER BY version DESC LIMIT 1",
                (str(run),),
            ).fetchone()
            snapshots["product_confirmation"] = cast(str, confirmation[0]) if confirmation else "{}"
            report = LibraryReport(
                id=uuid4(),
                project_id=project,
                run_id=run,
                created_at=datetime.now(UTC),
                request=data,
                rules=rules,
                entries=entries,
                snapshots=snapshots,
                source_sha256=hashlib.sha256(
                    json.dumps(snapshots, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest(),
                suggestions=[
                    AsinSuggestion(tier=tier, asin=asin, keyword_count=count)
                    for tier, counter in sorted(counts.items())
                    for asin, count in sorted(
                        counter.items(), key=lambda item: (-item[1], item[0])
                    )[: rules.suggestions_per_tier]
                ],
            )
            conn.execute(
                "INSERT INTO keyword_library_drafts (id, run_id, project_id, traffic_id, traffic_version, document, revision_of) VALUES(?,?,?,?,?,?,?)",
                (
                    str(report.id),
                    str(run),
                    str(project),
                    str(data.traffic_id),
                    data.traffic_version,
                    report.model_dump_json(),
                    str(data.revision_of or ""),
                ),
            )
        return report

    def refresh_attachments(self, project: UUID, run: UUID, key: UUID, expected_version: int) -> LibraryReport:
        """仅重取草稿已绑定来源的下载归档；不读取其他批次，不覆盖已发布快照。"""
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            conn.execute("SELECT id FROM keyword_library_drafts WHERE id=? AND run_id=? FOR UPDATE", (str(key), str(run)))
            report = self._stored(project, run, key)
            if report.publication_version is not None or report.version != expected_version:
                raise DomainError(409, "LIBRARY_VERSION_CONFLICT", "版本已发布或已更新，请刷新；已发布版本请创建修订草稿")
            self._fresh(project, run, report.request)
            cleaning = CleaningReport.model_validate_json(report.snapshots["cleaning"])
            report.snapshots["workbook_attachments"] = freeze_attachment_exports(
                self._attachment_snapshot(conn, run, cleaning, report.snapshots)
            )
            report.source_sha256 = hashlib.sha256(
                json.dumps(report.snapshots, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            report.version += 1
            conn.execute("UPDATE keyword_library_drafts SET document=? WHERE id=? AND run_id=?",
                         (report.model_dump_json(), str(key), str(run)))
        return report

    def edit(self, project: UUID, run: UUID, key: UUID, data: EntryEdit) -> LibraryReport:
        with self.repository.connect() as conn:
            conn.execute("SELECT id FROM keyword_library_drafts WHERE id=? AND run_id=? FOR UPDATE", (str(key), str(run)))
            report = self._stored(project, run, key)
            self._fresh(project, run, report.request)
            if report.publication_version is not None or report.version != data.expected_version:
                raise DomainError(409, "LIBRARY_VERSION_CONFLICT", "版本已发布或已更新，请刷新")
            entry = next((e for e in report.entries if e.keyword == data.keyword), None)
            if entry is None:
                raise DomainError(422, "LIBRARY_ENTRY_INVALID", "关键词不属于该草稿")
            entry.priority, entry.usage = data.priority, data.usage
            entry.auxiliary_asins = list(dict.fromkeys(data.auxiliary_asins))
            report.version += 1
            report.edits.append(
                EditRecord(created_at=datetime.now(UTC), version=report.version, data=data)
            )
            conn.execute(
                "UPDATE keyword_library_drafts SET document=? WHERE id=?",
                (report.model_dump_json(), str(key)),
            )
        return report

    def publish(self, project: UUID, run: UUID, key: UUID, data: PublishRequest) -> LibraryReport:
        with self.repository.connect() as conn:
            # Serialize publication-version allocation per project. A retry that waited for
            # the first publisher then observes the frozen draft and returns it idempotently.
            conn.execute("SELECT id FROM projects WHERE id=? FOR UPDATE", (str(project),))
            conn.execute("SELECT id FROM keyword_library_drafts WHERE id=? AND run_id=? FOR UPDATE", (str(key), str(run)))
            report = self._stored(project, run, key)
            if report.publication_version is not None:
                if report.publication == data:
                    return report
                raise DomainError(409, "LIBRARY_PUBLISHED", "此草稿已发布，不能覆盖")
            self._fresh(project, run, report.request)
            if report.version != data.expected_version:
                raise DomainError(409, "LIBRARY_VERSION_CONFLICT", "草稿已更新，请刷新后确认发布")
            # 发布只冻结本地版本，不执行广告投放；入库范围沿用第七步审核结果。
            # expected_version 在写事务中校验，防止用旧页面覆盖其他操作保存的新版本。
            version_row = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM keyword_library_publications WHERE project_id=?",
                (str(project),),
            ).fetchone()
            assert version_row is not None
            version = cast(
                int,
                version_row[0],
            )
            report.publication_version = version
            report.publication = data
            report.published_at = datetime.now(UTC)
            document = report.model_dump_json()
            conn.execute(
                "INSERT INTO keyword_library_publications VALUES(?,?,?,?)",
                (str(project), version, str(key), document),
            )
            conn.execute(
                "UPDATE keyword_library_drafts SET document=? WHERE id=?", (document, str(key))
            )
        return report

    def export(self, project: UUID, run: UUID, key: UUID, *, ranked_only: bool = False) -> str:
        # 导出使用存储快照，不因上游重新采集而改写历史；ranked_only 只过滤有效周排名。
        # Export the stored snapshot; upstream status never rewrites historical file content.
        report = self._stored(project, run, key)
        # Shared raw sources can be megabytes. Keep the full immutable snapshot in
        # data record 1; later records refer to it instead of multiplying its size.
        shared_snapshot = report.model_dump_json(exclude={"entries", "edits"})
        snapshot_reference = json.dumps(
            {"source_sha256": report.source_sha256, "snapshot_record": 1}, ensure_ascii=False
        )
        edits = json.dumps([e.model_dump(mode="json") for e in report.edits], ensure_ascii=False)
        publication = report.publication.model_dump_json() if report.publication else ""
        snapshot_written = False
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            [
                "项目",
                "关键词",
                "辅助ASIN",
                "ABA排名",
                "层级",
                "卖点词根",
                "场景词根",
                "偏好词根",
                "相关性",
                "垄断",
                "购买",
                "优先级",
                "用途",
                "CTR",
                "CVR",
                "花费",
                "订单",
                "ACOS",
                "发布版本",
                "来源及规则快照",
                "关键词证据",
                "编辑记录",
                "发布确认",
            ]
        )
        for entry in report.entries:
            if ranked_only and (
                entry.evidence.metrics.rank is None or entry.evidence.metrics.rank < 1
            ):
                continue
            review = entry.evidence.review
            values = [
                str(project),
                entry.keyword,
                ";".join(entry.auxiliary_asins),
                str(entry.evidence.metrics.rank or ""),
                entry.tier,
                ";".join(entry.roots["selling_point"]),
                ";".join(entry.roots["scene"]),
                ";".join(entry.roots["preference"]),
                review.relevance if review else "unknown",
                str(entry.evidence.metrics.monopoly)
                if entry.evidence.metrics.monopoly is not None
                else "",
                str(entry.evidence.metrics.purchase)
                if entry.evidence.metrics.purchase is not None
                else "",
                entry.priority,
                entry.usage,
                "",
                "",
                "",
                "",
                "",
                str(report.publication_version or ""),
                snapshot_reference if snapshot_written else shared_snapshot,
                entry.evidence.model_dump_json(),
                edits,
                publication,
            ]
            writer.writerow(
                ["'" + v if v.lstrip().startswith(("=", "+", "-", "@")) else v for v in values]
            )
            snapshot_written = True
        return output.getvalue()
