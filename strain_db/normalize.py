"""Turn source-native values into the controlled vocabulary in `models`.

Everything in this module is pure: no network, no I/O. That is deliberate, so
the messy part of the pipeline (deciding that "Sativa-dominant Hybrid", "70%
sativa" and "Sativa / Hybrid" are the same thing) is unit-testable offline.
"""

from __future__ import annotations

import re
import unicodedata

from .models import Environment, Measurement, ScoredTerm, StrainType

# --------------------------------------------------------------------------
# Strain type
# --------------------------------------------------------------------------

_PCT_PAIR_RE = re.compile(
    r"(?P<a>\d{1,3})\s*%?\s*(?P<atype>indica|sativa)\D{0,12}?(?P<b>\d{1,3})\s*%?\s*(?P<btype>indica|sativa)",
    re.I,
)
_PCT_SINGLE_RE = re.compile(r"(?P<pct>\d{1,3})\s*%\s*(?P<type>indica|sativa)", re.I)


def parse_type(text: str | None) -> tuple[StrainType, float | None, float | None]:
    """Parse a free-text type/ratio blurb into (type, indica_pct, sativa_pct).

    Handles the four shapes the target sites actually use:
      "Indica"                      -> INDICA
      "Sativa-dominant Hybrid"      -> SATIVA_DOMINANT
      "60% Indica / 40% Sativa"     -> INDICA_DOMINANT, 60.0, 40.0
      "Indica/Sativa/Ruderalis"     -> HYBRID (ruderalis flagged separately)
    """
    if not text:
        return StrainType.UNKNOWN, None, None

    t = text.strip().lower()
    indica_pct: float | None = None
    sativa_pct: float | None = None

    pair = _PCT_PAIR_RE.search(t)
    if pair:
        a, b = float(pair.group("a")), float(pair.group("b"))
        if pair.group("atype").lower() == "indica":
            indica_pct, sativa_pct = a, b
        else:
            sativa_pct, indica_pct = a, b
    else:
        single = _PCT_SINGLE_RE.search(t)
        if single:
            pct = float(single.group("pct"))
            if single.group("type").lower() == "indica":
                indica_pct, sativa_pct = pct, round(100.0 - pct, 2)
            else:
                sativa_pct, indica_pct = pct, round(100.0 - pct, 2)

    # 60/40 is conventionally sold as "indica-dominant hybrid", so the dominance
    # threshold sits at 60 rather than higher; 90+ reads as effectively pure.
    if indica_pct is not None and sativa_pct is not None:
        if indica_pct >= 60:
            return StrainType.INDICA if indica_pct >= 90 else StrainType.INDICA_DOMINANT, indica_pct, sativa_pct
        if sativa_pct >= 60:
            return StrainType.SATIVA if sativa_pct >= 90 else StrainType.SATIVA_DOMINANT, indica_pct, sativa_pct
        return StrainType.HYBRID, indica_pct, sativa_pct

    has_ind = "indica" in t
    has_sat = "sativa" in t
    dominant = "dominant" in t or "-dom" in t or "leaning" in t

    if "high cbd" in t or re.search(r"\bcbd\b", t) and not has_ind and not has_sat:
        return StrainType.CBD, None, None
    if has_ind and has_sat:
        if dominant and t.find("indica") < t.find("sativa"):
            return StrainType.INDICA_DOMINANT, indica_pct, sativa_pct
        if dominant:
            return StrainType.SATIVA_DOMINANT, indica_pct, sativa_pct
        return StrainType.HYBRID, indica_pct, sativa_pct
    if has_ind:
        return (StrainType.INDICA_DOMINANT if dominant else StrainType.INDICA), indica_pct, sativa_pct
    if has_sat:
        return (StrainType.SATIVA_DOMINANT if dominant else StrainType.SATIVA), indica_pct, sativa_pct
    if "ruderalis" in t:
        return StrainType.RUDERALIS, None, None
    if "hybrid" in t:
        return StrainType.HYBRID, indica_pct, sativa_pct
    return StrainType.UNKNOWN, indica_pct, sativa_pct


# --------------------------------------------------------------------------
# Cannabinoid measurements
# --------------------------------------------------------------------------

_RANGE_RE = re.compile(r"(\d{1,2}(?:[.,]\d+)?)\s*(?:%|percent)?\s*(?:-|–|—|to)\s*(\d{1,2}(?:[.,]\d+)?)\s*%?")
_SINGLE_RE = re.compile(r"(\d{1,2}(?:[.,]\d+)?)\s*%")
_UPTO_RE = re.compile(r"(?:up to|max(?:imum)?|<|under)\s*(\d{1,2}(?:[.,]\d+)?)\s*%?", re.I)
_BARE_RE = re.compile(r"^\s*(\d{1,2}(?:[.,]\d+)?)\s*$")


def _f(v: str) -> float:
    return float(v.replace(",", "."))


def parse_measurement(value: object) -> Measurement:
    """Parse "18%", "18-24%", "up to 24%", "THC: 20.5" or a bare number."""
    if value is None:
        return Measurement()
    if isinstance(value, (int, float)):
        return Measurement(avg=float(value))

    text = str(value).strip()
    if not text:
        return Measurement()

    m = _RANGE_RE.search(text)
    if m:
        lo, hi = _f(m.group(1)), _f(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        return Measurement(min=lo, max=hi, avg=round((lo + hi) / 2, 2))

    m = _UPTO_RE.search(text)
    if m:
        return Measurement(max=_f(m.group(1)))

    m = _SINGLE_RE.search(text)
    if m:
        return Measurement(avg=_f(m.group(1)))

    m = _BARE_RE.match(text)
    if m:
        return Measurement(avg=_f(m.group(1)))

    return Measurement()


# --------------------------------------------------------------------------
# Controlled vocabulary for effects / flavors / medical terms
# --------------------------------------------------------------------------

EFFECTS = {
    "relaxed": ["relaxed", "relaxing", "relaxation", "calm", "calming", "chilled", "chill"],
    "happy": ["happy", "happiness", "joyful", "cheerful"],
    "euphoric": ["euphoric", "euphoria", "blissful"],
    "uplifted": ["uplifted", "uplifting", "upbeat", "mood lift", "elevated"],
    "creative": ["creative", "creativity"],
    "energetic": ["energetic", "energizing", "energy", "stimulated", "stimulating"],
    "focused": ["focused", "focus", "clear headed", "clear-headed"],
    "giggly": ["giggly", "giggles", "laughter"],
    "talkative": ["talkative", "chatty", "sociable", "social"],
    "hungry": ["hungry", "hunger", "appetite", "munchies", "aroused appetite"],
    "sleepy": ["sleepy", "sedated", "sedative", "sleep", "drowsy", "couch lock", "couch-lock"],
    "tingly": ["tingly", "tingling", "body high", "body buzz"],
    "aroused": ["aroused", "arousing", "aphrodisiac"],
}

NEGATIVES = {
    "dry_mouth": ["dry mouth", "cottonmouth", "cotton mouth", "xerostomia"],
    "dry_eyes": ["dry eyes"],
    "dizzy": ["dizzy", "dizziness", "lightheaded"],
    "paranoid": ["paranoid", "paranoia"],
    "anxious": ["anxious", "anxiety", "nervous"],
    "headache": ["headache", "headaches", "migraine trigger"],
}

MEDICAL = {
    "stress": ["stress", "stressed"],
    "anxiety": ["anxiety", "anxious"],
    "depression": ["depression", "depressed", "low mood"],
    "pain": ["pain", "chronic pain", "aches", "body pain"],
    "insomnia": ["insomnia", "sleeplessness", "sleep disorders", "trouble sleeping"],
    "lack_of_appetite": ["lack of appetite", "appetite loss", "anorexia", "loss of appetite"],
    "nausea": ["nausea", "nauseous", "vomiting"],
    "inflammation": ["inflammation", "inflammatory"],
    "muscle_spasms": ["muscle spasms", "spasms", "cramps", "muscle cramps"],
    "headaches": ["headaches", "headache", "migraines", "migraine"],
    "ptsd": ["ptsd", "post traumatic stress"],
    "adhd": ["adhd", "add", "attention deficit"],
    "epilepsy": ["epilepsy", "seizures", "seizure"],
    "glaucoma": ["glaucoma"],
    "fatigue": ["fatigue", "tiredness"],
}

FLAVORS = {
    "earthy": ["earthy", "earth", "soil"],
    "sweet": ["sweet", "sugary", "candy"],
    "citrus": ["citrus", "lemon", "lime", "orange", "grapefruit", "tangy"],
    "pine": ["pine", "piney", "pinene"],
    "berry": ["berry", "blueberry", "strawberry", "raspberry", "blackberry"],
    "diesel": ["diesel", "fuel", "gassy", "gas", "petrol"],
    "skunk": ["skunk", "skunky"],
    "woody": ["woody", "wood", "cedar", "oak"],
    "spicy": ["spicy", "spicy/herbal", "peppery", "pepper", "herbal"],
    "floral": ["floral", "flowery", "lavender", "rose"],
    "cheese": ["cheese", "cheesy"],
    "tropical": ["tropical", "mango", "pineapple", "papaya", "banana"],
    "grape": ["grape", "grapey"],
    "mint": ["mint", "minty", "menthol"],
    "vanilla": ["vanilla"],
    "coffee": ["coffee", "espresso", "mocha"],
    "chocolate": ["chocolate", "cocoa"],
    "nutty": ["nutty", "nut", "almond"],
    "apple": ["apple"],
    "ammonia": ["ammonia", "chemical"],
    "tea": ["tea"],
    "honey": ["honey"],
    "tobacco": ["tobacco"],
    "pungent": ["pungent"],
}

TERPENES = {
    "myrcene": ["myrcene", "b-myrcene", "beta-myrcene"],
    "limonene": ["limonene", "d-limonene"],
    "caryophyllene": ["caryophyllene", "beta-caryophyllene", "b-caryophyllene"],
    "pinene": ["pinene", "alpha-pinene", "a-pinene", "beta-pinene"],
    "linalool": ["linalool"],
    "humulene": ["humulene", "alpha-humulene"],
    "terpinolene": ["terpinolene"],
    "ocimene": ["ocimene"],
    "bisabolol": ["bisabolol", "alpha-bisabolol"],
    "nerolidol": ["nerolidol"],
    "valencene": ["valencene"],
    "eucalyptol": ["eucalyptol", "cineole"],
}


def _build_lookup(vocab: dict[str, list[str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for canonical, variants in vocab.items():
        out[canonical.replace("_", " ")] = canonical
        for v in variants:
            out[v.lower()] = canonical
    return out


_LOOKUPS = {
    "effects": _build_lookup(EFFECTS),
    "negatives": _build_lookup(NEGATIVES),
    "medical": _build_lookup(MEDICAL),
    "flavors": _build_lookup(FLAVORS),
    "terpenes": _build_lookup(TERPENES),
}


def normalize_term(term: str, vocab: str) -> str | None:
    """Map a source term onto the controlled vocabulary.

    Returns None when the term is not recognised, so callers can decide whether
    to drop it or keep it as an uncontrolled extra. We prefer dropping silently
    over inventing a category, but `unknown_terms()` surfaces misses so the
    vocabulary can be extended from real crawl data.
    """
    if not term:
        return None
    lookup = _LOOKUPS[vocab]
    key = _clean_term(term)
    if key in lookup:
        return lookup[key]
    # Substring fallback: "feeling relaxed" -> relaxed. Longest variant wins so
    # "dry mouth" beats a bare "dry" style partial.
    best: tuple[int, str] | None = None
    for variant, canonical in lookup.items():
        if variant in key and (best is None or len(variant) > best[0]):
            best = (len(variant), canonical)
    return best[1] if best else None


def _clean_term(term: str) -> str:
    t = unicodedata.normalize("NFKD", str(term)).encode("ascii", "ignore").decode()
    t = t.lower().replace("/", " ").replace("-", " ")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def normalize_terms(
    terms: list, vocab: str, keep_unknown: bool = False
) -> tuple[list[ScoredTerm], list[str]]:
    """Normalize a list of terms or (term, score) pairs.

    Returns (normalized, unknown_terms). Duplicate canonical terms are merged,
    keeping the highest score seen.
    """
    out: dict[str, ScoredTerm] = {}
    unknown: list[str] = []
    for item in terms or []:
        if isinstance(item, ScoredTerm):
            raw, score = item.name, item.score
        elif isinstance(item, dict):
            raw = item.get("name") or item.get("term") or item.get("label") or ""
            score = item.get("score", item.get("value", item.get("percent")))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            raw, score = item
        else:
            raw, score = item, None

        if score is not None:
            try:
                score = float(score)
                if score > 1.0:  # sources report either 0-1 or 0-100
                    score = score / 100.0
            except (TypeError, ValueError):
                score = None

        canonical = normalize_term(str(raw), vocab)
        if canonical is None:
            unknown.append(str(raw))
            if keep_unknown:
                canonical = _clean_term(raw).replace(" ", "_")
            else:
                continue
        existing = out.get(canonical)
        if existing is None:
            out[canonical] = ScoredTerm(canonical, score)
        elif score is not None and (existing.score is None or score > existing.score):
            existing.score = score
    return list(out.values()), unknown


# --------------------------------------------------------------------------
# Name canonicalization (the dedup key)
# --------------------------------------------------------------------------

#: Well-known abbreviations. Kept deliberately small and hand-checked - a wrong
#: entry here silently merges two different strains, which is worse than a miss.
NAME_ALIASES = {
    "gsc": "girl scout cookies",
    "gg4": "gorilla glue 4",
    "original glue": "gorilla glue 4",
    "gdp": "granddaddy purple",
    "grandaddy purple": "granddaddy purple",
    "granddaddy purps": "granddaddy purple",
    "og": "og kush",
    "acdc": "ac dc",
    "gg": "gorilla glue",
    "bubba": "bubba kush",
    "pineapple express": "pineapple express",
    "gmo": "gmo cookies",
    "garlic cookies": "gmo cookies",
    "zkittlez": "skittles",
    "zkittles": "skittles",
}

#: Words that describe the *seed product*, not the genetics. Stripped from the
#: display name but preserved as flags, because "Auto Blue Dream" and
#: "Blue Dream" are different products and must not collapse into one row.
_SEED_MARKERS = {
    "autoflowering": "auto",
    "autoflower": "auto",
    "automatic": "auto",
    "auto": "auto",
    "feminized": "fem",
    "feminised": "fem",
    "fem": "fem",
    "regular": None,
    "seeds": None,
    "seed": None,
    "strain": None,
}

_NOISE_RE = re.compile(r"\((?:[^()]*)\)")


def canonical_name(name: str) -> tuple[str, bool, bool]:
    """Return (canonical_key, is_autoflower, is_feminized).

    The key is what dedup joins on. Two records merge only if their keys match
    exactly, so the key must be stable across sites but must NOT erase real
    product distinctions.
    """
    if not name:
        return "", False, False

    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = text.lower().strip()

    # Pull the parenthetical out first; it is usually an alias ("Wedding Cake
    # (Triangle Mints #23)") and should not affect the key.
    text = _NOISE_RE.sub(" ", text)

    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9#\s]+", " ", text)
    text = text.replace("#", " ")
    text = re.sub(r"\s+", " ", text).strip()

    is_auto = False
    is_fem = False
    tokens: list[str] = []
    for tok in text.split():
        marker = _SEED_MARKERS.get(tok, "__keep__")
        if marker == "__keep__":
            tokens.append(tok)
        elif marker == "auto":
            is_auto = True
        elif marker == "fem":
            is_fem = True
        # marker is None -> pure noise, drop

    tokens = _collapse_initials(tokens)
    base = " ".join(tokens).strip()
    base = NAME_ALIASES.get(base, base)
    base = re.sub(r"\s+", " ", base).strip()

    key = base.replace(" ", "-")
    if is_auto:
        # Autoflower variants stay distinct rows.
        key = f"{key}-auto"
    return key, is_auto, is_fem


def _collapse_initials(tokens: list[str]) -> list[str]:
    """Join runs of single letters, so "O.G. Kush" keys the same as "OG Kush".

    Only runs of two or more single-letter tokens collapse; a lone letter is
    left alone so "B 52" does not fuse into its neighbour.
    """
    out: list[str] = []
    run: list[str] = []
    for tok in tokens:
        if len(tok) == 1 and tok.isalpha():
            run.append(tok)
            continue
        if len(run) >= 2:
            out.append("".join(run))
        else:
            out.extend(run)
        run = []
        out.append(tok)
    if len(run) >= 2:
        out.append("".join(run))
    else:
        out.extend(run)
    return out


def display_name(name: str) -> str:
    """Tidy a name for display without changing its identity."""
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    return text.strip(" -,|")


def extract_aliases(name: str) -> list[str]:
    """Pull aliases out of "Girl Scout Cookies (GSC)" style names."""
    out: list[str] = []
    for match in re.findall(r"\(([^()]+)\)", str(name or "")):
        cleaned = match.strip(" .,")
        if cleaned and not cleaned.lower().startswith(("aka", "a.k.a")):
            out.append(cleaned)
        elif cleaned:
            out.append(re.sub(r"^a\.?k\.?a\.?\s*", "", cleaned, flags=re.I).strip())
    return [a for a in out if a]


# --------------------------------------------------------------------------
# Grow data
# --------------------------------------------------------------------------

_WEEKS_RE = re.compile(r"(\d{1,2})\s*(?:-|–|to)?\s*(\d{1,2})?\s*weeks?", re.I)
_DAYS_RE = re.compile(r"(\d{2,3})\s*(?:-|–|to)?\s*(\d{2,3})?\s*days?", re.I)


def parse_flowering(text: str | None) -> tuple[int | None, int | None]:
    """Parse "8-10 weeks" / "60 days" / "56 - 63 days" into a day range."""
    if not text:
        return None, None
    s = str(text)

    m = _DAYS_RE.search(s)
    if m:
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        return min(lo, hi), max(lo, hi)

    m = _WEEKS_RE.search(s)
    if m:
        lo = int(m.group(1)) * 7
        hi = int(m.group(2)) * 7 if m.group(2) else lo
        return min(lo, hi), max(lo, hi)

    return None, None


def parse_environment(text: str | None) -> Environment:
    if not text:
        return Environment.UNKNOWN
    t = str(text).lower()
    has_in = "indoor" in t
    has_out = "outdoor" in t
    if "greenhouse" in t and not (has_in or has_out):
        return Environment.GREENHOUSE
    if has_in and has_out:
        return Environment.UNKNOWN  # "indoor & outdoor" tells us nothing useful
    if has_in:
        return Environment.INDOOR
    if has_out:
        return Environment.OUTDOOR
    return Environment.UNKNOWN


_PARENT_SPLIT_RE = re.compile(r"\s*(?:\bx\b|×|\*|\bcross(?:ed)?\s+with\b|/)\s*", re.I)


def split_parents(text: str | None) -> list[str]:
    """Split "Blueberry x Haze" or "OG Kush × Durban Poison" into parents.

    Bare "/" is treated as a cross separator too, but only when it is not part
    of a percentage blurb - callers should pass the genetics field, not the
    type field.
    """
    if not text:
        return []
    cleaned = _NOISE_RE.sub(" ", str(text))
    parts = [p.strip(" .,-–—") for p in _PARENT_SPLIT_RE.split(cleaned)]
    out = []
    for p in parts:
        p = re.sub(r"\s+", " ", p).strip()
        if len(p) < 2 or p.lower() in {"unknown", "n/a", "na", "?"}:
            continue
        if re.fullmatch(r"[\d%.\s]+", p):
            continue
        out.append(display_name(p))
    return out[:6]


def parse_rating(value: object, scale: float = 5.0) -> float | None:
    """Normalize a rating onto a 0-5 scale."""
    if value is None:
        return None
    try:
        v = float(str(value).strip().split("/")[0].replace(",", "."))
    except (TypeError, ValueError):
        return None
    if scale and scale != 5.0 and scale > 0:
        v = v * 5.0 / scale
    if v < 0 or v > 5.0:
        return None
    return round(v, 2)


def parse_int(value: object) -> int | None:
    if value is None:
        return None
    m = re.search(r"\d[\d,\.]*", str(value))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", "").replace(".", ""))
    except ValueError:
        return None
