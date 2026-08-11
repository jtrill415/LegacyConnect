"""Canonical strain record shared by every source.

Each source produces `RawStrain` objects (whatever that site happens to expose).
`normalize.py` turns those into `Strain` objects using a controlled vocabulary,
and `merge.py` folds several `Strain` objects describing the same plant into a
single record with per-field provenance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class StrainType(str, Enum):
    INDICA = "indica"
    SATIVA = "sativa"
    HYBRID = "hybrid"
    INDICA_DOMINANT = "indica_dominant"
    SATIVA_DOMINANT = "sativa_dominant"
    RUDERALIS = "ruderalis"
    CBD = "cbd"
    UNKNOWN = "unknown"


class Environment(str, Enum):
    INDOOR = "indoor"
    OUTDOOR = "outdoor"
    GREENHOUSE = "greenhouse"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SourceRef:
    """Where one piece of a record came from."""

    source: str
    url: str
    source_id: str | None = None
    scraped_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )


@dataclass
class Measurement:
    """A percentage range, e.g. THC 18-24%.

    Sources are wildly inconsistent here: some give a single average, some a
    range, some a "up to X" claim from a breeder. We keep min/max/avg separately
    rather than collapsing, because collapsing loses the distinction between
    "tested at 20%" and "breeder claims up to 20%".
    """

    min: float | None = None
    max: float | None = None
    avg: float | None = None

    def is_empty(self) -> bool:
        return self.min is None and self.max is None and self.avg is None

    def merged_with(self, other: "Measurement") -> "Measurement":
        return Measurement(
            min=_pick_num(self.min, other.min, min),
            max=_pick_num(self.max, other.max, max),
            avg=self.avg if self.avg is not None else other.avg,
        )


def _pick_num(a: float | None, b: float | None, fn) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return fn(a, b)


@dataclass
class ScoredTerm:
    """An effect/flavor with an optional strength score.

    Leafly and Weedmaps report effects with a relative frequency (what share of
    reviewers reported it); SeedFinder and CannaConnection just list terms. A
    `score` of None means "reported, strength unknown" - which is different from
    a score of 0.0.
    """

    name: str
    score: float | None = None


@dataclass
class GrowInfo:
    flowering_days_min: int | None = None
    flowering_days_max: int | None = None
    yield_indoor: str | None = None
    yield_outdoor: str | None = None
    height_indoor: str | None = None
    height_outdoor: str | None = None
    environment: Environment = Environment.UNKNOWN
    harvest_month: str | None = None
    difficulty: str | None = None

    def is_empty(self) -> bool:
        return all(
            v in (None, Environment.UNKNOWN)
            for v in asdict(self).values()
        )


@dataclass
class Strain:
    """One cannabis strain, normalized across sources."""

    name: str
    slug: str = ""
    canonical_key: str = ""
    aka: list[str] = field(default_factory=list)

    type: StrainType = StrainType.UNKNOWN
    indica_pct: float | None = None
    sativa_pct: float | None = None

    breeder: str | None = None
    parents: list[str] = field(default_factory=list)
    lineage_text: str | None = None
    is_autoflower: bool | None = None
    is_feminized: bool | None = None

    thc: Measurement = field(default_factory=Measurement)
    cbd: Measurement = field(default_factory=Measurement)
    cbg: Measurement = field(default_factory=Measurement)

    effects: list[ScoredTerm] = field(default_factory=list)
    negatives: list[ScoredTerm] = field(default_factory=list)
    medical: list[ScoredTerm] = field(default_factory=list)
    flavors: list[ScoredTerm] = field(default_factory=list)
    aromas: list[ScoredTerm] = field(default_factory=list)
    terpenes: list[ScoredTerm] = field(default_factory=list)

    grow: GrowInfo = field(default_factory=GrowInfo)
    awards: list[str] = field(default_factory=list)

    description: str | None = None
    rating: float | None = None
    review_count: int | None = None
    images: list[str] = field(default_factory=list)

    sources: list[SourceRef] = field(default_factory=list)
    #: field name -> source name that supplied the winning value
    provenance: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        d["grow"]["environment"] = self.grow.environment.value
        return d


@dataclass
class RawStrain:
    """Whatever a source gave us, before normalization.

    `fields` holds source-native keys; keeping it untyped means a source adapter
    can pass through data we have not modelled yet without losing it, and
    `raw_html`/`raw_json` let us re-parse historical crawls after a parser fix
    without re-hitting the network.
    """

    source: str
    url: str
    name: str
    source_id: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    raw_json: dict[str, Any] | None = None
    raw_html: str | None = None
    scraped_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def ref(self) -> SourceRef:
        return SourceRef(
            source=self.source,
            url=self.url,
            source_id=self.source_id,
            scraped_at=self.scraped_at,
        )


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    return _SLUG_RE.sub("-", value.strip().lower()).strip("-")
