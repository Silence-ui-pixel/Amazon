"""清洗输出校验纠正：最多一次，保留诊断且不重试网络异常。"""

import json
from pathlib import Path

import httpx
import pytest
from test_workflow_auto import product, reply

from app.domain.cleaning import Frequency
from app.domain.errors import DomainError
from app.services.qwen import QwenClient, QwenConfig


def test_cleaning_roots_sorted_by_model_are_reordered_without_extra_call(tmp_path: Path) -> None:
    """模型把 roots 按字母排序但词根齐全：顺序不影响结论，本地重排即可。"""
    calls: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return reply(
            {
                "roots": [
                    {"term": "b", "category": "scene", "evidence": "证据二"},
                    {"term": "a", "category": "selling_point", "evidence": "证据一"},
                ],
                "exclusions": [],
                "reason": "均与商品相关",
            }
        )

    client = QwenClient(
        QwenConfig(api_key="test-secret"),
        transport=httpx.MockTransport(handle),
        diagnostic_dir=tmp_path,
    )
    roots = [
        Frequency(term="a", occurrences=2, phrase_count=2, density=20, suggestions=[]),
        Frequency(term="b", occurrences=1, phrase_count=1, density=10, suggestions=[]),
    ]
    decision = client.review_cleaning(product(), roots, [])

    assert [item.term for item in decision.roots] == ["a", "b"]
    assert len(calls) == 1


def test_cleaning_scope_mismatch_is_retried_once_then_fails(tmp_path: Path) -> None:
    """模型漏项或重复时仍要纠正一次；两次都不合要求才交人工。"""
    requests: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return reply(
            {
                "roots": [{"term": "a", "category": "scene", "evidence": "证据一"}],
                "exclusions": [],
                "reason": "缺少一个词根",
            }
        )

    client = QwenClient(
        QwenConfig(api_key="test-secret"),
        transport=httpx.MockTransport(handle),
        diagnostic_dir=tmp_path,
    )
    roots = [
        Frequency(term="a", occurrences=2, phrase_count=2, density=20, suggestions=[]),
        Frequency(term="b", occurrences=1, phrase_count=1, density=10, suggestions=[]),
    ]

    with pytest.raises(DomainError) as error:
        client.review_cleaning(product(), roots, [])

    assert error.value.code == "MODEL_OUTPUT_INVALID"
    assert len(requests) == 2
    assert "expected_terms" in str(requests[1])


@pytest.mark.parametrize("mode", ["repair", "invalid", "timeout", "changed", "valid", "truncated"])
def test_cleaning_repair_is_bounded_and_diagnosed(tmp_path: Path, mode: str) -> None:
    requests: list[dict[str, object]] = []
    original = {
        "roots": [
            {
                "term": "wheel",
                "category": "selling_point",
                "evidence": "商品功能包含滚轮移动",
            }
        ],
        "exclusions": [],
        "reason": "审" * 501,
    }

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if mode == "timeout":
            raise httpx.ReadTimeout("test", request=request)
        if mode == "truncated":
            return httpx.Response(
                200, json={"choices": [{"finish_reason": "length", "message": {"content": "{"}}]}
            )
        result = dict(original)
        if mode == "valid":
            result["reason"] = "与商品用途相关"
        if len(requests) == 2 and mode != "invalid":
            result["reason"] = "与商品用途相关"
            if mode == "changed":
                result["roots"] = [
                    {
                        "term": "wheel",
                        "category": "scene",
                        "evidence": "商品功能包含滚轮移动",
                    }
                ]
        return reply(result)

    client = QwenClient(
        QwenConfig(api_key="test-secret"),
        transport=httpx.MockTransport(handle),
        diagnostic_dir=tmp_path,
    )
    roots = [
        Frequency(term="wheel", occurrences=2, phrase_count=2, density=20, suggestions=[])
    ]
    if mode in {"repair", "valid"}:
        assert client.review_cleaning(product(), roots, []).reason == "与商品用途相关"
    else:
        with pytest.raises(DomainError) as error:
            client.review_cleaning(product(), roots, [])
        assert error.value.code == (
            "MODEL_OUTCOME_UNKNOWN" if mode == "timeout" else "MODEL_OUTPUT_INVALID"
        )
    assert len(requests) == (1 if mode in {"timeout", "valid", "truncated"} else 2)
    records = [json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.glob("*.json")]
    assert len(records) == len(requests)
    assert "test-secret" not in json.dumps(records)
    if mode == "truncated":
        assert records[0]["finish_reason"] == "length"
    if mode not in {"timeout", "valid", "truncated"}:
        assert records[0]["errors"][0]["loc"] == ["reason"]
        assert records[0]["finish_reason"] == "stop"
        assert len(json.loads(records[0]["raw"])["reason"]) == 501
        correction = str(requests[1])
        assert "reason" in correction and "500" in correction
