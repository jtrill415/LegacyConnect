"""Leafly adapter."""

from __future__ import annotations

from ..models import RawStrain, Strain
from ..normalize import parse_measurement
from .base import Source, _as_text


class LeaflySource(Source):
    name = "leafly"

    def post_normalize(self, strain: Strain, raw: RawStrain) -> Strain:
        f = raw.fields

        # Leafly reports potency as a single average (thcAvg). When only an
        # average is present, do not invent a range - a bare avg is a weaker
        # claim than min/max and merge.py needs to know the difference.
        if strain.thc.is_empty() and f.get("thc_percent") is not None:
            strain.thc = parse_measurement(f["thc_percent"])

        # Leafly's "category" is "Hybrid"/"Indica"/"Sativa"; its finer-grained
        # lean lives in a separate field when present.
        lean = _as_text(f.get("subcategory") or f.get("strainLean"))
        if lean:
            from ..normalize import parse_type

            stype, ind, sat = parse_type(f"{_as_text(f.get('type'))} {lean}")
            if stype.value != "unknown":
                strain.type = stype
            strain.indica_pct = strain.indica_pct or ind
            strain.sativa_pct = strain.sativa_pct or sat

        # Leafly phenotype lists are child strains, not parents; only treat them
        # as lineage when an explicit lineage field is absent.
        if strain.parents and f.get("lineage") is None and f.get("phenotypes"):
            strain.parents = []

        return strain
