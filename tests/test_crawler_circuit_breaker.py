from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tracker import crawler


@pytest.fixture(autouse=True)
def _reset_site_circuit_breaker():
    old_failures = crawler._site_cb_failures
    old_until = crawler._site_cb_cooldown_until
    crawler._site_cb_failures = 0
    crawler._site_cb_cooldown_until = None
    yield
    crawler._site_cb_failures = old_failures
    crawler._site_cb_cooldown_until = old_until


def test_site_cb_is_open_resets_cleanly_after_cooldown_expires():
    crawler._site_cb_failures = 3
    crawler._site_cb_cooldown_until = datetime.now(timezone.utc) - timedelta(seconds=1)

    assert crawler._site_cb_is_open() is False
    assert crawler._site_cb_failures == 0
    assert crawler._site_cb_cooldown_until is None


def test_index_url_uses_current_portal_render_param():
    assert crawler.INDEX_URL == f"{crawler.BASE_URL}{crawler.HOMEPAGE_PATH}?render=index"
    assert "p_p_id=" not in crawler.INDEX_URL


def test_navigate_continues_when_goto_times_out_after_dom_ready(monkeypatch):
    class FakePage:
        def __init__(self):
            self.goto_kwargs = None

        def goto(self, _url, **kwargs):
            self.goto_kwargs = kwargs
            raise TimeoutError("Page.goto: Timeout 45000ms exceeded.")

        def evaluate(self, _script):
            return True

    client = crawler.MuasamcongCrawler(use_playwright=False)
    page = FakePage()
    calls: list[str] = []
    monkeypatch.setattr(client, "_pw_stabilize", lambda _page, _nav_try: calls.append("stabilize"))
    monkeypatch.setattr(client, "_pw_extract_site_key", lambda _page: "site-key")

    try:
        returned_page, site_key = client._pw_navigate_and_prepare(object(), page, nav_try=0)
    finally:
        client.close()

    assert returned_page is page
    assert site_key == "site-key"
    assert page.goto_kwargs["wait_until"] == "domcontentloaded"
    assert calls == ["stabilize"]


def test_retry_predicate_does_not_retry_blocked_or_connection_reset():
    assert crawler._retry_search_if_transient(crawler.BlockedException(503)) is False
    assert (
        crawler._retry_search_if_transient(
            RuntimeError("Page.goto: net::ERR_CONNECTION_RESET at https://example.test")
        )
        is False
    )


def test_navigate_connection_reset_records_failure_and_raises_blocked():
    class FakePage:
        def goto(self, _url, **_kwargs):
            raise RuntimeError("Page.goto: net::ERR_CONNECTION_RESET at https://example.test")

    client = crawler.MuasamcongCrawler(use_playwright=False)
    try:
        with pytest.raises(crawler.BlockedException) as exc_info:
            client._pw_navigate_and_prepare(object(), FakePage(), nav_try=0)
    finally:
        client.close()

    assert exc_info.value.status_code == 503
    assert crawler._site_cb_failures == 1


def test_playwright_launch_uses_remote_connect_url(monkeypatch):
    class FakeChromium:
        def __init__(self):
            self.called = None

        def connect(self, url, **kwargs):
            self.called = ("connect", url, kwargs)
            return "remote-browser"

        def launch(self, **_kwargs):
            raise AssertionError("local launch should not be used")

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()

    fake = FakePlaywright()
    monkeypatch.setenv("PLAYWRIGHT_CONNECT_URL", "wss://browser.example/playwright")
    monkeypatch.delenv("PLAYWRIGHT_CDP_URL", raising=False)

    client = crawler.MuasamcongCrawler(use_playwright=False)
    try:
        browser = client._pw_launch_browser(fake)
    finally:
        client.close()

    assert browser == "remote-browser"
    assert fake.chromium.called == (
        "connect",
        "wss://browser.example/playwright",
        {"timeout": 20_000},
    )


def test_fetch_recent_bids_fails_fast_when_breaker_is_open(monkeypatch):
    client = crawler.MuasamcongCrawler(use_playwright=False)
    calls: list[str] = []
    monkeypatch.setattr(client, "_warmup_session", lambda: calls.append("warmup"))
    monkeypatch.setattr(client, "_load_field_names", lambda: calls.append("fields"))
    crawler._site_cb_failures = 3
    crawler._site_cb_cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=1)

    try:
        with pytest.raises(crawler.BlockedException) as exc_info:
            client.fetch_recent_bids(max_pages=1)
    finally:
        client.close()

    assert exc_info.value.status_code == 503
    assert calls == []


def test_fetch_bid_by_code_fails_fast_when_breaker_is_open(monkeypatch):
    client = crawler.MuasamcongCrawler(use_playwright=False)
    calls: list[str] = []
    monkeypatch.setattr(client, "_warmup_session", lambda: calls.append("warmup"))
    monkeypatch.setattr(client, "_load_field_names", lambda: calls.append("fields"))
    crawler._site_cb_failures = 3
    crawler._site_cb_cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=1)

    try:
        with pytest.raises(crawler.BlockedException) as exc_info:
            client.fetch_bid_by_code("IB2500579539")
    finally:
        client.close()

    assert exc_info.value.status_code == 503
    assert calls == []
