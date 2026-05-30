"""LLM curation pass over scraped MCP servers.

Loads every MCP-server entry from a catalog directory, asks the configured LLM
to score each one for quality/usefulness, then writes a curated top-N set.

Reuses code/llm_backend.py (its client pool + concurrency) via the dedicated
`score_quality` call, which judges each entry on its intrinsic quality and
usefulness (clear/specific description, real capabilities, legitimacy) rather
than matching it against a query.

Scores are cached by identifier in <out>/mcp_scores.json, so a re-run (or a run
after a crash) only scores servers it hasn't seen yet.

Usage:
    # score all MCP servers, keep the top 2000  (needs LLM_* env vars set)
    python -m scraper.eval_mcp

    python -m scraper.eval_mcp --in data/catalog --out data/catalog_curated \\
        --top 2000 --concurrency 50

    python -m scraper.eval_mcp --limit 100      # dry sample on 100 servers
"""
import os
import sys
import json
import asyncio
import argparse
from pathlib import Path
from typing import Any, Dict

# Reuse the project's swappable LLM backend (client pool, retries, scoring).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))
from llm_backend import get_llm_backend  # noqa: E402

MCP_MEDIA = "application/mcp-server+json"


def load_entries(in_dir: str):
    """Return (mcp_entries, other_entries), deduped by identifier across files."""
    mcp: Dict[str, Dict[str, Any]] = {}
    other: Dict[str, Dict[str, Any]] = {}
    for fp in sorted(Path(in_dir).glob("*.json")):
        if fp.name in ("mcp_scores.json", "mcp_top.json", "other.json"):
            continue
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  skipping {fp.name}: {e}")
            continue
        items = data if isinstance(data, list) else data.get("entries", [])
        for e in items:
            ident = e.get("identifier") or e.get("url")
            if not ident:
                continue
            bucket = mcp if e.get("mediaType") == MCP_MEDIA else other
            bucket.setdefault(ident, e)
    return list(mcp.values()), list(other.values())


def entry_text(e: Dict[str, Any]) -> str:
    name = e.get("displayName") or e.get("name") or ""
    return f"{name}\n{e.get('description', '')}".strip()


# Source authorities ranked by how much we trust them as a usage proxy when
# no harder signal (stars/package) is present. Higher = more authoritative.
_SOURCE_RANK = {
    "registry.modelcontextprotocol.io": 2,  # official MCP registry
    "pulsemcp.com": 1,                       # hand-reviewed
}


def usage_key(e: Dict[str, Any]):
    """Real-world-usage sort key (descending). Combines the sparse signals
    available in entry metadata into a comparable tuple:
      1. GitHub stars (PulseMCP entries) — the strongest popularity signal.
      2. "Official" provenance — official MCP registry, or Glama's
         `author:official` attribute.
      3. Published as an installable package (has package_name).
      4. Source authority as a final tie-breaker.
    Most entries lack stars, so the boolean/authority signals carry the tail.
    """
    meta = e.get("metadata") or {}
    stars = meta.get("github_stars") or 0
    attrs = meta.get("attributes") or []
    official = (
        meta.get("source") == "registry.modelcontextprotocol.io"
        or (isinstance(attrs, list) and "author:official" in attrs)
    )
    has_package = bool(meta.get("package_name"))
    return (
        int(stars),
        int(official),
        int(has_package),
        _SOURCE_RANK.get(meta.get("source"), 0),
    )


async def score_all(entries, backend, scores, concurrency, save_cb):
    sem = asyncio.Semaphore(concurrency)
    todo = [e for e in entries if e["identifier"] not in scores]
    print(f"Scoring {len(todo)} servers ({len(entries) - len(todo)} already cached)...")
    done = 0
    lock = asyncio.Lock()

    async def one(e):
        nonlocal done
        async with sem:
            result = await backend.score_quality(entry_text(e))
        async with lock:
            scores[e["identifier"]] = {
                "score": result.get("score", 0),
                "reason": result.get("description", ""),
            }
            done += 1
            if done % 100 == 0:
                print(f"  scored {done}/{len(todo)}")
                save_cb()

    if todo:
        await asyncio.gather(*(one(e) for e in todo), return_exceptions=True)
        save_cb()


async def run(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    scores_path = out / "mcp_scores.json"

    mcp, other = load_entries(args.in_dir)
    if args.limit:
        mcp = mcp[: args.limit]
    print(f"Loaded {len(mcp)} MCP servers, {len(other)} non-MCP entries from {args.in_dir}/")

    scores: Dict[str, Dict[str, Any]] = {}
    if scores_path.exists():
        scores = json.loads(scores_path.read_text(encoding="utf-8"))

    def save():
        scores_path.write_text(json.dumps(scores, indent=2), encoding="utf-8")

    # Only spin up the LLM backend if there's something new to score. A pure
    # re-select (e.g. tweaking --min-score) runs entirely off the cache and
    # needs no API keys.
    todo = [e for e in mcp if e["identifier"] not in scores]
    if todo:
        backend = get_llm_backend()
        await backend.initialize()
        try:
            await score_all(mcp, backend, scores, args.concurrency, save)
        finally:
            await backend.close()
    else:
        print(f"All {len(mcp)} servers already scored — re-selecting from cache.")

    # Keep every server that clears the quality bar, then order the survivors
    # by real-world usage (stars / official provenance / packaging) so the most
    # battle-tested servers surface first.
    passing = [
        e for e in mcp
        if scores.get(e["identifier"], {}).get("score", 0) >= args.min_score
    ]
    top = sorted(passing, key=usage_key, reverse=True)

    (out / "mcp_top.json").write_text(json.dumps(top, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "other.json").write_text(json.dumps(other, indent=2, ensure_ascii=False), encoding="utf-8")
    save()

    kept = len(top)
    print(
        f"\nKept {kept} of {len(mcp)} MCP servers scoring >= {args.min_score}, "
        f"ranked by real-world usage.\n"
        f"Wrote {out}/mcp_top.json and {out}/other.json "
        f"({kept + len(other)} total entries).\n"
        f"Use it:  SEARCH_PROVIDER=memory AGENT_FINDER_CATALOG_DIR={out} python code/agent_finder.py"
    )


def main(argv=None):
    p = argparse.ArgumentParser(description="LLM-curate scraped MCP servers down to the top N.")
    p.add_argument("--in", dest="in_dir", default="data/catalog", help="Catalog dir to read.")
    p.add_argument("--out", default="data/catalog_curated", help="Dir for curated output + score cache.")
    p.add_argument("--min-score", type=int, default=75, help="Keep every MCP server scoring at or above this quality threshold.")
    p.add_argument("--concurrency", type=int, default=50, help="Max concurrent LLM calls.")
    p.add_argument("--limit", type=int, default=0, help="Only score the first N servers (for a dry sample).")
    args = p.parse_args(argv)

    for var in ("LLM_ENDPOINT", "LLM_API_KEY"):
        if not os.getenv(var):
            print(f"Warning: {var} not set — the LLM backend will likely fail to initialize.", file=sys.stderr)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
