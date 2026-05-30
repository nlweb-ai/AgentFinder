# Catalog scraper

Gathers catalog entries (MCP servers, agent skills, A2A agents) from trustworthy
upstream sources and writes them as JSON files that the in-memory search backend
reads directly.

## Sources

| Key            | Source                                              | ~Count   | mediaType                          |
|----------------|-----------------------------------------------------|---------:|------------------------------------|
| `glama`        | Glama registry (glama.ai) — **largest free feed**   | ~27,000  | `application/mcp-server+json`      |
| `pulsemcp`     | PulseMCP (pulsemcp.com) — free tier caps at ~300*   | ~16,000  | `application/mcp-server+json`      |
| `mcp`          | Official MCP registry (registry.modelcontextprotocol.io) | ~2,000 | `application/mcp-server+json`  |
| `a2a_registry` | a2aregistry.org — live hosted agents (full cards)   |     ~100 | `application/a2a-agent-card+json`  |
| `a2a`          | github.com/a2aproject/a2a-samples                   |      ~30 | `application/a2a-agent-card+json`  |
| `skills`       | github.com/anthropics/skills + github.com/huggingface/skills | dozens | `application/ai-skill`     |

All sources are canonical registries or first-party GitHub repos — no scraping of arbitrary web pages.

\* PulseMCP's free `v0beta` endpoint returns `410 Gone` past offset ~300; the full
16k+ set requires an authenticated `v0.1` API key (hello@pulsemcp.com). The scraper
stops cleanly at the cap. For a large local catalog, use `glama`.

## Usage

```bash
# All sources -> data/catalog/
python -m scraper.run

# Specific sources, custom output dir and per-source cap
python -m scraper.run --source mcp a2a --out data/catalog --limit 500

# Set a token to avoid GitHub API rate limits
GITHUB_TOKEN=ghp_... python -m scraper.run
```

Each source writes its own file (`mcp_glama.json`, `mcp_pulsemcp.json`,
`mcp_registry.json`, `a2a_samples.json`, `skills.json`) — plain arrays of catalog
entries. Point the backend at the directory:

```bash
SEARCH_PROVIDER=memory AGENT_FINDER_CATALOG_DIR=data/catalog python code/agent_finder.py
```

## Curating MCP servers (LLM eval)

The scrapers pull thousands of MCP servers of mixed quality. `eval_mcp.py` runs
an LLM quality-scoring pass over every MCP-server entry, keeps everything scoring
at or above `--min-score`, and ranks the survivors by real-world usage (GitHub
stars, official provenance, packaging). It reuses `code/llm_backend.py`
(`score_quality`), so an initial scoring run needs the same `LLM_*` env vars set,
e.g. `source ./set_keys.sh`.

```bash
# First run — scores all MCP servers (needs LLM_* keys) -> data/catalog_curated/
source ./set_keys.sh
python -m scraper.eval_mcp --in data/catalog --out data/catalog_curated

# Re-select from the cache — no keys, no LLM calls. Cheap to rerun with a new bar.
python -m scraper.eval_mcp --min-score 80

python -m scraper.eval_mcp --limit 100          # dry sample on 100 servers
python -m scraper.eval_mcp --concurrency 80     # more parallel LLM calls
```

Scores are cached per-identifier in `mcp_scores.json`, so re-runs only score
servers not already present (a crashed run resumes cheaply). Once everything is
cached, the backend isn't initialized at all — re-running to change `--min-score`
or the usage ranking is instant and needs no API keys.

Output dir contains `mcp_top.json` (the kept MCP servers, ranked by usage),
`other.json` (all non-MCP entries passed through untouched), and
`mcp_scores.json` (the score cache). Then point the backend at the curated dir:

```bash
SEARCH_PROVIDER=memory AGENT_FINDER_CATALOG_DIR=data/catalog_curated python code/agent_finder.py
```

## Building a capability manifest (ai-catalog.json)

The scraped/curated files are flat arrays whose artifact type lives in
`mediaType`. The Agent Finder spec (§4.1) expects an `ai-catalog.json` manifest —
`{"specVersion", "host", "entries": [...]}` — where each entry uses the `type`
field (§4.2). `build_manifest.py` wraps the entries into that structure,
delegating the per-entry conversion to `who_handler._to_catalog_entry` (the same
mapper the `/who` query path uses), so manifest entries and search results are
byte-for-byte the same shape.

```bash
python -m scraper.build_manifest --in data/catalog_curated \
    --out data/catalog_manifest/ai-catalog.json --host-name "Agent Finder Catalog"

SEARCH_PROVIDER=memory AGENT_FINDER_CATALOG_DIR=data/catalog_manifest python code/agent_finder.py
```

Entries lacking both `url` and `data` are skipped (the spec requires exactly
one). The backend reads either form — flat arrays or a wrapped manifest — so this
step is only needed when you want a single hostable `ai-catalog.json`.

## Entry schema

Each entry matches what `code/search_backend.py` expects:

```json
{
  "identifier": "urn:ai:<authority>:<...>",
  "displayName": "...",
  "mediaType": "application/...",
  "url": "...",
  "description": "...",
  "tags": ["..."],
  "version": "...",
  "metadata": { "source": "..." }
}
```

## Adding a source

Write an `async fetcher(session, token, limit) -> list[entry]` in `sources.py`
(build entries with `base.make_entry`) and register it in the `SOURCES` dict.
