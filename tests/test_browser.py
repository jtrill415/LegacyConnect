"""Tests for the Playwright-backed fetcher, against a real local server.

These drive an actual Chromium instance. They are skipped when Playwright or a
browser binary is unavailable, so the suite still runs in a plain environment.

The JS-rendering test is the one that matters: it serves a page whose content
only exists after a script runs, which the plain HTTP client cannot see and the
browser client can. That is the whole reason this fetcher exists.
"""

import functools
import http.server
import socket
import threading

import pytest

from strain_db.http import Client, FetchError, RobotsDisallowed

playwright = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

from strain_db.browser import BrowserClient, find_chromium, _is_challenge  # noqa: E402

pytestmark = pytest.mark.skipif(
    find_chromium() is None, reason="no Chromium binary available"
)

ROBOTS = """User-agent: *
Disallow: /private/
"""

# Content that only exists after JS runs - invisible to a plain HTTP fetch.
JS_PAGE = """<!doctype html><html><head><title>loading</title></head>
<body><div id="app">loading...</div>
<script>
  document.getElementById('app').innerHTML =
    '<h1>Rendered Strain</h1><script-marker></script-marker>';
  var s = document.createElement('script');
  s.id = '__NEXT_DATA__'; s.type = 'application/json';
  s.textContent = JSON.stringify({props:{pageProps:{strain:{
    name:'JS Strain', category:'Indica', thcAvg:21.5,
    effects:[{name:'Relaxed',score:0.7}]}}}});
  document.head.appendChild(s);
</script></body></html>
"""

# Serves a challenge first, then real content - like a cleared JS challenge.
CHALLENGE_PAGE = """<!doctype html><html><body>
<h1>Just a moment...</h1><p>Checking your browser before accessing.</p>
<script>
  setTimeout(function(){
    document.body.innerHTML = '<h1>Real Content</h1><p>strain data here</p>';
  }, 1500);
</script></body></html>
"""

JSON_BODY = '{"data": {"strains": [{"slug": "blue-dream", "name": "Blue Dream"}]}}'


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        routes = {
            "/robots.txt": ("text/plain", ROBOTS),
            "/js.html": ("text/html", JS_PAGE),
            "/challenge.html": ("text/html", CHALLENGE_PAGE),
            "/api.json": ("application/json", JSON_BODY),
            "/private/secret.html": ("text/html", "<h1>Secret</h1>"),
            "/plain.html": ("text/html", "<html><body><h1>Plain</h1></body></html>"),
        }
        if self.path not in routes:
            self.send_error(404)
            return
        ctype, body = routes[self.path]
        payload = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture(scope="module")
def server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


@pytest.fixture
def browser(tmp_path):
    client = BrowserClient(
        cache_dir=tmp_path / "cache", delay=0.0, timeout=30, challenge_wait=8.0
    )
    yield client
    client.close()


class TestChallengeDetection:
    def test_recognises_markers(self):
        assert _is_challenge("<h1>Just a moment...</h1>")
        assert _is_challenge("<div>Checking your browser</div>")

    def test_ignores_normal_content(self):
        assert not _is_challenge("<h1>Blue Dream</h1><p>A hybrid strain.</p>")

    def test_handles_empty(self):
        assert not _is_challenge("")


class TestBrowserFetch:
    def test_fetches_plain_page(self, browser, server):
        result = browser.get(f"{server}/plain.html")
        assert result.status == 200
        assert "Plain" in result.text
        assert browser.stats["fetched"] == 1

    def test_sees_js_rendered_content(self, browser, server):
        """The point of the whole module: JS-injected content is in the DOM."""
        from strain_db.extract import app_state, soup_of

        soup = soup_of(browser.get(f"{server}/js.html").text)
        assert soup.select_one("h1").get_text() == "Rendered Strain"
        assert soup.find(id="__NEXT_DATA__") is not None

        name, state = app_state(browser.get(f"{server}/js.html").text)
        assert state["props"]["pageProps"]["strain"]["name"] == "JS Strain"

    def test_plain_client_cannot_see_it(self, tmp_path, server):
        """Contrast case, proving the browser client is doing real work.

        The raw HTML does contain these strings - inside a <script> body - so
        the assertion has to be about the parsed DOM, which is what every
        extraction strategy actually reads.
        """
        from strain_db.extract import app_state, soup_of

        plain = Client(cache_dir=tmp_path / "c2", delay=0.0, timeout=10)
        text = plain.get(f"{server}/js.html").text
        plain.close()

        soup = soup_of(text)
        assert soup.select_one("h1") is None, "no rendered heading without JS"
        assert soup.find(id="__NEXT_DATA__") is None, "the payload is injected by JS"
        assert app_state(text) == (None, None)

    def test_waits_out_a_challenge(self, browser, server):
        result = browser.get(f"{server}/challenge.html")
        assert "Real Content" in result.text
        assert browser.stats["challenges"] == 1

    def test_json_endpoint_returns_parseable_json(self, browser, server):
        """A browser wraps JSON in HTML; the client must hand back raw JSON."""
        result = browser.get(f"{server}/api.json")
        assert result.json()["data"]["strains"][0]["slug"] == "blue-dream"

    def test_404_raises(self, browser, server):
        with pytest.raises(FetchError):
            browser.get(f"{server}/nope.html")


class TestBrowserPolicy:
    def test_robots_is_enforced(self, browser, server):
        with pytest.raises(RobotsDisallowed):
            browser.get(f"{server}/private/secret.html")
        assert browser.stats["blocked"] == 1

    def test_robots_read_through_the_browser(self, browser, server):
        assert browser.robots.allowed(f"{server}/plain.html") is True
        assert browser.robots.allowed(f"{server}/private/x.html") is False

    def test_override(self, tmp_path, server):
        client = BrowserClient(
            cache_dir=tmp_path / "c3", delay=0.0, timeout=30, respect_robots=False
        )
        try:
            assert client.get(f"{server}/private/secret.html").status == 200
        finally:
            client.close()

    def test_cache_is_shared_with_the_plain_client(self, tmp_path, server):
        """A browser crawl must be reparseable by the offline path."""
        cache = tmp_path / "shared"
        bc = BrowserClient(cache_dir=cache, delay=0.0, timeout=30)
        try:
            bc.get(f"{server}/js.html")
        finally:
            bc.close()

        plain = Client(cache_dir=cache, delay=0.0, timeout=5)
        cached = plain.get(f"{server}/js.html")
        plain.close()
        assert cached.from_cache is True
        assert "JS Strain" in cached.text

    def test_second_fetch_hits_cache(self, browser, server):
        url = f"{server}/plain.html"
        browser.get(url)
        assert browser.get(url).from_cache is True
        assert browser.stats["cached"] == 1


class TestSourceIntegration:
    def test_source_parses_a_js_rendered_page(self, browser, server):
        """End to end: browser fetch -> spec extraction -> normalized strain."""
        from strain_db.sources import SOURCES

        spec = {
            "name": "leafly",
            "base_url": server,
            "listing": {"mode": ["sitemap"], "sitemaps": [f"{server}/sitemap.xml"]},
            "detail": {
                "state_root": "props.pageProps.strain",
                "state_paths": {
                    "name": "name",
                    "type": "category",
                    "thc": "thcAvg",
                    "effects": "effects",
                },
            },
        }
        source = SOURCES["leafly"](browser, spec=spec)
        url = f"{server}/js.html"
        raw = source.parse_detail(url, browser.get(url).text)
        assert raw is not None
        strain = source.to_strain(raw)
        assert strain.name == "JS Strain"
        assert strain.type.value == "indica"
        assert strain.thc.avg == 21.5
        assert "relaxed" in {t.name for t in strain.effects}


class TestClientSelection:
    def test_auto_uses_browser_only_for_protected_sources(self):
        from strain_db.cli import build_parser, wants_browser

        args = build_parser().parse_args(["crawl"])
        assert wants_browser(args, "leafly") is True
        assert wants_browser(args, "weedmaps") is True
        assert wants_browser(args, "cannaconnection") is False
        assert wants_browser(args, "seedfinder") is False

    def test_never_and_always_override(self):
        from strain_db.cli import build_parser, wants_browser

        parser = build_parser()
        never = parser.parse_args(["crawl", "--browser", "never"])
        always = parser.parse_args(["crawl", "--browser", "always"])
        assert wants_browser(never, "leafly") is False
        assert wants_browser(always, "seedfinder") is True

    def test_make_client_returns_the_right_type(self, tmp_path):
        from strain_db.cli import build_parser, make_client

        args = build_parser().parse_args(
            ["crawl", "--cache-dir", str(tmp_path / "c"), "--browser", "never"]
        )
        client = make_client(args, "leafly")
        try:
            assert isinstance(client, Client)
        finally:
            client.close()
