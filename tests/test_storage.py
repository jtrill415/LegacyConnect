import csv
import json

import pytest

from strain_db.models import (
    Environment,
    GrowInfo,
    Measurement,
    ScoredTerm,
    SourceRef,
    Strain,
    StrainType,
)
from strain_db.storage import Database, export


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


def sample() -> Strain:
    s = Strain(
        name="Blue Dream",
        slug="blue-dream",
        canonical_key="blue-dream",
        aka=["Azure Haze"],
        type=StrainType.SATIVA_DOMINANT,
        indica_pct=40.0,
        sativa_pct=60.0,
        breeder="DJ Short",
        parents=["Blueberry", "Haze"],
        lineage_text="Blueberry x Haze",
        thc=Measurement(min=17.0, max=24.0, avg=20.5),
        cbd=Measurement(avg=0.1),
        description="A sativa-dominant hybrid.",
        rating=4.4,
        review_count=12043,
        images=["https://img.test/bd.jpg"],
    )
    s.effects = [ScoredTerm("relaxed", 0.55), ScoredTerm("happy", 0.48)]
    s.flavors = [ScoredTerm("berry", None)]
    s.terpenes = [ScoredTerm("myrcene", 0.4)]
    s.medical = [ScoredTerm("stress", 0.3)]
    s.grow = GrowInfo(
        flowering_days_min=63,
        flowering_days_max=70,
        yield_indoor="500 g/m2",
        environment=Environment.INDOOR,
    )
    s.sources = [
        SourceRef("leafly", "https://leafly.test/blue-dream"),
        SourceRef("seedfinder", "https://seedfinder.test/blue-dream"),
    ]
    s.provenance = {"breeder": "seedfinder"}
    return s


class TestRoundTrip:
    def test_scalars_survive(self, db):
        db.upsert_many([sample()])
        got = db.get("blue-dream")
        assert got.name == "Blue Dream"
        assert got.type is StrainType.SATIVA_DOMINANT
        assert got.breeder == "DJ Short"
        assert got.rating == 4.4
        assert got.aka == ["Azure Haze"]

    def test_measurements_survive(self, db):
        db.upsert_many([sample()])
        got = db.get("blue-dream")
        assert (got.thc.min, got.thc.max, got.thc.avg) == (17.0, 24.0, 20.5)

    def test_terms_survive_with_scores(self, db):
        db.upsert_many([sample()])
        got = db.get("blue-dream")
        assert {t.name for t in got.effects} == {"relaxed", "happy"}
        assert dict((t.name, t.score) for t in got.effects)["relaxed"] == 0.55
        assert got.flavors[0].score is None

    def test_parents_keep_order(self, db):
        db.upsert_many([sample()])
        assert db.get("blue-dream").parents == ["Blueberry", "Haze"]

    def test_grow_survives(self, db):
        db.upsert_many([sample()])
        g = db.get("blue-dream").grow
        assert g.flowering_days_min == 63
        assert g.environment is Environment.INDOOR

    def test_sources_survive(self, db):
        db.upsert_many([sample()])
        assert {s.source for s in db.get("blue-dream").sources} == {"leafly", "seedfinder"}

    def test_missing_key_returns_none(self, db):
        assert db.get("nope") is None


class TestUpsert:
    def test_is_idempotent(self, db):
        db.upsert_many([sample(), sample()])
        assert len(db.all()) == 1
        assert len(db.get("blue-dream").effects) == 2, "terms must not accumulate duplicates"

    def test_does_not_null_out_existing_data(self, db):
        db.upsert_many([sample()])
        sparse = Strain(name="Blue Dream", slug="blue-dream", canonical_key="blue-dream")
        sparse.sources = [SourceRef("weedmaps", "https://weedmaps.test/blue-dream")]
        db.upsert_many([sparse])
        got = db.get("blue-dream")
        assert got.breeder == "DJ Short", "a sparse re-crawl must not erase known fields"
        assert got.thc.avg == 20.5

    def test_unknown_type_does_not_overwrite(self, db):
        db.upsert_many([sample()])
        sparse = Strain(name="Blue Dream", canonical_key="blue-dream", type=StrainType.UNKNOWN)
        db.upsert_many([sparse])
        assert db.get("blue-dream").type is StrainType.SATIVA_DOMINANT

    def test_requires_key(self, db):
        with pytest.raises(ValueError):
            db.upsert(Strain(name="X"))


class TestSearch:
    def test_by_name(self, db):
        db.upsert_many([sample()])
        assert db.search("Blue Dream")[0].canonical_key == "blue-dream"

    def test_by_breeder(self, db):
        db.upsert_many([sample()])
        assert db.search("DJ Short")

    def test_no_match(self, db):
        db.upsert_many([sample()])
        assert db.search("zzzznope") == []

    def test_malformed_fts_query_does_not_raise(self, db):
        db.upsert_many([sample()])
        assert isinstance(db.search('"unbalanced'), list)


class TestStats:
    def test_counts(self, db):
        db.upsert_many([sample()])
        st = db.stats()
        assert st["strains"] == 1
        assert st["with_thc"] == 1
        assert st["with_genetics"] == 1
        assert st["multi_source"] == 1
        assert st["by_source"]["leafly"] == 1


class TestExport:
    def test_json(self, db, tmp_path):
        db.upsert_many([sample()])
        out = tmp_path / "out.json"
        assert export(db, out, "json") == 1
        data = json.loads(out.read_text())
        assert data[0]["name"] == "Blue Dream"
        assert data[0]["thc"]["avg"] == 20.5

    def test_jsonl(self, db, tmp_path):
        db.upsert_many([sample()])
        out = tmp_path / "out.jsonl"
        export(db, out, "jsonl")
        lines = [json.loads(l) for l in out.read_text().splitlines()]
        assert len(lines) == 1

    def test_csv(self, db, tmp_path):
        db.upsert_many([sample()])
        out = tmp_path / "out.csv"
        export(db, out, "csv")
        rows = list(csv.DictReader(out.open()))
        assert rows[0]["name"] == "Blue Dream"
        assert rows[0]["parents"] == "Blueberry x Haze"
        assert "relaxed" in rows[0]["effects"]
        assert set(rows[0]["sources"].split("; ")) == {"leafly", "seedfinder"}

    def test_bad_format(self, db, tmp_path):
        with pytest.raises(ValueError):
            export(db, tmp_path / "x.xml", "xml")
