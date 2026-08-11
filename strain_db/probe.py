"""Spec calibration helper.

The selectors in `sources/specs/*.yaml` were written without access to the live
sites, so they need verifying on a network that can reach them. `probe` fetches
one page and reports exactly which extraction strategies fire, which spec fields
resolved, and - crucially - what the site's own embedded JSON payload looks like,
so the correct paths can be read off rather than guessed.

    strain-db probe leafly https://www.leafly.com/strains/blue-dream
"""

from __future__ import annotations

import json
from typing import Any

from . import extract
from .http import Client
from .sources import SOURCES

#: Field names worth hunting for inside an unknown JSON payload.
INTERESTING_KEYS = [
    "name", "slug", "id", "category", "subcategory", "strainType", "type",
    "thc", "thcAvg", "thc_percentage", "thcPercent", "cbd", "cbdAvg",
    "cbd_percentage", "effects", "negatives", "flavors", "terps", "terpenes",
    "conditions", "symptoms", "description", "rating", "averageRating",
    "reviewCount", "reviews_count", "genetics", "lineage", "parents",
    "phenotypes", "breeder", "floweringTime", "flowering_time", "awards",
]


def probe(source_name: str, url: str, client: Client, save_html: str | None = None) -> dict:
    source_cls = SOURCES.get(source_name)
    if source_cls is None:
        raise SystemExit(f"unknown source {source_name!r}; choose from {sorted(SOURCES)}")

    source = source_cls(client)
    result = client.get(url, force=True)
    html = result.text

    if save_html:
        from pathlib import Path

        Path(save_html).parent.mkdir(parents=True, exist_ok=True)
        Path(save_html).write_text(html, encoding="utf-8")

    report: dict[str, Any] = {
        "url": url,
        "source": source_name,
        "status": result.status,
        "bytes": len(html),
        "looks_like_challenge": _looks_like_challenge(html),
    }

    soup = extract.soup_of(html)

    # --- strategy 1: app state ---
    state_name, state = extract.app_state(html)
    report["app_state"] = {"found": state_name}
    if state is not None:
        report["app_state"]["top_level_keys"] = sorted(state.keys())[:40]
        configured = source.spec.get("detail", {}).get("state_root")
        report["app_state"]["configured_root"] = configured
        report["app_state"]["configured_root_resolves"] = (
            extract.json_path(state, configured) is not None if configured else None
        )
        report["app_state"]["candidate_paths"] = _candidate_paths(state)
        report["app_state"]["key_hits"] = {
            key: _preview(extract.deep_find(state, key, limit=2))
            for key in INTERESTING_KEYS
            if extract.deep_find(state, key, limit=1)
        }

    # --- strategy 2: JSON-LD ---
    ld_objects = extract.json_ld(soup)
    report["json_ld"] = {
        "count": len(ld_objects),
        "types": [o.get("@type") for o in ld_objects][:10],
        "keys": sorted({k for o in ld_objects for k in o})[:30],
    }

    # --- strategy 3: what the spec actually extracts right now ---
    raw = source.parse_detail(url, html)
    if raw is None:
        report["extracted"] = None
        report["verdict"] = "FAILED - no name extracted; spec needs correction"
        return report

    report["extracted"] = {
        k: _preview(v) for k, v in sorted(raw.fields.items()) if k != "_pairs"
    }
    report["pairs_found"] = raw.fields.get("_pairs", {})

    strain = source.to_strain(raw)
    populated = {k: v for k, v in strain.to_dict().items() if _nonempty(v)}
    missing = [
        f
        for f in ("type", "thc", "effects", "flavors", "description", "parents")
        if not _nonempty(strain.to_dict().get(f))
    ]
    report["normalized_fields"] = sorted(populated)
    report["missing_important_fields"] = missing
    report["normalized_sample"] = {
        "name": strain.name,
        "canonical_key": strain.canonical_key,
        "type": strain.type.value,
        "thc": strain.thc.__dict__,
        "effects": [t.name for t in strain.effects][:8],
        "flavors": [t.name for t in strain.flavors][:8],
        "parents": strain.parents,
        "breeder": strain.breeder,
    }
    report["verdict"] = "OK" if not missing else f"PARTIAL - missing {', '.join(missing)}"
    return report


def _candidate_paths(state: Any, max_depth: int = 5) -> list[str]:
    """Find dotted paths to dicts that look like a strain record."""
    hits: list[str] = []

    def walk(node: Any, path: str, depth: int) -> None:
        if depth > max_depth or len(hits) >= 15:
            return
        if isinstance(node, dict):
            keys = set(node.keys())
            if "name" in keys and keys & {
                "slug", "id", "category", "effects", "thc", "thcAvg", "description",
            }:
                hits.append(path or "<root>")
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k, depth + 1)
        elif isinstance(node, list):
            for i, item in enumerate(node[:3]):
                walk(item, f"{path}[{i}]", depth + 1)

    walk(state, "", 0)
    return hits


def _looks_like_challenge(html: str) -> bool:
    """Detect a bot-check interstitial rather than real content.

    Worth calling out explicitly: a 200 response carrying a challenge page will
    otherwise look like a broken selector, sending you off debugging YAML when
    the real problem is that the site did not serve you the page.
    """
    markers = [
        "cf-browser-verification",
        "Just a moment...",
        "Checking your browser",
        "captcha-delivery",
        "px-captcha",
        "Access to this page has been denied",
        "Enable JavaScript and cookies to continue",
    ]
    head = html[:20000]
    return any(m.lower() in head.lower() for m in markers)


def _preview(value: Any, limit: int = 160) -> Any:
    if isinstance(value, str):
        return value[:limit] + ("..." if len(value) > limit else "")
    if isinstance(value, list):
        return [_preview(v, 60) for v in value[:5]]
    if isinstance(value, dict):
        return {k: _preview(v, 60) for k, v in list(value.items())[:8]}
    return value


def _nonempty(value: Any) -> bool:
    if value in (None, "", [], {}, "unknown"):
        return False
    if isinstance(value, dict) and all(v is None for v in value.values()):
        return False
    return True


def format_report(report: dict) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False, default=str)
