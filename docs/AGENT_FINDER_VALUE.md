# Agent Finder: a generalization of Tool Search

## The problem everyone now agrees on

An agent can only act through tools whose definitions sit in its context. But
tool definitions are expensive and selection is fragile:

- A tool definition (name + description + JSON schema) runs ~100–400 tokens. A
  handful of MCP servers (GitHub, Slack, Sentry, Grafana, Splunk) already burns
  ~55k tokens *before the agent does any work*.
- Model tool-selection accuracy degrades sharply once more than ~30–50 tools are
  in context. At ToolLLM scale (~16,464 RapidAPI tools) pasting everything in is
  not merely expensive — it is physically impossible.

The field's answer, as of 2026, is **retrieval instead of dumping**: index tool
names + descriptions, and at query time pull only the 3–5 tools the task needs
into context. ToolLLM (Qin et al., 2023) pioneered this with an "API Retriever"
in front of 16k APIs; Anthropic shipped it as the **Tool Search Tool** in Claude
Code (on by default once tool definitions exceed ~10% of the context window,
~85%+ context reduction). This is no longer the contested claim. **Retrieval
beats in-context dumping — settled.**

## Where Tool Search stops

Client-side Tool Search has three structural limits, all stemming from the same
fact: *it runs inside the agent, over the tools that agent happens to have
wired up.*

1. **Scope = what you connected.** It retrieves over your locally configured MCP
   servers. If the tool a task needs was never connected, no amount of retrieval
   surfaces it. Coverage is bounded by what each user thought to install.
2. **Retriever must stay lightweight.** Because it runs in the agent's own loop
   and token budget, it is essentially embedding/keyword match over a
   name+description index. It cannot afford query understanding, multi-stage
   retrieval, or a reranker — those would cost the agent tokens and latency on
   every turn.
3. **One ecosystem.** It indexes MCP tools. Skills and A2A/remote agents — other
   ways to extend an agent — are outside its model.

## Agent Finder = Tool Search, generalized on three axes

Agent Finder keeps the *exact same interface* — "given a query, return the
top-k relevant capabilities to put in context" — and relaxes each limit. It is a
**remote retrieval backend you federate into the agent's existing tool search**,
not a competing client paradigm. The agent still pays only the flat top-k prompt
cost; everything else moves server-side.

| Axis | Client Tool Search | Agent Finder |
|------|--------------------|--------------|
| **Scope** | Tools you wired up (10⁰–10¹ servers) | Shared curated catalog, 10³–10⁴ entries (today: 4,619) |
| **Retriever** | Lightweight keyword/embedding, in the agent's budget | Server-side: query understanding + dense retrieval + small-LM rerank, **off** the agent's token budget |
| **Ecosystem** | MCP servers only | MCP servers **+ skills + A2A agents + remote agents** |

### Axis 1 — Scale and the *join*

The agent's local tool search and a remote Agent Finder are the *same operation*
over different indices. So you don't replace one with the other — you **join**
them: retrieve from the local index, retrieve from the remote catalog, merge,
select top-k as usual. Local tool search becomes the special case
`index = your servers`. Agent Finder is `index = the world`. The catalog covers
the union no single user anticipates; the join means you never lose the local
high-precision hits while gaining everything you forgot to install.

### Axis 2 — Retrieval quality, for free

Because the rerank happens on the server, Agent Finder can spend real
compute — a small language model judging candidate relevance, multi-stage
retrieval — that the in-process client could never afford. Per our cost model we
count **only the coding agent's tokens**; the server's rerank is out of the
agent's budget entirely. A tighter, higher-precision top-k is what makes the
needle findable in a 16k haystack *and* what reduces the agent's own token
spend (see below). This is the asymmetry same-process tool search cannot match.

### Axis 3 — The ecosystem dimension

"Extending an agent" is not only MCP servers. It includes **skills** (curated
procedural know-how) and **A2A / remote agents** (delegating a sub-task to
another agent). These are different *types* of augment with different
invocation models, and a user assembling MCP servers by hand will essentially
never wire them up. Agent Finder indexes all of them in one catalog (today:
4,372 MCP servers, 184 skills, 137 A2A agent cards, 100 remote agents) and
returns the best augment *regardless of type*. Built-in MCP tool search
structurally cannot return a skill or an A2A agent — it doesn't model them. This
is the clearest capability a same-ecosystem retriever can never reach.

## The two payoffs

1. **Capability (coverage).** When the right augment isn't in the local set, the
   locally-confined agent cannot solve the task at all. The catalog supplies it.
   More tasks solved.
2. **Tokens (less thrash).** This is the subtle one. When the needed tool is
   absent, a capable agent does *not* fail cheaply — it thrashes: tries a
   plausible substitute, gets a bad result, reasons, retries, re-reads context,
   burns turns, and often still fails. Surfacing the right tool ends the
   trajectory in one clean call. So coverage doesn't just raise success — it
   *lowers* the agent's token spend. A better server-side reranker tightens
   top-k, which means fewer wrong-tool calls, which means less thrash: every
   axis points the same direction — **more wins, fewer tokens.**

## The claim under test

> A shared, large, cross-ecosystem catalog with cheap (to the agent) selection
> is strictly better than a small, locally-curated, single-ecosystem toolset —
> measured as more tasks solved (coverage) and fewer agent tokens spent (less
> thrash), with no regression on tasks the local set already covered.

This is what the benchmark in [BENCHMARK_DESIGN.md](BENCHMARK_DESIGN.md) is built
to confirm or refute.
