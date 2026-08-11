"""Cross-source dedup and field-level merge.

Two records merge when their canonical keys match exactly. An optional fuzzy
pass catches near-misses ("Gorilla Glue 4" vs "Gorilla Glue #4"), but it is
deliberately conservative: a false merge silently destroys a strain, while a
false split just leaves two rows a human can reconcile later. When in doubt,
we split.
"""

from __future__ import annotations

import difflib
import logging
from collections import defaultdict

from .models import GrowInfo, Measurement, ScoredTerm, Strain, StrainType
from .sources import DEFAULT_PRIORITY, FIELD_PRIORITY

log = logging.getLogger(__name__)

LIST_FIELDS = ["effects", "negatives", "medical", "flavors", "aromas", "terpenes"]
SCALAR_FIELDS = [
    "breeder",
    "lineage_text",
    "description",
    "rating",
    "review_count",
    "type",
    "indica_pct",
    "sativa_pct",
    "is_autoflower",
    "is_feminized",
]


def _rank(source: str, field: str) -> int:
    order = FIELD_PRIORITY.get(field, DEFAULT_PRIORITY)
    try:
        return order.index(source)
    except ValueError:
        return len(order)


def merge_strains(records: list[Strain]) -> Strain:
    """Fold several records for the same strain into one."""
    if not records:
        raise ValueError("no records to merge")
    if len(records) == 1:
        return records[0]

    def source_of(rec: Strain) -> str:
        return rec.sources[0].source if rec.sources else "unknown"

    # The display name comes from the highest-priority source that has one,
    # preferring the longest form so "GSC" loses to "Girl Scout Cookies".
    by_name_pref = sorted(
        records, key=lambda r: (_rank(source_of(r), "name"), -len(r.name or ""))
    )
    base = by_name_pref[0]

    merged = Strain(
        name=base.name,
        slug=base.slug,
        canonical_key=base.canonical_key,
    )
    provenance: dict[str, str] = {}

    # --- scalars: highest-priority non-empty value wins -------------------
    for field in SCALAR_FIELDS:
        best_value = None
        best_rank = None
        for rec in records:
            value = getattr(rec, field)
            if _is_empty(value):
                continue
            r = _rank(source_of(rec), field)
            if best_rank is None or r < best_rank:
                best_value, best_rank = value, r
                provenance[field] = source_of(rec)
        if best_value is not None:
            setattr(merged, field, best_value)

    if merged.type == StrainType.UNKNOWN:
        # Fall back to any source that at least knows it is a hybrid.
        for rec in records:
            if rec.type != StrainType.UNKNOWN:
                merged.type = rec.type
                provenance["type"] = source_of(rec)
                break

    # --- measurements: widen the range, keep the best average ------------
    for field in ("thc", "cbd", "cbg"):
        combined = Measurement()
        contributors = []
        for rec in sorted(records, key=lambda r: _rank(source_of(r), field)):
            m: Measurement = getattr(rec, field)
            if m.is_empty():
                continue
            combined = combined.merged_with(m)
            contributors.append(source_of(rec))
        if not combined.is_empty():
            setattr(merged, field, combined)
            provenance[field] = "+".join(dict.fromkeys(contributors))

    # --- scored lists: union, keeping the highest-priority score ---------
    for field in LIST_FIELDS:
        pooled: dict[str, ScoredTerm] = {}
        winner: dict[str, int] = {}
        for rec in records:
            rank = _rank(source_of(rec), field)
            for term in getattr(rec, field) or []:
                current = pooled.get(term.name)
                if current is None:
                    pooled[term.name] = ScoredTerm(term.name, term.score)
                    winner[term.name] = rank
                    provenance.setdefault(field, source_of(rec))
                elif current.score is None and term.score is not None:
                    current.score = term.score
                    winner[term.name] = rank
                elif term.score is not None and rank < winner.get(term.name, 99):
                    current.score = term.score
                    winner[term.name] = rank
        if pooled:
            # Scored terms first (descending), then unscored ones alphabetically.
            setattr(
                merged,
                field,
                sorted(
                    pooled.values(),
                    key=lambda t: (t.score is None, -(t.score or 0), t.name),
                ),
            )

    # --- parents / aliases / awards / images: union, order-preserving ----
    merged.parents = _union_strings(records, "parents", provenance, "parents")
    merged.awards = _union_strings(records, "awards", provenance, "awards")
    merged.images = _union_strings(records, "images", provenance, "images")

    aliases = _union_strings(records, "aka", provenance, "aka")
    # Any differing display name from another source is itself a useful alias.
    for rec in records:
        if rec.name and rec.name.lower() != merged.name.lower():
            aliases.append(rec.name)
    merged.aka = list(dict.fromkeys(a for a in aliases if a.lower() != merged.name.lower()))

    # --- grow info: field-by-field, priority ordered ---------------------
    merged.grow = _merge_grow(records, provenance)

    merged.sources = [ref for rec in records for ref in rec.sources]
    merged.provenance = provenance
    return merged


def _merge_grow(records: list[Strain], provenance: dict[str, str]) -> GrowInfo:
    out = GrowInfo()
    fields = [
        "flowering_days_min",
        "flowering_days_max",
        "yield_indoor",
        "yield_outdoor",
        "height_indoor",
        "height_outdoor",
        "harvest_month",
        "difficulty",
    ]
    ordered = sorted(
        records,
        key=lambda r: _rank(r.sources[0].source if r.sources else "unknown", "grow"),
    )
    for rec in ordered:
        for field in fields:
            if getattr(out, field) is None:
                value = getattr(rec.grow, field)
                if not _is_empty(value):
                    setattr(out, field, value)
                    provenance[f"grow.{field}"] = (
                        rec.sources[0].source if rec.sources else "unknown"
                    )
        if out.environment.value == "unknown" and rec.grow.environment.value != "unknown":
            out.environment = rec.grow.environment
    return out


def _union_strings(
    records: list[Strain], field: str, provenance: dict[str, str], prov_key: str
) -> list[str]:
    seen: dict[str, str] = {}
    for rec in sorted(
        records, key=lambda r: _rank(r.sources[0].source if r.sources else "unknown", field)
    ):
        for value in getattr(rec, field) or []:
            if not value:
                continue
            key = str(value).strip().lower()
            if key not in seen:
                seen[key] = str(value).strip()
                provenance.setdefault(
                    prov_key, rec.sources[0].source if rec.sources else "unknown"
                )
    return list(seen.values())


def _is_empty(value) -> bool:
    if value is None or value == "" or value == []:
        return True
    if isinstance(value, StrainType) and value == StrainType.UNKNOWN:
        return True
    return False


# ----------------------------------------------------------------------
# Grouping
# ----------------------------------------------------------------------


def group_and_merge(
    records: list[Strain], fuzzy: bool = True, threshold: float = 0.93
) -> list[Strain]:
    """Group records by canonical key (plus an optional fuzzy pass) and merge."""
    groups: dict[str, list[Strain]] = defaultdict(list)
    for rec in records:
        key = rec.canonical_key or rec.slug or rec.name.lower()
        groups[key].append(rec)

    if fuzzy:
        groups = _fuzzy_collapse(groups, threshold)

    merged = []
    for key, recs in groups.items():
        try:
            strain = merge_strains(recs)
        except Exception as exc:  # noqa: BLE001
            log.warning("merge failed for %s: %s", key, exc)
            merged.append(recs[0])
            continue
        strain.canonical_key = key
        merged.append(strain)
    return sorted(merged, key=lambda s: s.name.lower())


def _fuzzy_collapse(
    groups: dict[str, list[Strain]], threshold: float
) -> dict[str, list[Strain]]:
    """Collapse near-identical keys.

    Guardrails, all of which must hold before two keys merge:
      * string similarity >= threshold (default 0.93 - very close)
      * identical token multiset once digits and separators are dropped, OR a
        one-token difference where that token is purely numeric
      * neither key is an autoflower variant of the other
    The numeric rule is what lets "gorilla-glue-4" meet "gorilla-glue" while
    keeping "cookies-1" and "cookies-2" apart, since both of those carry a digit
    the other lacks *and* differ from each other.
    """
    keys = sorted(groups, key=lambda k: (-len(groups[k]), k))
    merged_into: dict[str, str] = {}

    for i, key in enumerate(keys):
        if key in merged_into:
            continue
        for other in keys[i + 1 :]:
            if other in merged_into:
                continue
            if key.endswith("-auto") != other.endswith("-auto"):
                continue
            # A numeric conflict is always disqualifying: "cookies-1" and
            # "cookies-2" are different cuts no matter how similar they look.
            if _numeric_conflict(key, other):
                continue
            # Either the tokens line up exactly (modulo one numeric suffix), or
            # the strings are near-identical - a plural or a typo.
            if _tokens_compatible(key, other):
                merged_into[other] = key
                continue
            if difflib.SequenceMatcher(None, key, other).ratio() >= threshold:
                merged_into[other] = key

    out: dict[str, list[Strain]] = defaultdict(list)
    for key, recs in groups.items():
        out[merged_into.get(key, key)].extend(recs)
    return out


def _numeric_conflict(a: str, b: str) -> bool:
    """True when both keys carry numbers and those numbers disagree."""
    na = {t for t in a.split("-") if t.isdigit()}
    nb = {t for t in b.split("-") if t.isdigit()}
    return bool(na) and bool(nb) and na != nb


def _tokens_compatible(a: str, b: str) -> bool:
    ta = [t for t in a.split("-") if t]
    tb = [t for t in b.split("-") if t]
    sa, sb = set(ta), set(tb)
    if sa == sb:
        return True
    diff = sa.symmetric_difference(sb)
    # Allow exactly one extra token, and only when it is a bare number.
    if len(diff) == 1 and all(t.isdigit() for t in diff):
        return True
    return False
