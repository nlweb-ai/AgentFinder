"""CLI: scrape trustworthy sources into catalog JSON files.

Usage:
    python -m scraper.run                       # all sources -> data/catalog/
    python -m scraper.run --source mcp a2a      # only these
    python -m scraper.run --out data/catalog --limit 500
    GITHUB_TOKEN=... python -m scraper.run      # higher GitHub rate limit

Output files drop straight into the in-memory backend:
    SEARCH_PROVIDER=memory AGENT_FINDER_CATALOG_DIR=data/catalog
"""
import os
import sys
import asyncio
import argparse

import aiohttp

from . import base
from .sources import SOURCES


async def _run(source_keys, out_dir, limit, token):
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        total = 0
        for key in source_keys:
            filename, fetcher = SOURCES[key]
            print(f"[{key}] fetching (limit {limit})...")
            try:
                entries = base.dedupe(await fetcher(session, token, limit))
            except Exception as e:
                print(f"[{key}] failed: {e}")
                continue
            path = base.write_catalog(out_dir, filename, entries)
            print(f"[{key}] wrote {len(entries)} entries -> {path}")
            total += len(entries)
        print(f"Done. {total} entries across {len(source_keys)} source(s) in {out_dir}/")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scrape MCP/skills/A2A catalogs.")
    parser.add_argument(
        "--source", nargs="+", choices=list(SOURCES) + ["all"], default=["all"],
        help="Which sources to scrape (default: all).",
    )
    parser.add_argument("--out", default="data/catalog", help="Output directory.")
    parser.add_argument("--limit", type=int, default=1000, help="Max entries per source.")
    args = parser.parse_args(argv)

    keys = list(SOURCES) if "all" in args.source else args.source
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("Note: GITHUB_TOKEN not set — GitHub sources may hit rate limits.", file=sys.stderr)
    asyncio.run(_run(keys, args.out, args.limit, token))


if __name__ == "__main__":
    main()
