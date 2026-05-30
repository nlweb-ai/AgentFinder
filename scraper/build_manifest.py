"""Wrap flat catalog-entry files into a spec §4 capability manifest.

The scraper and curation steps emit plain arrays of catalog entries that are
already spec-shaped (domain-anchored `urn:ai:` identifiers, displayName, url,
description, ...) but carry the artifact type under `mediaType`. The Agent
Finder spec (§4.1) expects a `ai-catalog.json` manifest:

    {"specVersion": "1.0", "host": {...}, "entries": [ ...entry objects... ]}

where each entry's artifact type lives in the `type` field (§4.2), and exactly
one of `url` / `data` is present. This script reads the entry files, renames
`mediaType` -> `type`, carries through the spec's optional fields, and writes a
single manifest.

Usage:
    # Wrap the curated catalog into one manifest the backend can serve.
    python -m scraper.build_manifest --in data/catalog_curated \\
        --out data/catalog_manifest/ai-catalog.json \\
        --host-name "Agent Finder Catalog"

    SEARCH_PROVIDER=memory AGENT_FINDER_CATALOG_DIR=data/catalog_manifest \\
        python code/agent_finder.py
"""
import sys
import json
import argparse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# Reuse the spec §4.2 entry mapper the query path already uses, so manifest
# entries and /who results are produced by the exact same code.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))
from who_handler import _to_catalog_entry  # noqa: E402

# Non-entry files that may live in a catalog dir — never treat these as entries.
SKIP_FILES = {"mcp_scores.json", "ai-catalog.json"}


def load_entries(in_dir: str) -> Iterable[Dict[str, Any]]:
    """Yield raw catalog entries from every *.json file in a directory.

    Accepts both a bare array of entries and a `{... "entries": [...]}` wrapper.
    """
    for fp in sorted(Path(in_dir).glob("*.json")):
        if fp.name in SKIP_FILES:
            continue
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  skipping {fp.name}: {e}")
            continue
        items = data if isinstance(data, list) else data.get("entries", [])
        for e in items:
            if isinstance(e, dict):
                yield e


def to_spec_entry(e: Dict[str, Any]) -> Dict[str, Any]:
    """Convert one flat scraped entry into a §4.2 catalog-entry object.

    Delegates to who_handler._to_catalog_entry (the same mapper the query path
    uses). That mapper reads the artifact type from the entry's `type` field,
    so we copy our scraped `mediaType` across first.
    """
    json_ld = dict(e)
    if "type" not in json_ld and e.get("mediaType"):
        json_ld["type"] = e["mediaType"]
    doc = {
        "json_ld": json.dumps(json_ld),
        "url": e.get("url", ""),
        "name": e.get("displayName") or e.get("name", ""),
        "description": e.get("description", ""),
    }
    # ranking=None -> no score field; source="" -> no source field.
    return _to_catalog_entry(doc, None, "")


def build_manifest(entries: List[Dict[str, Any]], host_name: str, host_identifier: Optional[str]) -> Dict[str, Any]:
    host: Dict[str, Any] = {"displayName": host_name}
    if host_identifier:
        host["identifier"] = host_identifier

    spec_entries: List[Dict[str, Any]] = []
    seen = set()
    skipped = 0
    for e in entries:
        ident = e.get("identifier")
        if not ident or ident in seen:
            continue
        # Spec requires an identifier and one of url/data.
        if not (e.get("url") or e.get("data")):
            skipped += 1
            continue
        seen.add(ident)
        spec_entries.append(to_spec_entry(e))
    if skipped:
        print(f"  skipped {skipped} entries lacking both url and data")

    return {"specVersion": "1.0", "host": host, "entries": spec_entries}


def main(argv=None):
    p = argparse.ArgumentParser(description="Wrap catalog entry files into a spec §4 ai-catalog.json manifest.")
    p.add_argument("--in", dest="in_dir", default="data/catalog_curated", help="Dir of catalog entry files to wrap.")
    p.add_argument("--out", default="data/catalog_manifest/ai-catalog.json", help="Manifest file to write.")
    p.add_argument("--host-name", default="Agent Finder Catalog", help="host.displayName for the manifest.")
    p.add_argument("--host-identifier", default=None, help="Optional host.identifier (DID or domain).")
    args = p.parse_args(argv)

    entries = list(load_entries(args.in_dir))
    manifest = build_manifest(entries, args.host_name, args.host_identifier)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(
        f"Wrote {out} with {len(manifest['entries'])} entries "
        f"(from {args.in_dir}/).\n"
        f"Use it:  SEARCH_PROVIDER=memory AGENT_FINDER_CATALOG_DIR={out.parent} python code/agent_finder.py"
    )


if __name__ == "__main__":
    main()
