"""候选质量、版本隔离、真实进度及无结果人工恢复。外部服务均使用固定替身。"""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from sqlalchemy import Engine
from test_product_term_analysis import KeywordSearch, build_analysis, comparison_model

from app.api.v1.product_term_analysis import build_product_term_analysis_router
from app.api.v1.roots import build_root_router
from app.domain.errors import DomainError
from app.domain.product_understanding import ProductUnderstanding, UnderstandingResult
from app.domain.roots import RootCandidatesEdit, RootDraft, RootSelectionInput
from app.services.product_term_analysis import ProductTermAnalysisService
from app.services.product_term_quality import validate_candidates
from app.services.qwen import QwenClient, QwenConfig
from app.workflows.deps import WorkflowDeps
from app.workflows.nodes_steps_1_2 import build_nodes_1_2
from app.workflows.state import WorkflowState, initial_state


def draft(text: str, translation: str = "大门轮", reason: str = "完整商品品类") -> RootDraft:
    return RootDraft(text=text, translation=translation, reason=reason)


def prepare_understanding(service: ProductTermAnalysisService, project: UUID, run: UUID) -> None:
    result = UnderstandingResult(
        project_id=project,
        run_id=run,
        model="test",
        created_at=datetime.now(UTC),
        product=ProductUnderstanding(
            product_name="Gate wheel",
            product_type="gate wheel",
            features=["supports gates"],
            uncertainties=[],
        ),
        warnings=[],
    )
    service.roots.understanding = SimpleNamespace(get=lambda *_: result)


def test_quality_rejects_broad_subject_errors_and_self_negation_but_keeps_complete_categories() -> (
    None
):
    product = ProductUnderstanding(
        product_name="三层可调哑铃架", product_type="哑铃收纳架", features=[], uncertainties=[]
    )
    good, issues = validate_candidates(
        [
            draft("rack", "架"),
            draft("fitness", "健身"),
            draft("dumbbell", "哑铃"),
            draft("equipment", "器材", "过于宽泛，不推荐单独使用"),
            draft("dumbbell rack", "哑铃架"),
            draft("dumbbell stand", "哑铃支架"),
        ],
        product,
    )
    assert [row.text for row in good] == ["dumbbell rack", "dumbbell stand"]
    assert {row.code for row in issues} == {"TOO_BROAD", "WRONG_SUBJECT", "SELF_REJECTED"}
    mug = product.model_copy(update={"product_name": "杯子", "product_type": "马克杯"})
    assert validate_candidates([draft("mug", "马克杯")], mug)[1] == []


def test_model_diagnostics_preserve_raw_output_and_quality_issues(tmp_path: Path) -> None:
    raw = json.dumps(
        {"candidates": [{"text": "fitness", "translation": "健身", "reason": "不推荐单独使用"}]}
    )
    client = QwenClient(
        QwenConfig(api_key="private-key"),
        diagnostic_dir=tmp_path,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"choices": [{"finish_reason": "stop", "message": {"content": raw}}]}
            )
        ),
    )
    result = client.recommend_roots(
        "US",
        ProductUnderstanding(
            product_name="哑铃架", product_type="收纳架", features=[], uncertainties=[]
        ),
    )
    assert not result.candidates
    assert result.raw_response == raw
    saved = json.loads(
        (tmp_path / f"product-terms-{result.diagnostic_id}.json").read_text(encoding="utf-8")
    )
    assert saved["raw"] == raw and saved["validation_issues"][0]["code"] == "SELF_REJECTED"
    assert "private-key" not in json.dumps(saved)


def test_revision_preserves_history_profile_and_unchanged_evidence(
    engine: Engine, tmp_path: Path
) -> None:
    search = KeywordSearch({"gate wheel": ["B000000010"], "gate caster": ["B000000020"]})
    project, run, candidates, service, _ = build_analysis(
        engine, tmp_path, ["gate wheel"], search, comparison_model()
    )
    prepare_understanding(service, project, run)
    old = service.start(project, run)
    profile = service.profiles.get(project, run)
    revised = service.roots.revise(
        project,
        run,
        RootCandidatesEdit(
            expected_version=1, candidates=[draft("gate wheel"), draft("gate caster")]
        ),
    )
    assert revised.version == 2 and revised.history[0].candidates == candidates
    assert revised.candidates[0].id == candidates[0].id
    assert service.profiles.get(project, run) == profile
    with pytest.raises(DomainError) as error:
        service.get(project, run)
    assert error.value.status_code == 404
    with pytest.raises(DomainError):
        service.roots.get_selection(project, run)
    result = service.start(project, run)
    assert result.id != old.id and result.candidate_version == 2
    assert search.product_calls == ["B000000010", "B000000020"]
    assert service.get(project, run, old.id) == old
    assert len(service.history(project, run)) == 2
    with pytest.raises(DomainError) as conflict:
        service.roots.revise(
            project, run, RootCandidatesEdit(expected_version=1, candidates=[draft("gate caster")])
        )
    assert conflict.value.code == "ROOT_VERSION_CONFLICT"


def test_queue_deduplicates_and_blocks_candidate_edits(engine: Engine, tmp_path: Path) -> None:
    search = KeywordSearch({"gate wheel": ["B000000010"]})
    project, run, _, service, _ = build_analysis(
        engine, tmp_path, ["gate wheel"], search, comparison_model()
    )
    prepare_understanding(service, project, run)
    first = service.enqueue(project, run)
    assert first.status == "queued"
    assert service.enqueue(project, run, refresh=True).id == first.id
    assert search.product_calls == []
    with pytest.raises(DomainError) as conflict:
        service.roots.revise(
            project, run, RootCandidatesEdit(expected_version=1, candidates=[draft("gate caster")])
        )
    assert conflict.value.code == "PRODUCT_TERM_ANALYSIS_RUNNING"
    with pytest.raises(DomainError):
        service.start(project, run)
    service.execute_queued(project, run, first.id)
    service.execute_queued(project, run, first.id)
    assert search.product_calls == ["B000000010"]
    item = service.get(project, run).items[0]
    assert item.phase == "completed"
    assert (
        item.search_pages,
        item.found_products,
        item.collected_products,
        item.scored_products,
    ) == (1, 1, 1, 1)


def test_candidate_and_background_analysis_http_contract(engine: Engine, tmp_path: Path) -> None:
    search = KeywordSearch({"gate wheel": ["B000000010"], "gate caster": ["B000000020"]})
    project, run, _, service, _ = build_analysis(engine, tmp_path, ["gate wheel"], search, comparison_model())
    prepare_understanding(service, project, run)
    app = FastAPI()
    app.include_router(build_root_router(service.roots, service))
    app.include_router(build_product_term_analysis_router(service))

    @app.exception_handler(DomainError)
    async def domain_error(_request: Request, error: DomainError) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content={"code": error.code})

    with TestClient(app) as api:
        base = f"/api/v1/projects/{project}/pipeline-runs/{run}"
        started = api.post(base + "/product-term-analyses")
        assert started.status_code == 200 and started.json()["status"] == "queued"
        old = api.get(base + "/product-term-analyses/latest").json()
        assert old["status"] == "completed"
        summary_result = api.get(base + "/product-term-analyses/latest?summary=true").json()
        assert summary_result["items"][0]["comparisons"] == []
        assert summary_result["items"][0]["collection"] is None
        assert summary_result["items"][0]["evidence_loaded"] is False
        evidence = api.get(base + "/product-term-analyses/" + old["id"] + "/candidates/" + old["items"][0]["candidate_id"]).json()
        assert evidence["evidence_loaded"] is True and len(evidence["comparisons"]) == 1
        assert evidence["collection"]["products"] == []
        assert api.get(base + "/product-term-analyses/" + old["id"]).json() == old
        revised = api.put(base + "/product-term-candidates", json={"expected_version": 1, "candidates": [draft("gate caster").model_dump()]})
        assert revised.status_code == 200 and revised.json()["version"] == 2
        assert api.get(base + "/product-term-analyses/latest").status_code == 404
        assert api.post(base + "/product-term-analyses").json()["status"] == "queued"
        assert api.get(base + "/product-term-analyses/latest").json()["candidate_version"] == 2
        assert len(api.get(base + "/product-term-analyses/history").json()) == 2
        assert len(api.get(base + "/product-term-candidates/history").json()) == 2
        assert api.get(base + "/product-term-analyses/" + old["id"]).json() == old


def test_regeneration_keeps_old_candidates_when_model_response_is_invalid(
    engine: Engine, tmp_path: Path
) -> None:
    project, run, _, service, _ = build_analysis(
        engine, tmp_path, ["gate wheel"], KeywordSearch({}), comparison_model()
    )
    prepare_understanding(service, project, run)
    old = service.roots.get(project, run)
    service.roots.client = QwenClient(
        QwenConfig(api_key="test"),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "choices": [{"finish_reason": "stop", "message": {"content": "invalid json"}}]
                },
            )
        ),
    )
    with pytest.raises(DomainError):
        service.roots.generate(project, run, expected_version=1)
    assert service.roots.get(project, run) == old
    response = json.dumps(
        {"candidates": [{"text": "gate caster", "translation": "大门轮", "reason": "完整商品类别"}]}
    )
    service.roots.client = QwenClient(
        QwenConfig(api_key="test"),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"choices": [{"finish_reason": "stop", "message": {"content": response}}]}
            )
        ),
    )
    revised = service.roots.generate(project, run, expected_version=1)
    assert revised.version == 2 and revised.history[0].candidates == old.candidates
    assert revised.raw_response == response
    assert revised.candidates[0].id != old.candidates[0].id


def test_downstream_research_cannot_be_rewritten(engine: Engine, tmp_path: Path) -> None:
    project, run, _, service, _ = build_analysis(
        engine, tmp_path, ["gate wheel"], KeywordSearch({}), comparison_model()
    )
    prepare_understanding(service, project, run)
    # 已有核心选择即封闭第一步修改边界；无需改动或删除下游记录。
    with service.repository.connect() as conn:
        conn.execute(
            "INSERT INTO core_selections(id,run_id,version,document) VALUES(?,?,?,?)",
            (str(uuid4()), str(run), 1, "{}"),
        )
    with pytest.raises(DomainError) as error:
        service.roots.revise(
            project, run, RootCandidatesEdit(expected_version=1, candidates=[draft("gate caster")])
        )
    assert error.value.code == "ROOT_DOWNSTREAM_EXISTS"


def test_human_cannot_bypass_qualification(engine: Engine, tmp_path: Path) -> None:
    search = KeywordSearch({"gate wheel": ["B000000010"]})
    project, run, candidates, service, _ = build_analysis(
        engine, tmp_path, ["gate wheel"], search, comparison_model(valid=False)
    )
    service.start(project, run)
    with pytest.raises(DomainError) as error:
        service.roots.select(project, run, RootSelectionInput(candidate_ids=[candidates[0].id]))
    assert error.value.code == "PRODUCT_TERM_VALIDATION_REQUIRED"


def test_no_qualifying_terms_interrupt_and_resume_after_revision(
    engine: Engine, tmp_path: Path
) -> None:
    search = KeywordSearch({"gate wheel": ["B000000010"], "gate caster": ["B000000020"]})
    project, run, _, service, _ = build_analysis(
        engine, tmp_path, ["gate wheel"], search, comparison_model(valid=False)
    )
    prepare_understanding(service, project, run)
    deps = cast(WorkflowDeps, SimpleNamespace(roots=service.roots, term_analysis=service))
    node = build_nodes_1_2(deps)["root_selection"]
    builder = StateGraph(WorkflowState)
    builder.add_node("root_selection", node)
    builder.add_edge(START, "root_selection")
    builder.add_edge("root_selection", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}
    state = initial_state(
        workflow_id=str(uuid4()),
        run_id=str(run),
        project_id=str(project),
        options={"auto_decide": True},
    )
    first = graph.invoke(state, config)
    assert first["__interrupt__"][0].value["gate"] == "product_term_analysis"
    assert not first["failed_node"]
    old = service.get(project, run)
    again = graph.invoke(Command(resume={}), config)
    assert "__interrupt__" in again and len(search.product_calls) == 1
    service.roots.revise(
        project, run, RootCandidatesEdit(expected_version=1, candidates=[draft("gate caster")])
    )
    service.client = comparison_model()
    done = graph.invoke(Command(resume={}), config)
    assert "__interrupt__" not in done and done["node_status"]["root_selection"]["status"] == "done"
    assert len(search.product_calls) == 2
    assert service.get(project, run, old.id) == old
