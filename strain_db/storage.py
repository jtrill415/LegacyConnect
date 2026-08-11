"""SQLite persistence and exports.

The schema keeps one row per canonical strain plus child tables for the
many-valued fields, so questions like "which indica-dominant strains report
myrcene and relaxation" are ordinary SQL rather than JSON surgery.

Every write is an upsert keyed on `canonical_key`, which makes re-running a
crawl idempotent: a second pass over the same pages updates rows instead of
duplicating them.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from .models import (
    Environment,
    GrowInfo,
    Measurement,
    ScoredTerm,
    SourceRef,
    Strain,
    StrainType,
)

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS strains (
    canonical_key   TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    slug            TEXT,
    aka             TEXT,
    type            TEXT,
    indica_pct      REAL,
    sativa_pct      REAL,
    breeder         TEXT,
    lineage_text    TEXT,
    is_autoflower   INTEGER,
    is_feminized    INTEGER,
    thc_min         REAL, thc_max REAL, thc_avg REAL,
    cbd_min         REAL, cbd_max REAL, cbd_avg REAL,
    cbg_min         REAL, cbg_max REAL, cbg_avg REAL,
    flowering_days_min INTEGER,
    flowering_days_max INTEGER,
    yield_indoor    TEXT,
    yield_outdoor   TEXT,
    height_indoor   TEXT,
    height_outdoor  TEXT,
    environment     TEXT,
    harvest_month   TEXT,
    difficulty      TEXT,
    awards          TEXT,
    description     TEXT,
    rating          REAL,
    review_count    INTEGER,
    images          TEXT,
    provenance      TEXT,
    source_count    INTEGER DEFAULT 0,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS strain_terms (
    canonical_key TEXT NOT NULL REFERENCES strains(canonical_key) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    term          TEXT NOT NULL,
    score         REAL,
    PRIMARY KEY (canonical_key, kind, term)
);

CREATE TABLE IF NOT EXISTS strain_parents (
    canonical_key TEXT NOT NULL REFERENCES strains(canonical_key) ON DELETE CASCADE,
    parent_name   TEXT NOT NULL,
    position      INTEGER,
    PRIMARY KEY (canonical_key, parent_name)
);

CREATE TABLE IF NOT EXISTS strain_sources (
    canonical_key TEXT NOT NULL REFERENCES strains(canonical_key) ON DELETE CASCADE,
    source        TEXT NOT NULL,
    url           TEXT NOT NULL,
    source_id     TEXT,
    scraped_at    TEXT,
    PRIMARY KEY (canonical_key, source, url)
);

CREATE INDEX IF NOT EXISTS idx_terms_kind_term ON strain_terms(kind, term);
CREATE INDEX IF NOT EXISTS idx_strains_type ON strains(type);
CREATE INDEX IF NOT EXISTS idx_strains_breeder ON strains(breeder);
CREATE INDEX IF NOT EXISTS idx_parents_name ON strain_parents(parent_name);
CREATE INDEX IF NOT EXISTS idx_sources_source ON strain_sources(source);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS strains_fts USING fts5(
    canonical_key UNINDEXED,
    name,
    aka,
    breeder,
    lineage_text,
    description,
    tokenize = 'porter'
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.has_fts = self._try_fts()
        self.conn.commit()

    def _try_fts(self) -> bool:
        try:
            self.conn.executescript(FTS_SCHEMA)
            return True
        except sqlite3.OperationalError:
            # FTS5 is not compiled into every SQLite build; search degrades to
            # LIKE rather than failing outright.
            return False

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------

    def upsert(self, strain: Strain) -> None:
        key = strain.canonical_key or strain.slug
        if not key:
            raise ValueError(f"strain {strain.name!r} has no canonical key")

        g = strain.grow
        self.conn.execute(
            """
            INSERT INTO strains (
                canonical_key, name, slug, aka, type, indica_pct, sativa_pct,
                breeder, lineage_text, is_autoflower, is_feminized,
                thc_min, thc_max, thc_avg, cbd_min, cbd_max, cbd_avg,
                cbg_min, cbg_max, cbg_avg,
                flowering_days_min, flowering_days_max, yield_indoor, yield_outdoor,
                height_indoor, height_outdoor, environment, harvest_month, difficulty,
                awards, description, rating, review_count, images, provenance,
                source_count, updated_at
            ) VALUES (
                ?,?,?,?,?,?,?, ?,?,?,?, ?,?,?,?,?,?, ?,?,?,
                ?,?,?,?, ?,?,?,?,?, ?,?,?,?,?,?, ?, datetime('now')
            )
            ON CONFLICT(canonical_key) DO UPDATE SET
                name=excluded.name,
                slug=excluded.slug,
                aka=excluded.aka,
                type=COALESCE(NULLIF(excluded.type,'unknown'), strains.type),
                indica_pct=COALESCE(excluded.indica_pct, strains.indica_pct),
                sativa_pct=COALESCE(excluded.sativa_pct, strains.sativa_pct),
                breeder=COALESCE(excluded.breeder, strains.breeder),
                lineage_text=COALESCE(excluded.lineage_text, strains.lineage_text),
                is_autoflower=COALESCE(excluded.is_autoflower, strains.is_autoflower),
                is_feminized=COALESCE(excluded.is_feminized, strains.is_feminized),
                thc_min=COALESCE(excluded.thc_min, strains.thc_min),
                thc_max=COALESCE(excluded.thc_max, strains.thc_max),
                thc_avg=COALESCE(excluded.thc_avg, strains.thc_avg),
                cbd_min=COALESCE(excluded.cbd_min, strains.cbd_min),
                cbd_max=COALESCE(excluded.cbd_max, strains.cbd_max),
                cbd_avg=COALESCE(excluded.cbd_avg, strains.cbd_avg),
                cbg_min=COALESCE(excluded.cbg_min, strains.cbg_min),
                cbg_max=COALESCE(excluded.cbg_max, strains.cbg_max),
                flowering_days_min=COALESCE(excluded.flowering_days_min, strains.flowering_days_min),
                flowering_days_max=COALESCE(excluded.flowering_days_max, strains.flowering_days_max),
                yield_indoor=COALESCE(excluded.yield_indoor, strains.yield_indoor),
                yield_outdoor=COALESCE(excluded.yield_outdoor, strains.yield_outdoor),
                height_indoor=COALESCE(excluded.height_indoor, strains.height_indoor),
                height_outdoor=COALESCE(excluded.height_outdoor, strains.height_outdoor),
                environment=COALESCE(NULLIF(excluded.environment,'unknown'), strains.environment),
                harvest_month=COALESCE(excluded.harvest_month, strains.harvest_month),
                difficulty=COALESCE(excluded.difficulty, strains.difficulty),
                awards=excluded.awards,
                description=COALESCE(excluded.description, strains.description),
                rating=COALESCE(excluded.rating, strains.rating),
                review_count=COALESCE(excluded.review_count, strains.review_count),
                images=excluded.images,
                provenance=excluded.provenance,
                source_count=excluded.source_count,
                updated_at=datetime('now')
            """,
            (
                key,
                strain.name,
                strain.slug,
                json.dumps(strain.aka),
                strain.type.value,
                strain.indica_pct,
                strain.sativa_pct,
                strain.breeder,
                strain.lineage_text,
                _bool(strain.is_autoflower),
                _bool(strain.is_feminized),
                strain.thc.min,
                strain.thc.max,
                strain.thc.avg,
                strain.cbd.min,
                strain.cbd.max,
                strain.cbd.avg,
                strain.cbg.min,
                strain.cbg.max,
                strain.cbg.avg,
                g.flowering_days_min,
                g.flowering_days_max,
                g.yield_indoor,
                g.yield_outdoor,
                g.height_indoor,
                g.height_outdoor,
                g.environment.value,
                g.harvest_month,
                g.difficulty,
                json.dumps(strain.awards),
                strain.description,
                strain.rating,
                strain.review_count,
                json.dumps(strain.images),
                json.dumps(strain.provenance),
                len(strain.sources),
            ),
        )

        self.conn.execute("DELETE FROM strain_terms WHERE canonical_key = ?", (key,))
        rows = []
        for kind in ("effects", "negatives", "medical", "flavors", "aromas", "terpenes"):
            for term in getattr(strain, kind) or []:
                rows.append((key, kind, term.name, term.score))
        if rows:
            self.conn.executemany(
                "INSERT OR REPLACE INTO strain_terms (canonical_key, kind, term, score)"
                " VALUES (?,?,?,?)",
                rows,
            )

        self.conn.execute("DELETE FROM strain_parents WHERE canonical_key = ?", (key,))
        if strain.parents:
            self.conn.executemany(
                "INSERT OR REPLACE INTO strain_parents (canonical_key, parent_name, position)"
                " VALUES (?,?,?)",
                [(key, p, i) for i, p in enumerate(strain.parents)],
            )

        if strain.sources:
            self.conn.executemany(
                "INSERT OR REPLACE INTO strain_sources"
                " (canonical_key, source, url, source_id, scraped_at) VALUES (?,?,?,?,?)",
                [(key, s.source, s.url, s.source_id, s.scraped_at) for s in strain.sources],
            )

        if self.has_fts:
            self.conn.execute("DELETE FROM strains_fts WHERE canonical_key = ?", (key,))
            self.conn.execute(
                "INSERT INTO strains_fts (canonical_key, name, aka, breeder, lineage_text, description)"
                " VALUES (?,?,?,?,?,?)",
                (
                    key,
                    strain.name,
                    " ".join(strain.aka),
                    strain.breeder or "",
                    strain.lineage_text or "",
                    strain.description or "",
                ),
            )

    def upsert_many(self, strains: Iterable[Strain]) -> int:
        count = 0
        with self.transaction():
            for strain in strains:
                self.upsert(strain)
                count += 1
        return count

    # ------------------------------------------------------------------

    def get(self, key: str) -> Strain | None:
        row = self.conn.execute(
            "SELECT * FROM strains WHERE canonical_key = ?", (key,)
        ).fetchone()
        return self._hydrate(row) if row else None

    def all(self, limit: int | None = None) -> list[Strain]:
        sql = "SELECT * FROM strains ORDER BY name"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [self._hydrate(r) for r in self.conn.execute(sql)]

    def search(self, query: str, limit: int = 50) -> list[Strain]:
        if self.has_fts:
            try:
                rows = self.conn.execute(
                    "SELECT s.* FROM strains_fts f JOIN strains s"
                    " ON s.canonical_key = f.canonical_key"
                    " WHERE strains_fts MATCH ? ORDER BY rank LIMIT ?",
                    (query, limit),
                ).fetchall()
                return [self._hydrate(r) for r in rows]
            except sqlite3.OperationalError:
                pass  # malformed FTS query -> fall through to LIKE
        rows = self.conn.execute(
            "SELECT * FROM strains WHERE name LIKE ? OR aka LIKE ? OR breeder LIKE ?"
            " ORDER BY name LIMIT ?",
            (f"%{query}%", f"%{query}%", f"%{query}%", limit),
        ).fetchall()
        return [self._hydrate(r) for r in rows]

    def _hydrate(self, row: sqlite3.Row) -> Strain:
        key = row["canonical_key"]
        strain = Strain(
            name=row["name"],
            slug=row["slug"] or "",
            canonical_key=key,
            aka=_json_list(row["aka"]),
            type=_enum(StrainType, row["type"]),
            indica_pct=row["indica_pct"],
            sativa_pct=row["sativa_pct"],
            breeder=row["breeder"],
            lineage_text=row["lineage_text"],
            is_autoflower=_unbool(row["is_autoflower"]),
            is_feminized=_unbool(row["is_feminized"]),
            thc=Measurement(row["thc_min"], row["thc_max"], row["thc_avg"]),
            cbd=Measurement(row["cbd_min"], row["cbd_max"], row["cbd_avg"]),
            cbg=Measurement(row["cbg_min"], row["cbg_max"], row["cbg_avg"]),
            grow=GrowInfo(
                flowering_days_min=row["flowering_days_min"],
                flowering_days_max=row["flowering_days_max"],
                yield_indoor=row["yield_indoor"],
                yield_outdoor=row["yield_outdoor"],
                height_indoor=row["height_indoor"],
                height_outdoor=row["height_outdoor"],
                environment=_enum(Environment, row["environment"]),
                harvest_month=row["harvest_month"],
                difficulty=row["difficulty"],
            ),
            awards=_json_list(row["awards"]),
            description=row["description"],
            rating=row["rating"],
            review_count=row["review_count"],
            images=_json_list(row["images"]),
            provenance=json.loads(row["provenance"] or "{}"),
        )

        for r in self.conn.execute(
            "SELECT kind, term, score FROM strain_terms WHERE canonical_key = ?", (key,)
        ):
            getattr(strain, r["kind"]).append(ScoredTerm(r["term"], r["score"]))

        strain.parents = [
            r["parent_name"]
            for r in self.conn.execute(
                "SELECT parent_name FROM strain_parents WHERE canonical_key = ?"
                " ORDER BY position",
                (key,),
            )
        ]
        strain.sources = [
            SourceRef(r["source"], r["url"], r["source_id"], r["scraped_at"])
            for r in self.conn.execute(
                "SELECT source, url, source_id, scraped_at FROM strain_sources"
                " WHERE canonical_key = ?",
                (key,),
            )
        ]
        return strain

    # ------------------------------------------------------------------

    def stats(self) -> dict:
        c = self.conn
        out = {
            "strains": c.execute("SELECT COUNT(*) FROM strains").fetchone()[0],
            "with_thc": c.execute(
                "SELECT COUNT(*) FROM strains WHERE thc_avg IS NOT NULL OR thc_max IS NOT NULL"
            ).fetchone()[0],
            "with_genetics": c.execute(
                "SELECT COUNT(DISTINCT canonical_key) FROM strain_parents"
            ).fetchone()[0],
            "with_breeder": c.execute(
                "SELECT COUNT(*) FROM strains WHERE breeder IS NOT NULL"
            ).fetchone()[0],
            "multi_source": c.execute(
                "SELECT COUNT(*) FROM strains WHERE source_count > 1"
            ).fetchone()[0],
        }
        out["by_type"] = {
            r["type"]: r["n"]
            for r in c.execute("SELECT type, COUNT(*) n FROM strains GROUP BY type ORDER BY n DESC")
        }
        out["by_source"] = {
            r["source"]: r["n"]
            for r in c.execute(
                "SELECT source, COUNT(DISTINCT canonical_key) n FROM strain_sources"
                " GROUP BY source ORDER BY n DESC"
            )
        }
        out["top_effects"] = {
            r["term"]: r["n"]
            for r in c.execute(
                "SELECT term, COUNT(*) n FROM strain_terms WHERE kind='effects'"
                " GROUP BY term ORDER BY n DESC LIMIT 10"
            )
        }
        return out


# ----------------------------------------------------------------------
# Exports
# ----------------------------------------------------------------------

CSV_COLUMNS = [
    "canonical_key",
    "name",
    "aka",
    "type",
    "indica_pct",
    "sativa_pct",
    "breeder",
    "parents",
    "thc_min",
    "thc_max",
    "thc_avg",
    "cbd_min",
    "cbd_max",
    "cbd_avg",
    "effects",
    "flavors",
    "terpenes",
    "medical",
    "negatives",
    "flowering_days_min",
    "flowering_days_max",
    "yield_indoor",
    "yield_outdoor",
    "environment",
    "rating",
    "review_count",
    "sources",
    "description",
]


def export(db: Database, path: str | Path, fmt: str = "json") -> int:
    strains = db.all()
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "json":
        p.write_text(
            json.dumps([s.to_dict() for s in strains], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    elif fmt == "jsonl":
        with p.open("w", encoding="utf-8") as fh:
            for s in strains:
                fh.write(json.dumps(s.to_dict(), ensure_ascii=False) + "\n")
    elif fmt == "csv":
        with p.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for s in strains:
                writer.writerow(_csv_row(s))
    else:
        raise ValueError(f"unsupported format {fmt!r}")
    return len(strains)


def _csv_row(s: Strain) -> dict:
    return {
        "canonical_key": s.canonical_key,
        "name": s.name,
        "aka": "; ".join(s.aka),
        "type": s.type.value,
        "indica_pct": s.indica_pct,
        "sativa_pct": s.sativa_pct,
        "breeder": s.breeder or "",
        "parents": " x ".join(s.parents),
        "thc_min": s.thc.min,
        "thc_max": s.thc.max,
        "thc_avg": s.thc.avg,
        "cbd_min": s.cbd.min,
        "cbd_max": s.cbd.max,
        "cbd_avg": s.cbd.avg,
        "effects": "; ".join(t.name for t in s.effects),
        "flavors": "; ".join(t.name for t in s.flavors),
        "terpenes": "; ".join(t.name for t in s.terpenes),
        "medical": "; ".join(t.name for t in s.medical),
        "negatives": "; ".join(t.name for t in s.negatives),
        "flowering_days_min": s.grow.flowering_days_min,
        "flowering_days_max": s.grow.flowering_days_max,
        "yield_indoor": s.grow.yield_indoor or "",
        "yield_outdoor": s.grow.yield_outdoor or "",
        "environment": s.grow.environment.value,
        "rating": s.rating,
        "review_count": s.review_count,
        "sources": "; ".join(sorted({src.source for src in s.sources})),
        "description": (s.description or "").replace("\n", " ")[:2000],
    }


def _bool(value):
    return None if value is None else int(bool(value))


def _unbool(value):
    return None if value is None else bool(value)


def _json_list(value) -> list:
    if not value:
        return []
    try:
        data = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _enum(cls, value):
    try:
        return cls(value)
    except (ValueError, TypeError):
        return cls.UNKNOWN
