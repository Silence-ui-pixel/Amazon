"""真实 Qwen 客户端的热门词意图协议；仅替换外部 HTTP。"""

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.domain.core_terms import HotSearchCoreItem
from app.domain.errors import DomainError
from app.domain.product_terms import ProductProfile
from app.services.qwen import IntentDraftSet, QwenClient, QwenConfig

GATES = ("object_match", "purpose_match", "product_level_match", "modifiers_compatible")


def term(index: int = 1, keyword: str = "gate wheel") -> HotSearchCoreItem:
    return HotSearchCoreItem(
        candidate_id=UUID(int=index), keyword=keyword, search_frequency_rank=index
    )


def evidence(**changes: object) -> dict[str, Any]:
    value: dict[str, Any] = dict(
        object_match=True,
        purpose_match=True,
        product_level_match=True,
        modifiers_compatible=True,
        explicit_conflict=False,
        accessory_target=False,
        broad_category=False,
        category="exact",
        reasons={gate: "已确认商品为支撑大门的门轮，搜索词指向该商品" for gate in GATES},
    )
    value.update(changes)
    return value


def item(alias: str = "C1", *, brand: str = "generic", **changes: object) -> dict[str, Any]:
    return dict(candidate_id=alias, brand_category=brand, intent_evidence=evidence(**changes))


def classify(
    items: list[dict[str, Any]],
    terms: list[HotSearchCoreItem] | None = None,
    brands: list[str] | None = None,
    inspect: Callable[[dict[str, Any]], None] | None = None,
) -> IntentDraftSet:
    def provider(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if inspect:
            inspect(payload)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps({"items": items}, ensure_ascii=False)},
                    }
                ]
            },
        )

    client = QwenClient(QwenConfig(api_key="test"), transport=httpx.MockTransport(provider))
    assert callable(getattr(client, "classify_hot_search_intents", None)), (
        "缺少热门词意图客户端契约"
    )
    return client.classify_hot_search_intents(
        ProductProfile(
            purpose=["支撑大门"], style_structure=["门轮及夹具"], specifications=["4英寸，黑色"]
        ),
        terms if terms is not None else [term()],
        brands or [],
    )


def test_exact_without_search_modifiers_and_original_identity() -> None:
    draft = classify([item()])
    assert draft.items[0].candidate_id == UUID(int=1)
    assert draft.items[0].intent_evidence.category == "exact"
    assert draft.items[0].intent_evidence.modifiers_compatible is True


@pytest.mark.parametrize("gate", GATES)
def test_any_failed_gate_rejects_exact(gate: str) -> None:
    output = item()
    output["intent_evidence"][gate] = False
    with pytest.raises(DomainError) as caught:
        classify([output])
    assert caught.value.code == "MODEL_OUTPUT_INVALID"


@pytest.mark.parametrize(
    "keyword,changes,want",
    [
        (
            "gate wheel bracket",
            dict(
                object_match=False,
                product_level_match=False,
                accessory_target=True,
                category="accessory",
            ),
            "accessory",
        ),
        (
            "gate wheel installation tutorial",
            dict(
                object_match=False,
                purpose_match=False,
                product_level_match=False,
                category="unrelated",
            ),
            "unrelated",
        ),
        (
            "gate hardware",
            dict(
                object_match=False, product_level_match=False, broad_category=True, category="broad"
            ),
            "broad",
        ),
        (
            "8 inch gate wheel",
            dict(modifiers_compatible=False, explicit_conflict=True, category="unrelated"),
            "unrelated",
        ),
        (
            "8 inch gate wheel bracket",
            dict(
                object_match=False,
                product_level_match=False,
                modifiers_compatible=False,
                accessory_target=True,
                explicit_conflict=True,
                category="unrelated",
            ),
            "unrelated",
        ),
    ],
)
def test_non_exact_classification_uses_exclusion_priority(
    keyword: str,
    changes: dict[str, Any],
    want: str,
) -> None:
    draft = classify([item(**changes)], [term(keyword=keyword)])
    assert draft.items[0].intent_evidence.category == want


@pytest.mark.parametrize(
    "changes",
    [
        dict(
            object_match=False,
            product_level_match=False,
            accessory_target=True,
            category="unrelated",
        ),
        dict(
            modifiers_compatible=False,
            accessory_target=True,
            explicit_conflict=True,
            category="accessory",
        ),
        dict(object_match=False, accessory_target=True, broad_category=True, category="broad"),
        dict(broad_category=True),
    ],
)
def test_category_contradicting_facts_is_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(DomainError) as caught:
        classify([item(**changes)])
    assert caught.value.code == "MODEL_OUTPUT_INVALID"


@pytest.mark.parametrize("items", [[], [item(), item()], [item("C2")], [item("unknown")]])
def test_missing_duplicate_and_unknown_ids_are_rejected(items: list[dict[str, Any]]) -> None:
    with pytest.raises(DomainError) as caught:
        classify(items)
    assert caught.value.code == "MODEL_OUTPUT_INVALID"


@pytest.mark.parametrize("brand", ["generic", "competitor_brand", "brand_unknown"])
def test_own_brand_full_phrase_overrides_model(brand: str) -> None:
    draft = classify([item(brand=brand)], [term(keyword="ACME-Pro gate wheel")], ["acme pro"])
    assert draft.items[0].brand_category == "own_brand"


def test_brand_substring_is_not_own_brand_and_unsupported_own_brand_is_demoted() -> None:
    draft = classify(
        [item(brand="own_brand")], [term(keyword="acmeprofessional gate wheel")], ["acme pro"]
    )
    assert draft.items[0].brand_category == "brand_unknown"


@pytest.mark.parametrize(
    "field",
    [*GATES, "reasons", "explicit_conflict", "accessory_target", "broad_category", "category"],
)
def test_each_evidence_field_is_required(field: str) -> None:
    output = item()
    del output["intent_evidence"][field]
    with pytest.raises(DomainError) as caught:
        classify([output])
    assert caught.value.code == "MODEL_OUTPUT_INVALID"


def test_missing_reason_and_non_boolean_gate_are_rejected() -> None:
    for invalid in ("missing_reason", "string_bool"):
        output = item()
        if invalid == "missing_reason":
            output["intent_evidence"]["reasons"].pop("object_match")
        else:
            output["intent_evidence"]["object_match"] = "true"
        with pytest.raises(DomainError):
            classify([output])


@pytest.mark.parametrize("count", [0, 21])
def test_invalid_batch_size_rejected_before_http(count: int) -> None:
    def unexpected(payload: dict[str, Any]) -> None:
        pytest.fail("无效批次不应向上游发送请求")

    with pytest.raises(ValueError, match="1.*20"):
        classify([], [term(index + 1) for index in range(count)], inspect=unexpected)


def test_twenty_terms_supported_and_output_order_restored() -> None:
    result = classify([item(f"C{i}") for i in range(20, 0, -1)], [term(i) for i in range(1, 21)])
    assert [entry.candidate_id for entry in result.items] == [UUID(int=i) for i in range(1, 21)]


def test_http_prompt_contains_product_rules_and_strict_response_contract() -> None:
    def inspect(payload: dict[str, Any]) -> None:
        prompt = payload["messages"][0]["content"][0]["text"]
        rules, data_text = prompt.split("。数据：", 1)
        data = json.loads(data_text)
        assert data["product"]["specifications"] == ["4英寸，黑色"]
        assert data["terms"][0]["candidate_id"] == "C1"
        assert data["terms"][0]["keyword"] == "gate wheel"
        for concept in (
            "包含商品词",
            "ABA",
            "颜色相同",
            "少量相似结果",
            "modifiers_compatible=true",
            "已确认商品资料",
            "搜索词限定",
        ):
            assert concept in rules
        schema = json.loads(rules.split("JSON Schema：", 1)[1])
        contract = schema["$defs"]["ExactIntentEvidence"]
        assert set(contract["required"]) == {
            *GATES,
            "reasons",
            "category",
            "explicit_conflict",
            "accessory_target",
            "broad_category",
        }
        assert set(contract["properties"]["reasons"]["required"]) == set(GATES)
        assert payload["enable_thinking"] is False
        assert payload["response_format"] == {"type": "json_object"}

    classify([item()], inspect=inspect)
