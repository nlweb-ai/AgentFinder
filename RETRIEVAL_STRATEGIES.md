# Retrieval Strategies Implementation

## Overview

The WHO handler now supports **two different retrieval strategies** for finding and ranking agents. You can select which strategy to use via a URL parameter.

## Strategy 1: Agent-Level Retrieval (Default)

**Parameter:** `strategy=agent` (or omit the parameter)

### How It Works

1. **Embed** the user's query
2. **Search** for agent documents in the vector index
3. **Rank** each agent directly using GPT-4o-mini
4. **Filter** by score threshold and return top results

### When to Use

- **Simple queries**: Broad queries like "help me with travel planning"
- **Fast responses**: Lower latency (~500ms)
- **Agent-focused**: When you want to match against full agent descriptions

### Example Request

**REST API (GET):**
```bash
curl "http://localhost:8080/who?query=help+me+write&strategy=agent"
```

**REST API (POST):**
```bash
curl -X POST http://localhost:8080/who \
  -H "Content-Type: application/json" \
  -d '{
    "query": {
      "text": "help me write better"
    },
    "meta": {
      "strategy": "agent"
    }
  }'
```

**MCP Tool Call:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "who",
    "arguments": {
      "query": {
        "text": "help me write better"
      },
      "meta": {
        "strategy": "agent"
      }
    }
  }
}
```

---

## Strategy 2: Query-Level Retrieval with Aggregation

**Parameter:** `strategy=query`

### How It Works

1. **Embed** the user's query
2. **Search** for sample query documents in the vector index
3. **Aggregate** query documents by agent_id
4. **Rank** aggregated agents using their matched queries as context
5. **Filter** by score threshold and return top results

### When to Use

- **Specific queries**: Precise tasks like "translate text" or "critique my writing"
- **Better matching**: Direct query-to-query semantic matching
- **Explainability**: Results include which sample queries matched
- **M365 Apps**: Optimized for M365 apps with sample queries in FBV_Sentence

### Example Request

**REST API (GET):**
```bash
curl "http://localhost:8080/who?query=translate+text&strategy=query"
```

**REST API (POST):**
```bash
curl -X POST http://localhost:8080/who \
  -H "Content-Type: application/json" \
  -d '{
    "query": {
      "text": "translate text to Spanish"
    },
    "meta": {
      "strategy": "query"
    }
  }'
```

**MCP Tool Call:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "who",
    "arguments": {
      "query": {
        "text": "translate text to Spanish"
      },
      "meta": {
        "strategy": "query"
      }
    }
  }
}
```

### Response Format

Results from the query strategy include `matched_queries` for explainability:

```json
{
  "_meta": {
    "response_type": "answer",
    "version": "0.1",
    "result_count": 1
  },
  "results": [
    {
      "protocol": "a2a",
      "endpoint": "https://agent.example.com",
      "score": 90,
      "definition": {
        "name": "Writing Coach",
        "description": "Excellent writing assistant",
        "skills": [...]
      },
      "matched_queries": [
        {
          "query": "Translate a piece of text",
          "score": 0.95
        },
        {
          "query": "Change the tone of an email",
          "score": 0.85
        }
      ]
    }
  ]
}
```

---

## Comparison

| Aspect | Agent Strategy | Query Strategy |
|--------|----------------|----------------|
| **Index Size** | Smaller (1 doc/agent) | Larger (5-10 docs/agent) |
| **Latency** | Lower (~500ms) | Slightly higher (~700ms) |
| **Semantic Matching** | Indirect (query → description) | Direct (query → sample queries) |
| **Precision** | Medium | High |
| **Explainability** | Low | High (shows matched queries) |
| **Best For** | Broad queries | Specific tasks |
| **Corpus Type** | General agents | M365 apps with sample queries |

---

## Implementation Details

### Code Structure

Both strategies are implemented in [code/who_handler.py](code/who_handler.py):

- `_process_agent_strategy()`: Strategy 1 implementation (lines 156-299)
- `_process_query_strategy()`: Strategy 2 implementation (lines 301-445)
- `_aggregate_by_agent()`: Aggregation logic for query strategy (lines 447-495)
- `_rank_aggregated_agent()`: Ranking for aggregated agents (lines 497-527)

### Query Document Format (Strategy 2)

For Strategy 2 to work, query documents in the search index must have these fields:

```python
{
  "agent_id": "unique-agent-id",
  "agent_name": "Agent Name",
  "agent_url": "https://agent.example.com",
  "agent_json_ld": "{...}",  # JSON-LD for agent
  "query": "Sample query text",
  "query_detail": "Detailed description of what the query does",
  "@search.score": 0.95  # Search relevance score
}
```

### Testing

Both strategies have comprehensive test coverage in [code/test_who_handler.py](code/test_who_handler.py):

- `TestRetrievalStrategies`: 4 tests covering both strategies
- All 20 tests pass ✅

Run tests:
```bash
python -m pytest code/test_who_handler.py::TestRetrievalStrategies -v
```

---

## Next Steps: Creating the Corpus

Now that both retrieval strategies are implemented, the next step is to create the corpus (index):

### For Strategy 1 (Agent-Level)

1. Parse M365 Apps TSV data
2. Create one document per agent:
   - Use `Title` as name
   - Combine `Description` + `FBV_Sentence` for rich content
   - Use pre-computed `FBV_Embedding` or regenerate
3. Index into Azure Search

### For Strategy 2 (Query-Level)

1. Parse M365 Apps TSV data
2. Extract sample queries from `FBV_Sentence`:
   - For GPT apps: Parse alternating short/detailed descriptions
   - For Catalog apps: Synthesize queries or skip
3. Create multiple documents per agent (one per sample query)
4. Embed each query with text-embedding-3-small
5. Index into Azure Search

### See Also

- [DESIGN_DOC_RETRIEVAL_STRATEGIES.md](DESIGN_DOC_RETRIEVAL_STRATEGIES.md): Detailed design doc comparing strategies
- [data/m365/SCHEMA_ANALYSIS.md](data/m365/SCHEMA_ANALYSIS.md): M365 Apps schema analysis
- [data/m365/EMBEDDING_ANALYSIS.md](data/m365/EMBEDDING_ANALYSIS.md): Embedding format details
- [M365_INTEGRATION_PLAN.md](M365_INTEGRATION_PLAN.md): Full integration plan

---

## Configuration

Both strategies use the same environment variables for configuration:

- `WHO_SCORE_THRESHOLD`: Minimum score to include in results (default: 70)
- `WHO_EARLY_THRESHOLD`: Score for early return optimization (default: 85)
- `WHO_MAX_RESULTS`: Maximum results to return (default: 10)
- `WHO_SEARCH_TOP_K`: Number of documents to retrieve from search (default: 30)
  - Strategy 2 retrieves `2 × WHO_SEARCH_TOP_K` to account for aggregation

---

## Notes

- **Default strategy**: If no `strategy` parameter is provided, defaults to "agent"
- **Backward compatibility**: All existing queries continue to work (they use agent strategy)
- **Caching**: Both strategies benefit from embedding, search, and ranking caches
- **Federation**: Both strategies work with Who Protocol federation (Section 11)
