"""Discovery-agency eval: does the coding agent CALL Agent Finder, and does it
DECIDE to use what Finder returns?

`selection_eval` answers "is the right tool in Finder's top-k" — a property of
retrieval. This answers a behavioral question one level up: given Agent Finder as
a tool it *can* call (not pre-pasted into its context), does the agent actually
reach for it when its own toolbox falls short, and then adopt the discovered
tool? That is what a real Copilot/Codex/Claude Code does — or fails to do — when
Agent Finder is registered as an MCP server.

The subject is the `discover` agent (benchmarks/agents.py): a ReAct loop with a
FIND action wired to a running Agent Finder and a STUBBED executor for the tools
(gold solves, others return nothing), so no third-party app is ever touched. We
report a per-task funnel, split by partition:

  in_bundle      = gold is already in the agent's shipped tools (it shouldn't
                   need Finder; calling it anyway is wasted effort).
  out_of_bundle  = gold is only reachable via the catalog (Finder is the only
                   way to win — the partition that tests the behavior we want).

  called_finder     fraction of tasks where the agent invoked FIND
  used_discovered   fraction where it then CALLed a Finder-surfaced tool
  solved            fraction where that tool was the gold (task done)

Usage:
    source ./set_keys.sh                       # the agent needs LLM keys
    SEARCH_PROVIDER=memory WHO_SERVER_PORT=8090 python code/agent_finder.py &
    python -m benchmarks.discovery_eval \
        --tasks benchmarks/data/crossecosystem/tasks.jsonl \
        --bundle benchmarks/data/crossecosystem/default_tools.json \
        --finder-url http://127.0.0.1:8090

    # or spawn a Finder over a local catalog, on the first 5 tasks:
    python -m benchmarks.discovery_eval \
        --tasks benchmarks/data/crossecosystem/tasks.jsonl \
        --bundle benchmarks/data/crossecosystem/default_tools.json \
        --catalog-dir catalog_sample --limit 5
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from .core import Task, Toolset, load_agent
from .finder_tool import FinderClient
from .coverage_experiment import _load_jsonl, _make_executor, _start_finder


def _run(agent, client, tasks, bundle, bundle_names) -> List[Dict[str, Any]]:
    recs = []
    for t in tasks:
        tools = Toolset(
            default_tools=bundle,
            finder=client,
            execute=_make_executor(t["gold"]),
        )
        task = Task(id=t["id"], prompt=t["prompt"], metadata={"gold": t["gold"]})
        art = agent.solve(task, tools).artifacts or {}
        recs.append({
            "task_id": t["id"],
            "partition": "in_bundle" if t["gold"] in bundle_names else "out_of_bundle",
            "called_finder": bool(art.get("called_finder")),
            "used_discovered": bool(art.get("used_discovered")),
            "solved": bool(art.get("solved")),
            "turns": art.get("turns"),
        })
    return recs


def _funnel(recs: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    parts = sorted({r["partition"] for r in recs})
    for part in parts + ["all"]:
        rs = recs if part == "all" else [r for r in recs if r["partition"] == part]
        if not rs:
            continue
        n = len(rs)
        out[part] = {
            "n": n,
            "called_finder": round(sum(r["called_finder"] for r in rs) / n, 3),
            "used_discovered": round(sum(r["used_discovered"] for r in rs) / n, 3),
            "solved": round(sum(r["solved"] for r in rs) / n, 3),
        }
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="Does the agent call Agent Finder and use its result?")
    p.add_argument("--agent", default="discover", help="Agent name or file.py:factory.")
    p.add_argument("--tasks", required=True, help="JSONL of {id, prompt, gold}.")
    p.add_argument("--bundle", help="JSON list of the agent's shipped/default tools.")
    p.add_argument("--finder-url", help="Base URL of a running Agent Finder.")
    p.add_argument("--catalog-dir", help="Spawn a Finder over this catalog dir instead.")
    p.add_argument("--limit", type=int, default=0, help="Only run the first N tasks.")
    p.add_argument("--max-turns", type=int, default=8, help="Agent budget per task.")
    p.add_argument("--port", type=int, default=8097, help="Port when spawning (--catalog-dir).")
    p.add_argument("--out", help="Write full results JSON here.")
    args = p.parse_args(argv)

    if not args.finder_url and not args.catalog_dir:
        p.error("need --finder-url (a running Finder) or --catalog-dir (one to spawn)")

    tasks = _load_jsonl(args.tasks)[: args.limit] if args.limit else _load_jsonl(args.tasks)
    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8")) if args.bundle else []
    bundle_names = {b.get("name") or b.get("displayName") for b in bundle}
    agent = load_agent(args.agent, max_turns=args.max_turns)

    n_out = sum(1 for t in tasks if t["gold"] not in bundle_names)
    print(f"{len(tasks)} tasks ({len(tasks)-n_out} in-bundle, {n_out} out-of-bundle), "
          f"bundle={len(bundle)} tools, agent={agent.name}")

    proc = None
    try:
        if args.catalog_dir:
            proc = _start_finder(args.catalog_dir, args.port)
            client = FinderClient(f"http://127.0.0.1:{args.port}")
        else:
            client = FinderClient(args.finder_url)
            if not client.health():
                raise SystemExit(f"Agent Finder at {args.finder_url} is not healthy")
        recs = _run(agent, client, tasks, bundle, bundle_names)
    finally:
        if proc is not None:
            proc.terminate(); proc.wait()

    funnel = _funnel(recs)
    print(f"\n{'partition':>14} | {'n':>3} | {'called':>7} {'used':>6} {'solved':>7}")
    print("-" * 48)
    for part, c in funnel.items():
        print(f"{part:>14} | {c['n']:>3} | {c['called_finder']:>7} "
              f"{c['used_discovered']:>6} {c['solved']:>7}")

    if args.out:
        Path(args.out).write_text(json.dumps(
            {"funnel": funnel, "records": recs}, indent=2), encoding="utf-8")
        print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
