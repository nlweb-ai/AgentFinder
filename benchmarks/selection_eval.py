"""Selection-only eval: did Agent Finder surface the RIGHT tool — regardless of
whether the downstream task could be carried out?

Most benchmark tasks ("address my GitHub PR comments", "set up an A/B test")
ultimately need a live account on some third-party app. The person running the
benchmark usually has no such logins, and that's fine: the thing under test is
**tool selection**, not task completion. This eval measures exactly that and
nothing else.

For each {id, prompt, gold} task it sends the prompt to a running Agent Finder
and checks whether the gold augment appears in the top-k results — pure
retrieval. No coding agent, no LLM keys, no app credentials, no execution. It
runs instantly on a small sample, so a forker can sanity-check their Finder in
seconds.

Two stronger selection signals live elsewhere and also avoid app logins:
  * benchmarks.coverage_experiment runs an agent over a STUBBED executor, so
    "solved" means the agent *chose and called* the gold tool (still no real app).
  * benchmarks.runner --benchmark toolretrieval grades the agent NAMING the gold
    tool (instruct it to identify, not execute).
This module is the cheapest rung: retrieval only.

Usage:
    # against an already-running Agent Finder (your fork's deployment):
    SEARCH_PROVIDER=memory WHO_SERVER_PORT=8090 python code/agent_finder.py &
    python -m benchmarks.selection_eval \
        --tasks benchmarks/data/crossecosystem/tasks.jsonl \
        --finder-url http://127.0.0.1:8090 --depth 30

    # or let it spawn a Finder over a local catalog dir, on just 5 tasks:
    python -m benchmarks.selection_eval \
        --tasks benchmarks/data/crossecosystem/tasks.jsonl \
        --catalog-dir catalog_sample --limit 5
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .finder_tool import FinderClient
from .coverage_experiment import _start_finder


def _load_tasks(path: str, limit: int) -> List[Dict[str, Any]]:
    tasks = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            tasks.append(json.loads(line))
    return tasks[:limit] if limit else tasks


def _is_gold(entry: Dict[str, Any], gold: str) -> bool:
    """Match a catalog entry against a task's gold slug. The gold is the entry's
    displayName (and the trailing identifier segment), e.g. 'address-github-comments'."""
    g = gold.strip().lower()
    name = (entry.get("displayName") or entry.get("name") or "").strip().lower()
    ident = (entry.get("identifier") or "").lower()
    return g == name or ident.rsplit(":", 1)[-1] == g or g in ident


def rank_of_gold(client: FinderClient, prompt: str, gold: str, depth: int) -> Optional[int]:
    for i, e in enumerate(client.search(prompt, page_size=depth)):
        if _is_gold(e, gold):
            return i + 1
    return None


def score(client: FinderClient, tasks: List[Dict[str, Any]], depth: int) -> Dict[str, Any]:
    ks = sorted({1, 5, 10, depth})
    hits = {k: 0 for k in ks}
    mrr = 0.0
    rows = []
    for t in tasks:
        r = rank_of_gold(client, t["prompt"], t["gold"], depth)
        rows.append({"id": t["id"], "gold": t["gold"], "rank": r})
        if r:
            mrr += 1.0 / r
            for k in ks:
                if r <= k:
                    hits[k] += 1
    n = len(tasks)
    summary = {f"recall@{k}": round(hits[k] / n, 3) for k in ks} if n else {}
    summary["mrr"] = round(mrr / n, 3) if n else 0.0
    summary["n"] = n
    return {"summary": summary, "rows": rows}


def main(argv=None):
    p = argparse.ArgumentParser(description="Selection-only retrieval eval (right tool in top-k).")
    p.add_argument("--tasks", required=True, help="JSONL of {id, prompt, gold}.")
    p.add_argument("--finder-url", help="Base URL of a running Agent Finder.")
    p.add_argument("--catalog-dir", help="Spawn a Finder over this catalog dir instead.")
    p.add_argument("--depth", type=int, default=30, help="Top-k depth to scan (production WHO_SEARCH_TOP_K).")
    p.add_argument("--limit", type=int, default=0, help="Only score the first N tasks (small sample).")
    p.add_argument("--port", type=int, default=8099, help="Port when spawning (--catalog-dir).")
    p.add_argument("--out", help="Write full results JSON here.")
    args = p.parse_args(argv)

    if not args.finder_url and not args.catalog_dir:
        p.error("need --finder-url (a running Finder) or --catalog-dir (one to spawn)")

    tasks = _load_tasks(args.tasks, args.limit)
    print(f"Scoring {len(tasks)} tasks on tool selection (depth={args.depth})...")

    proc = None
    try:
        if args.catalog_dir:
            proc = _start_finder(args.catalog_dir, args.port)
            client = FinderClient(f"http://127.0.0.1:{args.port}", timeout=60)
        else:
            client = FinderClient(args.finder_url, timeout=60)
            if not client.health():
                raise SystemExit(f"Agent Finder at {args.finder_url} is not healthy")
        result = score(client, tasks, args.depth)
    finally:
        if proc is not None:
            proc.terminate(); proc.wait()

    print(f"\n{'task':>6} | {'rank':>5} | gold")
    print("-" * 48)
    for row in result["rows"]:
        r = row["rank"]
        print(f"{row['id']:>6} | {(str(r) if r else 'miss'):>5} | {row['gold']}")
    print("\n=== SELECTION SUMMARY ===")
    print(json.dumps(result["summary"], indent=2))

    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
