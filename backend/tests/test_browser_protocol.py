"""浏览器 Agent 协议信封：错误与文件的编解码。"""

from pathlib import Path

import pytest

from app.adapters.browser_protocol import (
    b64_files,
    decode_files,
    encode_error,
    raise_error,
    write_files,
)
from app.domain.errors import DomainError
from app.services.search_policy import SearchFailure


def test_files_round_trip_base64() -> None:
    files = {"shot.png": b"\x89PNG", "diag.json": b'{"a": 1}'}
    assert decode_files({"files": b64_files(files)}) == files


def test_domain_error_round_trip() -> None:
    payload = encode_error(DomainError(422, "BAD_INPUT", "坏请求"))
    with pytest.raises(DomainError) as caught:
        raise_error(payload)
    assert caught.value.code == "BAD_INPUT"
    assert caught.value.status_code == 422
    assert caught.value.message == "坏请求"


def test_search_failure_round_trip_preserves_evidence() -> None:
    error = SearchFailure(
        "SEARCH_ACCESS_CHECK",
        "需要人工确认",
        stage="results",
        screenshot="f.png",
        diagnostic="f.json",
        retry_after=3.0,
    )
    payload = encode_error(error)
    with pytest.raises(SearchFailure) as caught:
        raise_error(payload)
    assert caught.value.code == "SEARCH_ACCESS_CHECK"
    assert caught.value.screenshot == "f.png"
    assert caught.value.diagnostic == "f.json"
    assert caught.value.stage == "results"
    assert caught.value.retry_after == 3.0


def test_write_files_rejects_path_traversal(tmp_path: Path) -> None:
    write_files({"safe.png": b"ok", "../evil.png": b"no", "a/b.png": b"no"}, tmp_path)
    assert (tmp_path / "safe.png").read_bytes() == b"ok"
    assert not (tmp_path / "evil.png").exists()
