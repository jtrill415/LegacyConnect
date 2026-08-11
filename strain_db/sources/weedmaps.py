"""Weedmaps adapter."""

from __future__ import annotations

from ..models import RawStrain, Strain
from .base import Source, _as_text


class WeedmapsSource(Source):
    name = "weedmaps"

    def post_normalize(self, strain: Strain, raw: RawStrain) -> Strain:
        f = raw.fields

        # Weedmaps expresses potency as a 0-1 fraction in some API responses and
        # as a display percentage in others. A THC "0.22" means 22%, not 0.22%;
        # nothing on the market is under 1% THC except deliberate CBD strains,
        # so rescaling below that threshold is safe.
        for measurement in (strain.thc, strain.cbd):
            for attr in ("min", "max", "avg"):
                value = getattr(measurement, attr)
                if value is not None and 0 < value <= 1.0 and _looks_fractional(f):
                    setattr(measurement, attr, round(value * 100, 2))

        # The API exposes genetics as a description string more often than as a
        # structured parent list.
        if not strain.parents and f.get("genetics_description"):
            from ..normalize import split_parents

            strain.parents = split_parents(_as_text(f["genetics_description"]))

        return strain


def _looks_fractional(fields: dict) -> bool:
    """Only rescale when the source field itself carried no percent sign."""
    for key in ("thc_percentage", "cbd_percentage", "thc", "cbd"):
        value = fields.get(key)
        if isinstance(value, str) and "%" in value:
            return False
    return True
