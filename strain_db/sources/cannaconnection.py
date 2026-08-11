"""CannaConnection adapter."""

from __future__ import annotations

import re

from ..models import RawStrain, Strain
from ..normalize import parse_type
from .base import Source, _as_text


class CannaConnectionSource(Source):
    name = "cannaconnection"

    def post_normalize(self, strain: Strain, raw: RawStrain) -> Strain:
        pairs = raw.fields.get("_pairs") or {}

        # This site frequently states the ratio as a standalone "Sativa 60%
        # Indica 40%" row rather than inside the type field.
        if strain.indica_pct is None or strain.sativa_pct is None:
            for label, value in pairs.items():
                if re.search(r"sativa|indica|genetic|variet", label):
                    stype, ind, sat = parse_type(value)
                    if ind is not None or sat is not None:
                        strain.indica_pct = strain.indica_pct or ind
                        strain.sativa_pct = strain.sativa_pct or sat
                        if strain.type.value == "unknown":
                            strain.type = stype
                        break

        # Yield rows often read "450 - 500 gr/m2" with the environment implied
        # by an adjacent icon; fall back to a combined yield row.
        if not strain.grow.yield_indoor and not strain.grow.yield_outdoor:
            for label, value in pairs.items():
                if "yield" in label and re.search(r"\d", value):
                    strain.grow.yield_indoor = _as_text(value)
                    break

        return strain
