"""Coverage + token-thrash experiment (see docs/BENCHMARK_DESIGN.md).

Tests the claim that a large, cross-ecosystem catalog with cheap selection beats
a small locally-curated toolset: MORE WINS, FEWER TOKENS, no regression on tasks
the local set already covers.

Three arms, one retriever interface, different indices:
  * local    : retrieve over nothing; the agent has only its bundle in context.
  * catalog  : retrieve over the big catalog only (Agent Finder).
  * join     : bundle in context PLUS catalog retrieval (what you'd ship).

The subject is the multi-turn `loop` agent over a STUBBED executor: the task's
gold tool returns a useful result (and marks the task solved); every other tool
returns nothing useful, so a bundle that lacks the gold tool makes the agent
thrash until it gives up — that wasted trajectory is the token cost we count.

Per arm, split by task partition (in-bundle = gold already in the local set vs
out-of-bundle = gold only reachable via the catalog), we report wins / losses /
trajectory tokens.

Two corpus modes:
  * ingest mode (--tool-descs + --sizes): build a catalog of the gold tools plus
    N-G synthetic distractors, ingest it, serve it. Sweep N to ToolLLM scale.
  * existing-catalog mode (--catalog-dir): serve a pre-built catalog as-is
    (e.g. data/catalog_manifest for the cross-ecosystem run). No size sweep.

Usage:
    source ./set_keys.sh
    # offline coverage + scale sweep on the bundled tool universe:
    python -m benchmarks.coverage_experiment \
        --tasks benchmarks/data/toolretrieval/tasks.jsonl \
        --bundle benchmarks/data/toolretrieval/default_tools.json \
        --tool-descs benchmarks/data/toolretrieval/tools.json \
        --sizes 15,150,1500

    # cross-ecosystem run against the real catalog:
    python -m benchmarks.coverage_experiment \
        --tasks benchmarks/data/crossecosystem/tasks.jsonl \
        --bundle benchmarks/data/crossecosystem/default_tools.json \
        --catalog-dir data/catalog_manifest
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .core import Task, Toolset, load_agent
from .finder_tool import FinderClient
from .distractors import make_distractors
from . import ingest as ingest_mod

ROOT = Path(__file__).resolve().parent.parent
ARMS = ("local", "catalog", "join")


def _load_jsonl(path: str) -> List[Dict[str, Any]]:
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _make_executor(gold: str):
    """Stubbed tool execution. Calling the gold tool solves the task; anything
    else returns an unhelpful observation (the thing that drives thrashing)."""
    g = gold.strip().lower()

    def execute(tool_name: str, args: Dict[str, Any]) -> Tuple[str, bool]:
        t = (tool_name or "").strip().lower()
        if t == g or (g and (g in t or t in g)):
            return (f"{tool_name} ran successfully and returned the result the "
                    f"task needs.", True)
        return (f"{tool_name} ran but returned nothing that answers the task.", False)

    return execute


def _start_finder(catalog_dir: str, port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(SEARCH_PROVIDER="memory", AGENT_FINDER_CATALOG_DIR=catalog_dir,
               WHO_SERVER_PORT=str(port))
    proc = subprocess.Popen(
        [sys.executable, "code/agent_finder.py"], cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = FinderClient(f"http://127.0.0.1:{port}")
    for _ in range(180):
        if client.health():
            return proc
        time.sleep(1)
    proc.terminate()
    raise RuntimeError(f"Finder did not become healthy on port {port}")


def _build_ingest_catalog(tasks, tool_descs, n: int, seed: int, out_dir: Path) -> None:
    """Catalog = the gold tools (with real descriptions) + distractors up to N."""
    golds = {}
    for t in tasks:
        g = t["gold"]
        if g not in golds:
            golds[g] = {"name": g, "description": tool_descs.get(g, g)}
    gold_list = list(golds.values())
    gold_names = set(golds)
    pad = make_distractors(max(0, n - len(gold_list)), seed=seed, exclude=gold_names)
    pool = gold_list + pad
    out_dir.mkdir(parents=True, exist_ok=True)
    src = out_dir / "tools_src.json"
    src.write_text(json.dumps(pool), encoding="utf-8")
    ingest_mod.ingest(str(src), str(out_dir), "scale", ingest_mod.DEFAULT_MEDIA)
    src.unlink()


def _run_arm(agent, tasks, bundle, bundle_names, finder, arm: str) -> List[Dict[str, Any]]:
    recs = []
    for t in tasks:
        execute = _make_executor(t["gold"])
        tools = Toolset(
            default_tools=bundle if arm in ("local", "join") else [],
            finder=finder if arm in ("catalog", "join") else None,
            execute=execute,
        )
        task = Task(id=t["id"], prompt=t["prompt"], metadata={"gold": t["gold"]})
        attempt = agent.solve(task, tools)
        art = attempt.artifacts or {}
        recs.append({
            "task_id": t["id"], "arm": arm,
            "partition": "in_bundle" if t["gold"] in bundle_names else "out_of_bundle",
            "solved": bool(art.get("solved")),
            "tokens": art.get("agent_tokens"),
            "turns": art.get("turns"),
            "error": attempt.error,
        })
    return recs


def _tally(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for arm in ARMS:
        for part in ("in_bundle", "out_of_bundle"):
            rs = [r for r in records if r["arm"] == arm and r["partition"] == part]
            if not rs:
                continue
            toks = [r["tokens"] for r in rs if r["tokens"] is not None]
            out.setdefault(arm, {})[part] = {
                "wins": sum(r["solved"] for r in rs),
                "losses": sum(not r["solved"] for r in rs),
                "tokens": sum(toks),
                "mean_tokens": round(sum(toks) / len(toks), 1) if toks else None,
            }
    return out


def _print_tally(n_label: str, tally: Dict[str, Any]) -> None:
    print(f"\n=== N={n_label} ===")
    hdr = f"{'arm':>8} | {'partition':>13} | {'wins':>4} {'losses':>6} | {'tokens':>8} {'mean':>7}"
    print(hdr)
    print("-" * len(hdr))
    for arm in ARMS:
        for part in ("out_of_bundle", "in_bundle"):
            c = tally.get(arm, {}).get(part)
            if not c:
                continue
            print(f"{arm:>8} | {part:>13} | {c['wins']:>4} {c['losses']:>6} | "
                  f"{c['tokens']:>8} {str(c['mean_tokens']):>7}")


def run(args) -> List[Dict[str, Any]]:
    tasks = _load_jsonl(args.tasks)
    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8")) if args.bundle else []
    bundle_names = {b.get("name") or b.get("displayName") for b in bundle}
    arms = [a for a in args.arms.split(",") if a.strip() in ARMS]
    agent = load_agent(args.agent, max_turns=args.max_turns)

    n_in = sum(1 for t in tasks if t["gold"] in bundle_names)
    print(f"{len(tasks)} tasks ({n_in} in-bundle, {len(tasks)-n_in} out-of-bundle), "
          f"bundle={len(bundle)} tools, arms={arms}")

    rows = []
    if args.finder_url:
        # External-finder mode: score against an already-running Agent Finder
        # (e.g. a fork's own deployment) instead of spawning one. This is the
        # path for "run the benchmark against your own Agent Finder".
        client = FinderClient(args.finder_url)
        if not client.health():
            raise SystemExit(f"Agent Finder at {args.finder_url} is not healthy")
        recs = []
        for arm in arms:
            recs += _run_arm(agent, tasks, bundle, bundle_names,
                             client if arm != "local" else None, arm)
        tally = _tally(recs)
        _print_tally(f"finder:{args.finder_url}", tally)
        rows.append({"N": args.finder_url, "tally": tally, "records": recs})
        return rows

    if args.catalog_dir:
        # Existing-catalog mode: serve as-is, single run (no size sweep).
        proc = _start_finder(args.catalog_dir, args.port)
        try:
            client = FinderClient(f"http://127.0.0.1:{args.port}")
            recs = []
            for arm in arms:
                recs += _run_arm(agent, tasks, bundle, bundle_names,
                                 client if arm != "local" else None, arm)
        finally:
            proc.terminate(); proc.wait()
        tally = _tally(recs)
        _print_tally(f"catalog_dir:{Path(args.catalog_dir).name}", tally)
        rows.append({"N": args.catalog_dir, "tally": tally, "records": recs})
        return rows

    # Ingest mode: sweep N with synthetic distractors padding the gold pool.
    tool_descs = {}
    if args.tool_descs:
        for t in json.loads(Path(args.tool_descs).read_text(encoding="utf-8")):
            tool_descs[t["name"]] = t.get("description", "")
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    n_gold = len({t["gold"] for t in tasks})
    for n in sizes:
        if n < n_gold:
            print(f"  skip N={n} (< {n_gold} gold tools)"); continue
        cat_dir = ROOT / "data" / "coverage_cache" / f"pool_{n}"
        _build_ingest_catalog(tasks, tool_descs, n, args.seed, cat_dir)
        proc = _start_finder(str(cat_dir), args.port)
        try:
            client = FinderClient(f"http://127.0.0.1:{args.port}")
            recs = []
            for arm in arms:
                recs += _run_arm(agent, tasks, bundle, bundle_names,
                                 client if arm != "local" else None, arm)
        finally:
            proc.terminate(); proc.wait()
        tally = _tally(recs)
        _print_tally(str(n), tally)
        rows.append({"N": n, "tally": tally, "records": recs})
    return rows


def main(argv=None):
    p = argparse.ArgumentParser(description="Coverage + token-thrash experiment.")
    p.add_argument("--agent", default="loop", help="Agent name or file.py:factory.")
    p.add_argument("--tasks", required=True, help="JSONL of {id, prompt, gold}.")
    p.add_argument("--bundle", help="JSON list of the local/default toolset.")
    p.add_argument("--tool-descs", help="JSON list of {name,description} for gold tools (ingest mode).")
    p.add_argument("--catalog-dir", help="Serve this pre-built catalog as-is (existing-catalog mode).")
    p.add_argument("--finder-url", help="Score against an already-running Agent Finder at this URL "
                                        "(e.g. http://127.0.0.1:8090) instead of spawning one.")
    p.add_argument("--sizes", default="15,150,1500", help="Comma list of catalog sizes N (ingest mode).")
    p.add_argument("--arms", default="local,catalog,join", help="Comma list of arms.")
    p.add_argument("--max-turns", type=int, default=8, help="Agent budget cap per task.")
    p.add_argument("--port", type=int, default=8098, help="Port for the Finder instance.")
    p.add_argument("--seed", type=int, default=0, help="Distractor seed.")
    p.add_argument("--out", help="Write full results to JSON.")
    args = p.parse_args(argv)
    rows = run(args)
    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
