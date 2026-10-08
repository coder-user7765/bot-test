"""Polite HTTP client: one shared rate limiter, retries with exponential backoff, robots.txt,
and strict "stop, never bypass" handling of 429 / 403 / CAPTCHA / Cloudflare challenges."""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from tenacity import (AsyncRetrying, RetryCallState, retry_if_exception_type,
                      stop_after_attempt)

from crawler.config import CrawlerConfig
from crawler.logging_config import log_event
from crawler.utils.human import HumanCadence, think_time

BLOCK_MESSAGE = ("Crawler stopped because automated access appears restricted. "
                 "No bypass will be attempted.")
RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}
# Unmistakable challenge pages: checked on every HTML response.
STRONG_CHALLENGE_MARKERS = (
    "<title>just a moment", "cf-browser-verification", "cf-chl-", "attention required! | cloudflare",
)
# These also show up in the scripts of perfectly normal (large) pages, e.g. Cloudflare's
# injected beacon or a reCAPTCHA login widget, so they only count on small / error pages.
WEAK_CHALLENGE_MARKERS = (
    "challenge-platform", "g-recaptcha", "hcaptcha", "captcha", "automated access", "unusual traffic",
    "just a moment...",
)
WEAK_MARKER_MAX_BODY = 20_000


class CrawlAborted(Exception):
    """The crawl must stop (gracefully). `reason` is a short machine-friendly code."""

    reason = "aborted"


class BlockedError(CrawlAborted):
    reason = "blocked"

    def __init__(self, detail: str = "") -> None:
        super().__init__(f"{BLOCK_MESSAGE} ({detail})" if detail else BLOCK_MESSAGE)


class ConnectionFailure(CrawlAborted):
    reason = "connection_failure"


class StopRequested(CrawlAborted):
    reason = "interrupted"


class LimitReached(CrawlAborted):
    reason = "limit_reached"


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class _Retryable(Exception):
    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form is rare here; fall back to exponential backoff


class HttpClient:
    def __init__(self, cfg: CrawlerConfig, stop: threading.Event | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg = cfg
        self.stop = stop or threading.Event()
        self.requests = 0
        self.current_url = ""
        self.last_status: int | None = None
        self.slow_factor = 1.0
        self._next_slot = 0.0
        self._rate_lock = asyncio.Lock()
        self._consecutive_429 = 0
        self._consecutive_403 = 0
        self._consecutive_conn = 0
        self._ok_streak = 0
        self._robots: dict[str, RobotFileParser | None] = {}
        self._browser_enabled = cfg.browser_enabled and transport is None
        self._browser_lock = asyncio.Lock()
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self.last_search_url: str | None = None  # referer for the listing pages opened from it
        self.user_agent = cfg.user_agent
        self._cadence = HumanCadence(
            base_delay=cfg.request_delay_seconds,
            jitter=cfg.request_jitter_seconds,
        )
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(cfg.timeout_seconds),
            follow_redirects=True,
            max_redirects=5,
            headers={"User-Agent": cfg.user_agent, "Accept": "application/json, text/html;q=0.8, */*;q=0.5",
                     "Accept-Language": cfg.accept_language},
        )

    async def __aenter__(self) -> "HttpClient":
        if self._browser_enabled:
            self._playwright = await async_playwright().start()
            try:
                # A persistent profile keeps cookies / local storage between runs, so a `resume`
                # looks like the same returning visitor instead of a brand-new one every time.
                # no_viewport = use the real window size (a fixed 1280x720 viewport is a tell).
                profile = self.cfg.data_path / "browser-profile"
                profile.mkdir(parents=True, exist_ok=True)
                self._context = await self._playwright.chromium.launch_persistent_context(
                    str(profile),
                    channel=self.cfg.browser_channel,
                    headless=self.cfg.browser_headless,
                    locale=self.cfg.accept_language.split(",", maxsplit=1)[0],
                    no_viewport=True,
                )
                self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
                self.user_agent = await self._page.evaluate("navigator.userAgent")
                await self.get_text(self.cfg.base_url)
            except Exception:
                await self._close_browser()
                raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._close_browser()
        await self._http.aclose()

    async def _close_browser(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            finally:
                self._context = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    @property
    def using_browser(self) -> bool:
        return self._browser_enabled

    # ---- timing ------------------------------------------------------
    async def sleep(self, seconds: float) -> None:
        """Interruptible sleep: raises StopRequested as soon as Ctrl+C was pressed."""
        end = time.monotonic() + seconds
        while True:
            if self.stop.is_set():
                raise StopRequested("Stop requested.")
            left = end - time.monotonic()
            if left <= 0:
                return
            await asyncio.sleep(min(0.5, left))

    async def _throttle(self, after_page: bool = False) -> None:
        async with self._rate_lock:
            if self.stop.is_set():
                raise StopRequested("Stop requested.")
            # honour a pause requested by a 429 / slow-down (applies to every worker)
            await self.sleep(self._next_slot - time.monotonic())
            if after_page and self.cfg.request_delay_seconds > 0:
                # The API call a page makes right after it loads: a short beat, not a new
                # "reading time" (a real page fires it on its own within a second or two).
                await self.sleep(think_time(0.8, 2.5))
                return
            await self._cadence.wait(self.sleep)
            if self.slow_factor > 1:  # after rate limiting, stretch the gap between requests
                extra = (self.cfg.request_delay_seconds
                         + random.uniform(0, self.cfg.request_jitter_seconds)) * (self.slow_factor - 1)
                self._next_slot = max(self._next_slot, time.monotonic() + extra)

    def _check_budget(self) -> None:
        if self.cfg.max_requests and self.requests >= self.cfg.max_requests:
            raise LimitReached("max_requests reached")

    # ---- robots.txt --------------------------------------------------
    async def _ensure_allowed(self, url: str) -> None:
        if not self.cfg.respect_robots:
            return
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = await self._load_robots(origin)
        parser = self._robots[origin]
        if parser is not None and not parser.can_fetch(self.user_agent, url):
            raise BlockedError(f"robots.txt disallows {url}")

    async def _load_robots(self, origin: str) -> RobotFileParser | None:
        url = origin + "/robots.txt"
        try:
            resp = await self._fetch_robots(url)
        except FetchError as exc:
            if exc.status and 400 <= exc.status < 500 and exc.status not in (408, 425, 429):
                return None  # no robots.txt => everything allowed (RFC 9309)
            raise BlockedError(f"robots.txt unavailable for {origin}: {exc}") from exc
        parser = RobotFileParser()
        parser.parse(resp.text.splitlines())
        log_event("robots_loaded", url=url, status=resp.status_code)
        return parser

    async def _fetch_robots(self, url: str) -> httpx.Response:
        """robots.txt is read out-of-band: it must not navigate the visible browser tab (that
        would leave the page on the API origin and skew the Origin/Referer of later calls)."""
        if self._context is None:
            return await self._send("GET", url, check_robots=False, retry=False)
        await self._throttle()
        self._check_budget()
        self.requests += 1
        try:
            resp = await self._context.request.get(url, timeout=self.cfg.timeout_seconds * 1000)
            body = await resp.body()
        except PlaywrightError as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}", retryable=True) from exc
        if resp.status >= 400:
            raise FetchError(f"HTTP {resp.status}", status=resp.status,
                             retryable=resp.status in RETRYABLE_STATUSES)
        return httpx.Response(resp.status, headers=resp.headers, content=body,
                              request=httpx.Request("GET", url))

    # ---- core --------------------------------------------------------
    def _wait(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, _Retryable) and exc.retry_after is not None:
            wait = exc.retry_after
        else:
            wait = self.cfg.backoff_base_seconds * (2 ** (state.attempt_number - 1))
        wait = min(wait, self.cfg.backoff_max_seconds) if self.cfg.backoff_max_seconds else wait
        return wait + random.uniform(0, 1)

    async def _send(self, method: str, url: str, *, check_robots: bool = True, retry: bool = True,
                    expect_json: bool = False, after_page: bool = False,
                    **kwargs: Any) -> httpx.Response:
        if check_robots:
            await self._ensure_allowed(url)
        attempts = self.cfg.retry_count + 1 if retry else 1
        retrying = AsyncRetrying(
            stop=stop_after_attempt(attempts), wait=self._wait,
            retry=retry_if_exception_type(_Retryable), reraise=True, sleep=self.sleep,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    return await self._once(method, url, expect_json, after_page, **kwargs)
        except _Retryable as exc:
            raise FetchError(str(exc), status=exc.status, retryable=True) from None
        raise AssertionError("unreachable")  # pragma: no cover

    async def _once(self, method: str, url: str, expect_json: bool, after_page: bool = False,
                    **kwargs: Any) -> httpx.Response:
        await self._throttle(after_page)
        self._check_budget()
        self.requests += 1
        self.current_url = url
        try:
            if self._browser_enabled:
                async with self._browser_lock:
                    resp = await self._browser_request(method, url, **kwargs)
            else:
                referer = kwargs.pop("referer", None)
                if referer:
                    kwargs["headers"] = {**kwargs.get("headers", {}), "Referer": referer}
                resp = await self._http.request(method, url, **kwargs)
        except (httpx.TransportError, PlaywrightError) as exc:
            self._consecutive_conn += 1
            log_event("transport_error", level=logging.WARNING, url=url, error=f"{type(exc).__name__}: {exc}")
            if self._consecutive_conn >= self.cfg.max_connection_failures:
                raise ConnectionFailure(
                    f"{self._consecutive_conn} consecutive connection failures; stopping. "
                    "Re-run `resume` when the network is back.") from exc
            raise _Retryable(f"{type(exc).__name__}: {exc}") from exc
        self._consecutive_conn = 0
        status = resp.status_code
        self.last_status = status
        log_event("response", url=url, status=status)

        self._detect_challenge(resp)
        if status == 429:
            await self._on_rate_limited(resp, url)  # always raises
        if status == 403:
            self._consecutive_403 += 1
            if self._consecutive_403 >= self.cfg.max_block_events:
                await self._block(f"{self._consecutive_403} consecutive HTTP 403 responses", url)
            raise FetchError("HTTP 403 Forbidden", status=403)
        if status in RETRYABLE_STATUSES:
            raise _Retryable(f"HTTP {status}", status, _parse_retry_after(resp.headers.get("retry-after")))
        if status >= 400:
            raise FetchError(f"HTTP {status}", status=status)
        if expect_json:
            try:
                resp.json()
            except ValueError as exc:
                raise _Retryable("invalid JSON in response", status) from exc

        self._consecutive_403 = 0
        self._consecutive_429 = 0
        self._ok_streak += 1
        if self._ok_streak >= 50 and self.slow_factor > 1:
            self.slow_factor = max(1.0, self.slow_factor / 2)
            self._ok_streak = 0
        return resp

    async def _browser_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if method.upper() == "GET":
            response = await self._page.goto(
                url, wait_until="domcontentloaded", timeout=self.cfg.timeout_seconds * 1000,
                referer=kwargs.get("referer"),
            )
            if response is None:
                raise httpx.ConnectError("Chrome navigation returned no response")
            content = await response.body()
            return httpx.Response(
                response.status,
                headers=await response.all_headers(),
                content=content,
                request=httpx.Request(method, url),
            )

        body = kwargs.get("json")
        headers = kwargs.get("headers", {})
        result = await self._page.evaluate(
            """async ({url, body, headers}) => {
                const response = await fetch(url, {
                    method: 'POST',
                    credentials: 'include',
                    headers,
                    body: JSON.stringify(body)
                });
                return {
                    status: response.status,
                    headers: Object.fromEntries(response.headers.entries()),
                    body: await response.text()
                };
            }""",
            {"url": url, "body": body, "headers": headers},
        )
        return httpx.Response(
            result["status"],
            headers=result["headers"],
            content=result["body"].encode(),
            request=httpx.Request(method, url),
        )

    def _detect_challenge(self, resp: httpx.Response) -> None:
        if resp.headers.get("cf-mitigated", "").lower() == "challenge":
            raise BlockedError("Cloudflare challenge")
        if "html" not in resp.headers.get("content-type", "").lower():
            return
        body = resp.text[:60_000].lower()
        for marker in STRONG_CHALLENGE_MARKERS:
            if marker in body:
                raise BlockedError(f"challenge/denial marker {marker!r}")
        if resp.status_code >= 400 or len(resp.content) < WEAK_MARKER_MAX_BODY:
            for marker in WEAK_CHALLENGE_MARKERS:
                if marker in body:
                    raise BlockedError(f"challenge/denial marker {marker!r}")

    async def _block(self, detail: str, url: str) -> None:
        log_event("blocked", level=logging.ERROR, url=url, error=detail)
        if self.cfg.stop_on_block:
            raise BlockedError(detail)
        log_event("cooldown", level=logging.WARNING, seconds=self.cfg.cooldown_seconds)
        self._consecutive_429 = self._consecutive_403 = 0
        await self.sleep(self.cfg.cooldown_seconds)

    async def _on_rate_limited(self, resp: httpx.Response, url: str) -> None:
        self._consecutive_429 += 1
        self._ok_streak = 0
        self.slow_factor = min(16.0, self.slow_factor * 2)  # slow down everything
        retry_after = _parse_retry_after(resp.headers.get("retry-after"))
        backoff = retry_after if retry_after is not None else min(
            self.cfg.backoff_max_seconds,
            self.cfg.backoff_base_seconds * (2 ** self._consecutive_429))
        # pause *all* workers, not just this request
        self._next_slot = max(self._next_slot, time.monotonic() + backoff)
        log_event("rate_limited", level=logging.WARNING, url=url, status=429,
                  retry_after=retry_after, backoff=backoff, slow_factor=self.slow_factor)
        if self._consecutive_429 >= self.cfg.max_block_events:
            await self._block(f"{self._consecutive_429} consecutive HTTP 429 responses", url)
        raise _Retryable("HTTP 429", 429, backoff)

    # ---- public helpers ---------------------------------------------
    async def get_bytes(self, url: str) -> bytes:
        return (await self._send("GET", url)).content

    async def get_text(self, url: str, referer: str | None = None) -> str:
        kwargs = {"referer": referer} if referer else {}
        return (await self._send("GET", url, **kwargs)).text

    async def graphql(self, operation: str, query: str, variables: dict[str, Any],
                      after_page: bool = False) -> dict[str, Any]:
        """POST a GraphQL document; returns the `data` object.

        after_page=True marks the call a just-opened page would make itself (browser mode)."""
        resp = await self._send(
            "POST", self.cfg.api_url, expect_json=True, after_page=after_page and self._browser_enabled,
            json={"operationName": operation, "query": query, "variables": variables},
            headers={"Content-Type": "application/json"},
        )
        payload = resp.json()
        data = payload.get("data")
        errors = payload.get("errors")
        if errors:
            text = " ".join(str(e.get("message", e)) for e in errors).lower()
            if any(w in text for w in ("rate limit", "throttl", "too many", "captcha", "forbidden", "unauthenticated")):
                await self._block(f"GraphQL error: {text[:120]}", self.cfg.api_url)
            if not data:
                raise FetchError(f"GraphQL error: {text[:200]}", status=resp.status_code)
            log_event("graphql_partial_errors", level=logging.WARNING, error=text[:200])
        return data or {}
