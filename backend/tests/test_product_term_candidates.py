"""新商品词候选规则与历史词根契约兼容。"""

import json

import httpx

from app.domain.product_understanding import ProductUnderstanding
from app.domain.roots import RootCandidates
from app.services.qwen import QwenClient, QwenConfig


def test_product_term_prompt_uses_searchable_minimum_phrase_without_expansions() -> None:
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
                                    "candidates": [
                                        {
                                            "text": "gate wheel",
                                            "translation": "大门轮",
                                            "reason": "可搜索到同类商品",
                                        }
                                    ]
                                }
                            )
                        },
                    }
                ]
            },
        )

    client = QwenClient(QwenConfig(api_key="test"), transport=httpx.MockTransport(model))
    result = client.recommend_roots(
        "US",
        ProductUnderstanding(
            product_name="Gate wheel",
            product_type="gate wheel",
            features=["supports gates"],
            uncertainties=[],
        ),
    )
    assert result.candidates[0].text == "gate wheel"
    assert result.candidates[0].expansions == []
    assert "能搜索到同类产品的单词或词组" in requests[0]
    assert "语义不可再拆分" not in requests[0]
    assert "expansions" not in requests[0]
    assert "storage、fitness、exercise" in requests[0]
    assert "storage、fitness" in requests[0]


def test_legacy_root_candidates_without_rule_version_still_parse() -> None:
    legacy = RootCandidates.model_validate(
        {
            "project_id": "00000000-0000-0000-0000-000000000001",
            "run_id": "00000000-0000-0000-0000-000000000002",
            "model": "legacy",
            "marketplace": "US",
            "language": "en",
            "created_at": "2026-10-29T00:00:00Z",
            "candidates": [
                {
                    "id": "00000000-0000-0000-0000-000000000003",
                    "text": "wheel",
                    "translation": "轮子",
                    "reason": "旧规则",
                    "expansions": ["gate wheel"],
                }
            ],
            "warnings": [],
        }
    )
    assert legacy.rule_version == "legacy-root-v1"
    assert legacy.candidates[0].expansions == ["gate wheel"]
