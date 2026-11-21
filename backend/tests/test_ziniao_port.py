"""紫鸟调试端口发现：按优先级探测，多个店铺时不猜。"""

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters import ziniao
from app.domain.errors import DomainError


def _probe(open_ports: set[int]) -> Callable[[int], bool]:
    def probe(port: int) -> bool:
        return port in open_ports

    return probe


def _ports(ports: list[int]) -> Callable[[], list[int]]:
    def listening() -> list[int]:
        return ports

    return listening


def test_find_port_prefers_explicit_then_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "aba"
    shared.mkdir()
    monkeypatch.setattr(ziniao, "probe_port", _probe({2222, 3333}))
    monkeypatch.setattr(ziniao, "listening_ports", _ports([]))
    monkeypatch.setenv("KEYWORD_AMAZON_CDP_PORT", "3333")
    (shared / "cdp_port").write_text("2222", encoding="utf-8")
    directory = tmp_path / "amazon"
    assert ziniao.find_port(directory, "KEYWORD_AMAZON_CDP_PORT", shared=shared) == 3333
    monkeypatch.delenv("KEYWORD_AMAZON_CDP_PORT")
    # 本次发现的端口会被记住，优先于共用的 ABA 端口文件。
    assert ziniao.find_port(directory, "KEYWORD_AMAZON_CDP_PORT", shared=shared) == 3333
    (directory / "cdp_port").unlink()
    assert ziniao.find_port(directory, "KEYWORD_AMAZON_CDP_PORT", shared=shared) == 2222


def test_find_port_scans_and_rejects_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KEYWORD_AMAZON_CDP_PORT", raising=False)
    monkeypatch.setattr(ziniao, "probe_port", _probe({4444}))
    monkeypatch.setattr(ziniao, "listening_ports", _ports([4444]))
    assert ziniao.find_port(tmp_path, "KEYWORD_AMAZON_CDP_PORT") == 4444
    (tmp_path / "cdp_port").unlink()
    monkeypatch.setattr(ziniao, "probe_port", _probe({4444, 5555}))
    monkeypatch.setattr(ziniao, "listening_ports", _ports([4444, 5555]))
    with pytest.raises(DomainError) as exc:
        ziniao.find_port(tmp_path, "KEYWORD_AMAZON_CDP_PORT")
    assert exc.value.code == "ZINIAO_BROWSER_AMBIGUOUS"


def test_find_port_returns_none_without_ziniao(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KEYWORD_AMAZON_CDP_PORT", raising=False)
    monkeypatch.setattr(ziniao, "probe_port", _probe(set()))
    monkeypatch.setattr(ziniao, "listening_ports", _ports([]))
    assert ziniao.find_port(tmp_path, "KEYWORD_AMAZON_CDP_PORT") is None
    assert not (tmp_path / "cdp_port").exists()


def test_ensure_port_starts_agent_and_persists_discovered_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """抓住“只探测但不启动”的回归；CLI 是外部边界，其余启动流程跑真实代码。"""

    cli = tmp_path / "ziniao.exe"
    cli.write_bytes(b"test")
    probes = iter([None, None, 45678])
    commands: list[list[str]] = []

    def find_port(_directory: Path, _env: str, *, shared: Path | None = None) -> int | None:
        del shared
        return next(probes)

    monkeypatch.setattr(ziniao, "find_port", find_port)
    monkeypatch.setattr(ziniao, "find_cli", lambda: cli)

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        if command[-2:] == ["browser", "list"]:
            return SimpleNamespace(returncode=0, stdout="26932126933854\tUS store\n", stderr="")
        return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

    monkeypatch.setattr(ziniao.subprocess, "run", run)
    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ziniao.time, "sleep", no_sleep)

    port = ziniao.ensure_port(tmp_path / "amazon", "KEYWORD_AMAZON_CDP_PORT", shared=tmp_path / "aba")

    assert port == 45678
    assert commands == [
        [str(cli), "browser", "list"],
        [str(cli), "browser", "start", "--id", "26932126933854", "--mode", "agent", "--json"],
    ]
    assert (tmp_path / "amazon" / "cdp_port").read_text(encoding="utf-8") == "45678"


def test_ensure_port_reports_start_failure_without_anonymous_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = tmp_path / "ziniao.exe"
    cli.write_bytes(b"test")

    def no_port(_directory: Path, _env: str, *, shared: Path | None = None) -> None:
        del shared
        return None

    monkeypatch.setattr(ziniao, "find_port", no_port)
    monkeypatch.setattr(ziniao, "find_cli", lambda: cli)
    monkeypatch.setenv("KEYWORD_ZINIAO_STORE_ID", "26932126933854")

    def fail_start(command: list[str], **_kwargs: object) -> SimpleNamespace:
        del command
        return SimpleNamespace(returncode=1, stdout="", stderr="启动失败")

    monkeypatch.setattr(
        ziniao.subprocess,
        "run",
        fail_start,
    )

    with pytest.raises(DomainError) as exc:
        ziniao.ensure_port(tmp_path, "KEYWORD_AMAZON_CDP_PORT")

    assert exc.value.code == "ZINIAO_START_FAILED"


def test_ensure_port_uses_remembered_store_without_listing_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = tmp_path / "ziniao.exe"
    cli.write_bytes(b"test")
    directory = tmp_path / "amazon"
    shared = tmp_path / "aba"
    shared.mkdir()
    (shared / "store_id").write_text("26932126933854", encoding="utf-8")
    probes = iter([None, 45678])
    commands: list[list[str]] = []

    def find_port(_directory: Path, _env: str, *, shared: Path | None = None) -> int | None:
        del shared
        return next(probes)

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

    monkeypatch.setattr(ziniao, "find_port", find_port)
    monkeypatch.setattr(ziniao, "find_cli", lambda: cli)
    monkeypatch.setattr(ziniao.subprocess, "run", run)

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ziniao.time, "sleep", no_sleep)

    assert ziniao.ensure_port(directory, "KEYWORD_AMAZON_CDP_PORT", shared=shared) == 45678
    assert commands == [
        [str(cli), "browser", "start", "--id", "26932126933854", "--mode", "agent", "--json"]
    ]


def test_start_agent_starts_cli_service_and_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = tmp_path / "ziniao.exe"
    cli.write_bytes(b"test")
    commands: list[list[str]] = []
    results = iter(
        [
            SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Local Ziniao CLI service failed to start. Install the ziniao desktop client.",
            ),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr=""),
        ]
    )

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return next(results)

    monkeypatch.setattr(ziniao.subprocess, "run", run)

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ziniao.time, "sleep", no_sleep)

    ziniao._start_agent(cli, "26932126933854")  # pyright: ignore[reportPrivateUsage]

    browser_start = [
        str(cli),
        "browser",
        "start",
        "--id",
        "26932126933854",
        "--mode",
        "agent",
        "--json",
    ]
    assert commands == [browser_start, [str(cli), "start", "--remote"], browser_start]
