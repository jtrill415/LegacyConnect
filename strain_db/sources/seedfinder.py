"""SeedFinder adapter.

SeedFinder's value is genetics depth: it names the breeder and the exact cross,
including multi-generation lineage that the consumer sites flatten away.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from ..models import RawStrain, Strain
from ..normalize import display_name
from .base import Source, _as_text


class SeedFinderSource(Source):
    name = "seedfinder"

    def post_normalize(self, strain: Strain, raw: RawStrain) -> Strain:
        # Strain pages are /strain-info/<Strain>/<Breeder>/, so the URL itself
        # is a reliable fallback for both name and breeder when the page markup
        # shifts around.
        parts = [p for p in urlparse(raw.url).path.split("/") if p]
        if len(parts) >= 3 and parts[0] == "strain-info":
            url_name = display_name(unquote(parts[1]).replace("_", " "))
            url_breeder = display_name(unquote(parts[2]).replace("_", " "))
            if not strain.breeder and url_breeder:
                strain.breeder = url_breeder
                strain.provenance["breeder"] = self.name
            if not strain.name and url_name:
                strain.name = url_name

        # The h1 is usually "<Strain> (<Breeder>)" - strip the breeder so the
        # canonical key does not vary by seedbank.
        if strain.breeder and strain.name.endswith(f"({strain.breeder})"):
            strain.name = display_name(strain.name[: -len(f"({strain.breeder})")])
            from ..normalize import canonical_name

            strain.canonical_key, _, _ = canonical_name(strain.name)

        # SeedFinder ratings are out of 10; rating_scale in the spec handles the
        # conversion, but some pages render "8.4/10" inline.
        rating_text = _as_text(raw.fields.get("rating"))
        if strain.rating is None and rating_text:
            m = re.search(r"([\d.]+)\s*/\s*10", rating_text)
            if m:
                strain.rating = round(float(m.group(1)) / 2, 2)

        return strain
