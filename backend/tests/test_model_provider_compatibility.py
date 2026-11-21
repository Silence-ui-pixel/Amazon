"""模型供应商兼容边界：只放行明确端点，并按供应商发送兼容参数。"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from app.domain.errors import DomainError
from app.domain.product_understanding import ProductUnderstanding, UnderstandingResult
from app.domain.roots import RootCandidate, RootCandidates
from app.services.qwen import QwenClient, QwenConfig

RESULT = {
    "product_name": "文件图标",
    "product_type": "图片",
    "features": ["包含文件名"],
    "uncertainties": [],
}


def test_new_model_records_use_mimo_provider_and_accept_legacy_bailian() -> None:
    """若新结果仍标成百炼，或历史记录因迁移不可读，本测试应失败。"""
    now = datetime.now(UTC)
    project_id = uuid4()
    run_id = uuid4()
    understanding = UnderstandingResult(
        project_id=project_id,
        run_id=run_id,
        model="openai/mimo-v2.6-pro",
        created_at=now,
        product=ProductUnderstanding.model_validate(RESULT),
        warnings=[],
    )
    roots = RootCandidates(
        project_id=project_id,
        run_id=run_id,
        model="openai/mimo-v2.6-pro",
        marketplace="US",
        language="en",
        created_at=now,
        candidates=[
            RootCandidate(
                id=uuid4(), text="text file", translation="文本文件", reason="商品类型"
            )
        ],
        warnings=[],
    )
    assert understanding.provider == "mimo"
    assert roots.provider == "mimo"
    assert UnderstandingResult.model_validate(
        {**understanding.model_dump(), "provider": "bailian"}
    ).provider == "bailian"
    assert RootCandidates.model_validate({**roots.model_dump(), "provider": "bailian"}).provider == (
        "bailian"
    )


def test_mimo_endpoint_uses_openai_payload_without_dashscope_thinking_parameter() -> None:
    """若传输层重新发送百炼专用参数，本测试应捕获 MIMO 的协议冲突。"""

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url == "https://apimimo.zaiyunding.com/v1/chat/completions"
        assert body["model"] == "openai/mimo-v2.6-pro"
        assert body["response_format"] == {"type": "json_object"}
        assert "enable_thinking" not in body
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(RESULT, ensure_ascii=False)},
                    }
                ]
            },
        )

    config = QwenConfig(
        api_key="secret",
        base_url="https://apimimo.zaiyunding.com/v1",
        model="openai/mimo-v2.6-pro",
        vision_model="openai/mimo-v2.6-pro",
    )
    result = QwenClient(config, transport=httpx.MockTransport(handle)).understand(
        "US", "识别图片", [("image/png", b"image")]
    )
    assert result.product_name == "文件图标"


def test_dashscope_endpoint_keeps_non_thinking_parameter() -> None:
    """若兼容 MIMO 时误删百炼的延迟控制参数，本测试应失败。"""

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["enable_thinking"] is False
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(RESULT, ensure_ascii=False)},
                    }
                ]
            },
        )

    client = QwenClient(QwenConfig(api_key="secret"), transport=httpx.MockTransport(handle))
    assert client.understand("US", "识别", []).product_type == "图片"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://apimimo.zaiyunding.com.evil.example/v1",
        "https://zaiyunding.com/v1",
        "https://apimimo.zaiyunding.com/v2",
        "http://apimimo.zaiyunding.com/v1",
    ],
)
def test_mimo_endpoint_allowlist_rejects_lookalikes(base_url: str) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    config = QwenConfig(
        api_key="secret", base_url=base_url, model="openai/mimo-v2.6-pro"
    )
    with pytest.raises(DomainError) as error:
        QwenClient(config, transport=httpx.MockTransport(handle)).understand("US", "识别", [])
    assert error.value.code == "MODEL_CONFIG_INVALID"
    assert calls == []
