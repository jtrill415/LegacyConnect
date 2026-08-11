"""End-to-end pipeline tests over synthetic fixtures.

IMPORTANT: the fixtures mirror the *shape* each spec expects, not the real
sites' current markup (the build environment cannot reach them). These tests
prove the pipeline is correct - discovery, extraction, normalization, merge,
storage. They cannot prove a spec's selectors match production HTML; that is
what `strain-db probe` is for.
"""

from pathlib import Path

import pytest

from strain_db.merge import group_and_merge
from strain_db.models import StrainType
from strain_db.sources import SOURCES, CannaConnectionSource, LeaflySource
from strain_db.storage import Database

FIXTURES = Path(__file__).parent / "fixtures"


class FakeClient:
    """Stands in for http.Client; serves canned bodies, records requests."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.requested: list[str] = []
        self.stats = {}

    def get(self, url, *, headers=None, force=False):
        self.requested.append(url)
        if url not in self.pages:
            from strain_db.http import FetchError

            raise FetchError(f"no fixture for {url}")

        from strain_db.http import FetchResult

        return FetchResult(url=url, status=200, text=self.pages[url], from_cache=True)


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestSpecsLoad:
    @pytest.mark.parametrize("name", sorted(SOURCES))
    def test_every_source_has_a_valid_spec(self, name):
        source = SOURCES[name](FakeClient({}))
        assert source.spec.get("name") == name
        assert source.spec.get("base_url", "").startswith("http")
        assert source.spec.get("listing"), "spec must define discovery"
        assert source.spec.get("detail"), "spec must define detail extraction"


class TestLeaflyParse:
    def setup_method(self):
        url = "https://www.leafly.com/strains/blue-dream"
        self.source = LeaflySource(FakeClient({url: read("leafly_strain.html")}))
        self.raw = self.source.parse_detail(url, read("leafly_strain.html"))
        self.strain = self.source.to_strain(self.raw)

    def test_name_and_key(self):
        assert self.strain.name == "Blue Dream"
        assert self.strain.canonical_key == "blue-dream"

    def test_type_from_app_state(self):
        assert self.strain.type is StrainType.SATIVA_DOMINANT

    def test_potency(self):
        assert self.strain.thc.avg == 18.5

    def test_effects_normalized_with_scores(self):
        by_name = {t.name: t.score for t in self.strain.effects}
        assert by_name["relaxed"] == 0.55
        assert "euphoric" in by_name

    def test_negatives_separated_from_effects(self):
        assert "dry_mouth" in {t.name for t in self.strain.negatives}

    def test_medical_conditions(self):
        assert {"stress", "anxiety"} <= {t.name for t in self.strain.medical}

    def test_flavors_and_terpenes(self):
        assert "berry" in {t.name for t in self.strain.flavors}
        assert "myrcene" in {t.name for t in self.strain.terpenes}

    def test_lineage_becomes_parents(self):
        assert self.strain.parents == ["Blueberry", "Haze"]

    def test_rating_from_state(self):
        assert self.strain.rating == 4.4
        assert self.strain.review_count == 12043

    def test_source_recorded(self):
        assert self.strain.sources[0].source == "leafly"


class TestCannaConnectionParse:
    def setup_method(self):
        url = "https://www.cannaconnection.com/strains/blue-dream"
        html = read("cannaconnection_strain.html")
        self.source = CannaConnectionSource(FakeClient({url: html}))
        self.strain = self.source.to_strain(self.source.parse_detail(url, html))

    def test_name(self):
        assert self.strain.name == "Blue Dream"

    def test_type_from_table_ratio(self):
        assert self.strain.type is StrainType.SATIVA_DOMINANT
        assert self.strain.sativa_pct == 60.0
        assert self.strain.indica_pct == 40.0

    def test_thc_range_from_table(self):
        assert (self.strain.thc.min, self.strain.thc.max) == (17.0, 24.0)

    def test_breeder_from_table(self):
        assert self.strain.breeder == "DJ Short"

    def test_grow_data_from_table(self):
        g = self.strain.grow
        assert (g.flowering_days_min, g.flowering_days_max) == (63, 70)
        assert "500" in g.yield_indoor
        assert g.difficulty == "Easy"

    def test_genetics_from_table(self):
        assert self.strain.parents == ["Blueberry", "Haze"]

    def test_effects_normalized(self):
        assert "relaxed" in {t.name for t in self.strain.effects}

    def test_flavor_variant_normalized(self):
        # "Blueberry" must fold into the controlled "berry" term
        assert "berry" in {t.name for t in self.strain.flavors}

    def test_awards(self):
        assert self.strain.awards and "Cannabis Cup" in self.strain.awards[0]


class TestDiscovery:
    def test_html_listing_yields_detail_links(self):
        listing_html = """
          <a href="/strains/blue-dream">Blue Dream</a>
          <a href="/strains/og-kush">OG Kush</a>
          <a href="/about">About</a>
        """
        pages = {"https://www.cannaconnection.com/strains?page=1": listing_html}
        source = CannaConnectionSource(FakeClient(pages))
        source.spec["listing"]["mode"] = ["html"]
        urls = list(source.discover(limit=10))
        assert "https://www.cannaconnection.com/strains/blue-dream" in urls
        assert not any("/about" in u for u in urls)

    def test_sitemap_index_is_followed(self):
        index = """<sitemapindex><sitemap><loc>https://s.test/a.xml</loc></sitemap></sitemapindex>"""
        child = """<urlset>
            <url><loc>https://www.leafly.com/strains/blue-dream</loc></url>
            <url><loc>https://www.leafly.com/news/thing</loc></url>
        </urlset>"""
        pages = {"https://www.leafly.com/sitemap.xml": index, "https://s.test/a.xml": child}
        source = LeaflySource(FakeClient(pages))
        source.spec["listing"]["mode"] = ["sitemap"]
        urls = list(source.discover())
        assert urls == ["https://www.leafly.com/strains/blue-dream"]

    def test_limit_is_respected(self):
        child = "<urlset>" + "".join(
            f"<url><loc>https://www.leafly.com/strains/s{i}</loc></url>" for i in range(10)
        ) + "</urlset>"
        source = LeaflySource(FakeClient({"https://www.leafly.com/sitemap.xml": child}))
        source.spec["listing"]["mode"] = ["sitemap"]
        assert len(list(source.discover(limit=3))) == 3


class TestFullPipeline:
    def test_two_sources_merge_into_one_row(self, tmp_path):
        leafly_url = "https://www.leafly.com/strains/blue-dream"
        cc_url = "https://www.cannaconnection.com/strains/blue-dream"

        leafly = LeaflySource(FakeClient({leafly_url: read("leafly_strain.html")}))
        cc = CannaConnectionSource(FakeClient({cc_url: read("cannaconnection_strain.html")}))

        records = [
            leafly.to_strain(leafly.parse_detail(leafly_url, read("leafly_strain.html"))),
            cc.to_strain(cc.parse_detail(cc_url, read("cannaconnection_strain.html"))),
        ]
        merged = group_and_merge(records)
        assert len(merged) == 1

        strain = merged[0]
        # Breeder and grow data come from CannaConnection; potency range widens
        # to cover both sources; effects pool together.
        assert strain.breeder == "DJ Short"
        assert strain.grow.flowering_days_min == 63
        assert strain.thc.min == 17.0 and strain.thc.max == 24.0
        assert {"relaxed", "euphoric", "creative"} <= {t.name for t in strain.effects}
        assert len(strain.sources) == 2

        db = Database(tmp_path / "pipeline.db")
        assert db.upsert_many(merged) == 1
        stored = db.get("blue-dream")
        assert stored.breeder == "DJ Short"
        assert stored.thc.max == 24.0
        assert {s.source for s in stored.sources} == {"leafly", "cannaconnection"}
        db.close()

    def test_unparseable_page_is_skipped_not_fatal(self):
        url = "https://www.leafly.com/strains/x"
        source = LeaflySource(FakeClient({url: "<html><body>nothing here</body></html>"}))
        assert source.parse_detail(url, "<html><body>nothing</body></html>") is None
