"""Polite HTTP client: robots.txt, rate limiting, disk cache, retries.

The cache is not an optimization detail - it is what makes this pipeline
workable. Parsers for sites like these break whenever the site ships a redesign,
and re-crawling tens of thousands of pages to test a one-line selector fix is
both slow and rude. Every fetched body is written to disk, so `reparse` can
rebuild the whole database from a previous crawl with zero network traffic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "StrainDBBot/1.0 (+https://github.com/jtrill415/legacyconnect; "
    "research crawler; contact via repository issues)"
)


class RobotsDisallowed(RuntimeError):
    """Raised when robots.txt forbids a URL and robots are being respected."""


class FetchError(RuntimeError):
    pass


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    from_cache: bool
    content_type: str = ""

    def json(self):
        return json.loads(self.text)


class RateLimiter:
    """One token bucket per host. Serializes politeness across threads."""

    def __init__(self, delay: float, jitter: float = 0.3):
        self.delay = delay
        self.jitter = jitter
        self._next_ok: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                ready_at = self._next_ok.get(host, 0.0)
                if now >= ready_at:
                    delay = self.delay + random.uniform(0, self.jitter)
                    self._next_ok[host] = now + delay
                    return
                sleep_for = ready_at - now
            time.sleep(min(sleep_for, 5.0))


class Cache:
    """Content-addressed response cache keyed by URL."""

    def __init__(self, root: Path, enabled: bool = True):
        self.root = Path(root)
        self.enabled = enabled
        if enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        host = urlparse(url).netloc.replace(":", "_") or "unknown"
        return self.root / host / digest[:2] / f"{digest}.json"

    def get(self, url: str, max_age: float | None = None) -> FetchResult | None:
        if not self.enabled:
            return None
        p = self._path(url)
        if not p.exists():
            return None
        if max_age is not None and (time.time() - p.stat().st_mtime) > max_age:
            return None
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return FetchResult(
            url=payload.get("url", url),
            status=payload.get("status", 200),
            text=payload.get("text", ""),
            from_cache=True,
            content_type=payload.get("content_type", ""),
        )

    def put(self, result: FetchResult) -> None:
        if not self.enabled:
            return
        p = self._path(result.url)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "url": result.url,
                    "status": result.status,
                    "text": result.text,
                    "content_type": result.content_type,
                    "cached_at": time.time(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(p)

    def iter_urls(self, host_filter: str | None = None):
        if not self.root.exists():
            return
        for path in sorted(self.root.rglob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            url = payload.get("url", "")
            if host_filter and host_filter not in urlparse(url).netloc:
                continue
            yield FetchResult(
                url=url,
                status=payload.get("status", 200),
                text=payload.get("text", ""),
                from_cache=True,
                content_type=payload.get("content_type", ""),
            )


class RobotsPolicy:
    """robots.txt gate, with per-host crawl-delay support.

    Fetched through plain urllib rather than the caching client to avoid a
    circular dependency, and cached in memory per host.
    """

    def __init__(self, user_agent: str, respect: bool = True, session=None):
        self.user_agent = user_agent
        self.respect = respect
        self._parsers: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()
        self._session = session

    def _parser(self, url: str):
        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"
        with self._lock:
            if host in self._parsers:
                return self._parsers[host]
        rp = urllib.robotparser.RobotFileParser()
        robots_url = f"{host}/robots.txt"
        try:
            if self._session is not None:
                resp = self._session.get(robots_url, timeout=20)
                if resp.status_code >= 400:
                    raise FetchError(str(resp.status_code))
                rp.parse(resp.text.splitlines())
            else:
                rp.set_url(robots_url)
                rp.read()
        except Exception as exc:  # noqa: BLE001 - unreachable robots must not crash a crawl
            log.warning("could not read %s (%s); treating as unrestricted", robots_url, exc)
            rp = None
        with self._lock:
            self._parsers[host] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if not self.respect:
            return True
        rp = self._parser(url)
        if rp is None:
            return True
        return rp.can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        rp = self._parser(url)
        if rp is None:
            return None
        try:
            delay = rp.crawl_delay(self.user_agent)
        except Exception:  # noqa: BLE001
            return None
        return float(delay) if delay else None


def _is_proxy_denial(exc: Exception) -> bool:
    """True for a CONNECT rejected by an egress proxy on policy grounds."""
    if not isinstance(exc, requests.exceptions.ProxyError):
        return False
    text = str(exc)
    return "403" in text or "407" in text


class Client:
    """The fetch primitive every source adapter uses."""

    RETRY_STATUS = {429, 500, 502, 503, 504, 522, 524}

    def __init__(
        self,
        cache_dir: Path,
        user_agent: str = DEFAULT_UA,
        delay: float = 2.0,
        timeout: float = 30.0,
        max_retries: int = 4,
        respect_robots: bool = True,
        use_cache: bool = True,
        cache_ttl: float | None = None,
        proxies: dict[str, str] | None = None,
    ):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        if proxies:
            self.session.proxies.update(proxies)
        self.timeout = timeout
        self.max_retries = max_retries
        self.cache = Cache(cache_dir, enabled=use_cache)
        self.cache_ttl = cache_ttl
        self.limiter = RateLimiter(delay)
        self.robots = RobotsPolicy(user_agent, respect=respect_robots, session=self.session)
        self._delay_applied: set[str] = set()
        self.stats = {"fetched": 0, "cached": 0, "errors": 0, "blocked": 0}

    def _apply_crawl_delay(self, url: str) -> None:
        """Honor a site's declared crawl-delay if it is slower than ours."""
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
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            self.limiter.wait(host)
            try:
                resp = self.session.get(url, timeout=self.timeout, headers=headers)
            except requests.RequestException as exc:
                last_error = exc
                if _is_proxy_denial(exc):
                    # An egress-policy denial (403/407 on CONNECT) is terminal:
                    # retrying cannot change the answer, it just hammers the proxy.
                    self.stats["errors"] += 1
                    raise FetchError(
                        f"blocked by egress policy: {url} (proxy refused the tunnel). "
                        "This host is not permitted from this network."
                    ) from exc
                log.warning("fetch error %s (attempt %d): %s", url, attempt + 1, exc)
            else:
                if resp.status_code in self.RETRY_STATUS:
                    last_error = FetchError(f"HTTP {resp.status_code}")
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        time.sleep(min(float(retry_after), 60.0))
                    log.warning("HTTP %s for %s (attempt %d)", resp.status_code, url, attempt + 1)
                elif resp.status_code >= 400:
                    # 403/404 are terminal: retrying will not change the answer
                    # and hammering a 403 is exactly what gets a crawler banned.
                    self.stats["errors"] += 1
                    raise FetchError(f"HTTP {resp.status_code} for {url}")
                else:
                    result = FetchResult(
                        url=resp.url,
                        status=resp.status_code,
                        text=resp.text,
                        from_cache=False,
                        content_type=resp.headers.get("Content-Type", ""),
                    )
                    self.cache.put(result)
                    self.stats["fetched"] += 1
                    return result

            if attempt < self.max_retries:
                backoff = (2**attempt) + random.uniform(0, 1)
                time.sleep(backoff)

        self.stats["errors"] += 1
        raise FetchError(f"giving up on {url}: {last_error}")

    def get_json(self, url: str, **kwargs):
        return self.get(url, **kwargs).json()
