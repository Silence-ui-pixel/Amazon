"""浏览器端口解析：环境变量优先，其次复用 ABA 记录的文件。"""

from pathlib import Path

import pytest

from app.adapters.browser_port import resolve_cdp_port


def test_resolve_cdp_port_prefers_env_then_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = "KEYWORD_TEST_CDP_PORT"
    (tmp_path / "cdp_port").write_text("29265", encoding="utf-8")
    monkeypatch.delenv(env, raising=False)
    assert resolve_cdp_port(tmp_path, env) == 29265
    monkeypatch.setenv(env, "9333")
    assert resolve_cdp_port(tmp_path, env) == 9333
    monkeypatch.delenv(env, raising=False)
    assert resolve_cdp_port(tmp_path / "missing", env, default=None) is None
