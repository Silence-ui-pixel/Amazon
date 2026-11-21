"""本品四维基准生成、价格来源和版本确认。"""

import json
from decimal import Decimal
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.domain.asin_product import ProductSnapshot
from app.domain.product_terms import ProductProfile
from app.main import create_app
from app.services.asin_product import parse_product_snapshot
from app.services.product_profiles import resolve_profile_price
from app.services.qwen import QwenClient, QwenConfig


def profile(price: str | None = None) -> ProductProfile:
    return ProductProfile(
        purpose=["支撑大门"],
        style_structure=["夹具与轮子"],
        specifications=["户外使用"],
        price=Decimal(price) if price else None,
        currency="USD" if price else None,
        package_quantity=Decimal("1"),
        unit_basis="each",
    )


def snapshot(price: str | None, *, reliable: bool = True) -> ProductSnapshot:
    return ProductSnapshot(
        asin="B000000001",
        title="Gate wheel",
        price=Decimal(price) if price else None,
        currency="USD" if price else None,
        price_reliable=reliable,
    )


def test_reliable_asin_price_overrides_user_reference(db: str, engine: Engine, ) -> None:
    result = resolve_profile_price(profile(), snapshot("30"), Decimal("20"), "USD")
    assert result.price == Decimal("30")
    assert result.currency == "USD"
    assert result.price_source == "asin_current"


def test_unreliable_or_missing_asin_price_uses_confirmed_reference(db: str, engine: Engine, ) -> None:
    unreliable = resolve_profile_price(profile(), snapshot("30", reliable=False), Decimal("20"), "USD")
    missing = resolve_profile_price(profile(), snapshot(None), Decimal("20"), "USD")
    assert unreliable.price == missing.price == Decimal("20")
    assert unreliable.price_source == missing.price_source == "user_reference"


def test_pure_image_requires_reference_price(db: str, engine: Engine, ) -> None:
    try:
        resolve_profile_price(profile(), None, None, "USD")
    except ValueError as error:
        assert "参考售价" in str(error)
    else:
        raise AssertionError("纯图片缺价应失败")


def test_profile_requires_all_non_price_dimensions(db: str, engine: Engine, ) -> None:
    try:
        ProductProfile(
            purpose=[],
            style_structure=["夹具"],
            specifications=["户外"],
            price=Decimal("20"),
            currency="USD",
            package_quantity=Decimal("1"),
            unit_basis="each",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("用途缺失不应形成四维基准")


def test_subscription_coupon_and_range_prices_are_not_reliable(db: str, engine: Engine, ) -> None:
    for offer_kind in ("subscription", "coupon", "range"):
        value = parse_product_snapshot(
            {
                "asin": "B000000001",
                "title": "Gate wheel",
                "price_text": "$20.00",
                "currency": "USD",
                "offer_kind": offer_kind,
            },
            "B000000001",
        )
        assert value.price == Decimal("20")
        assert value.price_reliable is False


class FixedProductProvider:
    def fetch(self, marketplace: str, asin: str) -> ProductSnapshot:
        return snapshot("30")


def test_profile_api_generates_once_and_confirms_new_version(db: str, engine: Engine, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def model(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        content = (
            {
                "product_name": "Gate wheel",
                "product_type": "gate wheel",
                "features": ["supports a gate"],
                "uncertainties": [],
            }
            if len(calls) == 1
            else {
                "purpose": ["支撑大门"],
                "style_structure": ["夹具与轮子"],
                "specifications": ["户外使用"],
                "package_quantity": "1",
                "unit_basis": "each",
            }
        )
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]},
        )

    client = QwenClient(QwenConfig(api_key="test"), transport=httpx.MockTransport(model))
    with TestClient(
        create_app(
            db,
            storage_root=tmp_path,
            model_client=client,
            product_provider=FixedProductProvider(),
        )
    ) as api:
        project = api.post(
            "/api/v1/projects", json={"name": "四维", "marketplace": "US"}
        ).json()["id"]
        run = api.post(
            f"/api/v1/projects/{project}/pipeline-runs",
            headers={"Idempotency-Key": "profile"},
            json={"marketplace": "US", "asin": "B000000001"},
        ).json()
        base = f"/api/v1/projects/{project}/pipeline-runs/{run['id']}"
        assert api.post(base + "/product-understanding").status_code == 200
        confirmation = {
            "expected_version": 0,
            "product": {
                "product_name": "Gate wheel",
                "product_type": "gate wheel",
                "features": ["supports a gate"],
                "uncertainties": [],
            },
            "reviewer": "运营",
            "reason": "确认",
        }
        assert api.put(base + "/product-confirmation", json=confirmation).status_code == 200
        draft = api.post(base + "/product-profile")
        assert draft.status_code == 200, draft.text
        assert draft.json()["profile"]["price"] == "30"
        assert api.post(base + "/product-profile").json() == draft.json()
        assert len(calls) == 2
        confirmed = api.put(
            base + "/product-profile",
            json={
                "expected_version": 1,
                "purpose": ["支撑大门"],
                "style_structure": ["夹具与轮子"],
                "specifications": ["户外使用"],
                "package_quantity": "1",
                "unit_basis": "each",
                "currency": "USD",
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["version"] == 2
        assert confirmed.json()["status"] == "confirmed"
        assert len(api.get(base + "/product-profile/history").json()) == 2


def test_profile_prompt_does_not_ask_model_to_estimate_price(db: str, engine: Engine, ) -> None:
    requests: list[str] = []

    def model(request: httpx.Request) -> httpx.Response:
        requests.append(request.content.decode())
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "purpose": ["支撑大门"],
                                    "style_structure": ["夹具与轮子"],
                                    "specifications": ["户外使用"],
                                    "package_quantity": "1",
                                    "unit_basis": "each",
                                }
                            )
                        },
                    }
                ]
            },
        )

    client = QwenClient(QwenConfig(api_key="test"), transport=httpx.MockTransport(model))
    from app.domain.product_understanding import ProductUnderstanding

    result = client.extract_product_profile(
        ProductUnderstanding(
            product_name="Gate wheel",
            product_type="gate wheel",
            features=["supports a gate"],
            uncertainties=[],
        ),
        snapshot("30"),
    )
    assert result.price is None
    assert '"price"' not in requests[0]
