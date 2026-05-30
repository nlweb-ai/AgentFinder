"""A/B runner: control (agent alone) vs treatment (agent + Agent Finder).

For every task, in each requested condition, for each seed, run the agent and
score the attempt. Report per-condition success rate and the treatment-minus-
control delta — the number that answers "does Agent Finder help this agent?".

Usage:
    python -m benchmarks.runner --agent stub --benchmark sample
    python -m benchmarks.runner --agent ./my_agent.py:make --benchmark sample \\
        --finder-url http://127.0.0.1:8090 --seeds 3 --out results.json
    python -m benchmarks.runner --config benchmarks/config.example.yaml
    python -m benchmarks.runner --list
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict, List, Optional

from .core import Toolset, load_agent, load_benchmark, list_agents, list_benchmarks
from .finder_tool import FinderClient

CONDITIONS = ("control", "treatment")


def run_condition(agent, benchmark, condition: str, finder: Optional[FinderClient],
                  default_tools: list, seeds: int, verbose: bool) -> List[Dict[str, Any]]:
    records = []
    for task in benchmark.tasks():
        for seed in range(seeds):
            tools = Toolset(
                default_tools=default_tools,
                finder=finder if condition == "treatment" else None,
                seed=seed,
            )
            t0 = time.time()
            attempt = agent.solve(task, tools)
            ok = benchmark.evaluate(task, attempt)
            art = attempt.artifacts or {}
            rec = {
                "benchmark": benchmark.name,
                "agent": agent.name,
                "condition": condition,
                "task_id": task.id,
                "seed": seed,
                "success": bool(ok),
                "error": attempt.error,
                "elapsed_s": round(time.time() - t0, 3),
                "agent_tokens": art.get("agent_tokens"),
            }
            records.append(rec)
            if verbose:
                flag = "ok " if ok else "FAIL"
                print(f"  [{condition}] {task.id} seed={seed} -> {flag}"
                      + (f"  ({attempt.error})" if attempt.error else ""))
    return records


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else None


def summarize(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_cond: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        by_cond.setdefault(r["condition"], []).append(r)
    rates = {c: (sum(r["success"] for r in rs) / len(rs) if rs else None)
             for c, rs in by_cond.items()}
    tokens = {c: _mean([r["agent_tokens"] for r in rs]) for c, rs in by_cond.items()}
    delta = None
    token_delta = None
    if rates.get("control") is not None and rates.get("treatment") is not None:
        delta = rates["treatment"] - rates["control"]
    ct, tt = tokens.get("control"), tokens.get("treatment")
    if ct is not None and tt is not None:
        token_delta = round(tt - ct, 1)
    return {
        "n_per_condition": {c: len(rs) for c, rs in by_cond.items()},
        "success_rate": rates,
        "delta_treatment_minus_control": delta,
        "mean_agent_tokens_per_task": tokens,
        "token_delta_treatment_minus_control": token_delta,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description="A/B benchmark: coding agent +/- Agent Finder.")
    p.add_argument("--config", help="YAML/JSON config; CLI flags override its values.")
    p.add_argument("--agent", help="Registered agent name or path/to/file.py:factory.")
    p.add_argument("--benchmark", help="Registered benchmark name or path/to/file.py:factory.")
    p.add_argument("--finder-url", default="http://127.0.0.1:8090",
                   help="Base URL of a running Agent Finder (treatment arm).")
    p.add_argument("--default-tools",
                   help="JSON list of tools the agent ships with (shown in BOTH "
                        "arms). The treatment claim is that Finder beats these alone.")
    p.add_argument("--conditions", default="control,treatment",
                   help="Comma list of arms to run. Default both.")
    p.add_argument("--seeds", type=int, default=1, help="Repeats per task (variance control).")
    p.add_argument("--out", help="Write full per-attempt records + summary here (JSON).")
    p.add_argument("--list", action="store_true", help="List registered agents/benchmarks and exit.")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    if args.list:
        print("agents:    ", ", ".join(list_agents()))
        print("benchmarks:", ", ".join(list_benchmarks()))
        return 0

    cfg: Dict[str, Any] = {}
    agent_kwargs: Dict[str, Any] = {}
    bench_kwargs: Dict[str, Any] = {}
    if args.config:
        cfg = _load_config(args.config)
        agent_kwargs = (cfg.get("agent") or {}).get("args", {}) if isinstance(cfg.get("agent"), dict) else {}
        bench_kwargs = (cfg.get("benchmark") or {}).get("args", {}) if isinstance(cfg.get("benchmark"), dict) else {}

    agent_spec = args.agent or _spec_from_cfg(cfg.get("agent"))
    bench_spec = args.benchmark or _spec_from_cfg(cfg.get("benchmark"))
    if not agent_spec or not bench_spec:
        p.error("need --agent and --benchmark (or a --config providing them). Try --list.")

    finder_url = args.finder_url or cfg.get("finder_url") or "http://127.0.0.1:8090"
    conditions = [c.strip() for c in (args.conditions or "control,treatment").split(",") if c.strip()]
    seeds = args.seeds if args.seeds else int(cfg.get("seeds", 1))
    verbose = not args.quiet

    agent = load_agent(agent_spec, **agent_kwargs)
    benchmark = load_benchmark(bench_spec, **bench_kwargs)

    default_tools = []
    dt_src = args.default_tools or cfg.get("default_tools")
    if dt_src:
        if isinstance(dt_src, str):
            with open(dt_src, encoding="utf-8") as f:
                default_tools = json.load(f)
        else:
            default_tools = dt_src

    finder = None
    if "treatment" in conditions:
        finder = FinderClient(finder_url)
        if not finder.health():
            print(f"WARNING: Agent Finder at {finder_url} is not healthy — "
                  f"treatment arm searches will fail. Start the server first.",
                  file=sys.stderr)

    print(f"agent={agent.name}  benchmark={benchmark.name}  "
          f"conditions={conditions}  seeds={seeds}  finder={finder_url}  "
          f"default_tools={len(default_tools)}")

    all_records: List[Dict[str, Any]] = []
    for cond in conditions:
        if verbose:
            print(f"\n== {cond} ==")
        all_records += run_condition(agent, benchmark, cond, finder, default_tools, seeds, verbose)

    summary = summarize(all_records)
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))

    if args.out:
        payload = {
            "config": {"agent": agent.name, "benchmark": benchmark.name,
                       "conditions": conditions, "seeds": seeds, "finder_url": finder_url},
            "summary": summary,
            "records": all_records,
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nWrote {args.out} ({len(all_records)} attempts).")
    return 0


def _spec_from_cfg(node) -> Optional[str]:
    if node is None:
        return None
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        return node.get("spec") or node.get("name")
    return None


def _load_config(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if path.endswith((".yaml", ".yml")):
        import yaml
        return yaml.safe_load(text) or {}
    return json.loads(text)


if __name__ == "__main__":
    raise SystemExit(main())
