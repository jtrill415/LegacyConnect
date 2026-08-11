"""Source registry."""

from __future__ import annotations

from .base import Source, load_spec
from .cannaconnection import CannaConnectionSource
from .leafly import LeaflySource
from .seedfinder import SeedFinderSource
from .weedmaps import WeedmapsSource

SOURCES: dict[str, type[Source]] = {
    cls.name: cls
    for cls in (LeaflySource, WeedmapsSource, CannaConnectionSource, SeedFinderSource)
}

#: Per-field trust order used by `merge.py`. Earlier sources win a contested
#: field. These reflect what each site is actually good at rather than a single
#: global ranking: SeedFinder is the authority on breeder/lineage, while the
#: consumer sites carry review-driven effect and potency data.
FIELD_PRIORITY: dict[str, list[str]] = {
    "breeder": ["seedfinder", "cannaconnection", "leafly", "weedmaps"],
    "parents": ["seedfinder", "cannaconnection", "leafly", "weedmaps"],
    "lineage_text": ["seedfinder", "cannaconnection", "leafly", "weedmaps"],
    "awards": ["seedfinder", "cannaconnection", "leafly", "weedmaps"],
    "grow": ["seedfinder", "cannaconnection", "leafly", "weedmaps"],
    "thc": ["leafly", "weedmaps", "cannaconnection", "seedfinder"],
    "cbd": ["leafly", "weedmaps", "cannaconnection", "seedfinder"],
    "effects": ["leafly", "weedmaps", "cannaconnection", "seedfinder"],
    "negatives": ["leafly", "weedmaps", "cannaconnection", "seedfinder"],
    "medical": ["leafly", "weedmaps", "cannaconnection", "seedfinder"],
    "flavors": ["leafly", "weedmaps", "cannaconnection", "seedfinder"],
    "terpenes": ["leafly", "weedmaps", "cannaconnection", "seedfinder"],
    "rating": ["leafly", "weedmaps", "cannaconnection", "seedfinder"],
    "review_count": ["leafly", "weedmaps", "cannaconnection", "seedfinder"],
    "description": ["leafly", "cannaconnection", "weedmaps", "seedfinder"],
    "type": ["leafly", "seedfinder", "weedmaps", "cannaconnection"],
}

DEFAULT_PRIORITY = ["leafly", "weedmaps", "seedfinder", "cannaconnection"]

__all__ = [
    "SOURCES",
    "FIELD_PRIORITY",
    "DEFAULT_PRIORITY",
    "Source",
    "load_spec",
    "LeaflySource",
    "WeedmapsSource",
    "CannaConnectionSource",
    "SeedFinderSource",
]
