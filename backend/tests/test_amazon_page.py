"""Amazon 业务采集必须使用自动启动后的紫鸟，不得静默切匿名浏览器。"""

from pathlib import Path

import pytest

from app.domain.errors import DomainError
from app.services import amazon_page as module


def test_amazon_page_stops_when_ziniao_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> int:
        raise DomainError(503, "ZINIAO_START_FAILED", "紫鸟启动失败")

    monkeypatch.setattr(module, "ensure_port", unavailable, raising=False)

    with pytest.raises(DomainError) as exc, module.amazon_page(tmp_path):
        pass

    assert exc.value.code == "ZINIAO_START_FAILED"
