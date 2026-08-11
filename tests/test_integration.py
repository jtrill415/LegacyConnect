"""Integration test against a real local HTTP server.

Everything else in the suite mocks the transport. This one exercises the actual
`http.Client` - robots.txt parsing, rate limiting, the disk cache, retries - plus
the CLI end to end, against a throwaway server on localhost. It is the closest
thing to a live crawl that runs without touching the target sites.
"""

import functools
import http.server
import json
import socket
import threading
from pathlib import Path

import pytest
import yaml

from strain_db.cli import main
from strain_db.http import Client, RobotsDisallowed
from strain_db.sources import SOURCES
from strain_db.sources.base import Source
from strain_db.storage import Database

STRAIN_PAGE = """<!doctype html>
<html><head>
<script id="__NEXT_DATA__" type="application/json">
{{"props":{{"pageProps":{{"strain":{{
  "id":"{slug}","name":"{name}","category":"{category}",
  "description":"Test strain {name}.","thcAvg":{thc},"cbdAvg":0.2,
  "rating":4.2,"reviewCount":100,"lineage":"{lineage}",
  "effects":[{{"name":"Relaxed","score":0.6}},{{"name":"Happy","score":0.4}}],
  "flavors":[{{"name":"Berry"}}],
  "conditions":[{{"name":"Stress"}}]
}}}}}}}}
</script></head><body><h1>{name}</h1></body></html>
"""

ROBOTS = """User-agent: *
Disallow: /private/
Crawl-delay: 0
"""

STRAINS = [
    ("blue-dream", "Blue Dream", "Sativa-dominant Hybrid", 18.5, "Blueberry x Haze"),
    ("og-kush", "OG Kush", "Indica-dominant Hybrid", 22.0, "Chemdawg x Hindu Kush"),
    ("northern-lights", "Northern Lights", "Indica", 20.0, "Afghani x Thai"),
]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    (root / "strains").mkdir()
    (root / "private").mkdir()
    (root / "robots.txt").write_text(ROBOTS)

    for slug, name, category, thc, lineage in STRAINS:
        (root / "strains" / f"{slug}.html").write_text(
            STRAIN_PAGE.format(slug=slug, name=name, category=category, thc=thc, lineage=lineage)
        )
    (root / "private" / "secret.html").write_text("<h1>Secret</h1>")

    port = _free_port()
    urls = "".join(
        f"<url><loc>http://127.0.0.1:{port}/strains/{slug}.html</loc></url>"
        for slug, *_ in STRAINS
    )
    (root / "sitemap.xml").write_text(f"<urlset>{urls}</urlset>")

    handler = functools.partial(_QuietHandler, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def spec(server, tmp_path):
    return {
        "name": "leafly",  # reuse the leafly adapter's __NEXT_DATA__ handling
        "base_url": server,
        "listing": {
            "mode": ["sitemap"],
            "sitemaps": [f"{server}/sitemap.xml"],
            "detail_url_re": r"/strains/[a-z-]+\.html$",
        },
        "detail": {
            "state_root": "props.pageProps.strain",
            "state_paths": {
                "name": "name",
                "source_id": "id",
                "type": "category",
                "description": "description",
                "thc": "thcAvg",
                "cbd": "cbdAvg",
                "rating": "rating",
                "review_count": "reviewCount",
                "lineage": "lineage",
                "effects": "effects",
                "flavors": "flavors",
                "medical": "conditions",
            },
        },
    }


def make_client(tmp_path, **kw):
    return Client(cache_dir=tmp_path / "cache", delay=0.0, timeout=10, **kw)


class TestRobots:
    def test_allowed_path_fetches(self, server, tmp_path):
        client = make_client(tmp_path)
        assert client.get(f"{server}/robots.txt").status == 200

    def test_disallowed_path_is_blocked(self, server, tmp_path):
        client = make_client(tmp_path)
        with pytest.raises(RobotsDisallowed):
            client.get(f"{server}/private/secret.html")
        assert client.stats["blocked"] == 1

    def test_override_allows_it(self, server, tmp_path):
        client = make_client(tmp_path, respect_robots=False)
        assert client.get(f"{server}/private/secret.html").status == 200


class TestCache:
    def test_second_fetch_is_served_from_cache(self, server, tmp_path):
        client = make_client(tmp_path)
        url = f"{server}/strains/blue-dream.html"
        first = client.get(url)
        second = client.get(url)
        assert first.from_cache is False
        assert second.from_cache is True
        assert client.stats == {"fetched": 1, "cached": 1, "errors": 0, "blocked": 0}

    def test_force_bypasses_cache(self, server, tmp_path):
        client = make_client(tmp_path)
        url = f"{server}/strains/og-kush.html"
        client.get(url)
        assert client.get(url, force=True).from_cache is False

    def test_cache_survives_a_new_client(self, server, tmp_path):
        url = f"{server}/strains/og-kush.html"
        make_client(tmp_path).get(url)
        assert make_client(tmp_path).get(url).from_cache is True

    def test_missing_page_raises(self, server, tmp_path):
        from strain_db.http import FetchError

        with pytest.raises(FetchError):
            make_client(tmp_path).get(f"{server}/strains/nope.html")


class TestCrawl:
    def test_discovers_all_strains(self, tmp_path, spec):
        source = Source(make_client(tmp_path), spec=spec)
        source.name = "leafly"
        assert len(list(source.discover())) == len(STRAINS)

    def test_crawls_and_normalizes(self, tmp_path, spec):
        source = SOURCES["leafly"](make_client(tmp_path), spec=spec)
        strains = list(source.crawl())
        assert len(strains) == len(STRAINS)

        by_key = {s.canonical_key: s for s in strains}
        bd = by_key["blue-dream"]
        assert bd.name == "Blue Dream"
        assert bd.type.value == "sativa_dominant"
        assert bd.thc.avg == 18.5
        assert bd.parents == ["Blueberry", "Haze"]
        assert {t.name for t in bd.effects} == {"relaxed", "happy"}
        assert dict((t.name, t.score) for t in bd.effects)["relaxed"] == 0.6
        assert "berry" in {t.name for t in bd.flavors}
        assert "stress" in {t.name for t in bd.medical}

    def test_reparse_from_cache_needs_no_network(self, tmp_path, spec, server):
        client = make_client(tmp_path)
        source = SOURCES["leafly"](client, spec=spec)
        assert len(list(source.crawl())) == len(STRAINS)

        # Point the offline client at a dead port: everything must come from cache.
        offline = Client(cache_dir=tmp_path / "cache", delay=0.0, timeout=1)
        count = 0
        for cached in offline.cache.iter_urls(host_filter="127.0.0.1"):
            if "/strains/" not in cached.url:
                continue
            raw = source.parse_detail(cached.url, cached.text)
            if raw:
                count += 1
        assert count == len(STRAINS)


class TestCliEndToEnd:
    def test_crawl_export_search_stats(self, tmp_path, spec, server, monkeypatch, capsys):
        # Point the leafly source at the local test server.
        spec_path = tmp_path / "leafly.yaml"
        spec_path.write_text(yaml.safe_dump(spec))
        monkeypatch.setattr(
            "strain_db.sources.base.load_spec", lambda name: yaml.safe_load(spec_path.read_text())
        )

        db_path = str(tmp_path / "cli.db")
        cache = str(tmp_path / "clicache")
        base = ["--db", db_path, "--cache-dir", cache, "--delay", "0"]

        assert main(base + ["crawl", "leafly"]) == 0

        db = Database(db_path)
        assert len(db.all()) == len(STRAINS)
        assert db.get("blue-dream").thc.avg == 18.5
        db.close()

        out_json = tmp_path / "out.json"
        assert main(base + ["export", str(out_json), "--format", "json"]) == 0
        assert len(json.loads(out_json.read_text())) == len(STRAINS)

        out_csv = tmp_path / "out.csv"
        assert main(base + ["export", str(out_csv), "--format", "csv"]) == 0
        assert "Blue Dream" in out_csv.read_text()

        assert main(base + ["search", "Blue Dream"]) == 0
        assert "Blue Dream" in capsys.readouterr().out

        assert main(base + ["stats"]) == 0
        stats = json.loads(capsys.readouterr().out)
        assert stats["strains"] == len(STRAINS)

    def test_crawl_is_idempotent(self, tmp_path, spec, monkeypatch):
        spec_path = tmp_path / "leafly.yaml"
        spec_path.write_text(yaml.safe_dump(spec))
        monkeypatch.setattr(
            "strain_db.sources.base.load_spec", lambda name: yaml.safe_load(spec_path.read_text())
        )
        db_path = str(tmp_path / "idem.db")
        base = ["--db", db_path, "--cache-dir", str(tmp_path / "c"), "--delay", "0"]
        main(base + ["crawl", "leafly"])
        main(base + ["crawl", "leafly"])
        db = Database(db_path)
        assert len(db.all()) == len(STRAINS), "re-crawling must update, not duplicate"
        db.close()

    def test_probe_reports_on_a_live_page(self, tmp_path, spec, server, monkeypatch, capsys):
        spec_path = tmp_path / "leafly.yaml"
        spec_path.write_text(yaml.safe_dump(spec))
        monkeypatch.setattr(
            "strain_db.sources.base.load_spec", lambda name: yaml.safe_load(spec_path.read_text())
        )
        rc = main(
            ["--cache-dir", str(tmp_path / "c"), "--delay", "0", "probe", "leafly",
             f"{server}/strains/blue-dream.html"]
        )
        report = json.loads(capsys.readouterr().out)
        assert report["app_state"]["found"] == "__NEXT_DATA__"
        assert report["app_state"]["configured_root_resolves"] is True
        assert report["normalized_sample"]["name"] == "Blue Dream"
        assert report["looks_like_challenge"] is False
        assert rc in (0, 2)
