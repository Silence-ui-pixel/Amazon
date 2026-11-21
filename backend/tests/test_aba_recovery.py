"""连接恢复契约：断网换浏览器、失效刷新、端口变化和多店铺隔离。"""

import json
from pathlib import Path
from unittest.mock import MagicMock, Mock

import httpx
import pytest
from test_aba import auth_file, row

from app.adapters.aba import AbaClient
from app.domain.errors import DomainError


def test_network_failure_uses_browser_and_avoids_repeated_direct_timeouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.adapters.aba_browser import AbaBrowserSession

    session = AbaBrowserSession(tmp_path)
    calls: list[str] = []

    def direct(request: httpx.Request) -> httpx.Response:
        calls.append("direct")
        raise httpx.ConnectError("offline", request=request)

    monkeypatch.setattr(
        session,
        "request",
        Mock(
            return_value=(
                200,
                json.dumps({"reportsV2": [{"totalItems": 1, "rows": [row("gate wheel")]}]}),
            )
        ),
    )
    client = AbaClient(
        auth_file(tmp_path),
        transport=httpx.MockTransport(direct),
        attempts=1,
        browser_session=session,
    )
    assert client.fetch("wheel", "2026-10-17").fetched == 1
    assert client.fetch("wheel", "2026-10-17").fetched == 1
    assert calls == ["direct"]


def test_expired_cookie_refreshes_only_current_project_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.adapters.aba_browser import AbaBrowserSession

    path = auth_file(tmp_path)
    session = AbaBrowserSession(tmp_path, auth_path=path)
    refreshed: list[bool] = []

    def refresh() -> None:
        refreshed.append(True)
        path.write_text(json.dumps({"cookie": "session=new", "ua": "browser"}))

    monkeypatch.setattr(session, "refresh_auth", refresh)

    def direct(request: httpx.Request) -> httpx.Response:
        if request.headers["cookie"] == "session=new":
            return httpx.Response(200, json={"reportsV2": [{"totalItems": 0, "rows": []}]})
        return httpx.Response(401)

    client = AbaClient(
        path, transport=httpx.MockTransport(direct), attempts=1, browser_session=session
    )
    assert client.fetch("wheel", "2026-10-17").total == 0
    assert refreshed == [True]


def test_rate_limit_does_not_switch_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.adapters.aba_browser import AbaBrowserSession

    session = AbaBrowserSession(tmp_path)

    def unexpected(*_: object) -> None:
        pytest.fail("限流不可通过切换浏览器重试")

    monkeypatch.setattr(session, "request", unexpected)
    client = AbaClient(
        auth_file(tmp_path),
        transport=httpx.MockTransport(lambda _: httpx.Response(429)),
        attempts=1,
        browser_session=session,
    )
    with pytest.raises(DomainError) as error:
        client.fetch("wheel", "2026-10-17")
    assert error.value.code == "ABA_RATE_LIMITED"


def test_stale_port_discovers_new_port_and_persists_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.adapters.aba_browser import AbaBrowserSession

    session = AbaBrowserSession(tmp_path, port=12345)
    monkeypatch.setattr(
        session, "_probe", Mock(side_effect={12345: False, 38979: False, 23456: True}.__getitem__)
    )
    monkeypatch.setattr(session, "_listening_ports", lambda: [23456])
    assert session.discover_port() == 23456
    assert (tmp_path / "cdp_port").read_text() == "23456"


def test_multiple_browsers_never_select_arbitrary_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.adapters.aba_browser import AbaBrowserSession

    session = AbaBrowserSession(tmp_path)
    monkeypatch.setattr(
        session, "_probe", Mock(side_effect={38979: False, 23456: True, 34567: True}.__getitem__)
    )
    monkeypatch.setattr(session, "_listening_ports", lambda: [23456, 34567])
    with pytest.raises(DomainError) as error:
        session.discover_port()
    assert error.value.code == "ABA_BROWSER_AMBIGUOUS"


def test_missing_cdp_automatically_starts_ziniao(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.adapters import aba_browser
    from app.adapters.aba_browser import AbaBrowserSession

    session = AbaBrowserSession(tmp_path)
    monkeypatch.setattr(session, "discover_port", Mock(return_value=None))
    start = Mock(return_value=23456)
    monkeypatch.setattr(aba_browser, "ensure_ziniao_port", start)

    assert session.ensure_port() == 23456
    assert session.port == 23456
    start.assert_called_once_with(tmp_path, "KEYWORD_ABA_CDP_PORT")


def test_browser_refresh_filters_cookies_and_never_closes_user_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.adapters.aba_browser import AbaBrowserSession

    session = AbaBrowserSession(tmp_path)
    monkeypatch.setattr(session, "ensure_port", Mock(return_value=23456))
    runtime = MagicMock()
    browser = runtime.__enter__.return_value.chromium.connect_over_cdp.return_value
    context = MagicMock()
    page = MagicMock()
    page.url = "https://sellercentral.amazon.com/brand-analytics"
    page.evaluate.side_effect = ["user-agent-test", "csrf-test"]
    context.pages = [page]
    context.cookies.return_value = [
        {"domain": ".amazon.com", "name": "session", "value": "private"},
        {"domain": ".other.com", "name": "secret", "value": "excluded"},
    ]
    browser.contexts = [context]
    monkeypatch.setattr("playwright.sync_api.sync_playwright", Mock(return_value=runtime))
    session.refresh_auth()
    saved = json.loads((tmp_path / "aba_auth.json").read_text())
    assert saved["cookie"] == "session=private"
    assert saved["ua"] == "user-agent-test"
    assert not (tmp_path / "aba_auth.tmp").exists()
    browser.close.assert_not_called()
    page.close.assert_not_called()
    # 冷却期内不重复读取浏览器 Cookie。
    session.refresh_auth()
    context.cookies.assert_called_once()


def test_browser_fallback_opens_seller_page_and_returns_real_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.adapters.aba_browser import AbaBrowserSession

    session = AbaBrowserSession(tmp_path)
    monkeypatch.setattr(session, "ensure_port", Mock(return_value=23456))
    runtime = MagicMock()
    browser = runtime.__enter__.return_value.chromium.connect_over_cdp.return_value
    context = MagicMock()
    context.pages = []
    page = context.new_page.return_value
    page.url = "https://sellercentral.amazon.com/"
    page.evaluate.return_value = {"status": 200, "text": '{"reportsV2": []}'}
    browser.contexts = [context]
    monkeypatch.setattr("playwright.sync_api.sync_playwright", Mock(return_value=runtime))
    assert session.request("https://sellercentral.amazon.com/api/test", {}) == (
        200,
        '{"reportsV2": []}',
    )
    page.goto.assert_called_once()
    browser.close.assert_not_called()


def test_seller_login_redirect_is_not_treated_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.adapters.aba_browser import AbaBrowserSession

    session = AbaBrowserSession(tmp_path)
    monkeypatch.setattr(session, "ensure_port", Mock(return_value=23456))
    runtime = MagicMock()
    browser = runtime.__enter__.return_value.chromium.connect_over_cdp.return_value
    context = MagicMock()
    context.pages = []
    context.new_page.return_value.url = "https://sellercentral.amazon.com/ap/signin"
    browser.contexts = [context]
    monkeypatch.setattr("playwright.sync_api.sync_playwright", Mock(return_value=runtime))
    with pytest.raises(DomainError) as error:
        session.request("https://sellercentral.amazon.com/api/test", {})
    assert error.value.code == "ABA_AUTH_REQUIRED"
    browser.close.assert_not_called()


def test_invalid_response_after_refresh_is_not_hidden_by_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.adapters.aba_browser import AbaBrowserSession

    session = AbaBrowserSession(tmp_path)
    monkeypatch.setattr(session, "refresh_auth", Mock())
    browser_request = Mock()
    monkeypatch.setattr(session, "request", browser_request)
    responses = iter([httpx.Response(401), httpx.Response(200, text="unexpected")])
    client = AbaClient(
        auth_file(tmp_path),
        transport=httpx.MockTransport(lambda _: next(responses)),
        attempts=1,
        browser_session=session,
    )
    with pytest.raises(DomainError) as error:
        client.fetch("wheel", "2026-10-17")
    assert error.value.code == "ABA_INVALID_RESPONSE"
    browser_request.assert_not_called()
