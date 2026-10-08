"""Safety behaviour of the HTTP client, using httpx's mock transport (no network)."""

import asyncio

import httpx
import pytest

from crawler.config import BROWSER_USER_AGENT, CrawlerConfig
from crawler.http.client import (BLOCK_MESSAGE, BlockedError, ConnectionFailure, FetchError,
                                 HttpClient, LimitReached)

URL = "https://api.example.test/graphql"


def cfg(**kw):
    base = dict(request_delay_seconds=0, request_jitter_seconds=0, backoff_base_seconds=0,
                backoff_max_seconds=0, retry_count=2, respect_robots=False, api_url=URL)
    return CrawlerConfig(**{**base, **kw})


def run(handler, coro_fn, **cfg_kw):
    async def go():
        async with HttpClient(cfg(**cfg_kw), transport=httpx.MockTransport(handler)) as client:
            return await coro_fn(client)
    return asyncio.run(go())


def gql(client):
    return client.graphql("Q", "query Q { x }", {})


def test_success_and_user_agent():
    seen = {}

    def handler(request):
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, json={"data": {"x": 1}})

    assert run(handler, gql) == {"x": 1}
    assert seen["ua"] == BROWSER_USER_AGENT


def test_transient_500_is_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={"data": {"ok": True}})

    assert run(handler, gql) == {"ok": True} and len(calls) == 3


def test_retries_are_bounded():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500)

    with pytest.raises(FetchError) as e:
        run(handler, gql)
    assert e.value.retryable and len(calls) == 3        # retry_count=2 -> 3 attempts, then give up


def test_permanent_404_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    with pytest.raises(FetchError) as e:
        run(handler, gql)
    assert not e.value.retryable and len(calls) == 1


def test_repeated_429_stops_crawl_with_clear_message():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "0"})

    with pytest.raises(BlockedError) as e:
        run(handler, gql, max_block_events=3)
    assert BLOCK_MESSAGE in str(e.value) and len(calls) == 3


def test_429_then_success_slows_down_but_continues():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "0"}) if len(calls) == 1 else httpx.Response(200, json={"data": {"x": 1}})

    async def scenario(client):
        data = await gql(client)
        return data, client.slow_factor

    data, factor = run(handler, scenario)
    assert data == {"x": 1} and factor == 2.0           # request rate halved after the 429


def test_repeated_403_stops():
    def handler(request):
        return httpx.Response(403, json={})

    async def scenario(client):
        outcomes = []
        for _ in range(3):
            try:
                await gql(client)
            except FetchError as exc:
                outcomes.append(exc.status)
        return outcomes

    with pytest.raises(BlockedError):
        run(handler, scenario, max_block_events=3)


@pytest.mark.parametrize("body", ["<title>Just a moment...</title>", "<div class='g-recaptcha'></div>"])
def test_captcha_or_cloudflare_challenge_stops_immediately(body):
    def handler(request):
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    with pytest.raises(BlockedError):
        run(handler, lambda c: c.get_text("https://www.example.test/"))


def test_cloudflare_header_stops():
    def handler(request):
        return httpx.Response(403, headers={"cf-mitigated": "challenge"})

    with pytest.raises(BlockedError):
        run(handler, gql)


def test_repeated_connection_failures_stop():
    def handler(request):
        raise httpx.ConnectError("down")

    with pytest.raises(ConnectionFailure):
        run(handler, gql, max_connection_failures=3, retry_count=5)


def test_request_budget():
    def handler(request):
        return httpx.Response(200, json={"data": {}})

    async def scenario(client):
        await gql(client)
        await gql(client)
        await gql(client)

    with pytest.raises(LimitReached):
        run(handler, scenario, max_requests=2)


def test_graphql_error_without_data_is_permanent_fetch_error():
    def handler(request):
        return httpx.Response(200, json={"data": None, "errors": [{"message": "BAD_USER_INPUT"}]})

    with pytest.raises(FetchError) as e:
        run(handler, gql)
    assert not e.value.retryable


def test_robots_disallow_is_respected():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /graphql\n")
        return httpx.Response(200, json={"data": {}})

    with pytest.raises(BlockedError):
        run(handler, gql, respect_robots=True)


def test_robots_404_means_allowed():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="nope")
        return httpx.Response(200, json={"data": {"x": 1}})

    assert run(handler, gql, respect_robots=True) == {"x": 1}
