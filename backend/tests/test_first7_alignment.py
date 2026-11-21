"""当前确认价格与严格购买比口径，须在各步骤一致。"""

from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import Engine
from test_competitors import ready

from app.db.repository import Repository
from app.domain.product_terms import ProductProfile, ProductProfileRecord
from app.domain.traffic import MetricInput, RuleSet, evaluate


def test_discovery_inherits_confirmed_price_and_preserves_workspace(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    client, base = ready(db, engine, tmp_path)
    project, run = UUID(base.split("/")[4]), UUID(base.split("/")[6])
    profile = ProductProfileRecord(
        project_id=project,
        run_id=run,
        version=2,
        status="confirmed",
        profile=ProductProfile(
            purpose=["保温"],
            style_structure=["毯子"],
            specifications=["双人"],
            price=Decimal("30"),
            currency="USD",
            price_source="asin_current",
        ),
    )
    try:
        with Repository(engine, tmp_path).connect() as conn:
            conn.execute(
                "INSERT INTO product_profiles VALUES(?,?,?,?)",
                (str(run), 2, "confirmed", profile.model_dump_json()),
            )
        first = client.post(
            base + "/competitors/discover",
            json={
                "source": "manual",
                "expected_version": 0,
                "asins": ["B012345678"],
            },
        )
        assert first.status_code == 200, first.text
        assert Decimal(first.json()["reference_price"]) == Decimal("30")
        selected = client.put(
            base + "/competitors/selection",
            json={
                "expected_version": first.json()["version"],
                "reference_price": "25",
                "currency": "USD",
                "tolerance": "0.15",
                "reviews": [
                    {
                        "asin": "B012345678",
                        "decision": "include",
                        "reason": "用途一致",
                        "price_override_reason": "已人工核对当前价",
                    }
                ],
            },
        )
        assert selected.status_code == 200, selected.text
        second = client.post(
            base + "/competitors/discover",
            json={
                "source": "manual",
                "expected_version": selected.json()["version"],
                "asins": ["B087654321"],
            },
        )
        assert second.status_code == 200, second.text
        assert Decimal(second.json()["reference_price"]) == Decimal("25")
    finally:
        client.close()


def test_current_purchase_boundary_and_legacy_snapshot() -> None:
    values = MetricInput(click=Decimal("0.20"), conversion=Decimal("0.12"))
    assert RuleSet().id == "sop-traffic-v3"
    assert evaluate(values, RuleSet()).purchase is False
    assert (
        evaluate(values.model_copy(update={"conversion": Decimal("0.120001")}), RuleSet()).purchase
        is True
    )
    assert evaluate(values, RuleSet(id="sop-traffic-v2")).purchase is True
    assert evaluate(values, RuleSet(id="sop-traffic-v1")).purchase is False
