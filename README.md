# strain-db

Builds a single normalized cannabis strain database from four public sources:

| Source | URL | Strongest at |
|---|---|---|
| Leafly | https://www.leafly.com/strains | Review-driven effects, potency, ratings |
| Weedmaps | https://weedmaps.com/strains | Effects, flavors, potency |
| CannaConnection | https://www.cannaconnection.com/strains | Grow specs, seedbank data |
| SeedFinder | https://seedfinder.eu/en | Breeder attribution and deep lineage |

The point is not four scrapers — it is one database. The same plant is called
`GSC` on one site and `Girl Scout Cookies (GSC)` on another, rated out of 5
here and out of 10 there, with THC as `18.5` in one payload and `"17-24%"` in
another. This project resolves all of that into one row per strain, with
per-field provenance recording which source supplied each value.

---

## ⚠️ Read this first: the selectors are unverified

The pipeline is built and tested. **The four site specs are not yet calibrated
against the live sites.**

This was developed in an environment whose egress policy blocks
`weedmaps.com`, `leafly.com`, `cannaconnection.com`, and `seedfinder.eu` — every
request returns `403` at the proxy before it reaches the network. I could not
load a single real page, so the CSS selectors and JSON paths in
`strain_db/sources/specs/*.yaml` are **informed guesses about each site's
structure, not observed facts**.

What that means concretely:

- ✅ **Verified**: extraction strategies, normalization, dedup/merge, storage,
  export, robots handling, caching, retries, the browser fetcher, and the CLI —
  178 tests, including integration tests that crawl a real local HTTP server end
  to end with both the plain and browser clients.
- ❌ **Unverified**: whether `props.pageProps.strain` is really where Leafly
  keeps its data, whether `api-g.weedmaps.com/discovery/v1/strains` is really
  Weedmaps' catalogue endpoint, and every CSS selector in the four specs.

**Expect the first live run to extract little or nothing until you calibrate.**
That is a ten-minute job, and `probe` exists to make it mechanical — see below.

Two further things you will hit on a real network, which no amount of selector
tuning fixes:

- **Bot protection.** Leafly and Weedmaps sit behind Cloudflare-class defenses,
  which answer a plain `requests` call with a challenge page instead of content.
  The Playwright-backed fetcher (below) handles this by driving a real browser.
  `probe` also detects challenges explicitly (`looks_like_challenge`) rather
  than letting one masquerade as a broken selector.
- **Terms of service.** Leafly's and Weedmaps' ToS restrict automated
  collection. This tool honors `robots.txt` by default and rate-limits itself,
  but robots compliance is not the same as ToS compliance. Whether to crawl,
  and under what agreement, is your call to make — `--ignore-robots` exists but
  is off by default and warns when used.

---

## The browser fetcher

For sites that refuse a plain HTTP client, `strain_db/browser.py` drives a real
Chromium via Playwright, so the JS challenge executes and the page renders as a
browser would see it.

```bash
pip install -e ".[browser]"     # Chromium: playwright install chromium
strain-db crawl                 # --browser auto is the default
```

`--browser auto` (default) consults each site spec's `requires_browser` flag, so
only Leafly and Weedmaps pay the browser cost while CannaConnection and
SeedFinder keep the fast HTTP path. Force it either way with `--browser
always` / `--browser never`, and watch it work with `--headful`.

`BrowserClient` is a drop-in replacement for `http.Client` — same `get()`, same
`FetchResult`, same cache, robots policy, and rate limiter. Nothing downstream
of the fetch knows which one it is talking to, which is why this dropped in
without touching extraction, normalization, merge, or storage. Both clients
share one cache format, so a browser crawl is fully `reparse`-able offline.

It also: reads `robots.txt` *through the browser* (a protected host can
otherwise hide its rules and get treated as unrestricted by accident); waits out
a challenge for a bounded window instead of storing the interstitial as content;
and returns raw JSON for API endpoints rather than the `<pre>` a browser wraps
them in.

Two honest caveats. A page load is roughly an order of magnitude slower and
heavier than an HTTP GET — use `auto`. And the browser path sends an ordinary
desktop user-agent rather than the transparent `StrainDBBot` string the plain
client uses, because a protected site rejects the latter outright; that is a
real trade-off in transparency, and an official data agreement beats both.

The tests in `tests/test_browser.py` drive real Chromium against a local server,
including the case that justifies the module: a page whose content exists only
after JS runs, which the plain client provably cannot see.

---

## Calibrating a spec

On a network that can reach the sites:

```bash
strain-db probe leafly https://www.leafly.com/strains/blue-dream
```

`probe` fetches one page and reports:

- whether the response is real content or a bot-check interstitial
- which extraction strategies fired (JSON-LD / app state / CSS)
- the **actual** top-level keys and candidate paths inside the site's embedded
  JSON, so you can read the correct path off rather than guess it
- which spec fields currently resolve, and which important ones are missing

Then edit `strain_db/sources/specs/<source>.yaml` — selectors live in YAML, not
Python, so this never requires touching code — and re-run `probe` until the
verdict is `OK`. Save a page as a fixture with `--save-html` to lock the fix in
with a test.

Because every fetched page is cached on disk, you can iterate on parsing without
re-crawling:

```bash
strain-db reparse            # rebuild the DB from cached pages, zero network
```

---

## Usage

```bash
pip install -e .

# Smoke-test one source before committing to a full crawl
strain-db crawl leafly --limit 20 -v

# Full crawl of everything, politely
strain-db crawl --delay 3 -v

# Inspect and export
strain-db stats
strain-db search "blue dream"
strain-db export strains.csv --format csv
strain-db export strains.json --format json
```

Useful flags: `--limit` (cap per source), `--no-merge` (keep per-source rows,
for spec debugging), `--no-fuzzy` (exact-key dedup only), `--delay` (seconds
between requests per host), `--dump-raw` (write every pre-merge record to
JSONL), `--browser auto|never|always` (see [The browser fetcher](#the-browser-fetcher)).
Global flags work before or after the subcommand.

---

## How it works

```
discover ──► fetch ──► extract ──► normalize ──► merge ──► store
 sitemap     robots     JSON-LD     controlled   dedup +   SQLite
 /API/HTML   cache      app state   vocabulary   provenance  + FTS
             retries    CSS
```

**Discovery** prefers sitemaps (cheapest and politest), falling back to a JSON
API, paginated HTML, or A–Z index pages depending on the site.

**Extraction** runs a three-rung ladder, most durable first: JSON-LD, then the
embedded app-state blob (`__NEXT_DATA__` and friends — usually the full API
payload the page was rendered from), then CSS selectors. Rendered class names
are the *least* stable thing about a modern JS site, so they are the last
resort, and every one of them lives in YAML.

**Normalization** maps everything onto a controlled vocabulary: `"Relaxing"`,
`"Relaxed"` and `"couch lock"` become `relaxed` and `sleepy`; `"60% Indica /
40% Sativa"` becomes `INDICA_DOMINANT` plus explicit percentages; `"8-10
weeks"` becomes 56–70 days.

**Merging** groups records by a canonical key and folds them with a per-field
trust order (`sources/__init__.py:FIELD_PRIORITY`) — SeedFinder wins genetics,
Leafly wins effects, potency ranges widen to cover every source. Two rules the
tests pin down, because getting them wrong silently destroys data:

- **Autoflower variants never merge with their photoperiod parent.** `Auto Blue
  Dream` is a different product from `Blue Dream`.
- **Numbered cuts never merge.** `Cookies #1` and `Cookies #2` stay apart, while
  `Gorilla Glue #4` and `Gorilla Glue` are allowed to combine.

A false split leaves two rows a human can reconcile. A false merge destroys a
strain. When in doubt, the merger splits.

**Storage** is SQLite: one row per canonical strain, child tables for terms,
parents, and sources, plus FTS5 search. Writes are upserts keyed on the
canonical key, so re-crawling updates rather than duplicates, and a sparse
re-crawl never nulls out data an earlier richer crawl found.

---

## Data model

Per strain: name and aliases, type with indica/sativa percentages, breeder,
parents and lineage, THC/CBD/CBG as min/max/avg, effects / negatives / medical
uses / flavors / aromas / terpenes (each with an optional strength score), grow
data (flowering days, yields, heights, environment, difficulty), awards,
description, rating, review count, images, plus every contributing source URL
and a `provenance` map of which source won each field.

Potency is stored as min/max/avg rather than a single number on purpose: a
lab-tested `20%` and a breeder's `up to 20%` are different claims, and
collapsing them loses that.

---

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q
```

178 tests, no external network required. `tests/test_integration.py` starts a
real HTTP server on localhost and exercises the actual client — robots, cache,
retries — plus the CLI end to end. `tests/test_browser.py` does the same with
real Chromium, and skips cleanly when Playwright or a browser binary is absent.

The fixtures in `tests/fixtures/` are **synthetic**. They mirror the shape each
spec expects so the pipeline can be tested offline; they are not copies of real
pages and do not validate the specs against production markup. Only `probe`
does that.
