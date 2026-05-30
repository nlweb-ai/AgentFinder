# Benchmark harness

Answers one question: **does a coding/tool-use agent do better *with* Agent
Finder than without it?** It runs the same task suite in two arms — control
(agent alone) and treatment (agent + Agent Finder discovery) — and reports the
delta in task success.

The harness is deliberately agent-agnostic. **Whoever runs it chooses the
agent** — nothing about the subject agent is baked in.

## Choosing the agent

Pass `--agent`. Three ways, no harness edits needed:

| Form | Example | Use when |
|------|---------|----------|
| Registered name | `--agent stub` / `--agent llm` | built-in reference adapters (`stub` = no-token wiring check; `llm` = single-shot model on the project's llm_backend) |
| External CLI | `--agent shell` + `BENCH_AGENT_CMD="aider --stdin"` | you have an agent CLI (Aider, OpenHands, Claude Code, SWE-agent…) |
| Your own adapter | `--agent ./my_agent.py:make_agent` | you want full control in Python |

The built-in `llm` agent reuses `code/llm_backend.py`, so it needs the same
`LLM_*` env vars as Agent Finder (`source ./set_keys.sh`). It is the reference
**descriptions-only** subject: treatment = query Finder, paste discovered
augment descriptions into context, then answer; control = answer from the
prompt alone.

A custom adapter is one factory returning an object with `name` and
`solve(task, tools) -> Attempt`. In the **treatment** arm `tools.finder` is a
client over a running Agent Finder; in **control** it's `None`. A good adapter
queries the finder and feeds the discovered augments into its own context, and
degrades to "agent alone" when `finder is None`.

```python
# my_agent.py
from benchmarks.core import Attempt

class MyAgent:
    name = "my-agent"
    def solve(self, task, tools):
        ctx = task.prompt
        if tools.finder is not None:
            hits = tools.finder.search(task.prompt, page_size=5)
            ctx += "\n\nAUGMENTS:\n" + "\n".join(h["displayName"] for h in hits)
        answer = my_model(ctx)          # however your agent actually runs
        return Attempt(task.id, output=answer)

def make_agent(**kwargs):
    return MyAgent()
```

## Choosing the benchmark

`--benchmark` works the same way. Built-ins:

| Name | What it is |
|------|------------|
| `sample` | toy substring wiring check (no setup) |
| `jsonl` | one `{id, prompt, expect?}` object per line |
| `toolretrieval` | tool-selection suite — the canonical descriptions-only experiment (bundled dataset, runs offline) |
| `appworld` | wraps the real AppWorld suite + its grader (needs `pip install appworld`) |

Other real suites (tau-bench, the MCP benchmarks) are added as adapter files
selected with `--benchmark ./tau.py:make_benchmark`; that adapter owns dataset
download, workspace prep, and the suite's own grader.

## Catalogs checked into this repo

Two catalogs ship in the repo so a fork can run the benchmarks immediately:

| Path | Size | Use |
|------|------|-----|
| `catalog/` | ~4.6k entries | full deduped + enriched catalog; **Agent Finder's GitHub-load default** |
| `catalog_sample/` | ~300 entries | stratified sample containing every benchmark gold; fast local runs |

Agent Finder loads `catalog/` **from GitHub at startup** by default
(`AGENT_FINDER_GITHUB_REPO=nlweb-ai/AgentFinder`, `AGENT_FINDER_GITHUB_PATH=catalog`).
Fork it and point at your fork:

```bash
export AGENT_FINDER_GITHUB_REPO=<your-org>/AgentFinder   # your fork
SEARCH_PROVIDER=memory WHO_SERVER_PORT=8090 python code/agent_finder.py
# …or serve a local dir instead: AGENT_FINDER_CATALOG_DIR=catalog_sample
```

The `.embeddings.pkl` beside a catalog (and `.agent_finder_cache/` for GitHub
loads) is a derived cache, gitignored, built on first serve and reused after.
Rebuild the catalog from a raw scrape with `scraper/dedup.py` + `scraper/enrich.py`.

## Catalog model: what Agent Finder is allowed to discover

For the descriptions-only delta to be *attributable to Agent Finder*, the
augments it returns must be usable in the benchmark task. Two supported modes:

1. **Ingest the benchmark's tools** (recommended for tool/app benchmarks). Load
   the benchmark's own tool/API universe into a catalog dir and point Finder at
   it, so treatment retrieves exactly the candidates a task needs:
   ```bash
   python -m benchmarks.ingest \
       --in benchmarks/data/toolretrieval/tools.json \
       --out data/bench_toolretrieval --authority toolbench
   SEARCH_PROVIDER=memory AGENT_FINDER_CATALOG_DIR=data/bench_toolretrieval \
       WHO_SERVER_PORT=8096 python code/agent_finder.py &
   ```
2. **Use the existing scraped catalog** (`data/catalog_manifest`). Only valid for
   tasks solvable with the real public MCP servers/skills/agents already indexed;
   otherwise the discovered augments can't complete the task and the delta is noise.

Either way the harness just talks to a Finder URL — `--finder-url` selects which
catalog is in play.

## The headline experiment: coverage + token-thrash

The primary experiment is `benchmarks.coverage_experiment` — it tests the
claim in [../docs/AGENT_FINDER_VALUE.md](../docs/AGENT_FINDER_VALUE.md) the way
[../docs/BENCHMARK_DESIGN.md](../docs/BENCHMARK_DESIGN.md) lays out: a small
locally-curated toolset vs. a large cross-ecosystem catalog, scored as **wins /
losses / trajectory-tokens**, split by whether the gold tool is already in the
local bundle.

It runs the multi-turn `loop` agent over a *stubbed* executor: the task's gold
tool returns a useful result (and solves the task); every other tool returns
nothing useful, so a bundle missing the gold tool makes the agent thrash until
it gives up — that wasted trajectory is the token cost. Three arms (`local`,
`catalog`, `join`) share one retriever interface and differ only in the index.

```bash
source ./set_keys.sh
# offline coverage + scale sweep on the bundled tool universe:
python -m benchmarks.coverage_experiment \
    --tasks benchmarks/data/toolretrieval/tasks.jsonl \
    --bundle benchmarks/data/toolretrieval/default_tools.json \
    --tool-descs benchmarks/data/toolretrieval/tools.json \
    --sizes 15,150,1500           # N-sweep via synthetic distractors

# cross-ecosystem run against the checked-in sample catalog (skills + A2A golds):
python -m benchmarks.coverage_experiment \
    --tasks benchmarks/data/crossecosystem/tasks.jsonl \
    --bundle benchmarks/data/crossecosystem/default_tools.json \
    --catalog-dir catalog_sample        # or --catalog-dir catalog for the full ~4.6k

# …or score against an Agent Finder you're already running (e.g. your fork's):
python -m benchmarks.coverage_experiment \
    --tasks benchmarks/data/crossecosystem/tasks.jsonl \
    --finder-url http://127.0.0.1:8090 --arms local,catalog,join
```

A representative 4-task slice (2 in-bundle, 2 out-of-bundle) prints:

```
     arm |     partition | wins losses |   tokens    mean
   local | out_of_bundle |    0      2 |     1117   558.5   <- thrash, loses
   local |     in_bundle |    2      0 |      528   264.0
 catalog | out_of_bundle |    2      0 |      370   185.0   <- coverage win, cheaper
    join | out_of_bundle |    2      0 |      582   291.0
    join |     in_bundle |    2      0 |      580   290.0   <- no regression
```

It takes the coding agent (`--agent`, any registered name or `file.py:factory`),
the task set, the bundle, and the corpus/size sweep as arguments — nothing about
the subject agent is baked in.

## The A/B runner (`runner.py`): success/token delta

Picture someone installing a coding agent. It ships with a fixed **default set
of tools**. The claim under test is that letting the agent discover tools
through Agent Finder beats that static default set. So:

* **control** = agent + its default toolset (`--default-tools`), shown in the prompt.
* **treatment** = agent + the same defaults **+** the tools Agent Finder
  retrieves for the task.

Both arms have the defaults, so the delta isolates what *discovery* adds. Two
numbers come out:

* `delta_treatment_minus_control` — change in task success.
* `token_delta_treatment_minus_control` — change in the **coding agent's** tokens
  per task (`mean_agent_tokens_per_task.{control,treatment}`), from the agent's
  own `response.usage`. Discovery grows the agent's prompt with the retrieved
  tools, so this captures the prompt-size cost the agent actually pays. (Agent
  Finder's own server-side rerank cost is out of scope — we only count the
  coding agent.)

### Tool-retrieval pilot (end-to-end, runs now)

```bash
# 1. ingest the 15-tool pool so Finder can discover it
python -m benchmarks.ingest --in benchmarks/data/toolretrieval/tools.json \
    --out data/bench_toolretrieval --authority toolbench
# 2. serve that catalog
SEARCH_PROVIDER=memory AGENT_FINDER_CATALOG_DIR=data/bench_toolretrieval \
    WHO_SERVER_PORT=8096 python code/agent_finder.py &
# 3. A/B: default toolset alone vs default + Finder
source ./set_keys.sh
python -m benchmarks.runner --agent llm --benchmark toolretrieval \
    --default-tools benchmarks/data/toolretrieval/default_tools.json \
    --finder-url http://127.0.0.1:8096 --seeds 1
```

A representative run: success **0.2 → 1.0** (the 6 default tools cover only 2 of
10 tasks; Finder surfaces the rest), at a modest cost in the agent's own tokens
(its prompt grows by the handful of retrieved tool descriptions). Report both —
accuracy gain and the agent's token cost — not a headline delta in isolation.

Scale up by enlarging `default_tools.json` toward a realistic shipped toolset and
pointing `BENCH_TR_TASKS` / the `tasks` arg at a full ToolBench or ToolRet export
(same `{id, prompt, gold}` shape), ingesting its full tool set.

## Running

```bash
# 1. Start an Agent Finder instance (treatment arm talks to it over /search).
SEARCH_PROVIDER=memory AGENT_FINDER_CATALOG_DIR=data/catalog_manifest \
    WHO_SERVER_PORT=8090 python code/agent_finder.py &

# 2. Smoke-test the wiring (no tokens, no external agent).
python -m benchmarks.runner --agent stub --benchmark sample

# 3. A real A/B run, both arms, 3 seeds, full records to disk.
python -m benchmarks.runner \
    --agent ./my_agent.py:make_agent \
    --benchmark ./appworld.py:make_benchmark \
    --finder-url http://127.0.0.1:8090 --seeds 3 --out results.json

# Or drive it all from a config file:
python -m benchmarks.runner --config benchmarks/config.example.yaml

python -m benchmarks.runner --list   # registered agents + benchmarks
```

## Output

Per-arm success rate (`delta_treatment_minus_control`) and per-arm mean coding-
agent tokens (`mean_agent_tokens_per_task`, plus
`token_delta_treatment_minus_control`). With `--out`, every attempt — including
its `agent_tokens` — is written for inspection. Run multiple `--seeds` (and
ideally pin the agent's model/scaffold/budget identically across arms) so the
deltas reflect Agent Finder, not noise.

## Files

| File | Role |
|------|------|
| `core.py` | data types (`Task`, `Attempt`, `Toolset`) + `Agent`/`Benchmark` contracts + registry/loader |
| `agents.py` | reference agent adapters (`stub`, `shell`, `llm`) |
| `suites.py` | reference benchmark suites (`sample`, `jsonl`, `toolretrieval`, `appworld`) |
| `finder_tool.py` | client over a running Agent Finder `/search` (the treatment-arm tool) |
| `ingest.py` | load a benchmark's tools into a Finder-servable catalog dir (reuses `scraper.base`) |
| `runner.py` | the A/B loop + success & token delta report (`python -m benchmarks.runner`) |
| `data/toolretrieval/` | bundled tool pool, tasks, and default toolset for the offline pilot |
| `config.example.yaml` | example config |
