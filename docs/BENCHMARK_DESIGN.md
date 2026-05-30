# Benchmark design: does the Agent Finder claim hold?

This document specifies the experiments that confirm or refute the claim in
[AGENT_FINDER_VALUE.md](AGENT_FINDER_VALUE.md):

> A shared, large, cross-ecosystem catalog with cheap (to the agent) selection
> beats a small, locally-curated, single-ecosystem toolset — **more wins, fewer
> tokens**, with **no regression** on tasks the local set already covers.

## What we are *not* re-testing

"Retrieval beats pasting 16k tools into the prompt" is settled (ToolLLM,
Anthropic's Tool Search). We do not re-litigate it. Every arm here uses
retrieval and keeps only a few tools in context, so no arm is a strawman. The
only things that vary are **what is in the index** and **how good the retriever
is** — exactly the three axes Agent Finder generalizes.

## The arms (one retriever interface, different indices)

| Arm | Index it retrieves over | Retriever | Models |
|-----|-------------------------|-----------|--------|
| **local-only** | the agent's wired-up bundle (10¹ tools) | lightweight (in-budget) | client Tool Search today |
| **catalog** | the full catalog (10³–10⁴) | Agent Finder server-side rerank | remote-only |
| **join** | bundle ∪ catalog | merge then select | what you'd actually ship |

All arms put the same number of tools (top-k) in the agent's context, so the
prompt-definition token cost is held flat across arms *by construction*. Any
difference comes from coverage and from trajectory thrash, not from prompt size.

## The subject agent: a multi-turn loop with stubbed execution

A single-shot "name the tool" agent cannot exhibit the token signal we care
about. To observe thrash, the agent must **call** tools, get results, and decide
again. So the subject is a small ReAct-style loop:

```
loop until FINAL or budget exhausted:
    model emits  CALL <tool> {args}   or   FINAL <answer>
    harness executes the tool (stub) -> observation
    observation appended to context
```

**Stubbed execution** (no live auth needed, deferred per the roadmap): the
task's *gold* tool returns a useful result that lets the task complete; any
other tool returns an empty/error observation. This deterministically
manufactures realistic thrash: lacking the gold tool, the agent tries
substitutes, gets nothing useful, and burns turns until it gives up or the
budget cap fires. The cap bounds a losing trajectory so it can't run forever.

**Trajectory tokens** = sum of `response.usage.total_tokens` over every turn.
Only the coding agent's tokens are counted; Agent Finder's server-side rerank is
out of scope by design.

## The task split: coverage gap is the whole point

Each task carries a `gold` augment and we know the local bundle. That induces
two partitions, and they answer different questions:

- **in-bundle** (`gold ∈ bundle`): the local set already covers it. Both arms
  should win. This is the **regression guard** — joining a 16k catalog must not
  *hurt* here by letting distractors crowd out the gold tool.
- **out-of-bundle** (`gold ∉ bundle`): the local set lacks it. This is where the
  contrast lives. We expect local-only to thrash (tokens pinned near the cap,
  frequent loss) and catalog/join to win cheaply.

To avoid a tautological loss, we **score task completion, not "named the gold
tool."** The local arm is free to attempt with substitutes and may legitimately
win; a bad retrieval in the catalog arm may legitimately lose.

## The measurements (counts, per arm × partition)

- **wins** / **losses** — tasks completed vs not.
- **trajectory tokens** — total and mean per task.

Expected result table (the thing that confirms the claim):

| | out-of-bundle wins | out-of-bundle tokens | in-bundle wins | in-bundle tokens |
|--|--|--|--|--|
| local-only | low | high (thrash to cap) | high | low |
| join | high | low | high | low (no regression) |

If **join ≥ local-only on out-of-bundle wins, with fewer tokens, and ties
local-only on in-bundle**, the claim holds.

## Sweeps and ablations — attributing *why* it wins

A reviewer will say "you only won because you used a better retriever / a bigger
catalog." We decompose the win into its two causes:

1. **Coverage / scale sweep.** Hold the retriever fixed; grow catalog size
   N → 10⁴ (real tools + synthetic distractors). Isolates the coverage effect
   and proves selection *survives* at scale — out-of-bundle wins should stay
   high and tokens flat as N grows. This is the most adversarial test: if recall
   collapses in a 16k haystack, the thesis dies here.
2. **Retrieval-quality ablation.** Hold the big index fixed; swap keyword-only
   vs. small-LM rerank. Isolates how much server-side retrieval contributes —
   expected to be what keeps recall (and thus wins/tokens) high at large N.

## Corpora

| Corpus | Tests which axis | Notes |
|--------|------------------|-------|
| **toolretrieval** (bundled, offline) | coverage + token-thrash, the end-to-end pilot | 15-tool universe, 6-tool bundle; 8 of 10 tasks are out-of-bundle. Runs now, no download. |
| **distractor-scaled** (synthetic, deterministic) | scale sweep N → 10⁴ | gold + N−G synthetic tools; the only way to dial N to ToolLLM scale reproducibly. |
| **catalog cross-ecosystem** (the real 4,793-entry catalog) | the ecosystem axis | tasks whose gold is a **skill** or **A2A agent** — wins built-in MCP tool search structurally cannot get. |

ToolLLM's full 16k RapidAPI export plugs in as a drop-in corpus (same
`{id, prompt, gold}` shape) for an external-credibility headline run.

## Running it

The rig (`benchmarks/coverage_experiment.py`) takes the coding agent, the task
set, the corpus/catalog, and the size sweep as arguments — nothing about the
subject agent is baked in. See `benchmarks/README.md` for invocation.
