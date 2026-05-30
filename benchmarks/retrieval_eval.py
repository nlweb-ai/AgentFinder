"""Held-out paraphrase retrieval eval: does the factual-blurb + example-queries
enrichment actually retrieve better than terse/marketing descriptions?

The cross-ecosystem task set is saturated — its golds have distinctive names, so
they rank #1 regardless of description, and the win is invisible. This eval
removes that crutch:

  * Sample golds stratified across types (mcp-server / ai-skill / a2a / agent).
  * For each gold, generate ONE realistic user request from its ORIGINAL scraped
    description, with the tool's name and distinctive proper nouns STRIPPED, so
    retrieval can't win on a lexical name match. The query is HELD OUT — it is
    not the entry's stored example_queries and is never added to any index.
  * Serve each arm (a catalog dir) and rank the gold for every query.

Because every arm is the SAME entry set differing only in the embedded text
(terse original vs marketing rewrite vs factual+queries), the delta in
recall@k / MRR is attributable to the description treatment alone.

Usage:
    source ./set_keys.sh
    python -m benchmarks.retrieval_eval \
        --sample-from data/catalog_enriched2 \
        --arm terse=data/catalog_dedup \
        --arm marketing=data/catalog_enriched \
        --arm factual_queries=data/catalog_enriched2 \
        --n 60 --model gpt-5.1 --out results/retrieval_eval.json

Generated queries are cached in <out-dir>/eval_queries.json so reruns (e.g. to
add an arm) don't re-call the model.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .finder_tool import FinderClient
from .coverage_experiment import _start_finder
from scraper.enrich import load_catalog

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))
from llm_backend import get_llm_backend  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

GEN_SYSTEM = (
    "You write ONE realistic, first-person user request that a person would type "
    "to get the described tool to do its job. HARD RULES: do NOT name the tool, "
    "its vendor, or any distinctive proper noun from its description; phrase it as "
    "a natural task ('I need to ...', 'Help me ...'), not a keyword query; keep it "
    "to one sentence. Output only the request text."
)


def _name(e: Dict[str, Any]) -> str:
    return e.get("displayName") or e.get("name") or ""


def _type(e: Dict[str, Any]) -> str:
    return (e.get("type") or e.get("mediaType") or "?").split("/")[-1]


def _orig_desc(e: Dict[str, Any]) -> str:
    """The neutral source for query generation: the original scraped text, so the
    query isn't derived from whichever treatment we're scoring."""
    meta = e.get("metadata") or {}
    return (meta.get("original_description") or e.get("description") or "").strip()


def sample_golds(entries, shared: set, n: int, seed: int) -> List[Dict[str, Any]]:
    """Stratified sample across types, restricted to entries present in every arm
    and having enough source text to generate a query from."""
    rng = random.Random(seed)
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for e in entries:
        if _name(e) in shared and len(_orig_desc(e)) >= 40:
            by_type.setdefault(_type(e), []).append(e)
    types = sorted(by_type)
    for t in types:
        rng.shuffle(by_type[t])
    picked, i = [], 0
    # round-robin across types so small types (a2a, agent) are represented
    while len(picked) < n and any(by_type.values()):
        t = types[i % len(types)]
        if by_type[t]:
            picked.append(by_type[t].pop())
        i += 1
    return picked


async def gen_queries(golds, backend, model, cache, concurrency, save_cb):
    sem = asyncio.Semaphore(concurrency)
    todo = [g for g in golds if (g.get("identifier") or g.get("url")) not in cache]
    print(f"Generating {len(todo)} held-out queries ({len(golds)-len(todo)} cached)...")
    lock = asyncio.Lock()
    done = 0

    async def one(g):
        nonlocal done
        ident = g.get("identifier") or g.get("url")
        prompt = f"Tool name (DO NOT mention): {_name(g)}\nWhat it does: {_orig_desc(g)}"
        async with sem:
            try:
                text, _ = await backend.generate(
                    [{"role": "system", "content": GEN_SYSTEM},
                     {"role": "user", "content": prompt}],
                    model=model, max_tokens=300)
                text = (text or "").strip().strip('"')
            except Exception as ex:
                text = f"__ERROR__ {ex}"
        async with lock:
            cache[ident] = text
            done += 1
            if done % 25 == 0:
                print(f"  generated {done}/{len(todo)}"); save_cb()

    if todo:
        await asyncio.gather(*(one(g) for g in todo), return_exceptions=True)
        save_cb()


def mean_embed_len(catalog_dir: str) -> int:
    """Mean length of the text each entry actually embeds (search_text if the
    enrichment set one, else name+description) — the per-entry context cost an
    agent pays when this entry is retrieved into its prompt."""
    _, entries = load_catalog(catalog_dir)
    tot = 0
    for e in entries:
        name = e.get("displayName") or e.get("name") or ""
        txt = e.get("search_text") or f"{name}\n{e.get('description','')}"
        tot += len(txt.strip())
    return round(tot / max(1, len(entries)))


def rank_of(client: FinderClient, query: str, gold_name: str, depth: int) -> Optional[int]:
    for i, it in enumerate(client.search(query, page_size=depth)):
        if (it.get("displayName") or it.get("name") or it.get("identifier")) == gold_name:
            return i + 1
    return None


def score_arm(client, evalset, depth) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    recs, mrr = [], 0.0
    hits = {1: 0, 5: 0, 10: 0, 30: 0}
    for q in evalset:
        r = rank_of(client, q["query"], q["gold"], depth)
        recs.append({"gold": q["gold"], "type": q["type"], "rank": r})
        if r:
            mrr += 1.0 / r
            for k in hits:
                if r <= k:
                    hits[k] += 1
    n = len(evalset)
    summ = {
        "n": n,
        "recall@1": round(hits[1] / n, 3),
        "recall@5": round(hits[5] / n, 3),
        "recall@10": round(hits[10] / n, 3),
        "recall@30": round(hits[30] / n, 3),  # production candidate depth (WHO_SEARCH_TOP_K)
        "mrr": round(mrr / n, 3),
    }
    return summ, recs


async def run(args):
    out_dir = Path(args.out).parent if args.out else ROOT / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    qcache_path = out_dir / "eval_queries.json"

    arms: List[Tuple[str, Optional[str]]] = []
    for spec in args.arm:
        name, _, d = spec.partition("=")
        arms.append((name, d))
    # External-finder mode: a single arm scored against an already-running Agent
    # Finder (a fork's own deployment). d=None signals "don't spawn / no catalog".
    if args.finder_url:
        arms.append(("finder", None))
    if not arms:
        raise SystemExit("need at least one --arm name=catalog_dir (or --finder-url)")

    # entries to sample golds from + the name set shared by every catalog arm
    _, entries = load_catalog(args.sample_from)
    shared = None
    for _, d in arms:
        if d is None:  # external finder: not a local catalog, don't restrict
            continue
        names = {_name(e) for e in load_catalog(d)[1]}
        shared = names if shared is None else (shared & names)
    if shared is None:  # only the external arm => sample from the full set
        shared = {_name(e) for e in entries}
    golds = sample_golds(entries, shared, args.n, args.seed)
    print(f"Sampled {len(golds)} golds across types "
          f"{sorted({_type(g) for g in golds})}; arms={[a for a,_ in arms]}")

    qcache: Dict[str, str] = {}
    if qcache_path.exists():
        qcache = json.loads(qcache_path.read_text(encoding="utf-8"))

    def save():
        qcache_path.write_text(json.dumps(qcache, ensure_ascii=False, indent=2), encoding="utf-8")

    todo = [g for g in golds if (g.get("identifier") or g.get("url")) not in qcache]
    if todo:
        backend = get_llm_backend()
        await backend.initialize()
        try:
            await gen_queries(golds, backend, args.model, qcache, args.concurrency, save)
        finally:
            await backend.close()
    save()

    evalset = []
    for g in golds:
        q = qcache.get(g.get("identifier") or g.get("url"), "")
        if q and not q.startswith("__ERROR__"):
            evalset.append({"gold": _name(g), "type": _type(g), "query": q})
    print(f"{len(evalset)} usable queries.\n")

    results = {}
    for name, d in arms:
        if d is None:  # external finder: score the already-running server
            elen = 0
            client = FinderClient(args.finder_url, timeout=60)
            if not client.health():
                raise SystemExit(f"Agent Finder at {args.finder_url} is not healthy")
            summ, recs = score_arm(client, evalset, args.depth)
        else:
            elen = mean_embed_len(d)
            proc = _start_finder(d, args.port)
            try:
                client = FinderClient(f"http://127.0.0.1:{args.port}", timeout=60)
                summ, recs = score_arm(client, evalset, args.depth)
            finally:
                proc.terminate(); proc.wait()
        # embed_chars is informational only: search_text is precomputed
        # server-side, so its length is free at query time (NOT agent context).
        summ["embed_chars"] = elen
        results[name] = {"summary": summ, "records": recs}
        print(f"  {name:>16}: R@5={summ['recall@5']:.3f}  R@10={summ['recall@10']:.3f}  "
              f"R@30={summ['recall@30']:.3f}  MRR={summ['mrr']:.3f}  (embed_chars={elen})")

    print(f"\n{'arm':>16} | {'R@1':>6} {'R@5':>6} {'R@10':>6} {'R@30':>6} {'MRR':>6} | {'chars':>6}")
    print("-" * 68)
    for name, _ in arms:
        s = results[name]["summary"]
        print(f"{name:>16} | {s['recall@1']:>6} {s['recall@5']:>6} {s['recall@10']:>6} "
              f"{s['recall@30']:>6} {s['mrr']:>6} | {s['embed_chars']:>6}")

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"evalset": evalset, "results": results}, indent=2), encoding="utf-8")
        print(f"\nWrote {args.out}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Held-out paraphrase retrieval eval.")
    p.add_argument("--sample-from", default="data/catalog_enriched2",
                   help="Catalog dir to sample golds from.")
    p.add_argument("--arm", action="append", default=[],
                   help="name=catalog_dir (repeatable). Each arm is served and scored.")
    p.add_argument("--finder-url", help="Also score an already-running Agent Finder at this URL "
                                        "(e.g. http://127.0.0.1:8090) as a 'finder' arm.")
    p.add_argument("--n", type=int, default=60, help="Number of golds to sample.")
    p.add_argument("--depth", type=int, default=10, help="Top-k depth to scan for the gold.")
    p.add_argument("--model", default=None, help="Query-generation model.")
    p.add_argument("--concurrency", type=int, default=20, help="Max concurrent gen calls.")
    p.add_argument("--port", type=int, default=8105, help="Port for the served arm.")
    p.add_argument("--seed", type=int, default=0, help="Sampling seed.")
    p.add_argument("--out", help="Write full results JSON here.")
    args = p.parse_args(argv)

    for var in ("LLM_ENDPOINT", "LLM_API_KEY"):
        if not os.getenv(var):
            print(f"Warning: {var} not set — backend will likely fail.", file=sys.stderr)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
