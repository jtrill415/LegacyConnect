"""Spec-driven source adapter.

A source is described by a YAML file in `specs/`. This class turns that spec
into a crawler: discover detail URLs, fetch them politely, extract fields with
the strategy ladder in `extract.py`, then normalize into a `Strain`.

Site-specific quirks are handled by subclassing and overriding a hook, not by
forking the whole crawl loop.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse

import yaml

from .. import extract, normalize
from ..http import Client, FetchError, RobotsDisallowed
from ..models import (
    Environment,
    GrowInfo,
    RawStrain,
    Strain,
    StrainType,
    slugify,
)

log = logging.getLogger(__name__)

SPEC_DIR = Path(__file__).parent / "specs"


class Source:
    #: Overridden by subclasses; must match the spec filename.
    name: str = ""

    def __init__(self, client: Client, spec: dict | None = None):
        self.client = client
        self.spec = spec if spec is not None else load_spec(self.name)
        self.base_url = self.spec.get("base_url", "").rstrip("/")
        self.stats = {"discovered": 0, "parsed": 0, "failed": 0, "skipped": 0}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(self, limit: int | None = None) -> Iterator[str]:
        """Yield detail-page URLs for this source."""
        listing = self.spec.get("listing", {})
        modes = listing.get("mode", "sitemap")
        if isinstance(modes, str):
            modes = [modes]

        seen: set[str] = set()
        count = 0
        for mode in modes:
            try:
                if mode == "sitemap":
                    it = self._discover_sitemap(listing)
                elif mode == "html":
                    it = self._discover_html(listing)
                elif mode == "links":
                    it = self._discover_links(listing)
                elif mode == "api":
                    it = self._discover_api(listing)
                else:
                    log.warning("[%s] unknown discovery mode %r", self.name, mode)
                    continue

                for url in it:
                    if url in seen:
                        continue
                    seen.add(url)
                    self.stats["discovered"] += 1
                    count += 1
                    yield url
                    if limit and count >= limit:
                        return
            except (FetchError, RobotsDisallowed) as exc:
                log.warning("[%s] discovery mode %s failed: %s", self.name, mode, exc)
                continue

            if count:
                # A working mode is enough; later modes are fallbacks only.
                return

    def _discover_sitemap(self, listing: dict) -> Iterator[str]:
        roots = listing.get("sitemaps") or []
        if isinstance(roots, str):
            roots = [roots]
        pattern = re.compile(listing["detail_url_re"]) if listing.get("detail_url_re") else None
        max_sitemaps = int(listing.get("max_sitemaps", 200))

        queue = list(roots)
        visited: set[str] = set()
        processed = 0

        while queue and processed < max_sitemaps:
            sm_url = queue.pop(0)
            if sm_url in visited:
                continue
            visited.add(sm_url)
            processed += 1
            try:
                body = self.client.get(sm_url).text
            except (FetchError, RobotsDisallowed) as exc:
                log.warning("[%s] sitemap %s: %s", self.name, sm_url, exc)
                continue

            # Nested sitemap index -> queue children first.
            children = re.findall(r"<sitemap>.*?<loc>\s*(.*?)\s*</loc>.*?</sitemap>", body, re.S)
            if children:
                queue.extend(c.strip() for c in children)
                continue

            for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", body, re.S):
                url = loc.strip()
                if not url:
                    continue
                if pattern and not pattern.search(url):
                    continue
                yield url

    def _discover_html(self, listing: dict) -> Iterator[str]:
        url_pattern = listing.get("url_pattern")
        if not url_pattern:
            return
        page = int(listing.get("page_start", 1))
        max_pages = int(listing.get("max_pages", 100))
        link_sel = listing.get("link_sel", "a")
        link_attr = listing.get("link_attr", "href")
        detail_re = re.compile(listing["detail_url_re"]) if listing.get("detail_url_re") else None
        empty_streak = 0

        while page < int(listing.get("page_start", 1)) + max_pages:
            page_url = url_pattern.format(page=page)
            try:
                result = self.client.get(page_url)
            except (FetchError, RobotsDisallowed) as exc:
                log.info("[%s] listing stopped at page %d: %s", self.name, page, exc)
                return

            soup = extract.soup_of(result.text)
            found = 0
            for node in soup.select(link_sel):
                href = node.get(link_attr)
                if not href:
                    continue
                full = urljoin(self.base_url or page_url, href.split("#")[0])
                if detail_re and not detail_re.search(full):
                    continue
                found += 1
                yield full

            if found == 0:
                empty_streak += 1
                # Two consecutive empty pages means we ran off the end of the
                # list; one alone can just be a layout hiccup.
                if empty_streak >= 2:
                    return
            else:
                empty_streak = 0
            page += int(listing.get("page_step", 1))

    def _discover_links(self, listing: dict) -> Iterator[str]:
        """Scrape detail links from a fixed set of index pages.

        Used for A-Z / by-breeder indexes, which are not page-numbered and so
        do not fit the `html` pagination mode.
        """
        seeds = listing.get("seed_urls") or []
        if isinstance(seeds, str):
            seeds = [seeds]
        # Expand "{letter}" style templates against a declared alphabet.
        expanded: list[str] = []
        for seed in seeds:
            if "{letter}" in seed:
                for letter in listing.get("letters", "abcdefghijklmnopqrstuvwxyz"):
                    expanded.append(seed.format(letter=letter))
            else:
                expanded.append(seed)

        link_sel = listing.get("link_sel", "a")
        link_attr = listing.get("link_attr", "href")
        detail_re = re.compile(listing["detail_url_re"]) if listing.get("detail_url_re") else None
        follow_re = (
            re.compile(listing["follow_url_re"]) if listing.get("follow_url_re") else None
        )

        queue = list(expanded)
        visited: set[str] = set()
        max_pages = int(listing.get("max_pages", 500))

        while queue and len(visited) < max_pages:
            index_url = queue.pop(0)
            if index_url in visited:
                continue
            visited.add(index_url)
            try:
                result = self.client.get(index_url)
            except (FetchError, RobotsDisallowed) as exc:
                log.info("[%s] index %s: %s", self.name, index_url, exc)
                continue

            soup = extract.soup_of(result.text)
            for node in soup.select(link_sel):
                href = node.get(link_attr)
                if not href:
                    continue
                full = urljoin(self.base_url or index_url, href.split("#")[0])
                if detail_re and detail_re.search(full):
                    yield full
                elif follow_re and follow_re.search(full) and full not in visited:
                    # Pagination inside an index page (page 2, 3, ... of "A").
                    queue.append(full)

    def _discover_api(self, listing: dict) -> Iterator[str]:
        url_pattern = listing["api_url_pattern"]
        page = int(listing.get("page_start", 1))
        max_pages = int(listing.get("max_pages", 200))
        items_paths = listing.get("items_path", "data")
        if isinstance(items_paths, str):
            items_paths = [items_paths]
        slug_path = listing.get("slug_key", "slug")
        detail_pattern = listing.get("detail_url_pattern", "{base}/strains/{slug}")

        for _ in range(max_pages):
            api_url = url_pattern.format(page=page, page_size=listing.get("page_size", 100))
            try:
                payload = self.client.get(api_url).json()
            except (FetchError, RobotsDisallowed, ValueError) as exc:
                log.info("[%s] api listing stopped at page %d: %s", self.name, page, exc)
                return

            items: Any = []
            for path in items_paths:
                candidate = extract.json_path(payload, path)
                if isinstance(candidate, list) and candidate:
                    items = candidate
                    break
            if not items:
                return
            for item in items:
                slug = item.get(slug_path) if isinstance(item, dict) else None
                if not slug:
                    continue
                yield detail_pattern.format(base=self.base_url, slug=slug)
            page += 1

    # ------------------------------------------------------------------
    # Detail parsing
    # ------------------------------------------------------------------

    def fetch_detail(self, url: str) -> RawStrain | None:
        try:
            result = self.client.get(url)
        except RobotsDisallowed as exc:
            log.debug("[%s] %s", self.name, exc)
            self.stats["skipped"] += 1
            return None
        except FetchError as exc:
            log.warning("[%s] %s", self.name, exc)
            self.stats["failed"] += 1
            return None
        return self.parse_detail(url, result.text)

    def parse_detail(self, url: str, html: str) -> RawStrain | None:
        """Run the strategy ladder and return whatever we could find."""
        detail = self.spec.get("detail", {})
        fields: dict[str, Any] = {}
        raw_json: dict | None = None

        # 1. embedded app state
        state_name, state = extract.app_state(html)
        if state is not None:
            root_path = detail.get("state_root")
            root = extract.json_path(state, root_path) if root_path else state
            if root is None and root_path:
                # The site moved the payload; fall back to a keyed deep search.
                anchor = detail.get("state_anchor_key")
                if anchor:
                    hits = extract.deep_find(state, anchor, limit=1)
                    root = hits[0] if hits else None
            if isinstance(root, dict):
                raw_json = root
                for field, path in (detail.get("state_paths") or {}).items():
                    value = extract.json_path(root, path)
                    if value not in (None, "", []):
                        fields[field] = value

        soup = extract.soup_of(html)

        # 2. JSON-LD fills gaps
        ld = extract.json_ld_of_type(soup, "Product", "Thing", "Drug", "Article")
        if ld:
            for field, key in (detail.get("jsonld_paths") or {}).items():
                if field in fields:
                    continue
                value = extract.json_path(ld, key)
                if value not in (None, "", []):
                    fields[field] = value

        # 3. CSS selectors fill whatever is still missing
        for field, css_spec in (detail.get("css") or {}).items():
            if fields.get(field) in (None, "", []):
                value = extract.select(soup, css_spec)
                if value not in (None, "", []):
                    fields[field] = value

        # 4. label/value tables (grow data on most of these sites)
        pairs = extract.select_pairs(soup, detail.get("pairs") or {})
        if pairs:
            fields.setdefault("_pairs", pairs)
            for field, labels in (detail.get("pair_fields") or {}).items():
                if fields.get(field) not in (None, "", []):
                    continue
                for label in labels if isinstance(labels, list) else [labels]:
                    for k, v in pairs.items():
                        if label.lower() in k:
                            fields[field] = v
                            break
                    if fields.get(field):
                        break

        name = normalize.display_name(fields.get("name") or "")
        if not name:
            self.stats["failed"] += 1
            log.debug("[%s] no name found at %s", self.name, url)
            return None

        self.stats["parsed"] += 1
        return RawStrain(
            source=self.name,
            url=url,
            name=name,
            source_id=fields.get("source_id") or self._slug_from_url(url),
            fields=fields,
            raw_json=raw_json,
        )

    @staticmethod
    def _slug_from_url(url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        return path.rsplit("/", 1)[-1] if path else ""

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    def to_strain(self, raw: RawStrain) -> Strain:
        """Map source-native fields onto the canonical model.

        Subclasses override `post_normalize` for site quirks rather than
        reimplementing this.
        """
        f = raw.fields
        name = normalize.display_name(raw.name)
        key, is_auto, is_fem = normalize.canonical_name(name)

        stype, ind, sat = normalize.parse_type(
            _as_text(f.get("type")) or _as_text(f.get("genetics_ratio"))
        )
        if ind is None and f.get("indica_pct") is not None:
            ind = _as_float(f.get("indica_pct"))
        if sat is None and f.get("sativa_pct") is not None:
            sat = _as_float(f.get("sativa_pct"))

        effects, unknown_e = normalize.normalize_terms(_as_list(f.get("effects")), "effects")
        negatives, _ = normalize.normalize_terms(_as_list(f.get("negatives")), "negatives")
        medical, _ = normalize.normalize_terms(_as_list(f.get("medical")), "medical")
        flavors, unknown_f = normalize.normalize_terms(_as_list(f.get("flavors")), "flavors")
        aromas, _ = normalize.normalize_terms(_as_list(f.get("aromas")), "flavors")
        terpenes, _ = normalize.normalize_terms(_as_list(f.get("terpenes")), "terpenes")

        # Some sites file negatives inside the effects list; split them back out.
        if not negatives and unknown_e:
            recovered, _ = normalize.normalize_terms(unknown_e, "negatives")
            negatives = recovered

        flo_min, flo_max = normalize.parse_flowering(
            _as_text(f.get("flowering_time")) or _as_text(f.get("flowering_days"))
        )
        grow = GrowInfo(
            flowering_days_min=flo_min,
            flowering_days_max=flo_max,
            yield_indoor=_as_text(f.get("yield_indoor")) or None,
            yield_outdoor=_as_text(f.get("yield_outdoor")) or None,
            height_indoor=_as_text(f.get("height_indoor")) or None,
            height_outdoor=_as_text(f.get("height_outdoor")) or None,
            environment=normalize.parse_environment(_as_text(f.get("environment"))),
            harvest_month=_as_text(f.get("harvest_month")) or None,
            difficulty=_as_text(f.get("difficulty")) or None,
        )

        parents = _as_list(f.get("parents"))
        if parents and all(isinstance(p, str) for p in parents):
            parents = [normalize.display_name(p) for p in parents if p]
        else:
            parents = normalize.split_parents(_as_text(f.get("lineage")))
        if not parents:
            parents = normalize.split_parents(_as_text(f.get("genetics")))

        strain = Strain(
            name=name,
            slug=slugify(name),
            canonical_key=key,
            aka=normalize.extract_aliases(raw.name) + _as_list(f.get("aka")),
            type=stype,
            indica_pct=ind,
            sativa_pct=sat,
            breeder=_as_text(f.get("breeder")) or None,
            parents=parents,
            lineage_text=_as_text(f.get("lineage")) or _as_text(f.get("genetics")) or None,
            is_autoflower=True if is_auto else None,
            is_feminized=True if is_fem else None,
            thc=normalize.parse_measurement(f.get("thc")),
            cbd=normalize.parse_measurement(f.get("cbd")),
            cbg=normalize.parse_measurement(f.get("cbg")),
            effects=effects,
            negatives=negatives,
            medical=medical,
            flavors=flavors,
            aromas=aromas,
            terpenes=terpenes,
            grow=grow,
            awards=_as_list(f.get("awards")),
            description=_as_text(f.get("description")) or None,
            rating=normalize.parse_rating(f.get("rating"), float(self.spec.get("rating_scale", 5))),
            review_count=normalize.parse_int(f.get("review_count")),
            images=[u for u in _as_list(f.get("images")) if isinstance(u, str)],
            sources=[raw.ref()],
        )
        strain.provenance = {
            field: self.name
            for field in _populated_fields(strain)
        }
        return self.post_normalize(strain, raw)

    def post_normalize(self, strain: Strain, raw: RawStrain) -> Strain:
        """Hook for site-specific cleanup. Default is a no-op."""
        return strain

    # ------------------------------------------------------------------

    def crawl(self, limit: int | None = None) -> Iterator[Strain]:
        for url in self.discover(limit=limit):
            raw = self.fetch_detail(url)
            if raw is None:
                continue
            try:
                yield self.to_strain(raw)
            except Exception as exc:  # noqa: BLE001 - one bad page must not stop a crawl
                log.warning("[%s] normalize failed for %s: %s", self.name, url, exc)
                self.stats["failed"] += 1


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = [_as_text(v) for v in value]
        return ", ".join(p for p in parts if p)
    if isinstance(value, dict):
        for key in ("name", "value", "text", "label", "title"):
            if key in value:
                return _as_text(value[key])
    return str(value)


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        # Sites delimit with commas, pipes, or bullets depending on the field.
        parts = re.split(r"\s*[,|•·/]\s*|\s{2,}", value)
        return [p.strip() for p in parts if p.strip()]
    return [value]


def _populated_fields(strain: Strain) -> list[str]:
    out = []
    for field, value in strain.to_dict().items():
        if field in {"sources", "provenance", "name", "slug", "canonical_key"}:
            continue
        if value in (None, "", [], {}, "unknown"):
            continue
        if isinstance(value, dict) and all(v is None for v in value.values()):
            continue
        out.append(field)
    return out


def load_spec(name: str) -> dict:
    path = SPEC_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no spec for source {name!r} at {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
