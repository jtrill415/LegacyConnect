"""Playwright-backed fetcher for sites that refuse a plain HTTP client.

Leafly and Weedmaps sit behind Cloudflare-class bot protection, which answers a
bare `requests` call with a challenge page instead of content. This client
drives a real Chromium instance, so the JS challenge executes and the page
renders as a browser would see it.

It is a drop-in replacement for `http.Client` - same `get()` signature, same
`FetchResult`, same cache, robots policy, and rate limiter - so nothing
downstream of the fetch knows or cares which one it is talking to. That is why
this could be added without touching the extraction, normalization, merge, or
storage layers at all.

Cost: a browser page load is roughly an order of magnitude slower and heavier
than an HTTP GET, so use it only for the sources that need it (`--browser auto`
does exactly that, driven by `requires_browser` in each site spec).
"""

from __future__ import annotations

import logging
import os
import random
import time
from pathlib import Path
from urllib.parse import urlparse

from .http import (
    Cache,
    DEFAULT_UA,
    FetchError,
    FetchResult,
    RateLimiter,
    RobotsPolicy,
    RobotsDisallowed,
)

log = logging.getLogger(__name__)

#: A headless browser advertising StrainDBBot would be turned away instantly,
#: so the browser path uses a normal desktop UA. This is a real trade-off: it is
#: less transparent than the honest UA the plain client sends. Prefer the plain
#: client wherever it works, and prefer an official data agreement over either.
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

CHALLENGE_MARKERS = [
    "cf-browser-verification",
    "just a moment",
    "checking your browser",
    "captcha-delivery",
    "px-captcha",
    "enable javascript and cookies to continue",
    "verifying you are human",
]

#: Chromium locations to try when none is configured. The managed environments
#: this runs in ship a browser already; downloading another is wasteful.
_BROWSER_CANDIDATES = [
    "/opt/pw-browsers/chromium",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
]


def find_chromium() -> str | None:
    """Locate a pre-installed Chromium, or None to let Playwright choose."""
    configured = os.environ.get("STRAIN_DB_CHROMIUM")
    if configured:
        return configured
    for path in _BROWSER_CANDIDATES:
        if Path(path).exists():
            return path
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    if root.is_dir():
        for pattern in ("chromium-*/chrome-linux/chrome", "chromium_headless_shell-*/chrome-linux/headless_shell"):
            matches = sorted(root.glob(pattern))
            if matches:
                return str(matches[-1])
    return None


class BrowserClient:
    """Same interface as `http.Client`, backed by a real browser."""

    def __init__(
        self,
        cache_dir: Path,
        user_agent: str = BROWSER_UA,
        delay: float = 3.0,
        timeout: float = 45.0,
        max_retries: int = 2,
        respect_robots: bool = True,
        use_cache: bool = True,
        cache_ttl: float | None = None,
        headless: bool = True,
        wait_until: str = "domcontentloaded",
        wait_selector: str | None = None,
        challenge_wait: float = 12.0,
        executable_path: str | None = None,
    ):
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_retries = max_retries
        self.headless = headless
        self.wait_until = wait_until
        self.wait_selector = wait_selector
        self.challenge_wait = challenge_wait
        self.executable_path = executable_path or find_chromium()

        self.cache = Cache(cache_dir, enabled=use_cache)
        self.cache_ttl = cache_ttl
        self.limiter = RateLimiter(delay)
        # robots.txt goes through the browser too, so a protected host cannot
        # hide its rules from us and get treated as unrestricted by accident.
        self.robots = RobotsPolicy(
            user_agent, respect=respect_robots, fetch_text=self._fetch_text_raw
        )
        self.stats = {"fetched": 0, "cached": 0, "errors": 0, "blocked": 0, "challenges": 0}

        self._playwright = None
        self._browser = None
        self._context = None
        self._delay_applied: set[str] = set()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def _ensure_browser(self):
        if self._context is not None:
            return self._context
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise FetchError(
                "the browser fetcher needs Playwright: pip install playwright "
                "(a Chromium binary must also be available; set STRAIN_DB_CHROMIUM "
                "to point at one, or run `playwright install chromium`)"
            ) from exc

        self._playwright = sync_playwright().start()
        launch_kwargs = {
            "headless": self.headless,
            # Reduce the most obvious headless tells.
            "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        }
        if self.executable_path:
            launch_kwargs["executable_path"] = self.executable_path

        try:
            self._browser = self._playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:  # noqa: BLE001
            self.close()
            raise FetchError(
                f"could not launch Chromium ({exc}). Set STRAIN_DB_CHROMIUM to a "
                "Chromium/Chrome binary, or install one with `playwright install chromium`."
            ) from exc

        self._context = self._browser.new_context(
            user_agent=self.user_agent,
            locale="en-US",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        # navigator.webdriver is the single most-checked automation signal.
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self._context.set_default_timeout(self.timeout * 1000)
        return self._context

    def close(self) -> None:
        for attr in ("_context", "_browser"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:  # noqa: BLE001
                    pass
                setattr(self, attr, None)
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    # ------------------------------------------------------------------
    # fetching
    # ------------------------------------------------------------------

    def _fetch_text_raw(self, url: str) -> str:
        """Fetch a URL's text with no robots check - used for robots.txt itself."""
        context = self._ensure_browser()
        page = context.new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
            if response is not None and response.status >= 400:
                raise FetchError(f"HTTP {response.status} for {url}")
            # robots.txt renders inside a <pre>; innerText avoids HTML escaping.
            return page.evaluate("() => document.body ? document.body.innerText : ''")
        finally:
            page.close()

    def _apply_crawl_delay(self, url: str) -> None:
        host = urlparse(url).netloc
        if host in self._delay_applied:
            return
        self._delay_applied.add(host)
        declared = self.robots.crawl_delay(url)
        if declared and declared > self.limiter.delay:
            log.info("%s declares crawl-delay=%.1fs; slowing down", host, declared)
            self.limiter.delay = declared

    def get(self, url: str, *, headers: dict | None = None, force: bool = False) -> FetchResult:
        cached = None if force else self.cache.get(url, self.cache_ttl)
        if cached is not None:
            self.stats["cached"] += 1
            return cached

        if not self.robots.allowed(url):
            self.stats["blocked"] += 1
            raise RobotsDisallowed(f"robots.txt disallows {url}")

        self._apply_crawl_delay(url)
        host = urlparse(url).netloc
        context = self._ensure_browser()
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            self.limiter.wait(host)
            page = context.new_page()
            try:
                response = page.goto(
                    url, wait_until=self.wait_until, timeout=self.timeout * 1000
                )
                status = response.status if response is not None else 0

                if status >= 400 and status not in (403, 503):
                    raise FetchError(f"HTTP {status} for {url}")

                if self.wait_selector:
                    try:
                        page.wait_for_selector(self.wait_selector, timeout=self.timeout * 1000)
                    except Exception:  # noqa: BLE001 - absent selector is not fatal
                        log.debug("selector %r never appeared on %s", self.wait_selector, url)

                content_type = ""
                if response is not None:
                    content_type = response.headers.get("content-type", "")

                if "json" in content_type.lower():
                    # A JSON endpoint gets rendered into a <pre> by the browser;
                    # returning page.content() would hand the caller HTML and
                    # break json(). Take the text the document actually holds.
                    body = page.evaluate(
                        "() => document.body ? document.body.innerText : ''"
                    )
                    result = FetchResult(
                        url=page.url or url,
                        status=status or 200,
                        text=body,
                        from_cache=False,
                        content_type=content_type,
                    )
                    self.cache.put(result)
                    self.stats["fetched"] += 1
                    return result

                html = page.content()

                # A challenge page resolves itself once the JS finishes; give it
                # a bounded window rather than treating it as content.
                if _is_challenge(html):
                    self.stats["challenges"] += 1
                    log.info("challenge detected on %s; waiting for it to clear", url)
                    html = self._wait_out_challenge(page, html)

                if _is_challenge(html):
                    last_error = FetchError(f"bot challenge did not clear for {url}")
                    log.warning("%s", last_error)
                else:
                    result = FetchResult(
                        url=page.url or url,
                        status=status or 200,
                        text=html,
                        from_cache=False,
                        content_type="text/html",
                    )
                    self.cache.put(result)
                    self.stats["fetched"] += 1
                    return result
            except FetchError:
                raise
            except Exception as exc:  # noqa: BLE001 - navigation/timeouts
                last_error = exc
                if _is_proxy_denial(exc):
                    # Same reasoning as the plain client: an egress-policy
                    # refusal is terminal, so fail fast instead of retrying.
                    self.stats["errors"] += 1
                    raise FetchError(
                        f"blocked by egress policy: {url} (proxy refused the tunnel). "
                        "This host is not permitted from this network."
                    ) from exc
                log.warning("browser fetch error %s (attempt %d): %s", url, attempt + 1, exc)
            finally:
                page.close()

            if attempt < self.max_retries:
                time.sleep((2**attempt) + random.uniform(0, 1))

        self.stats["errors"] += 1
        raise FetchError(f"giving up on {url}: {last_error}")

    def _wait_out_challenge(self, page, html: str) -> str:
        deadline = time.monotonic() + self.challenge_wait
        while time.monotonic() < deadline:
            page.wait_for_timeout(1000)
            try:
                html = page.content()
            except Exception:  # noqa: BLE001 - navigation mid-poll
                continue
            if not _is_challenge(html):
                log.info("challenge cleared for %s", page.url)
                return html
        return html

    def get_json(self, url: str, **kwargs):
        return self.get(url, **kwargs).json()


def _is_proxy_denial(exc: Exception) -> bool:
    """True for a navigation refused by an egress proxy rather than the site."""
    text = str(exc)
    return any(
        marker in text
        for marker in ("ERR_TUNNEL_CONNECTION_FAILED", "ERR_PROXY_CONNECTION_FAILED")
    )


def _is_challenge(html: str) -> bool:
    head = (html or "")[:20000].lower()
    return any(marker in head for marker in CHALLENGE_MARKERS)
