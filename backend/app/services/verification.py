"""持久化核验任务；浏览器串行采集所有词根，部分失败绝不自动确认。"""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock
from uuid import UUID, uuid4

from app.db.repository import Repository
from app.domain.errors import DomainError
from app.domain.roots import RootSelection
from app.domain.verification import (
    PageCheckpoint,
    Verification,
    VerificationInput,
    VerificationItem,
)
from app.services.amazon_search import AmazonSearch, SearchProvider
from app.services.image_matching import ImageMatcher
from app.services.images import ImageService
from app.services.qwen import QwenClient, QwenConfig
from app.services.roots import RootService
from app.services.search_policy import PAUSE_CODES, SearchFailure


def choose_best(items: list[VerificationItem]) -> tuple[list[UUID], bool]:
    complete = bool(items) and all(i.status in ("matched", "not_found_in_scope") for i in items)
    matches = [
        i for i in items if i.status == "matched" and i.page is not None and i.position is not None
    ]
    if any(i.status == "matched" and (i.page is None or i.position is None) for i in items):
        complete = False
    if not matches:
        return [], complete
    rank = min((i.page or 999, i.position or 999) for i in matches)
    return [i.candidate_id for i in matches if (i.page, i.position) == rank], complete


class VerificationService:
    def __init__(
        self,
        repository: Repository,
        roots: RootService,
        images: ImageService,
        provider: SearchProvider | None = None,
        model: QwenClient | None = None,
        auto_execute: bool = True,
    ) -> None:
        self.repository = repository
        self.roots = roots
        self.images = images
        self.provider = provider or AmazonSearch()
        self.model = model
        self.auto_execute = auto_execute
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="root-verification")
        self.review_events: dict[UUID, Event] = {}
        self.review_lock = Lock()
        self.evidence_root = repository.storage_root / "search-evidence"

    def recover(self) -> None:
        # 重启后不重复抓取或调用大模型，保留中断证据供人工查看。
        with self.repository.connect() as conn:
            rows = conn.execute("SELECT document FROM root_verifications").fetchall()
            for row in rows:
                task = Verification.model_validate_json(row[0])
                if task.review_status in ("opening", "open"):
                    task.review_status = "closed"
                    conn.execute(
                        "UPDATE root_verifications SET document=? WHERE id=?",
                        (task.model_dump_json(), str(task.id)),
                    )
                if task.status in ("queued", "running"):
                    task.status = "interrupted"
                    task.message = "服务重启导致核验中断，未自动重试"
                    for item in task.items:
                        if item.status in ("waiting", "running"):
                            item.status = "failed"
                            item.message = task.message
                    conn.execute(
                        "UPDATE root_verifications SET document=? WHERE id=?",
                        (task.model_dump_json(), str(task.id)),
                    )

    def close(self) -> None:
        for event in self.review_events.values():
            event.set()
        self.executor.shutdown(wait=True, cancel_futures=True)

    def save(self, task: Verification) -> None:
        with self.repository.connect() as conn:
            conn.execute(
                "UPDATE root_verifications SET document=? WHERE id=?",
                (task.model_dump_json(), str(task.id)),
            )

    def get(self, project: UUID, run: UUID, task_id: UUID) -> Verification:
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM root_verifications WHERE id=? AND run_id=?",
                (str(task_id), str(run)),
            ).fetchone()
        if not row:
            raise DomainError(404, "VERIFICATION_NOT_FOUND", "此任务下未找到核验记录")
        return Verification.model_validate_json(row[0])

    def start(self, project: UUID, run_id: UUID, data: VerificationInput) -> Verification:
        run = self.repository.get_run(project, run_id)
        roots = self.roots.get(project, run_id)
        selected = set(data.candidate_ids) if data.candidate_ids is not None else None
        if selected is not None and not selected <= {c.id for c in roots.candidates}:
            raise DomainError(422, "INVALID_ROOT_SELECTION", "只能验证当前任务推荐的词根")
        # 同一选择集合不受勾选顺序影响；旧调用未传候选时仍保留历史行为。
        data = data.model_copy(
            update={"candidate_ids": sorted(selected, key=str) if selected is not None else None}
        )
        if data.reference_image_id is not None:
            if data.reference_image_id not in run.input.image_ids:
                raise DomainError(422, "INVALID_REFERENCE", "请从当前任务上传图片中指定主图")
            self.images.content(project, data.reference_image_id)
        fingerprint = hashlib.sha256(
            ("atomic-expansion-v4:" + roots.model_dump_json() + data.model_dump_json()).encode()
        ).hexdigest()
        task = Verification(
            id=uuid4(),
            project_id=project,
            run_id=run_id,
            created_at=datetime.now(UTC),
            max_pages=data.max_pages,
            reference_image_id=data.reference_image_id,
            manual_candidates=selected is not None,
            items=[
                VerificationItem(
                    candidate_id=c.id,
                    text=c.text,
                    translation=c.translation,
                    expansions=c.expansions,
                )
                for c in roots.candidates
                if selected is None or c.id in selected
            ],
        )
        if not run.input.asin and data.reference_image_id is None:
            task.status = "needs_reference"
            task.message = "请指定上传图片中的商品主图，不能默认把场景图当主图"
            for item in task.items:
                item.status = "needs_reference"
                item.message = task.message
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM root_verifications WHERE run_id=? AND fingerprint=?",
                (str(run_id), fingerprint),
            ).fetchone()
            if row:
                return Verification.model_validate_json(row[0])
            conn.execute(
                "INSERT INTO root_verifications VALUES (?,?,?,?)",
                (str(task.id), str(run_id), fingerprint, task.model_dump_json()),
            )
        if task.status == "queued" and self.auto_execute:
            self.executor.submit(self.execute, project, run_id, task.id)
        return task

    def execute(self, project: UUID, run_id: UUID, task_id: UUID) -> None:
        # 原子领取，重复提交执行不会同时采集同一个任务。
        with self.repository.connect() as conn:
            task = self.get(project, run_id, task_id)
            if task.status != "queued":
                return
            task.status = "running"
            conn.execute(
                "UPDATE root_verifications SET document=? WHERE id=?",
                (task.model_dump_json(), str(task.id)),
            )
        run = self.repository.get_run(project, run_id)
        try:
            matcher = None
            if not run.input.asin and task.reference_image_id:
                _, path = self.images.content(project, task.reference_image_id)
                client = self.model or QwenClient(QwenConfig.load(self.repository.storage_root))
                matcher = ImageMatcher(client, path.read_bytes())
                for previous in task.items:
                    for comparison in previous.image_comparisons:
                        if comparison.verdict != "uncertain":
                            matcher.cache[comparison.image_url] = comparison.verdict
                            matcher.evidence[comparison.image_url] = comparison
            with self.provider.open(
                run.input.marketplace, self.evidence_root / str(task.id)
            ) as session:
                for item in task.items:
                    if item.status in ("matched", "not_found_in_scope"):
                        continue
                    item.status = "running"
                    self.save(task)
                    try:
                        # 最小词根优先，只有正常查完范围未命中才进入拓词。
                        for query_index, query in enumerate([item.text, *item.expansions]):
                            if query in item.completed_queries:
                                continue
                            item.active_query = query
                            self.save(task)
                            for number in range(1, task.max_pages + 1):
                                checkpoint = next(
                                    (
                                        p
                                        for p in item.pages
                                        if p.number == number and (p.query or item.text) == query
                                    ),
                                    None,
                                )
                                if checkpoint and checkpoint.processed:
                                    if not checkpoint.result.next_page or number == task.max_pages:
                                        break
                                    continue
                                if checkpoint is None:
                                    page = session.search(
                                        query,
                                        number,
                                        f"{item.candidate_id}-q{query_index}-{number}",
                                    )
                                    checkpoint = PageCheckpoint(
                                        query=query, number=number, result=page
                                    )
                                    item.pages.append(checkpoint)
                                    self.save(task)
                                page = checkpoint.result
                                item.checked_at = datetime.now(UTC)
                                item.search_url = page.url
                                if page.screenshot:
                                    item.evidence_url = f"/api/v1/projects/{project}/pipeline-runs/{run_id}/root-verifications/{task.id}/evidence/{page.screenshot}"
                                if page.diagnostic:
                                    item.diagnostic_url = f"/api/v1/projects/{project}/pipeline-runs/{run_id}/root-verifications/{task.id}/evidence/{page.diagnostic}"
                                item.region = page.region
                                if not page.region or (task.region and page.region != task.region):
                                    raise DomainError(
                                        502,
                                        "REGION_CHANGED",
                                        "配送地区缺失或发生变化，不能比较排名",
                                    )
                                task.region = page.region
                                if run.input.asin:
                                    matches = [c for c in page.cards if c.asin == run.input.asin]
                                elif matcher:
                                    matches = []
                                    # 按自然顺序分批比对，找到最早匹配后不再调用后续图片。
                                    for offset in range(0, len(page.cards), 8):
                                        batch = page.cards[offset : offset + 8]
                                        # 写前日志：进程在付费请求中断时，禁止恢复后重复计费。
                                        task.model_outcome_pending = True
                                        self.save(task)
                                        verdicts = matcher.compare(batch)
                                        task.model_outcome_pending = matcher.blocked
                                        for candidate in batch:
                                            evidence = matcher.evidence[
                                                candidate.image_url
                                            ].model_copy(update={"asin": candidate.asin})
                                            item.image_comparisons.append(evidence)
                                        self.save(task)
                                        for card, verdict in zip(batch, verdicts, strict=True):
                                            if verdict == "uncertain":
                                                raise DomainError(
                                                    422,
                                                    "IMAGE_MATCH_UNCERTAIN",
                                                    "较靠前的商品主图无法确认，不能确定最早位置",
                                                )
                                            if verdict == "match":
                                                matches = [card]
                                                break
                                        if matches:
                                            break
                                else:
                                    raise DomainError(422, "IMAGE_MATCH_UNCERTAIN", "缺少商品主图")
                                checkpoint.processed = True
                                item.checked_pages = sum(p.processed for p in item.pages)
                                if matches:
                                    card = matches[0]
                                    item.status = "matched"
                                    item.matched_query = query
                                    item.page = number
                                    item.position = card.position
                                    item.matched_asin = card.asin
                                    item.image_url = card.image_url
                                    item.match_method = "asin" if run.input.asin else "main_image"
                                    item.message = (
                                        "ASIN 精确匹配"
                                        if run.input.asin
                                        else "主图匹配商品（不证明店铺归属）"
                                    )
                                    break
                                self.save(task)
                                if not page.next_page or number == task.max_pages:
                                    break
                            if item.status == "matched":
                                break
                            item.completed_queries.append(query)
                            self.save(task)
                        if item.status != "matched":
                            item.status = "not_found_in_scope"
                            item.message = "最小词根及扩展搜索词在指定范围内均未找到"
                    except DomainError as error:
                        item.error_code = error.code
                        if isinstance(error, SearchFailure):
                            item.failure_stage = error.stage
                            prefix = f"/api/v1/projects/{project}/pipeline-runs/{run_id}/root-verifications/{task.id}/evidence/"
                            if error.screenshot:
                                item.evidence_url = prefix + error.screenshot
                            if error.diagnostic:
                                item.diagnostic_url = prefix + error.diagnostic
                        if error.code in PAUSE_CODES:
                            item.status = "paused"
                            item.message = error.message
                            task.status = "paused"
                            task.message = (
                                error.message + "；处理访问检查后可继续，已完成页面会保留"
                            )
                            self.save(task)
                            return
                        item.status = (
                            "uncertain" if error.code == "IMAGE_MATCH_UNCERTAIN" else "failed"
                        )
                        item.message = error.message
                    self.save(task)
            task.best_candidate_ids, task.final = choose_best(task.items)
            task.status = "completed" if task.final else "incomplete"
            task.message = (
                (
                    "核验完成；并列最优全部保留"
                    if task.best_candidate_ids
                    else "检查范围内未找到目标商品，不自动选择词根"
                )
                if task.final
                else "部分词根未核验成功，仅展示暂时最优，不自动选择"
            )
            # 与人工保存争用同一写锁，人工记录始终优先，不被后台结果覆盖。
            with self.repository.connect() as conn:
                row = conn.execute(
                    "SELECT document FROM root_selections WHERE run_id=?", (str(run_id),)
                ).fetchone()
                manual = bool(row and RootSelection.model_validate_json(row[0]).source == "manual")
                if task.manual_candidates:
                    task.message += "；验证结果仅作参考，请人工保存最终词根选择"
                elif task.final and task.best_candidate_ids and not manual:
                    selection = RootSelection(
                        project_id=project,
                        run_id=run_id,
                        candidate_ids=task.best_candidate_ids,
                        confirmed_at=datetime.now(UTC),
                        source="automatic",
                        verification_id=task.id,
                    )
                    conn.execute(
                        "INSERT INTO root_selections VALUES (?,?) ON CONFLICT(run_id) DO UPDATE SET document=excluded.document",
                        (str(run_id), selection.model_dump_json()),
                    )
                    task.selection_saved = True
                elif manual:
                    task.message += "；保留已有人工选择"
                conn.execute(
                    "UPDATE root_verifications SET document=? WHERE id=?",
                    (task.model_dump_json(), str(task.id)),
                )
        except Exception as error:
            task.status = "incomplete"
            task.final = False
            task.message = (
                error.message
                if isinstance(error, DomainError)
                else "核验执行失败，请检查浏览器和后端日志"
            )
            for item in task.items:
                if item.status in ("waiting", "running"):
                    item.status = "failed"
                    item.message = task.message
            task.best_candidate_ids, _ = choose_best(task.items)
            self.save(task)

    def resume(self, project: UUID, run: UUID, task_id: UUID) -> Verification:
        # 先验证项目归属，再触碰运行中的人工窗口。
        existing = self.get(project, run, task_id)
        if existing.model_outcome_pending and existing.status not in (
            "queued",
            "running",
            "completed",
        ):
            raise DomainError(
                409, "MODEL_OUTCOME_PENDING", "上次模型调用结果待确认，禁止自动重试以避免重复计费"
            )
        with self.review_lock:
            event = self.review_events.get(task_id)
            if event:
                event.set()
                return self.get(project, run, task_id)
        with self.repository.connect() as conn:
            task = self.get(project, run, task_id)
            if task.status in ("queued", "running", "completed"):
                return task
            if task.model_outcome_pending:
                raise DomainError(
                    409,
                    "MODEL_OUTCOME_PENDING",
                    "上次模型调用结果待确认，禁止自动重试以避免重复计费",
                )
            if task.status == "needs_reference":
                raise DomainError(409, "REFERENCE_REQUIRED", "请先指定主图")
            task.status = "queued"
            task.message = "已保留完成的页面，等待继续核验"
            for item in task.items:
                if item.status not in ("matched", "not_found_in_scope"):
                    item.status = "waiting"
                    item.error_code = ""
            conn.execute(
                "UPDATE root_verifications SET document=? WHERE id=?",
                (task.model_dump_json(), str(task.id)),
            )
        if self.auto_execute:
            self.executor.submit(self.execute, project, run, task_id)
        return task

    def open_review(self, project: UUID, run: UUID, task_id: UUID) -> Verification:
        task = self.get(project, run, task_id)
        if task.status not in ("paused", "incomplete", "interrupted"):
            raise DomainError(409, "REVIEW_NOT_AVAILABLE", "请等待采集结束或暂停后再打开检查窗口")
        if not isinstance(self.provider, AmazonSearch):
            raise DomainError(503, "REVIEW_NOT_AVAILABLE", "当前采集服务不支持本地检查窗口")
        with self.review_lock:
            if task_id in self.review_events:
                return task
            event = Event()
            self.review_events[task_id] = event
            task.review_status = "opening"
            self.save(task)
            self.executor.submit(self._review, project, run, task_id, event)
        return task

    def _review(self, project: UUID, run: UUID, task_id: UUID, event: Event) -> None:
        task = self.get(project, run, task_id)
        provider = self.provider
        assert isinstance(provider, AmazonSearch)

        def ready() -> None:
            task.review_status = "open"
            self.save(task)

        try:
            provider.review(
                self.repository.get_run(project, run).input.marketplace,
                self.evidence_root / str(task.id),
                next(
                    (
                        i.active_query or i.text
                        for i in task.items
                        if i.status in ("paused", "failed", "uncertain")
                    ),
                    task.items[0].text,
                ),
                event,
                ready,
            )
            task.review_status = "closed"
        except Exception:
            task.review_status = "error"
            task.message = "检查窗口打开失败或已关闭，可重新打开"
        finally:
            self.save(task)
            with self.review_lock:
                self.review_events.pop(task_id, None)
        if event.is_set():
            self.resume(project, run, task_id)

    def evidence(self, project: UUID, run: UUID, task_id: UUID, filename: str) -> Path:
        task = self.get(project, run, task_id)
        # 只允许数据库中已登记的证据文件，拒绝路径穿越或别的任务文件。
        allowed = {
            url.rsplit("/", 1)[-1]
            for i in task.items
            for url in (i.evidence_url, i.diagnostic_url)
            if url
        }
        allowed.update(
            filename
            for i in task.items
            for p in i.pages
            for filename in (p.result.screenshot, p.result.diagnostic)
            if filename
        )
        if (
            filename not in allowed
            or Path(filename).name != filename
            or not filename.endswith((".png", ".json"))
        ):
            raise DomainError(404, "EVIDENCE_NOT_FOUND", "核验证据不存在")
        path = self.evidence_root / str(task.id) / filename
        if not path.is_file():
            raise DomainError(404, "EVIDENCE_NOT_FOUND", "核验证据文件不存在")
        return path
