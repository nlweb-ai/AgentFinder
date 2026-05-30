"""LLM enrichment pass: rewrite each catalog entry's description into a richer,
example-laden one so retrieval recall improves.

The benchmark showed the failure mode: at ~5k entries the right tool isn't always
in the retrieved top-k, because terse scraped one-liners don't match how a user
phrases a task. This pass sends every entry (MCP server, skill, A2A agent, or
remote agent) to a high-quality LLM and asks it to expand the description with:
what it does, concrete example user requests that should route to it, key
capabilities, and synonyms/keywords. The original description is preserved in
`metadata.original_description`; the entry's `description` becomes the enriched
text, so a Finder served on the output dir embeds and retrieves over it.

Reuses code/llm_backend.py (client pool + concurrency) exactly like eval_mcp.py.
Results are cached by identifier in <out>/enrich_cache.json, so a re-run (or a
run after a crash) only enriches entries it hasn't done yet.

Usage:
    source ./set_keys.sh
    # dry sample on 10 entries first:
    python -m scraper.enrich --in data/catalog_manifest --out data/catalog_enriched --limit 10
    # full run:
    python -m scraper.enrich --in data/catalog_manifest --out data/catalog_enriched --concurrency 30
    # then serve the enriched catalog and re-run the benchmark against it:
    SEARCH_PROVIDER=memory AGENT_FINDER_CATALOG_DIR=data/catalog_enriched \
        WHO_SERVER_PORT=8096 python code/agent_finder.py
"""
import os
import sys
import json
import asyncio
import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Reuse the project's swappable LLM backend (client pool, retries).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))
from llm_backend import get_llm_backend  # noqa: E402

SKIP_FILES = {"enrich_cache.json"}

SYSTEM = (
    "You prepare catalog entries for a retrieval system over AI tools (MCP "
    "servers, skills, and agents). Given a tool's name, type, and current "
    "description, output STRICT JSON (no markdown, no prose outside the JSON) "
    "with exactly two keys:\n"
    '  "factual": a plain, factual description of what the tool actually does '
    "and its concrete capabilities. STRIP ALL MARKETING language, superlatives, "
    "and vendor hype (\"powerful\", \"seamless\", \"best-in-class\", \"revolutionary\"). "
    "Just the capabilities, in 30-70 words. Do not invent capabilities the source "
    "doesn't support; if the source is thin, stay terse.\n"
    '  "queries": an array of 5-8 short, natural example user requests (first '
    "person, the way a user would actually phrase a task) that SHOULD route to "
    "this tool. Vary the phrasing and vocabulary; include synonyms a user might "
    "say. Each query is one sentence.\n"
    'Example: {"factual": "...", "queries": ["...", "..."]}'
)


def load_catalog(in_dir: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Return (manifest_header, entries) merged across *.json in the dir,
    deduped by identifier. `manifest_header` keeps specVersion/host if present."""
    header: Dict[str, Any] = {}
    entries: Dict[str, Dict[str, Any]] = {}
    for fp in sorted(Path(in_dir).glob("*.json")):
        if fp.name in SKIP_FILES:
            continue
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  skipping {fp.name}: {e}")
            continue
        if isinstance(data, dict):
            for k, v in data.items():
                if k != "entries":
                    header.setdefault(k, v)
            items = data.get("entries", [])
        else:
            items = data
        for e in items:
            ident = e.get("identifier") or e.get("url")
            if ident:
                entries.setdefault(ident, e)
    return header, list(entries.values())


def entry_prompt(e: Dict[str, Any]) -> str:
    name = e.get("displayName") or e.get("name") or "(unnamed)"
    typ = (e.get("type") or e.get("mediaType") or "tool").split("/")[-1]
    desc = (e.get("description") or "").strip() or "(no description provided)"
    return f"Name: {name}\nType: {typ}\nCurrent description: {desc}"


async def enrich_all(entries, backend, cache, model, max_tokens, concurrency, save_cb):
    sem = asyncio.Semaphore(concurrency)
    todo = [e for e in entries if (e.get("identifier") or e.get("url")) not in cache]
    print(f"Enriching {len(todo)} entries ({len(entries) - len(todo)} already cached)...")
    done = 0
    lock = asyncio.Lock()

    async def one(e):
        nonlocal done
        ident = e.get("identifier") or e.get("url")
        async with sem:
            try:
                text, _usage = await backend.generate(
                    [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": entry_prompt(e)}],
                    model=model, max_tokens=max_tokens,
                )
                text = (text or "").strip()
            except Exception as ex:
                text = f"__ERROR__ {ex}"
        async with lock:
            cache[ident] = text
            done += 1
            if done % 50 == 0:
                print(f"  enriched {done}/{len(todo)}")
                save_cb()

    if todo:
        await asyncio.gather(*(one(e) for e in todo), return_exceptions=True)
        save_cb()


def parse_enrichment(raw: str) -> Optional[Tuple[str, List[str]]]:
    """Parse the model's JSON output into (factual, queries). Tolerates a JSON
    object embedded in extra text. Returns None if it can't be parsed."""
    if not raw or raw.startswith("__ERROR__"):
        return None
    txt = raw.strip()
    if not txt.startswith("{"):
        i, j = txt.find("{"), txt.rfind("}")
        if i == -1 or j <= i:
            return None
        txt = txt[i:j + 1]
    try:
        obj = json.loads(txt)
    except json.JSONDecodeError:
        return None
    factual = (obj.get("factual") or "").strip()
    queries = [q.strip() for q in (obj.get("queries") or []) if isinstance(q, str) and q.strip()]
    if not factual and not queries:
        return None
    return factual, queries


def apply_enrichment(entries, cache) -> Tuple[List[Dict[str, Any]], int]:
    """Return (new_entries, n_applied). Each enriched entry gets a marketing-free
    factual `description`, an embed-only `search_text` (original + factual +
    example queries), and keeps the original under metadata.original_description
    plus the queries under metadata.example_queries."""
    out, applied = [], 0
    for e in entries:
        ident = e.get("identifier") or e.get("url")
        parsed = parse_enrichment(cache.get(ident, ""))
        ne = dict(e)
        if parsed:
            factual, queries = parsed
            orig = e.get("description", "")
            meta = dict(ne.get("metadata") or {})
            meta.setdefault("original_description", orig)
            meta["example_queries"] = queries
            meta["enriched"] = True
            ne["metadata"] = meta
            if factual:
                ne["description"] = factual
            # Embedded text is a SUPERSET: original scraped text + factual blurb +
            # the example queries. The retrieval eval (results/retrieval_eval_decomp)
            # showed stripping the original's vocabulary HURTS recall@30 — dense
            # embeddings reward surface area, and search_text is precomputed
            # server-side so its length is free at query time. We keep the original
            # for its vocabulary, factual for cleanliness, and queries for the
            # doc2query (query-to-query) lift. The served `description` stays the
            # clean factual blurb; only `search_text` is embedded.
            name = ne.get("displayName") or ne.get("name") or ""
            parts = [name, orig, factual] + queries
            ne["search_text"] = "\n".join(p for p in parts if p).strip()
            applied += 1
        out.append(ne)
    return out, applied


async def run(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache_path = out / "enrich_cache.json"

    header, entries = load_catalog(args.in_dir)
    if args.limit:
        entries = entries[: args.limit]
    print(f"Loaded {len(entries)} entries from {args.in_dir}/")

    cache: Dict[str, str] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    def save():
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    todo = [e for e in entries if (e.get("identifier") or e.get("url")) not in cache]
    if todo:
        backend = get_llm_backend()
        await backend.initialize()
        try:
            await enrich_all(entries, backend, cache, args.model,
                             args.max_tokens, args.concurrency, save)
        finally:
            await backend.close()
    else:
        print(f"All {len(entries)} entries already enriched — rebuilding output from cache.")

    new_entries, applied = apply_enrichment(entries, cache)
    errors = sum(1 for v in cache.values() if v.startswith("__ERROR__"))
    manifest = {**header, "entries": new_entries}
    manifest.setdefault("specVersion", "1.0")
    (out / "ai-catalog.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    save()
    print(
        f"\nEnriched {applied}/{len(new_entries)} entries ({errors} errors).\n"
        f"Wrote {out}/ai-catalog.json.\n"
        f"Serve it:  SEARCH_PROVIDER=memory AGENT_FINDER_CATALOG_DIR={out} "
        f"WHO_SERVER_PORT=8096 python code/agent_finder.py"
    )


def main(argv=None):
    p = argparse.ArgumentParser(description="LLM-enrich catalog descriptions for better retrieval.")
    p.add_argument("--in", dest="in_dir", default="data/catalog_manifest", help="Catalog dir to read.")
    p.add_argument("--out", default="data/catalog_enriched", help="Dir for enriched catalog + cache.")
    p.add_argument("--model", default=None, help="Override model (default: LLM_MODEL / backend default).")
    p.add_argument("--max-tokens", type=int, default=800, help="Max tokens per enriched entry (JSON factual+queries).")
    p.add_argument("--concurrency", type=int, default=30, help="Max concurrent LLM calls.")
    p.add_argument("--limit", type=int, default=0, help="Only enrich the first N entries (dry sample).")
    args = p.parse_args(argv)

    for var in ("LLM_ENDPOINT", "LLM_API_KEY"):
        if not os.getenv(var):
            print(f"Warning: {var} not set — the LLM backend will likely fail to initialize.", file=sys.stderr)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
