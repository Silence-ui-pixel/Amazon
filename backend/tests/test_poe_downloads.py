"""下载原件归档：校验后保存，原始字节和来源可追溯。"""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine

from app.adapters.poe_downloads import PoeArchive
from app.db.repository import Repository
from app.domain.asin_product import ProductSnapshot
from app.domain.competitors import Competitors, PoeIntentCandidate, PoeIntentChoice
from app.domain.errors import DomainError
from app.domain.product_terms import (
    BatchComparisonDraft,
    BatchComparisonSet,
    DimensionScores,
    ProductProfile,
    ProductProfileRecord,
)
from app.services.qwen import QwenClient
from app.workflows.deps import WorkflowDeps


def save_confirmed_profile(engine: Engine, tmp_path: Path, base: str) -> ProductProfileRecord:
    project_id, run_id = (UUID(part) for part in (base.split("/")[4], base.split("/")[6]))
    profile = ProductProfileRecord(
        project_id=project_id,
        run_id=run_id,
        version=1,
        status="confirmed",
        profile=ProductProfile(
            asin="B0G3GRFJYB",
            purpose=["支撑大门"],
            style_structure=["夹具与轮子"],
            specifications=["户外使用"],
            package_quantity=Decimal("1"),
            unit_basis="each",
            price=Decimal("20"),
            currency="USD",
            price_source="user_reference",
        ),
        created_at=datetime.now(UTC),
    )
    with Repository(engine, tmp_path).connect() as conn:
        conn.execute(
            "INSERT INTO product_profiles VALUES(?,?,?,?)",
            (str(run_id), 1, "confirmed", profile.model_dump_json()),
        )
    return profile


def single_intent(keyword: str) -> PoeIntentCandidate:
    return PoeIntentCandidate(
        ref=f"/opportunity-explorer/explore/niche/{keyword.replace(' ', '-')}",
        name=f"{keyword} intent",
    )


def choose_first_intent(
    _self: QwenClient,
    _profile: ProductProfile,
    _keyword: str,
    candidates: list[PoeIntentCandidate],
) -> PoeIntentChoice:
    return PoeIntentChoice(
        candidate_ref=candidates[0].ref,
        reason="与本品相对最接近",
        confidence="medium",
    )


def test_archive_keeps_original_bytes_and_manifest(tmp_path: Path) -> None:
    from app.adapters.poe_downloads import PoeDownloadStore

    raw = b"\xef\xbb\xbfASIN,Product Name\r\nB012345678,Gate wheel\r\n"
    source = tmp_path / "download.tmp"
    source.write_bytes(raw)
    run = uuid4()
    result = PoeDownloadStore(tmp_path / "archive").archive(
        source,
        run,
        "gate wheel",
        "Products.csv",
        "https://sellercentral.amazon.com/opportunity-explorer/explore",
    )
    assert result.path.read_bytes() == raw
    assert result.sha256 == hashlib.sha256(raw).hexdigest()
    assert result.items[0].asin == "B012345678"
    manifest = json.loads(result.path.with_suffix(".json").read_text(encoding="utf-8"))
    assert manifest["keyword"] == "gate wheel"
    assert manifest["run_id"] == str(run)
    assert manifest["original_filename"] == "Products.csv"
    assert manifest["sha256"] == result.sha256


@pytest.mark.parametrize(
    "raw,name",
    [
        (b"<html>sign in</html>", "Products.csv"),
        (b"ASIN,Title\nnot-an-asin,Invalid\n", "Products.csv"),
        (b"ASIN,Title\nB012345678,Gate\n", "../../Products.csv"),
        (b"ASIN,Title\nB012345678,Gate\n", "Products.exe"),
        (b"x" * 2_000_001, "Products.csv"),
    ],
    ids=["html", "asin", "path", "extension", "oversize"],
)
def test_invalid_download_never_creates_archive(tmp_path: Path, raw: bytes, name: str) -> None:
    from app.adapters.poe_downloads import PoeDownloadStore

    source = tmp_path / "download.tmp"
    source.write_bytes(raw)
    root = tmp_path / "archive"
    with pytest.raises(DomainError):
        PoeDownloadStore(root).archive(
            source,
            uuid4(),
            "gate wheel",
            name,
            "https://sellercentral.amazon.com/opportunity-explorer/explore",
        )
    assert not list(root.rglob("*.csv"))
    assert not list(root.rglob("*.json"))


def test_real_poe_preamble_and_360_day_currency_header() -> None:
    from app.adapters.competitor_csv import parse_competitor_csv

    text = (
        "细分市场名称: gate wheel\n细分市场详情 - “商品” 选项卡\n"
        "上次更新日期：10/17/2026\n\n"
        '"商品名称","ASIN","平均售价（过去 360 天内）(USD)"\n'
        '"Gate wheel","B012345678","18.49"\n'
    )
    items = parse_competitor_csv(text, "真实导出.csv")
    assert str(items[0].price) == "18.49"
    assert items[0].currency == "USD"
    assert items[0].price_basis == "average_360d"


def test_poe_30_day_average_is_explicit_and_automatic_mode_rejects_other_periods() -> None:
    """把 30 天列误认成 90/360 天会让价格预筛使用错误口径。"""
    from app.adapters.competitor_csv import parse_competitor_csv

    text = (
        'ASIN,Product Name,Average Selling Price (Past 30 Days) (USD)\n'
        'B012345678,Gate wheel,23.00\n'
    )
    items = parse_competitor_csv(text, "筛选竞对ASIN<gate wheel>.csv", require_30_day=True)
    assert items[0].price == Decimal("23.00")
    assert items[0].currency == "USD"
    assert items[0].price_basis == "average_30d"

    with pytest.raises(DomainError) as error:
        parse_competitor_csv(
            "ASIN,Average Selling Price (Past 90 Days) (USD)\nB012345678,23.00\n",
            "wrong-period.csv",
            require_30_day=True,
        )
    assert error.value.code == "COMPETITOR_CSV_30_DAY_REQUIRED"


def test_poe_intent_rows_keep_page_reference_keywords_and_metrics() -> None:
    """页面提取器若接受越域链接或丢掉意图证据，模型会在错误范围内选择。"""
    from app.adapters.poe_downloads import parse_poe_intent_rows

    result = parse_poe_intent_rows(
        [
            {
                "ref": "/opportunity-explorer/explore/niche/niche-123",
                "name": "adjustable dumbbell stand",
                "top_keywords": ["adjustable dumbbell stand", "powerblock stand"],
                "metrics": {"搜索量": "19,565", "购买率": "5.86%"},
            }
        ]
    )
    assert result[0].ref == "/opportunity-explorer/explore/niche/niche-123"
    assert result[0].top_keywords == ["adjustable dumbbell stand", "powerblock stand"]
    assert result[0].metrics == {"搜索量": "19,565", "购买率": "5.86%"}

    with pytest.raises(DomainError) as error:
        parse_poe_intent_rows(
            [{"ref": "https://example.com/niche/evil", "name": "evil", "top_keywords": []}]
        )
    assert error.value.code == "POE_INTENT_RESULTS_INVALID"


def test_business_filename_sanitizes_keyword_without_losing_identity() -> None:
    from app.adapters.poe_downloads import competitor_export_filename

    assert competitor_export_filename("dumbbell rack") == "筛选竞对ASIN-dumbbell rack.csv"
    assert competitor_export_filename('rack: 5/50 <US>') == "筛选竞对ASIN-rack_ 5_50 _US_.csv"


def test_conflicting_header_currency_is_rejected() -> None:
    from app.adapters.competitor_csv import parse_competitor_csv

    with pytest.raises(DomainError):
        parse_competitor_csv(
            "ASIN,Average Selling Price (Past 360 Days) (USD),Currency\nB012345678,18.49,EUR\n",
            "conflict.csv",
        )


def test_download_import_and_original_are_scoped_and_versioned(
    db: str, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_competitors import ready

    from app.adapters.poe_downloads import PoeBrowserDownloader

    client, base = ready(db, engine, tmp_path)
    save_confirmed_profile(engine, tmp_path, base)
    calls: list[str] = []

    def intents(_self: PoeBrowserDownloader, keyword: str) -> list[PoeIntentCandidate]:
        return [single_intent(keyword)]

    def download(
        self: PoeBrowserDownloader, run_id: UUID, keyword: str, intent_ref: str
    ) -> PoeArchive:
        assert intent_ref == single_intent(keyword).ref
        source = tmp_path / "browser.csv"
        source.write_bytes(b"ASIN,Product Name\nB012345678,Gate wheel\n")
        calls.append(keyword)
        return self.store.archive(
            source,
            UUID(str(run_id)),
            keyword,
            "Products.csv",
            "https://sellercentral.amazon.com/opportunity-explorer/explore",
        )

    monkeypatch.setattr(PoeBrowserDownloader, "intents", intents)
    monkeypatch.setattr(PoeBrowserDownloader, "download", download)
    monkeypatch.setattr(QwenClient, "select_poe_intent", choose_first_intent)
    try:
        keyword = client.get(base + "/core-selection").json()["items"][0]["keyword"]
        endpoint = base + "/competitors/poe-download"
        assert (
            client.post(endpoint, json={"keyword": "invalid", "expected_version": 0}).status_code
            == 422
        )
        assert not calls
        response = client.post(endpoint, json={"keyword": keyword, "expected_version": 0})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["version"] == 1 and data["confirmed_at"] is None
        assert len(data["items"]) == 1 and data["items"][0]["decision"] == "pending"
        artifact = data["poe_downloads"][0]
        original = base + "/competitors/poe-downloads/" + artifact["id"]
        assert client.get(original).content == b"ASIN,Product Name\nB012345678,Gate wheel\n"
        assert client.get(original.replace(artifact["id"], str(uuid4()))).status_code == 404
        assert (
            client.post(endpoint, json={"keyword": keyword, "expected_version": 0}).status_code
            == 409
        )
        assert len(calls) == 1
        assert client.get(base + "/competitors").json() == data
    finally:
        client.close()


def test_failed_browser_download_preserves_candidates(
    db: str, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_competitors import ready

    from app.adapters.poe_downloads import PoeBrowserDownloader

    client, base = ready(db, engine, tmp_path)
    save_confirmed_profile(engine, tmp_path, base)

    def intents(_self: PoeBrowserDownloader, keyword: str) -> list[PoeIntentCandidate]:
        return [single_intent(keyword)]

    def fail(
        self: PoeBrowserDownloader, run_id: UUID, keyword: str, intent_ref: str
    ) -> PoeArchive:
        raise DomainError(504, "POE_DOWNLOAD_TIMEOUT", "下载超时")

    monkeypatch.setattr(PoeBrowserDownloader, "intents", intents)
    monkeypatch.setattr(PoeBrowserDownloader, "download", fail)
    monkeypatch.setattr(QwenClient, "select_poe_intent", choose_first_intent)
    try:
        url = base + "/competitors"
        before = client.post(
            url + "/discover",
            json={
                "source": "manual",
                "asins": ["B012345678"],
                "expected_version": 0,
            },
        ).json()
        keyword = client.get(base + "/core-selection").json()["items"][0]["keyword"]
        assert (
            client.post(
                url + "/poe-download",
                json={
                    "keyword": keyword,
                    "expected_version": 1,
                },
            ).status_code
            == 504
        )
        assert client.get(url).json() == before
    finally:
        client.close()


def test_batch_download_requires_confirmed_four_dimension_profile(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    from test_competitors import ready

    client, base = ready(db, engine, tmp_path)
    try:
        response = client.post(
            base + "/competitors/poe-download-all",
            json={"expected_version": 0},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PRODUCT_PROFILE_REQUIRED"
    finally:
        client.close()


def test_batch_failure_keeps_stage_and_selected_intent(
    db: str, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_competitors import ready

    from app.adapters.poe_downloads import PoeBrowserDownloader

    client, base = ready(db, engine, tmp_path)
    save_confirmed_profile(engine, tmp_path, base)

    def intents(_self: PoeBrowserDownloader, keyword: str) -> list[PoeIntentCandidate]:
        return [single_intent(keyword)]

    def fail(
        _self: PoeBrowserDownloader, _run_id: UUID, _keyword: str, _intent_ref: str
    ) -> PoeArchive:
        raise DomainError(504, "POE_DOWNLOAD_TIMEOUT", "下载超时")

    monkeypatch.setattr(PoeBrowserDownloader, "intents", intents)
    monkeypatch.setattr(PoeBrowserDownloader, "download", fail)
    monkeypatch.setattr(QwenClient, "select_poe_intent", choose_first_intent)
    try:
        response = client.post(
            base + "/competitors/poe-download-all",
            json={"expected_version": 0, "retry_keywords": []},
        )
        assert response.status_code == 200, response.text
        failures = response.json()["poe_downloads"]
        assert failures and all(item["status"] == "failed" for item in failures)
        assert all(item["stage"] == "download" for item in failures)
        assert all(item["intent_name"] and item["intent_reason"] for item in failures)
        assert response.json()["confirmed_at"] is None
    finally:
        client.close()


def test_retry_one_failed_keyword_reuses_completed_archives(
    db: str, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_competitors import ready, replace_core_keywords

    from app.adapters.poe_downloads import PoeBrowserDownloader

    client, base = ready(db, engine, tmp_path)
    replace_core_keywords(engine, tmp_path, ["core one", "core two"])
    save_confirmed_profile(engine, tmp_path, base)
    calls: list[str] = []

    def intents(_self: PoeBrowserDownloader, keyword: str) -> list[PoeIntentCandidate]:
        return [single_intent(keyword)]

    def download(
        self: PoeBrowserDownloader, target: UUID, keyword: str, intent_ref: str
    ) -> PoeArchive:
        calls.append(keyword)
        if keyword == "core two" and calls.count(keyword) == 1:
            raise DomainError(504, "POE_DOWNLOAD_TIMEOUT", "下载超时")
        source = tmp_path / f"{keyword}-{calls.count(keyword)}.csv"
        asin = "B000000001" if keyword == "core one" else "B000000002"
        source.write_text(
            "ASIN,Product Name,Average Selling Price (Past 30 Days),Currency\n"
            f"{asin},Item,20,USD\n",
            encoding="utf-8",
        )
        return self.store.archive(
            source,
            target,
            keyword,
            f"筛选竞对ASIN-{keyword}.csv",
            f"https://sellercentral.amazon.com{intent_ref}/product",
        )

    def fetch(_self: object, _marketplace: str, asin: str) -> ProductSnapshot:
        return ProductSnapshot(asin=asin, title=f"Item {asin}")

    def compare(
        _self: QwenClient, _reference: ProductProfile, products: list[ProductSnapshot]
    ) -> BatchComparisonSet:
        return BatchComparisonSet(
            items=[
                BatchComparisonDraft(
                    asin=item.asin,
                    purpose=["支撑大门"],
                    style_structure=["夹具与轮子"],
                    specifications=["户外使用"],
                    scores=DimensionScores(
                        purpose=80,
                        style_structure=80,
                        specifications=80,
                        purpose_reason="用途",
                        style_structure_reason="结构",
                        specifications_reason="规格",
                    ),
                )
                for item in products
            ]
        )

    monkeypatch.setattr(PoeBrowserDownloader, "intents", intents)
    monkeypatch.setattr(PoeBrowserDownloader, "download", download)
    monkeypatch.setattr(QwenClient, "select_poe_intent", choose_first_intent)
    monkeypatch.setattr("app.services.asin_product.AmazonProductProvider.fetch", fetch)
    monkeypatch.setattr(QwenClient, "compare_product_batch", compare)
    try:
        endpoint = base + "/competitors/poe-download-all"
        first = client.post(endpoint, json={"expected_version": 0, "retry_keywords": []})
        assert first.status_code == 200, first.text
        assert first.json()["confirmed_at"] is None
        assert [item["status"] for item in first.json()["poe_downloads"]] == [
            "completed",
            "failed",
        ]

        retried = client.post(
            endpoint,
            json={"expected_version": 1, "retry_keywords": ["core two"]},
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["confirmed_at"] is not None
        assert all(item["status"] == "completed" for item in retried.json()["poe_downloads"])
        assert calls == ["core one", "core two", "core two"]
    finally:
        client.close()


def test_batch_download_auto_confirms_qualified_asins_for_reverse_expansion(
    db: str, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """若生产代码遗漏自动 decision/confirmed_at，反向拓词上下文必须抓住回归。"""
    from test_competitors import ready

    from app.adapters.poe_downloads import PoeBrowserDownloader

    client, base = ready(db, engine, tmp_path)
    project_id, run_id = (UUID(part) for part in (base.split("/")[4], base.split("/")[6]))
    profile = ProductProfileRecord(
        project_id=project_id,
        run_id=run_id,
        version=1,
        status="confirmed",
        profile=ProductProfile(
            asin="B0G3GRFJYB",
            purpose=["支撑大门"],
            style_structure=["夹具与轮子"],
            specifications=["户外使用"],
            package_quantity=Decimal("1"),
            unit_basis="each",
            price=Decimal("20"),
            currency="USD",
            price_source="asin_current",
        ),
        created_at=datetime.now(UTC),
    )
    with Repository(engine, tmp_path).connect() as conn:
        conn.execute(
            "INSERT INTO product_profiles VALUES(?,?,?,?)",
            (str(run_id), 1, "confirmed", profile.model_dump_json()),
        )

    def intents(self: PoeBrowserDownloader, keyword: str) -> list[PoeIntentCandidate]:
        return [
            PoeIntentCandidate(
                ref=f"/opportunity-explorer/explore/niche/{keyword.replace(' ', '-')}",
                name=f"{keyword} intent",
            )
        ]

    def choose(
        _self: QwenClient,
        _profile: ProductProfile,
        _keyword: str,
        choices: list[PoeIntentCandidate],
    ) -> PoeIntentChoice:
        return PoeIntentChoice(
            candidate_ref=choices[0].ref,
            reason="与本品用途最接近",
            confidence="high",
        )

    def download(
        self: PoeBrowserDownloader, target: UUID, keyword: str, intent_ref: str
    ) -> PoeArchive:
        assert intent_ref.endswith(keyword.replace(" ", "-"))
        source = tmp_path / f"{keyword}.csv"
        rows = "".join(f"B{index:09d},Gate wheel,21\n" for index in range(101))
        source.write_text(
            "ASIN,Product Name,Average Selling Price (Past 30 Days) (USD)\n" + rows,
            encoding="utf-8",
        )
        return self.store.archive(
            source,
            target,
            keyword,
            f"筛选竞对ASIN-{keyword}.csv",
            "https://sellercentral.amazon.com/opportunity-explorer/explore",
        )

    def fetch(_self: object, _marketplace: str, asin: str) -> ProductSnapshot:
        return ProductSnapshot(
            asin=asin,
            title="Gate wheel",
            price=Decimal("21"),
            currency="USD",
            price_reliable=True,
        )

    def compare(
        _self: QwenClient, _reference: ProductProfile, products: list[ProductSnapshot]
    ) -> BatchComparisonSet:
        return BatchComparisonSet(
            items=[
                BatchComparisonDraft(
                    asin=item.asin,
                    purpose=["支撑大门"],
                    style_structure=["夹具与轮子"],
                    specifications=["户外使用"],
                    package_quantity=Decimal("1"),
                    unit_basis="each",
                    scores=DimensionScores(
                        purpose=90,
                        style_structure=90,
                        specifications=90,
                        purpose_reason="用途一致",
                        style_structure_reason="结构一致",
                        specifications_reason="规格一致",
                    ),
                )
                for item in products
            ]
        )

    monkeypatch.setattr(PoeBrowserDownloader, "intents", intents)
    monkeypatch.setattr(PoeBrowserDownloader, "download", download)
    monkeypatch.setattr(QwenClient, "select_poe_intent", choose)
    monkeypatch.setattr("app.services.asin_product.AmazonProductProvider.fetch", fetch)
    monkeypatch.setattr(QwenClient, "compare_product_batch", compare)
    try:
        response = client.post(
            base + "/competitors/poe-download-all",
            json={"expected_version": 0},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["confirmed_at"] is not None
        assert result["reference_price"] == "20"
        assert len(result["items"]) == 100
        assert [item["asin"] for item in result["items"][:2]] == ["B000000000", "B000000001"]
        assert all(item["decision"] == "include" for item in result["items"])
        assert all(item["reason"] for item in result["items"])

        deps = cast(
            WorkflowDeps,
            cast(FastAPI, client.app).state.workflow_service.deps,
        )
        assert len(deps.expansion.context(project_id, run_id, "reverse")[2]) == 100
    finally:
        client.close()


def test_batch_filters_30_day_price_before_details_and_deduplicates_cross_keyword_asins(
    db: str, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """价格预筛顺序、边界和相关度阈值不能被后续优化打乱。"""
    from test_competitors import ready, replace_core_keywords

    from app.adapters.poe_downloads import PoeBrowserDownloader

    client, base = ready(db, engine, tmp_path)
    project_id, run_id = (UUID(part) for part in (base.split("/")[4], base.split("/")[6]))
    replace_core_keywords(engine, tmp_path, ["core one", "core two"])
    profile = ProductProfileRecord(
        project_id=project_id,
        run_id=run_id,
        version=1,
        status="confirmed",
        profile=ProductProfile(
            asin="B0G3GRFJYB",
            purpose=["哑铃收纳"],
            style_structure=["落地支架"],
            specifications=["可调节"],
            package_quantity=Decimal("1"),
            unit_basis="each",
            price=Decimal("100"),
            currency="USD",
            price_source="user_reference",
        ),
    )
    with Repository(engine, tmp_path).connect() as conn:
        conn.execute(
            "INSERT INTO product_profiles VALUES(?,?,?,?)",
            (str(run_id), 1, "confirmed", profile.model_dump_json()),
        )
        core_row = conn.execute(
            "SELECT id FROM core_selections WHERE run_id=? ORDER BY version DESC LIMIT 1",
            (str(run_id),),
        ).fetchone()
        assert core_row is not None
        previous = Competitors(
            id=uuid4(),
            project_id=project_id,
            run_id=run_id,
            core_selection_id=UUID(core_row[0]),
            core_keywords=["core one", "core two"],
            version=1,
            created_at=datetime.now(UTC),
            reference_price=Decimal("100"),
            currency="USD",
            tolerance=Decimal("0.50"),
        )
        conn.execute(
            "INSERT INTO competitor_versions (id,run_id,version,document) VALUES(?,?,?,?)",
            (str(previous.id), str(run_id), 1, previous.model_dump_json()),
        )

    rows = {
        "core one": [
            ("B000000001", "85.00", "USD"),
            ("B000000002", "84.99", "USD"),
            ("B000000003", "115.00", "USD"),
            ("B000000004", "115.01", "USD"),
            ("B000000005", "100.00", "EUR"),
            ("B000000006", "100.00", "USD"),
        ],
        "core two": [
            ("B000000001", "85.00", "USD"),
            ("B000000007", "100.00", "USD"),
        ],
    }

    def intents(_self: PoeBrowserDownloader, keyword: str) -> list[PoeIntentCandidate]:
        return [
            PoeIntentCandidate(
                ref=f"/opportunity-explorer/explore/niche/{keyword.replace(' ', '-')}",
                name=f"{keyword} intent",
            )
        ]

    def choose(
        _self: QwenClient,
        _profile: ProductProfile,
        _keyword: str,
        candidates: list[PoeIntentCandidate],
    ) -> PoeIntentChoice:
        return PoeIntentChoice(
            candidate_ref=candidates[0].ref,
            reason="相对最相关",
            confidence="medium",
        )

    def download(
        _self: PoeBrowserDownloader,
        target: UUID,
        keyword: str,
        intent_ref: str,
    ) -> PoeArchive:
        source = tmp_path / f"{keyword}.csv"
        source.write_text(
            "ASIN,Product Name,Average Selling Price (Past 30 Days),Currency\n"
            + "".join(f"{asin},Item,{price},{currency}\n" for asin, price, currency in rows[keyword]),
            encoding="utf-8",
        )
        return _self.store.archive(
            source,
            target,
            keyword,
            f"筛选竞对ASIN-{keyword}.csv",
            f"https://sellercentral.amazon.com{intent_ref}/product",
        )

    fetched: list[str] = []

    def fetch(_self: object, _marketplace: str, asin: str) -> ProductSnapshot:
        fetched.append(asin)
        return ProductSnapshot(asin=asin, title=f"Item {asin}", price_reliable=False)

    scores = {
        "B000000001": 50,
        "B000000003": 51,
        "B000000006": 80,
        "B000000007": 70,
    }

    def compare(
        _self: QwenClient,
        _reference: ProductProfile,
        products: list[ProductSnapshot],
    ) -> BatchComparisonSet:
        return BatchComparisonSet(
            items=[
                BatchComparisonDraft(
                    asin=item.asin,
                    purpose=["哑铃收纳"],
                    style_structure=["落地支架"],
                    specifications=["可调节"],
                    scores=DimensionScores(
                        purpose=scores[item.asin],
                        style_structure=scores[item.asin],
                        specifications=scores[item.asin],
                        purpose_reason="用途",
                        style_structure_reason="结构",
                        specifications_reason="规格",
                    ),
                )
                for item in products
            ]
        )

    monkeypatch.setattr(PoeBrowserDownloader, "intents", intents)
    monkeypatch.setattr(PoeBrowserDownloader, "download", download)
    monkeypatch.setattr(QwenClient, "select_poe_intent", choose)
    monkeypatch.setattr("app.services.asin_product.AmazonProductProvider.fetch", fetch)
    monkeypatch.setattr(QwenClient, "compare_product_batch", compare)
    try:
        response = client.post(
            base + "/competitors/poe-download-all", json={"expected_version": 1}
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["tolerance"] == "0.15"
        assert fetched == ["B000000001", "B000000003", "B000000006", "B000000007"]
        assert [item["asin"] for item in result["items"]] == [
            "B000000006",
            "B000000007",
            "B000000003",
        ]
        assert all(item["price_basis"] == "average_30d" for item in result["items"])
        assert len(result["poe_downloads"]) == 2
        assert result["poe_downloads"][0]["price_qualified_rows"] == 3
        assert result["poe_downloads"][1]["price_qualified_rows"] == 2
        assert result["poe_downloads"][0]["intent_name"] == "core one intent"
        assert result["confirmed_at"] is not None
    finally:
        client.close()
