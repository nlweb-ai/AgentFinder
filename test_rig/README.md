# Test Rig for Retrieval Strategies

## Overview

This test rig compares the two retrieval strategies (agent-level vs query-level) using mock backends and sample agent data.

## Components

### 1. Sample Agent Data
**File:** [test_data/sample_agents.json](../test_data/sample_agents.json)

Contains 5 sample agents:
- **Writing Coach** (A2AAgent) - 6 sample queries
- **Travel Concierge** (A2AAgent) - 5 sample queries
- **Code Reviewer** (A2AAgent) - 5 sample queries
- **Recipe Finder** (MCPServer) - 4 sample queries
- **Weather Service** (MCPServer) - 4 sample queries

Total: 24 sample queries across all agents

### 2. Mock Backends
**File:** [mock_backends.py](mock_backends.py)

Provides:
- `MockVectorSearchBackend`: In-memory vector search with mock embeddings
  - Creates agent-level documents (5 docs)
  - Creates query-level documents (24 docs)
  - Uses deterministic embeddings based on text hash
  - Computes cosine similarity for ranking

- `MockLLMBackend`: Mock LLM for scoring
  - Keyword-based scoring (realistic but fast)
  - Scoring rules match queries to expected agents
  - Returns scores 50-100 based on query-agent match quality

### 3. Test Script
**File:** [test_retrieval_strategies.py](test_retrieval_strategies.py)

Runs 13 test queries:
- 3 writing queries
- 3 travel queries
- 3 code queries
- 2 recipe queries
- 2 weather queries

Each query is tested with both strategies and results are compared.

## Running the Test Rig

```bash
# From the project root
python test_rig/test_retrieval_strategies.py
```

## Test Results

### Summary (Latest Run)

```
Agent Strategy:  0 wins
Query Strategy: 10 wins
Ties:            3
```

### Key Findings

✅ **Query strategy consistently scores higher** (10 out of 13 queries)

✅ **Both strategies find the correct agent** for 10/13 queries

⚠️ **Both struggle with** very specific queries like:
- "critique my essay" (too specific, no exact match)
- "find security vulnerabilities" (keywords too generic)

### Example Comparison

| Query | Agent Strategy | Query Strategy | Winner |
|-------|---------------|----------------|---------|
| "help me improve my writing" | ✓ #1 (85) | ✓ #1 (100) | Query |
| "translate text to Spanish" | ✓ #1 (80) | ✓ #1 (95) | Query |
| "plan a trip to Japan" | ✓ #1 (85) | ✓ #1 (95) | Query |
| "review my Python code" | ✓ #1 (90) | ✓ #1 (90) | Tie |
| "meal planning for the week" | ✓ #1 (80) | ✓ #1 (100) | Query |

### Why Query Strategy Wins

1. **Direct query-to-query matching**: User query matches sample queries semantically
2. **Better scoring**: Mock LLM can see which specific capabilities matched
3. **Explainability**: Matched queries provide context for ranking

### Output Format

The test rig produces:

1. **Comparison Table**: Side-by-side results for both strategies
2. **Detailed Results**: Per-query analysis showing:
   - Top result from each strategy
   - Expected agent rank and score
   - Matched queries (for query strategy)

See [test_results.txt](test_results.txt) for full output.

## Modifying the Test Rig

### Adding New Agents

Edit [test_data/sample_agents.json](../test_data/sample_agents.json):

```json
{
  "id": "my-agent",
  "name": "My Agent",
  "type": "A2AAgent",
  "url": "https://example.com/my-agent",
  "description": "Agent description",
  "capabilities": "Full capability text",
  "sample_queries": [
    {
      "short": "Do something",
      "detail": "Detailed description of what this does"
    }
  ],
  "skills": [...]
}
```

### Adding New Test Queries

Edit `TEST_QUERIES` in [test_retrieval_strategies.py](test_retrieval_strategies.py):

```python
{
    "query": "your test query",
    "expected_agent": "Expected Agent Name",
    "category": "Category"
}
```

### Adjusting Scoring Rules

Edit `scoring_rules` in `MockLLMBackend` ([mock_backends.py](mock_backends.py)):

```python
self.scoring_rules = {
    "keyword": ["Agent Name 1", "Agent Name 2"],
    # Agents that should match queries containing "keyword"
}
```

## Integration with Real Backends

To test with real backends:

1. Set environment variables for Azure Search and OpenAI
2. Run the WHO handler server: `python code/agent_finder.py`
3. Use curl or the MCP client to test queries

Example:
```bash
# Agent strategy (default)
curl "http://localhost:8080/who?query=help+me+write"

# Query strategy
curl "http://localhost:8080/who?query=help+me+write&strategy=query"
```

## Next Steps

### For Real Testing:

1. **Index real M365 data**:
   - Parse `data/m365/App_Entities-*.tsv`
   - Extract sample queries from FBV_Sentence
   - Create query documents
   - Index into Azure Search

2. **Test with real queries**:
   - Use M365 query sets from the team
   - Measure Precision@5, Recall@10
   - Compare both strategies on real data

3. **Optimize**:
   - Tune score thresholds
   - Adjust aggregation parameters
   - Experiment with embedding models

---

## Files in This Directory

```
test_rig/
├── README.md                      # This file
├── mock_backends.py               # Mock search and LLM backends
├── test_retrieval_strategies.py   # Main test script
└── test_results.txt               # Latest test run output

test_data/
└── sample_agents.json             # Sample agent data
```
