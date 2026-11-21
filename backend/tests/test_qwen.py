"""百炼适配器：图片字节、结构化输出及敏感错误不得泄露。"""

import json
from pathlib import Path

import httpx
import pytest

from app.domain.competitors import PoeIntentCandidate
from app.domain.errors import DomainError
from app.domain.product_terms import ProductProfile
from app.services.qwen import QwenClient, QwenConfig

RESULT = {
    "product_name": "哑铃架",
    "product_type": "健身器材收纳架",
    "features": ["多层结构"],
    "uncertainties": ["尺寸未知"],
}


def test_blank_model_environment_keeps_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_MODEL", "")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret")
    assert QwenConfig.load(tmp_path).model == "qwen3.8-max"


def test_csv_key_and_workspace_endpoint(tmp_path: Path) -> None:
    csv = tmp_path / "key.csv"
    csv.write_text(
        "id,example\napiKey,sk-new.format-token-value\nopenAiCompatible,https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1\n",
        encoding="utf-8",
    )
    config = QwenConfig.from_key_file(csv)
    assert config.model == "qwen3.8-max"
    assert config.base_url == "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    assert config.api_key == "sk-new.format-token-value"
    assert config.api_key not in repr(config)


def test_uploaded_bytes_and_validated_result() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "qwen3.8-max"
        assert body["enable_thinking"] is False
        assert (
            body["messages"][0]["content"][1]["image_url"]["url"]
            == "data:image/png;base64,aW1hZ2U="
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(RESULT)}}]
            },
        )

    client = QwenClient(QwenConfig(api_key="secret"), transport=httpx.MockTransport(handle))
    assert (
        client.understand("US", "描述", [("image/png", b"image")]).product_type
        == RESULT["product_type"]
    )


@pytest.mark.parametrize("status", [401, 429, 500])
def test_provider_error_is_sanitized(status: int) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(status, text="secret-key-and-private-body")
    )
    with pytest.raises(DomainError) as error:
        QwenClient(QwenConfig(api_key="secret"), transport=transport).understand("US", "描述", [])
    assert "secret" not in error.value.message
    assert error.value.status_code in (502, 503)


def test_invalid_output_and_timeout() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
        )
    )
    with pytest.raises(DomainError, match="结构"):
        QwenClient(QwenConfig(api_key="secret"), transport=transport).understand("US", "描述", [])

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret", request=request)

    with pytest.raises(DomainError) as error:
        QwenClient(QwenConfig(api_key="secret"), transport=httpx.MockTransport(timeout)).understand(
            "US", "描述", []
        )
    assert error.value.code == "MODEL_OUTCOME_UNKNOWN"


def test_credential_never_sent_to_untrusted_host() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    with pytest.raises(DomainError) as error:
        QwenClient(
            QwenConfig(api_key="secret", base_url="https://example.com/compatible-mode/v1"),
            transport=httpx.MockTransport(handle),
        ).understand("US", "杯子", [])
    assert error.value.code == "MODEL_CONFIG_INVALID"
    assert calls == []


def test_select_poe_intent_returns_exactly_one_candidate_even_when_confidence_is_low() -> None:
    """模型不能返回空选择，也不能编造页面中不存在的购买意图。"""
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"][0]["text"]
        assert "即使整体匹配度低" in prompt
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "candidate_ref": "I2",
                                    "reason": "用途相对最接近",
                                    "confidence": "low",
                                }
                            )
                        },
                    }
                ]
            },
        )

    profile = ProductProfile(
        purpose=["收纳哑铃"],
        style_structure=["落地支架"],
        specifications=["可调节"],
    )
    candidates = [
        PoeIntentCandidate(ref="niche-a", name="kettlebell rack"),
        PoeIntentCandidate(ref="niche-b", name="adjustable dumbbell stand"),
    ]
    client = QwenClient(QwenConfig(api_key="secret"), transport=httpx.MockTransport(handle))
    choice = client.select_poe_intent(profile, "dumbbell rack", candidates)
    assert choice.candidate_ref == "niche-b"
    assert choice.confidence == "low"


def test_select_poe_intent_rejects_reference_outside_page_candidates() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "candidate_ref": "I9",
                                    "reason": "编造候选",
                                    "confidence": "high",
                                }
                            )
                        },
                    }
                ]
            },
        )
    )
    client = QwenClient(QwenConfig(api_key="secret"), transport=transport)
    with pytest.raises(DomainError) as error:
        client.select_poe_intent(
            ProductProfile(purpose=["收纳"], style_structure=["支架"], specifications=["家用"]),
            "rack",
            [PoeIntentCandidate(ref="niche-a", name="rack")],
        )
    assert error.value.code == "MODEL_OUTPUT_INVALID"
