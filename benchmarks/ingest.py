"""Ingest a benchmark's tool/API universe into a Finder-servable catalog dir.

This realizes the "ingest benchmark tools" catalog model: so that the augments
Agent Finder can discover are exactly the ones a benchmark task needs, we load
the benchmark's own tools into a catalog directory and point Agent Finder at it.
The harness then exercises Finder over /search as usual.

Input is a simple JSON list of tools:
    [{"name": "...", "description": "...", "url"?: "...", "type"?: "...",
      "authority"?: "...", "tags"?: [...]}, ...]

Output is a flat catalog file (data/<name>.json) in the schema the in-memory
backend reads — built with scraper.base.make_entry so entries are byte-identical
to scraped ones. Then:

    SEARCH_PROVIDER=memory AGENT_FINDER_CATALOG_DIR=<out_dir> \\
        WHO_SERVER_PORT=8090 python code/agent_finder.py

Usage:
    python -m benchmarks.ingest --in tools.json --out data/bench_toolretrieval \\
        --authority toolbench --media application/mcp-server+json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# Reuse the scraper's entry builder so ingested tools match scraped catalog shape.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))
from base import make_entry, dedupe, write_catalog  # noqa: E402

DEFAULT_MEDIA = "application/mcp-server+json"


def tools_to_entries(tools: List[Dict[str, Any]], authority: str,
                     media: str) -> List[Dict[str, Any]]:
    entries = []
    for t in tools:
        name = t.get("name") or t.get("displayName") or t.get("identifier")
        if not name:
            continue
        entries.append(make_entry(
            authority=t.get("authority", authority),
            name_parts=[name],
            display_name=name,
            media_type=t.get("type") or t.get("mediaType") or media,
            # The spec needs url OR data; tools without a URL get a data: stub so
            # they survive manifest building and remain selectable by description.
            url=t.get("url") or f"urn:tool:{authority}:{name}",
            description=t.get("description", ""),
            tags=t.get("tags"),
            metadata={"source": f"benchmark:{authority}", **(t.get("metadata") or {})},
        ))
    return dedupe(entries)


def ingest(in_path: str, out_dir: str, authority: str, media: str,
           filename: str = None) -> Path:
    tools = json.loads(Path(in_path).read_text(encoding="utf-8"))
    if isinstance(tools, dict):
        tools = tools.get("tools") or tools.get("entries") or []
    entries = tools_to_entries(tools, authority, media)
    return write_catalog(out_dir, filename or f"{authority}_tools.json", entries)


def main(argv=None):
    p = argparse.ArgumentParser(description="Ingest benchmark tools into a Finder catalog dir.")
    p.add_argument("--in", dest="in_path", required=True, help="JSON list of tools.")
    p.add_argument("--out", required=True, help="Catalog dir to write (point Finder here).")
    p.add_argument("--authority", default="benchmark", help="URN authority for identifiers.")
    p.add_argument("--media", default=DEFAULT_MEDIA, help="Default mediaType for tools.")
    args = p.parse_args(argv)
    path = ingest(args.in_path, args.out, args.authority, args.media)
    n = len(json.loads(path.read_text(encoding="utf-8")))
    print(f"Wrote {path} with {n} tool entries.\n"
          f"Serve it:  SEARCH_PROVIDER=memory AGENT_FINDER_CATALOG_DIR={args.out} "
          f"WHO_SERVER_PORT=8090 python code/agent_finder.py")


if __name__ == "__main__":
    main()
