"""自动判断模式：模型完成全部判断，竞对发现与三方采集也自动执行。"""

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import Engine
from test_aba import auth_file, row
from test_hot_search_core_analysis import setup_analysis
from test_roots import CANDIDATES, PRODUCT, FixedProductProvider
from test_verification_jobs import FakeSearch

from app.adapters.aba import AbaClient
from app.adapters.sellersprite_browser import SellerspriteBrowserDownloader
from app.domain.aba import AbaReport
from app.domain.asin_product import ProductSnapshot
from app.domain.cleaning import Frequency
from app.domain.competitors import Competitor
from app.domain.errors import DomainError
from app.domain.product_understanding import ProductUnderstanding
from app.domain.roots import RootCandidate
from app.domain.workflow import WorkflowView
from app.main import create_app
from app.services.amazon_search import SearchCard, SearchPage
from app.services.qwen import QwenClient, QwenConfig
from app.services.search_policy import SearchFailure
from app.workflows.deps import WorkflowDeps
from app.workflows.nodes_steps_1_2 import build_nodes_1_2
from app.workflows.state import WorkflowState, initial_state

SELLER_CSV = (
    "关键词,关键词翻译,月搜索量,ABA月排名,#1 点击共享,#1转化共享\n"
    "cooling blanket,凉毯,1200,20,10%,6%\n"
).encode()


def reply(content: object) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]},
    )


def prompt_data(prompt: str) -> dict[str, object]:
    parsed = json.loads(prompt.split("数据：", 1)[1])
    return cast(dict[str, object], parsed) if isinstance(parsed, dict) else {}


def fake_aba_auto(tmp_path: Path) -> AbaClient:
    """非纯词根的候选才能被推荐为核心词。"""
    return AbaClient(
        auth_file(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"reportsV2": [{"totalItems": 1, "rows": [row("coffee mug", 5000)]}]}
            )
        ),
    )


def fake_auto_model(core_intent: str = "exact") -> QwenClient:
    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        prompt = payload["messages"][0]["content"][0]["text"]
        data = prompt_data(prompt) if "数据：" in prompt else {}
        if "terms" in data:
            terms = cast(list[dict[str, object]], data["terms"])
            return reply(
                {
                    "items": [
                        {
                            "candidate_id": term["candidate_id"],
                            "brand_category": "generic",
                            "intent_evidence": {
                                "object_match": core_intent == "exact",
                                "purpose_match": True,
                                "product_level_match": True,
                                "modifiers_compatible": True,
                                "explicit_conflict": False,
                                "accessory_target": False,
                                "broad_category": core_intent == "broad",
                                "category": core_intent,
                                "reasons": {
                                    key: "匹配商品"
                                    for key in (
                                        "object_match",
                                        "purpose_match",
                                        "product_level_match",
                                        "modifiers_compatible",
                                    )
                                },
                            },
                        }
                        for term in terms
                    ]
                }
            )
        if "相关性粗筛" in prompt:
            candidates = cast(list[dict[str, object]], prompt_data(prompt)["candidates"])
            return reply({"candidate_ids": [c["id"] for c in candidates]})
        if "从给定词根候选中选择" in prompt:
            candidates = cast(list[dict[str, object]], prompt_data(prompt)["candidates"])
            return reply(
                {
                    "selected": [{"candidate_id": candidates[0]["id"], "reason": "最小品类词"}],
                    "reason": "唯一匹配",
                }
            )
        if "为商品筛选 ABA 周报核心大词" in prompt:
            candidates = cast(list[dict[str, object]], prompt_data(prompt)["candidates"])
            return reply(
                {
                    "items": [
                        {
                            "candidate_id": candidates[0]["id"],
                            "translation": "咖啡杯",
                            "relevance": "high",
                            "intent": core_intent,
                            "category": "generic",
                            "keyword_role": "core",
                            "reason": "同类商品",
                        }
                    ]
                }
            )
        if "判断竞对 ASIN 是否应纳入" in prompt:
            candidates = cast(list[dict[str, object]], prompt_data(prompt)["candidates"])
            return reply(
                {
                    "items": [
                        {
                            "asin": item["asin"],
                            "decision": "include",
                            "reason": "同类商品",
                            "price_override_reason": (
                                "" if item.get("price_status") == "within" else "价格待复核"
                            ),
                        }
                        for item in candidates
                    ]
                }
            )
        if "根据已确认商品事实自动分类" in prompt:
            frequencies = cast(list[dict[str, object]], prompt_data(prompt)["frequencies"])
            return reply(
                {
                    "roots": [
                        {
                            "term": item["term"],
                            "category": "preference",
                            "evidence": "结合已确认商品属性判断",
                        }
                        for item in frequencies
                    ],
                    "exclusions": [],
                    "reason": "结合商品事实自动分类",
                }
            )
        if "逐词判断与已确认商品事实的语义相关性" in prompt:
            rows = cast(list[dict[str, object]], prompt_data(prompt)["rows"])
            return reply(
                {
                    "items": [
                        {
                            "keyword": item["keyword"],
                            "decision": "keep",
                            "reason": "与商品相关",
                            "evidence_note": "词义匹配",
                        }
                        for item in rows
                    ]
                }
            )
        if "生成候选商品词" in prompt:
            return reply(CANDIDATES)
        if "提取商品四维基准" in prompt:
            return reply(
                {
                    "purpose": ["饮用咖啡"],
                    "style_structure": ["带手柄杯体"],
                    "specifications": ["马克杯"],
                    "package_quantity": "1",
                    "unit_basis": "each",
                }
            )
        if "逐个比较搜索商品与本品" in prompt:
            products = cast(list[dict[str, object]], data["products"])
            return reply(
                {
                    "items": [
                        {
                            "asin": item["asin"],
                            "valid": True,
                            "exclusion_reason": "",
                            "purpose": ["饮用咖啡"],
                            "style_structure": ["带手柄杯体"],
                            "specifications": ["马克杯"],
                            "package_quantity": "1",
                            "unit_basis": "each",
                            "scores": {
                                "purpose": 90,
                                "style_structure": 90,
                                "specifications": 90,
                                "purpose_reason": "相同",
                                "style_structure_reason": "相同",
                                "specifications_reason": "相同",
                            },
                        }
                        for item in products
                    ]
                }
            )
        return reply(PRODUCT)

    return QwenClient(QwenConfig(api_key="secret"), transport=httpx.MockTransport(handle))


class ProductSearch(FakeSearch):
    block_products = False

    def product(self, asin: str) -> ProductSnapshot:
        if self.block_products:
            raise SearchFailure("SEARCH_ACCESS_CHECK", "Amazon 要求人工确认访问，请打开检查窗口处理")
        return ProductSnapshot(
            asin=asin, title="Coffee mug", price=Decimal("20"), currency="USD", price_reliable=True
        )


def fake_search() -> ProductSearch:
    page = SearchPage(
        region="New York 10001",
        cards=[
            SearchCard(asin="B0ABCDEFG1", position=1),
            SearchCard(asin="B0ABCDEFG2", position=2),
        ],
        next_page=False,
        url="https://www.amazon.com/s?k=coffee+mug",
    )
    return ProductSearch(
        {
            ("mug", 1): page,
            ("coffee mug", 1): SearchPage(
                region="New York 10001",
                cards=[SearchCard(asin="B0ABCDEFG3", position=1)],
                next_page=False,
                url="https://www.amazon.com/s?k=coffee+mug",
            ),
        }
    )


@pytest.mark.parametrize("core_intent", ["exact", "broad"])
def test_auto_mode_runs_whole_chain(
    db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core_intent: str
) -> None:
    def download(_self: object, _request: object, _target: Path) -> tuple[str, bytes, int]:
        return "seller.csv", SELLER_CSV, 1

    monkeypatch.setattr(SellerspriteBrowserDownloader, "download", download)
    with TestClient(
        create_app(
            db,
            model_client=fake_auto_model(core_intent),
            aba_client=fake_aba_auto(tmp_path),
            search_provider=fake_search(),
            workflow_checkpointer=InMemorySaver(),
            workflow_inline=True,
            product_provider=FixedProductProvider(),
        )
    ) as api:
        project = api.post("/api/v1/projects", json={"name": "杯子", "marketplace": "US"}).json()[
            "id"
        ]
        run = api.post(
            f"/api/v1/projects/{project}/pipeline-runs",
            json={"marketplace": "US", "asin": "B0ABCDEF12", "description": "红色杯子"},
            headers={"Idempotency-Key": "auto"},
        ).json()
        base = f"/api/v1/projects/{project}/pipeline-runs/{run['id']}"
        started = api.post(base + "/workflow", json={"options": {"auto_decide": True}})
        assert started.status_code == 202, started.text
        view = WorkflowView.model_validate(started.json())

        if core_intent == "broad":
            assert view.status == "waiting_human", view
            assert view.waiting_for is not None
            assert view.waiting_for.gate == "core_terms"
            assert "25 万" in view.waiting_for.instructions
            assert api.get(base + "/core-selection").status_code == 404
            return

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if view.status == "failed":
                raise AssertionError(view.error)
            if view.waiting_for is not None and view.waiting_for.gate == "library_publish":
                break
            if view.waiting_for is not None and view.waiting_for.kind == "external":
                # 给后台采集线程留出执行时间，避免轮询把工作线程饿死。
                time.sleep(0.1)
                response = api.post(
                    base + f"/workflow/{view.workflow_id}/resume",
                    json={
                        "gate": view.waiting_for.gate,
                        "action": "poll",
                        "expected_revision": view.revision,
                        "payload": {},
                    },
                )
                assert response.status_code == 202, response.text
                view = WorkflowView.model_validate(response.json())
                continue
            time.sleep(0.05)
            view = WorkflowView.model_validate(
                api.get(base + f"/workflow/{view.workflow_id}").json()
            )
        assert view.waiting_for is not None
        assert view.waiting_for.gate == "library_publish", view
        statuses = {node.key: node.status for node in view.nodes}
        for key in (
            "product_confirmation",
            "root_selection",
            "week",
            "core_terms",
            "competitor_discovery",
            "competitor_selection",
            "third_party",
            "cleaning_review",
            "traffic_review",
            "create_library",
        ):
            assert statuses[key] == "done", (key, statuses[key])
        assert statuses["library_publish"] == "waiting_human"
        jobs = api.get(base + "/third-party/browser-jobs").json()
        reverse_jobs = [job for job in jobs if job["request"]["direction"] == "reverse"]
        assert reverse_jobs
        assert all(job["request"]["method"] == "traffic_extension" for job in reverse_jobs)
        assert all(job["request"]["variant_scope"] == "all" for job in reverse_jobs)


def test_access_check_pauses_root_selection_and_resumes(
    db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 详情页被访问检查拦截时必须暂停等人工处理，不能把候选记成“无有效样本”后报空结果。
    def download(_self: object, _request: object, _target: Path) -> tuple[str, bytes, int]:
        return "seller.csv", SELLER_CSV, 1

    monkeypatch.setattr(SellerspriteBrowserDownloader, "download", download)
    search = fake_search()
    search.block_products = True
    with TestClient(
        create_app(
            db,
            model_client=fake_auto_model(),
            aba_client=fake_aba_auto(tmp_path),
            search_provider=search,
            workflow_checkpointer=InMemorySaver(),
            workflow_inline=True,
            product_provider=FixedProductProvider(),
        )
    ) as api:
        project = api.post("/api/v1/projects", json={"name": "杯子", "marketplace": "US"}).json()[
            "id"
        ]
        run = api.post(
            f"/api/v1/projects/{project}/pipeline-runs",
            json={"marketplace": "US", "asin": "B0ABCDEF12", "description": "红色杯子"},
            headers={"Idempotency-Key": "blocked"},
        ).json()
        base = f"/api/v1/projects/{project}/pipeline-runs/{run['id']}"
        started = api.post(base + "/workflow", json={"options": {"auto_decide": True}})
        assert started.status_code == 202, started.text
        view = WorkflowView.model_validate(started.json())
        assert view.status == "waiting_human", view
        assert view.waiting_for is not None
        assert view.waiting_for.gate == "product_term_analysis"
        statuses = {node.key: node.status for node in view.nodes}
        assert statuses["root_selection"] == "waiting_human"

        def resume(gate: str, revision: int) -> WorkflowView:
            response = api.post(
                base + f"/workflow/{view.workflow_id}/resume",
                json={
                    "gate": gate,
                    "action": "continue",
                    "expected_revision": revision,
                    "payload": {},
                },
            )
            assert response.status_code == 202, response.text
            return WorkflowView.model_validate(response.json())

        # 访问检查还没处理完时继续运行，必须重新暂停而不是把空结果当成成功。
        view = resume("product_term_analysis", view.revision)
        assert view.status == "waiting_human", view
        assert view.waiting_for is not None
        assert view.waiting_for.gate == "product_term_analysis"

        search.block_products = False
        view = resume("product_term_analysis", view.revision)
        statuses = {node.key: node.status for node in view.nodes}
        assert statuses["root_selection"] == "done", view
        assert "PRODUCT_TERM_ANALYSIS_EMPTY" not in view.error


def qwen(content: object) -> QwenClient:
    return QwenClient(
        QwenConfig(api_key="secret"),
        transport=httpx.MockTransport(lambda _: reply(content)),
    )


def product() -> ProductUnderstanding:
    return ProductUnderstanding.model_validate(PRODUCT)


def test_decision_payloads_are_validated(db: str, engine: Engine, ) -> None:
    candidate_id = UUID("00000000-0000-0000-0000-000000000001")
    roots = [RootCandidate(id=candidate_id, text="mug", translation="杯子", reason="品类")]
    decision = qwen(
        {
            "selected": [{"candidate_id": str(candidate_id), "reason": "品类词"}],
            "reason": "匹配",
        }
    ).select_roots("US", product(), roots)
    assert decision.selected[0].candidate_id == candidate_id

    competitors = [Competitor(asin="B012345678", title="杯子", price_status="within")]
    competitor_decision = qwen(
        {
            "items": [
                {
                    "asin": "B012345678",
                    "decision": "include",
                    "reason": "同类",
                    "price_override_reason": "",
                }
            ]
        }
    ).review_competitors(product(), competitors)
    assert competitor_decision.items[0].decision == "include"

    frequencies = [
        Frequency(
            term="ceramic",
            occurrences=3,
            phrase_count=2,
            density=10.0,
            suggestions=["selling_point"],
        )
    ]
    cleaning = qwen(
        {
            "roots": [
                {
                    "term": "ceramic",
                    "category": "preference",
                    "evidence": "商品核心特征明确为陶瓷材质",
                }
            ],
            "reason": "结合已确认商品属性自动分类",
        }
    ).review_cleaning(product(), frequencies, [])
    assert cleaning.roots[0].term == "ceramic"
    assert cleaning.roots[0].category == "preference"


def test_cleaning_classification_requires_every_input_root_once(db: str, engine: Engine, ) -> None:
    frequencies = [
        Frequency(term="ceramic", occurrences=3, phrase_count=2, density=10.0, suggestions=[]),
        Frequency(term="bedroom", occurrences=1, phrase_count=1, density=3.0, suggestions=[]),
    ]
    with pytest.raises(DomainError) as error:
        qwen(
            {
                "roots": [
                    {
                        "term": "ceramic",
                        "category": "preference",
                    }
                ],
                "exclusions": [],
                "reason": "分类完成",
            }
        ).review_cleaning(product(), frequencies, [])
    assert error.value.code == "MODEL_OUTPUT_INVALID"

    traffic = qwen(
        {
            "items": [
                {
                    "keyword": "ceramic mug",
                    "decision": "keep",
                    "reason": "相关",
                    "evidence_note": "词义匹配",
                }
            ]
        }
    ).review_traffic(product(), [{"keyword": "ceramic mug", "rank": 100}])
    assert traffic.items[0].decision == "keep"


def test_root_decision_tolerates_missing_summary(db: str, engine: Engine, ) -> None:
    candidate_id = UUID(int=1)
    decision = qwen(
        {"selected": [{"candidate_id": str(candidate_id), "reason": "品类词"}]}
    ).select_roots(
        "US",
        product(),
        [RootCandidate(id=candidate_id, text="mug", translation="杯子", reason="品类")],
    )
    assert decision.selected[0].candidate_id == candidate_id
    assert decision.reason == ""


def test_invalid_decision_output_is_rejected(db: str, engine: Engine, ) -> None:
    with pytest.raises(DomainError) as error:
        qwen({"selected": []}).select_roots(
            "US",
            product(),
            [RootCandidate(id=UUID(int=1), text="mug", translation="杯子", reason="品类")],
        )
    assert error.value.code == "MODEL_OUTPUT_INVALID"
    # 报错要指出具体字段，方便判断是模型格式问题还是业务不匹配。
    assert "selected" in error.value.message


def test_cleaning_invalid_output_reports_field_without_raw_values(db: str, engine: Engine, ) -> None:
    with pytest.raises(DomainError) as error:
        qwen({"roots": [], "reason": "private-model-text"}).review_cleaning(product(), [], [])
    assert error.value.code == "MODEL_OUTPUT_INVALID"
    assert "roots" in error.value.message
    assert "private-model-text" not in error.value.message


def hot_workflow(
    db: str,
    engine: Engine,
    tmp_path: Path,
    terms: list[str],
    *,
    failing: tuple[str, ...] = (),
) -> tuple[WorkflowDeps, WorkflowState, AbaReport]:
    service, project, run, report = setup_analysis(
        db, engine, tmp_path, terms, count=1, failing=failing
    )
    app = create_app(
        db,
        storage_root=tmp_path,
        model_client=service.client,
        search_provider=service.search,
        aba_client=service.core.aba.client,
        workflow_checkpointer=InMemorySaver(),
        workflow_inline=True,
    )
    deps = cast(WorkflowDeps, app.state.workflow_service.deps)
    state = initial_state(
        workflow_id="hot-workflow",
        project_id=str(project),
        run_id=str(run),
        options={"auto_decide": True, "report_week_end": "2026-10-17"},
    )
    return deps, state, report


def apply_step(deps: WorkflowDeps, state: WorkflowState, node: str) -> dict[str, object]:
    update = build_nodes_1_2(deps)[node](state)
    state.update(update)  # type: ignore[typeddict-item]
    return update


def test_product_terms_workflow_uses_latest_report_and_saves_all_eligible(db: str, engine: Engine, tmp_path: Path) -> None:
    # Catches legacy query mode, old selected weeks, and truncating selections to ten.
    deps, state, report = hot_workflow(
        db, engine, tmp_path, [f"shoe storage {i}" for i in range(12)]
    )
    apply_step(deps, state, "root_selection")
    apply_step(deps, state, "week")
    apply_step(deps, state, "collect_core_report")
    assert state["aba_report_id"] == str(report.id)
    assert state["aba_week_end"] == report.week_end.isoformat()
    assert state.get("hot_search_core_analysis_id")
    result = apply_step(deps, state, "core_terms")
    assert result["failed_node"] is None, result
    saved = deps.core.selection(UUID(state["project_id"]), UUID(state["run_id"]))
    assert saved.rule_version == "hot-search-core-v1"
    assert len(saved.items) == 12
    assert {item.keyword for item in saved.items} == {f"shoe storage {i}" for i in range(12)}
    assert state.get("core_rule_version") == "hot-search-core-v1"


@pytest.mark.parametrize(
    ("terms", "failing", "status", "selected"),
    [
        (["shoe storage", "furniture"], ("furniture",), "incomplete", ["shoe storage"]),
        (["shoe storage"], ("shoe storage",), "failed", []),
    ],
)
def test_hot_workflow_keeps_explainable_partial_or_empty_results(
    db: str,
    engine: Engine,
    tmp_path: Path,
    terms: list[str],
    failing: tuple[str, ...],
    status: str,
    selected: list[str],
) -> None:
    # Catches saving failed analyses and discarding successful partial terms. The all-ineligible
    # terminal case is exercised through the real graph above because it now pauses for review.
    deps, state, report = hot_workflow(db, engine, tmp_path, terms, failing=failing)
    state["aba_report_id"] = str(report.id)
    state["aba_week_end"] = report.week_end.isoformat()
    apply_step(deps, state, "collect_core_report")
    result = apply_step(deps, state, "core_terms")
    assert state.get("hot_search_core_analysis_id"), result
    assert state.get("hot_search_core_analysis_status") == status
    if selected:
        saved = deps.core.selection(UUID(state["project_id"]), UUID(state["run_id"]))
        assert [item.keyword for item in saved.items] == selected
        assert result["failed_node"] is None
        assert "不完整" in str(result["node_status"])
    else:
        with pytest.raises(DomainError) as error:
            deps.core.selection(UUID(state["project_id"]), UUID(state["run_id"]))
        assert error.value.status_code == 404
        assert result["failed_node"] == "core_terms"
        assert "合格" in str(result["error"]) or "失败" in str(result["error"])


@pytest.mark.parametrize("status", ["queued", "running", "completed", "incomplete", "failed"])
def test_checkpoint_analysis_is_restored_without_starting_another_analysis(
    db: str,
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    # Catches re-issuing paid model/browser work when replaying a checkpoint.
    deps, state, report = hot_workflow(db, engine, tmp_path, ["shoe storage"])
    service = getattr(deps, "hot_search_core_analysis", None)
    assert service is not None, "WorkflowDeps must inject the real analysis service"
    project, run = UUID(state["project_id"]), UUID(state["run_id"])
    analysis = service.start(project, run, report.id)
    if status in ("completed", "incomplete"):
        analysis = service.execute(project, run, analysis.id)
    analysis.status = status
    analysis.complete = status == "completed"
    service._save(analysis)
    state.update(
        {
            "aba_report_id": str(report.id),
            "aba_week_end": report.week_end.isoformat(),
            "hot_search_core_analysis_id": str(analysis.id),
        }
    )  # type: ignore[typeddict-item]

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("checkpoint must restore this durable artifact")

    monkeypatch.setattr(service, "start", forbidden)
    monkeypatch.setattr(deps.aba, "collect", forbidden)
    if status != "queued":
        monkeypatch.setattr(service, "execute", forbidden)
    apply_step(deps, state, "collect_core_report")
    apply_step(deps, state, "core_terms")
    assert state.get("hot_search_core_analysis_id") == str(analysis.id)
    expected = "completed" if status == "queued" else status
    assert state.get("hot_search_core_analysis_status") == expected
    with deps.repository.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM hot_search_core_analyses").fetchone()[0] == 1


def test_checkpoint_analysis_pointer_restores_its_report_without_collecting(
    db: str, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches an analysis-only checkpoint issuing another upstream collection.
    deps, state, report = hot_workflow(db, engine, tmp_path, ["shoe storage"])
    project, run = UUID(state["project_id"]), UUID(state["run_id"])
    analysis = deps.hot_search_core_analysis.start(project, run, report.id)
    state["hot_search_core_analysis_id"] = str(analysis.id)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("analysis checkpoint must restore its original report")

    monkeypatch.setattr(deps.aba, "collect", forbidden)
    result = apply_step(deps, state, "collect_core_report")
    assert result["failed_node"] is None
    assert state["aba_report_id"] == str(report.id)


def test_missing_checkpoint_selection_does_not_silently_save_again(db: str, engine: Engine, tmp_path: Path) -> None:
    # Catches silently replacing a checkpoint's already-confirmed selection with a new write.
    deps, state, report = hot_workflow(db, engine, tmp_path, ["shoe storage"])
    state["aba_report_id"] = str(report.id)
    apply_step(deps, state, "collect_core_report")
    apply_step(deps, state, "core_terms")
    saved_id = state["core_selection_id"]
    with deps.repository.connect() as conn:
        conn.execute("DELETE FROM core_selections")
    result = apply_step(deps, state, "core_terms")
    assert result["failed_node"] == "core_terms"
    assert state["core_selection_id"] == saved_id
    with deps.repository.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM core_selections").fetchone()[0] == 0
