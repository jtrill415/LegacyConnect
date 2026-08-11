"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from contextlib import contextmanager

from . import probe as probe_mod
from .http import Client, DEFAULT_UA
from .merge import group_and_merge
from .models import Strain
from .sources import SOURCES
from .sources.base import load_spec
from .storage import Database, export

log = logging.getLogger("strain_db")

DEFAULT_DB = "data/strains.db"
DEFAULT_CACHE = "data/cache"


def _add_global_options(parser: argparse.ArgumentParser, suppress: bool) -> None:
    """Register the global options.

    They are added twice: once on the root parser with real defaults, and once
    on every subcommand with `SUPPRESS` defaults so that `crawl leafly -v` works
    as well as `-v crawl leafly`. SUPPRESS matters - without it the subparser
    would write its own defaults over whatever the root parser already parsed.
    """
    d = (lambda value: argparse.SUPPRESS) if suppress else (lambda value: value)

    parser.add_argument("--db", default=d(DEFAULT_DB), help=f"SQLite path (default {DEFAULT_DB})")
    parser.add_argument("--cache-dir", default=d(DEFAULT_CACHE), help="HTTP cache directory")
    parser.add_argument(
        "--delay", type=float, default=d(2.0), help="Seconds between requests per host"
    )
    parser.add_argument("--user-agent", default=d(DEFAULT_UA))
    parser.add_argument("--timeout", type=float, default=d(30.0))
    parser.add_argument(
        "--no-cache",
        action="store_true",
        default=d(False),
        help="Bypass the HTTP cache",
    )
    parser.add_argument(
        "--ignore-robots",
        action="store_true",
        default=d(False),
        help="Do not consult robots.txt. Off by default; only use where you have "
        "permission or a licensing arrangement with the site.",
    )
    parser.add_argument(
        "--browser",
        choices=["auto", "never", "always"],
        default=d("auto"),
        help="Use a real browser (Playwright) to fetch. 'auto' (default) uses one "
        "only for sources whose spec sets requires_browser; 'always' forces it "
        "everywhere; 'never' keeps the plain HTTP client.",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        default=d(False),
        help="Show the browser window (debugging only; implies --browser always)",
    )
    parser.add_argument("-v", "--verbose", action="count", default=d(0))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="strain-db",
        description="Build a normalized cannabis strain database from multiple sources.",
    )
    _add_global_options(p, suppress=False)

    # Accepted after the subcommand too, which is where people naturally type them.
    common = argparse.ArgumentParser(add_help=False)
    _add_global_options(common, suppress=True)

    sub = p.add_subparsers(dest="command", required=True, parser_class=argparse.ArgumentParser)

    def add(name: str, **kw):
        return sub.add_parser(name, parents=[common], **kw)

    c = add("crawl", help="Crawl one or more sources into the database")
    c.add_argument(
        "sources",
        nargs="*",
        default=list(SOURCES),
        help=f"sources to crawl (default: all of {', '.join(SOURCES)})",
    )
    c.add_argument("--limit", type=int, help="Max strains per source (for smoke tests)")
    c.add_argument("--no-merge", action="store_true", help="Store per-source rows unmerged")
    c.add_argument("--no-fuzzy", action="store_true", help="Exact-key dedup only")
    c.add_argument(
        "--dump-raw",
        help="Also write every normalized per-source record to this JSONL file",
    )

    d = add("discover", help="List detail URLs a source would crawl")
    d.add_argument("source", choices=sorted(SOURCES))
    d.add_argument("--limit", type=int, default=25)

    pr = add("probe", help="Diagnose a single page against a source spec")
    pr.add_argument("source", choices=sorted(SOURCES))
    pr.add_argument("url")
    pr.add_argument("--save-html", help="Write the fetched HTML here for use as a fixture")

    rp = add(
        "reparse",
        help="Rebuild the database from cached pages, with no network traffic",
    )
    rp.add_argument("sources", nargs="*", default=list(SOURCES))
    rp.add_argument("--no-fuzzy", action="store_true")

    e = add("export", help="Export the database")
    e.add_argument("path")
    e.add_argument("--format", choices=["json", "jsonl", "csv"], default="json")

    s = add("search", help="Search stored strains")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=20)

    add("stats", help="Show database coverage statistics")

    return p


def wants_browser(args, source_name: str | None = None) -> bool:
    """Decide whether this fetch should go through a real browser.

    'auto' defers to the site spec's `requires_browser`, so the browser cost is
    paid only by the sources that actually need it.
    """
    mode = getattr(args, "browser", "auto")
    if getattr(args, "headful", False):
        return True
    if mode == "always":
        return True
    if mode == "never":
        return False
    if source_name is None:
        return False
    try:
        return bool(load_spec(source_name).get("requires_browser", False))
    except FileNotFoundError:
        return False


def make_client(args, source_name: str | None = None):
    """Build the fetcher for a source: plain HTTP, or Playwright-backed."""
    if wants_browser(args, source_name):
        from .browser import BROWSER_UA, BrowserClient

        # The honest bot UA defeats the purpose here - a protected site rejects
        # it outright - so use the browser UA unless one was set explicitly.
        ua = args.user_agent if args.user_agent != DEFAULT_UA else BROWSER_UA
        log.info("using browser fetcher for %s", source_name or "request")
        return BrowserClient(
            cache_dir=Path(args.cache_dir),
            user_agent=ua,
            delay=max(args.delay, 1.0),
            timeout=max(args.timeout, 45.0),
            respect_robots=not args.ignore_robots,
            use_cache=not args.no_cache,
            headless=not getattr(args, "headful", False),
        )

    return Client(
        cache_dir=Path(args.cache_dir),
        user_agent=args.user_agent,
        delay=args.delay,
        timeout=args.timeout,
        respect_robots=not args.ignore_robots,
        use_cache=not args.no_cache,
    )


@contextmanager
def client_for(args, source_name: str | None = None):
    """Yield a client and always release browser resources afterwards."""
    client = make_client(args, source_name)
    try:
        yield client
    finally:
        if hasattr(client, "close"):
            client.close()


def cmd_crawl(args) -> int:
    db = Database(args.db)
    collected: list[Strain] = []
    dump_fh = open(args.dump_raw, "w", encoding="utf-8") if args.dump_raw else None
    http_stats: dict[str, dict] = {}

    try:
        for name in args.sources:
            source_cls = SOURCES.get(name)
            if source_cls is None:
                log.error("unknown source %r (choose from %s)", name, ", ".join(SOURCES))
                continue
            # One client per source, so only the sources that need a browser
            # start one, and it is torn down as soon as that source is done.
            with client_for(args, name) as client:
                source = source_cls(client)
                log.info("crawling %s ...", name)
                count = 0
                for strain in source.crawl(limit=args.limit):
                    collected.append(strain)
                    count += 1
                    if dump_fh:
                        dump_fh.write(json.dumps(strain.to_dict(), ensure_ascii=False) + "\n")
                    if count % 25 == 0:
                        log.info("[%s] %d strains", name, count)
                log.info("[%s] done: %d strains (%s)", name, count, source.stats)
                http_stats[name] = dict(client.stats)
                if client.stats.get("challenges"):
                    log.warning(
                        "[%s] hit %d bot challenges; consider a slower --delay",
                        name,
                        client.stats["challenges"],
                    )
    finally:
        if dump_fh:
            dump_fh.close()

    if not collected:
        log.error(
            "no strains collected. Run `strain-db probe <source> <url>` to check "
            "whether the site is reachable and the spec still matches its markup."
        )
        return 1

    if args.no_merge:
        # Keep sources apart by suffixing the key, for spec debugging.
        for s in collected:
            src = s.sources[0].source if s.sources else "unknown"
            s.canonical_key = f"{s.canonical_key}@{src}"
        written = db.upsert_many(collected)
    else:
        merged = group_and_merge(collected, fuzzy=not args.no_fuzzy)
        log.info("merged %d records into %d strains", len(collected), len(merged))
        written = db.upsert_many(merged)

    log.info("wrote %d strains to %s", written, args.db)
    for name, stats in http_stats.items():
        log.info("http[%s]: %s", name, stats)
    return 0


def cmd_discover(args) -> int:
    with client_for(args, args.source) as client:
        source = SOURCES[args.source](client)
        for url in source.discover(limit=args.limit):
            print(url)
    return 0


def cmd_probe(args) -> int:
    with client_for(args, args.source) as client:
        report = probe_mod.probe(args.source, args.url, client, save_html=args.save_html)
    print(probe_mod.format_report(report))
    if report.get("looks_like_challenge"):
        print(
            "\nNOTE: this response looks like a bot-check page, not real content. "
            "The spec cannot be calibrated against it.",
            file=sys.stderr,
        )
    return 0 if report.get("verdict") == "OK" else 2


def cmd_reparse(args) -> int:
    """Rebuild from cached HTML - no network, so parser fixes are cheap to test."""
    # Reparse reads only from the disk cache, so it never needs a browser.
    args.browser = "never"
    client = make_client(args)
    collected: list[Strain] = []

    for name in args.sources:
        source_cls = SOURCES.get(name)
        if source_cls is None:
            continue
        source = source_cls(client)
        host = source.base_url.split("//")[-1]
        count = 0
        for cached in client.cache.iter_urls(host_filter=host):
            raw = source.parse_detail(cached.url, cached.text)
            if raw is None:
                continue
            try:
                collected.append(source.to_strain(raw))
                count += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] %s: %s", name, cached.url, exc)
        log.info("[%s] reparsed %d cached pages", name, count)

    if not collected:
        log.error("nothing in the cache to reparse (cache dir: %s)", args.cache_dir)
        return 1

    merged = group_and_merge(collected, fuzzy=not args.no_fuzzy)
    db = Database(args.db)
    written = db.upsert_many(merged)
    log.info("wrote %d strains to %s", written, args.db)
    return 0


def cmd_export(args) -> int:
    db = Database(args.db)
    n = export(db, args.path, args.format)
    log.info("exported %d strains to %s", n, args.path)
    return 0


def cmd_search(args) -> int:
    db = Database(args.db)
    results = db.search(args.query, args.limit)
    if not results:
        print("no matches")
        return 1
    for s in results:
        bits = [s.type.value]
        if s.thc.avg or s.thc.max:
            bits.append(f"THC {s.thc.avg or s.thc.max}%")
        if s.breeder:
            bits.append(s.breeder)
        effects = ", ".join(t.name for t in s.effects[:4])
        print(f"{s.name}  [{' | '.join(bits)}]")
        if effects:
            print(f"    effects: {effects}")
        if s.parents:
            print(f"    genetics: {' x '.join(s.parents)}")
        print(f"    sources: {', '.join(sorted({r.source for r in s.sources}))}")
    return 0


def cmd_stats(args) -> int:
    db = Database(args.db)
    print(json.dumps(db.stats(), indent=2))
    return 0


COMMANDS = {
    "crawl": cmd_crawl,
    "discover": cmd_discover,
    "probe": cmd_probe,
    "reparse": cmd_reparse,
    "export": cmd_export,
    "search": cmd_search,
    "stats": cmd_stats,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.WARNING - min(args.verbose, 2) * 10
    logging.basicConfig(
        level=level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    if args.ignore_robots:
        log.warning(
            "robots.txt checks are disabled; make sure you have permission to crawl these sites"
        )
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
