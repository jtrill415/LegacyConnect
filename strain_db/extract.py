"""Extraction strategies, ordered from most to least stable.

Sites of this kind are modern JS apps, and their rendered class names are the
*least* durable thing about them. So we try, in order:

  1. JSON-LD   (`<script type="application/ld+json">`) - schema.org, rarely churns
  2. App state (`__NEXT_DATA__`, `__NUXT__`, `__APOLLO_STATE__`) - full API payload
  3. CSS       (declarative selectors from a YAML spec) - last resort

Every selector lives in `sources/specs/*.yaml`, never in Python. When a site
ships a redesign the fix is a YAML edit, and `probe` (see probe.py) reports
which strategies still fire on a live page.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.S)


def soup_of(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001 - lxml missing in some environments
        return BeautifulSoup(html, "html.parser")


# --------------------------------------------------------------------------
# Strategy 1: JSON-LD
# --------------------------------------------------------------------------


def json_ld(soup: BeautifulSoup) -> list[dict]:
    """Return every JSON-LD object on the page, flattening @graph containers."""
    out: list[dict] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Some sites emit trailing commas / HTML comments around the JSON.
            m = _JSON_OBJ_RE.search(raw)
            if not m:
                continue
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
        for obj in _iter_ld(data):
            out.append(obj)
    return out


def _iter_ld(data: Any) -> Iterable[dict]:
    if isinstance(data, list):
        for item in data:
            yield from _iter_ld(item)
    elif isinstance(data, dict):
        if "@graph" in data:
            yield from _iter_ld(data["@graph"])
        else:
            yield data


def json_ld_of_type(soup: BeautifulSoup, *types: str) -> dict | None:
    wanted = {t.lower() for t in types}
    for obj in json_ld(soup):
        t = obj.get("@type", "")
        names = {t.lower()} if isinstance(t, str) else {str(x).lower() for x in t or []}
        if names & wanted:
            return obj
    return None


# --------------------------------------------------------------------------
# Strategy 2: embedded application state
# --------------------------------------------------------------------------

_STATE_PATTERNS = [
    ("__NEXT_DATA__", re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)),
    ("__NUXT__", re.compile(r"window\.__NUXT__\s*=\s*(\{.*?\});?\s*</script>", re.S)),
    ("__APOLLO_STATE__", re.compile(r"window\.__APOLLO_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.S)),
    ("__INITIAL_STATE__", re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.S)),
    ("__PRELOADED_STATE__", re.compile(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.S)),
]


def app_state(html: str) -> tuple[str, dict] | tuple[None, None]:
    """Find an embedded JSON app-state blob. Returns (name, data)."""
    for name, pattern in _STATE_PATTERNS:
        m = pattern.search(html)
        if not m:
            continue
        raw = m.group(1).strip()
        try:
            return name, json.loads(raw)
        except json.JSONDecodeError:
            candidate = _balanced_json(raw)
            if candidate:
                try:
                    return name, json.loads(candidate)
                except json.JSONDecodeError:
                    pass
            log.debug("found %s but could not parse it as JSON", name)
    return None, None


def _balanced_json(text: str) -> str | None:
    """Trim to the first balanced {...} block, ignoring braces inside strings."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def json_path(data: Any, path: str, default: Any = None) -> Any:
    """Traverse a dotted path with list indexing and `[*]` wildcards.

        json_path(d, "props.pageProps.strain.name")
        json_path(d, "results[0].slug")
        json_path(d, "effects[*].name")   -> list

    Returns `default` when any hop is missing, so specs can list optional paths.
    """
    if data is None or not path:
        return default

    current: Any = data
    for token in _split_path(path):
        if current is None:
            return default
        if token == "*":
            # Stay on the list and let the next token map across it; returning
            # here would silently ignore the rest of the path.
            if not isinstance(current, list):
                return default
            continue
        if isinstance(token, int):
            if not isinstance(current, (list, tuple)) or token >= len(current):
                return default
            current = current[token]
        elif isinstance(current, dict):
            if token not in current:
                return default
            current = current[token]
        elif isinstance(current, list):
            # Mapping a key across a list: effects[*].name after the wildcard
            collected = [
                item.get(token) for item in current if isinstance(item, dict) and token in item
            ]
            current = collected or default
        else:
            return default
    return current if current is not None else default


_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(\*|\d+)\]")


def _split_path(path: str) -> list:
    tokens: list = []
    for name, index in _TOKEN_RE.findall(path):
        if name:
            tokens.append(name)
        elif index == "*":
            tokens.append("*")
        elif index:
            tokens.append(int(index))
    return tokens


def deep_find(data: Any, key: str, limit: int = 50) -> list:
    """Find every value stored under `key` anywhere in a nested structure.

    Used by `probe` and as a fallback when a site moves a field around inside
    its app state but keeps the field name - which is the common case.
    """
    found: list = []

    def walk(node: Any, depth: int = 0) -> None:
        if len(found) >= limit or depth > 12:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if k == key:
                    found.append(v)
                    if len(found) >= limit:
                        return
                walk(v, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(data)
    return found


# --------------------------------------------------------------------------
# Strategy 3: declarative CSS selectors
# --------------------------------------------------------------------------


def select(soup: BeautifulSoup, spec: Any) -> Any:
    """Apply one field spec from a YAML site spec.

    A spec is either a bare CSS selector string, or a mapping:

        sel:    CSS selector (required)
        attr:   attribute to read; "text" (default) or "html"
        many:   return a list instead of the first match
        regex:  regex applied to the value; group 1 if present, else group 0
        join:   separator used when many=false but the selector matches several
        default: value when nothing matches
    """
    if spec is None:
        return None
    if isinstance(spec, str):
        spec = {"sel": spec}
    if isinstance(spec, list):
        # A list of alternatives: first one that yields a value wins.
        for alt in spec:
            value = select(soup, alt)
            if value not in (None, "", []):
                return value
        return None

    sel = spec.get("sel")
    if not sel:
        return spec.get("default")

    try:
        nodes = soup.select(sel)
    except Exception as exc:  # noqa: BLE001 - a bad selector must not kill a crawl
        log.warning("invalid selector %r: %s", sel, exc)
        return spec.get("default")

    if not nodes:
        return spec.get("default")

    attr = spec.get("attr", "text")
    values = [_node_value(n, attr) for n in nodes]
    values = [v for v in (_clean(v) for v in values) if v]

    if spec.get("regex"):
        pattern = re.compile(spec["regex"], re.I | re.S)
        matched = []
        for v in values:
            m = pattern.search(v)
            if m:
                matched.append(m.group(1) if m.groups() else m.group(0))
        values = matched

    if not values:
        return spec.get("default")
    if spec.get("many"):
        # Preserve order, drop duplicates.
        return list(dict.fromkeys(values))
    if spec.get("join"):
        return spec["join"].join(dict.fromkeys(values))
    return values[0]


def _node_value(node, attr: str) -> str:
    if attr == "text":
        return node.get_text(" ", strip=True)
    if attr == "html":
        return node.decode_contents()
    value = node.get(attr)
    if isinstance(value, list):
        return " ".join(value)
    return value or ""


def _clean(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def select_pairs(soup: BeautifulSoup, spec: dict) -> dict[str, str]:
    """Extract a definition-list style table into a dict.

    Many of these sites render grow data as label/value rows; this turns
    "Flowering Time | 8-10 weeks" rows into {"flowering time": "8-10 weeks"}.
    """
    if not spec or not spec.get("row"):
        return {}
    out: dict[str, str] = {}
    for row in soup.select(spec["row"]):
        label = _clean(_first_text(row, spec.get("label")))
        value = _clean(_first_text(row, spec.get("value")))
        if label and value:
            out[label.lower().rstrip(":").strip()] = value
    return out


def _first_text(row, sel: str | None) -> str:
    if not sel:
        return ""
    node = row.select_one(sel)
    return node.get_text(" ", strip=True) if node else ""
