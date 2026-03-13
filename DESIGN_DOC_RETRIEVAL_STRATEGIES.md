# Agent Retrieval Strategies: Design Document

**Date:** March 12, 2026
**Version:** 0.1 (Draft)
**Status:** Proposal

## Executive Summary

This document compares two retrieval strategies for the AgentFinder Who Protocol implementation:

1. **Agent-Level Retrieval**: One document per agent, retrieved and ranked directly
2. **Query-Level Retrieval**: Multiple sample query documents per agent, aggregated before ranking

Both strategies aim to improve agent discovery by leveraging the M365 Apps data, which contains pre-computed embeddings and implicit sample queries in the `FBV_Sentence` field.

---

## Strategy 1: Agent-Level Retrieval

### Overview

In this approach, each agent is represented by a **single document** in the vector index. The document contains the agent's full description, capabilities, and metadata.

### Architecture

```
User Query
    ↓
[Embed query with text-embedding-3-small]
    ↓
[Vector search in index]
    ↓
[Retrieve top-k agents (documents)]
    ↓
[Rank with GPT-4o-mini]
    ↓
Return ranked results
```

### Document Structure

Each document represents one agent:

```json
{
  "id": "agent-{AppId}",
  "type": "agent",
  "name": "Writing Coach",
  "description": "Writing Coach to help you in your writing.",
  "capabilities": "Critique my writing. Provide detailed and constructive feedback...",
  "embedding": [0.062, -0.008, 0.010, ...],  // 384-dim or 1536-dim
  "metadata": {
    "app_id": "f72d7797-c6ee-4fd3-9454-028d0095068b",
    "app_type": "GPT",
    "publisher": "Microsoft Corporation",
    "version": "1.0.0",
    "categories": ["productivity", "writing"]
  }
}
```

### Implementation Details

#### Indexing Phase

1. **Parse M365 TSV data**
   - Read `App_Entities-*.tsv`
   - Extract relevant fields: Title, Description, FBV_Sentence, AppType, etc.

2. **Create agent documents**
   - One document per agent
   - Combine Description + FBV_Sentence for rich content
   - Use pre-computed FBV_Embedding (384-dim) OR regenerate with text-embedding-3-small (1536-dim)

3. **Index into Azure Search**
   - Vector field: `embedding`
   - Searchable text fields: `name`, `description`, `capabilities`
   - Filterable fields: `app_type`, `categories`, `publisher`

#### Query Phase

1. **Embed user query**
   - Use same embedding model as indexing (consistency required)
   - text-embedding-3-small (1536-dim) or use M365's 384-dim model

2. **Vector search**
   - Retrieve top-k agents (e.g., k=20-50)
   - Use cosine similarity

3. **Rank with GPT-4o-mini**
   - For each retrieved agent, call `llm_backend.rank_site(query, agent_json)`
   - Returns `{"score": 0-100, "description": "..."}`
   - Filter out agents with score < threshold (e.g., 50)

4. **Return top-N results**
   - Sort by score descending
   - Format as Who Protocol response

### Pros

✅ **Simple architecture**: One document per agent, straightforward retrieval
✅ **Lower index size**: Fewer total documents in vector index
✅ **Fast retrieval**: Single vector search, no aggregation needed
✅ **Lower latency**: One retrieval step, then ranking
✅ **Easier debugging**: Direct mapping from query → agents
✅ **Can use pre-computed embeddings**: Zero cost if using FBV_Embedding directly

### Cons

❌ **Semantic mismatch risk**: User query may not match agent-level description well
❌ **Long descriptions**: Agents with many capabilities may have diluted embeddings
❌ **Harder to match specific use cases**: Query "translate text" might not strongly match "Writing Coach" even though it has translation capability
❌ **Embedding quality dependency**: If agent description is generic, retrieval suffers

### Cost Analysis

**Indexing (one-time):**
- Using pre-computed embeddings: $0 (embeddings already exist)
- Regenerating with text-embedding-3-small: ~$0.02 per 1M tokens
  - Assume 500 tokens/agent × 1000 agents = 500K tokens = **~$0.01**

**Query (per request):**
- Embed query: ~20 tokens × $0.02/1M = **~$0.0000004**
- Rank with GPT-4o-mini: Assume 50 agents × 500 tokens = 25K tokens
  - Input: $0.15/1M tokens = **~$0.00375**
  - Output: ~100 tokens × $0.60/1M = **~$0.00006**
- **Total per query: ~$0.004** (0.4 cents)

### Expected Performance

- **Precision**: Medium (depends on embedding quality)
- **Recall**: Medium-High (if agent descriptions are comprehensive)
- **Latency**: Low (~500ms for retrieval + ranking)

---

## Strategy 2: Query-Level Retrieval with Aggregation

### Overview

In this approach, each agent is represented by **multiple documents** in the vector index. Each document corresponds to a **sample query** that the agent can handle. Retrieved documents are aggregated by agent, then ranked.

### Architecture

```
User Query
    ↓
[Embed query with text-embedding-3-small]
    ↓
[Vector search in index]
    ↓
[Retrieve top-k query documents]
    ↓
[Group documents by agent_id]
    ↓
[Aggregate: create agent summary from matched queries]
    ↓
[Rank aggregated agents with GPT-4o-mini]
    ↓
Return ranked results
```

### Document Structure

Each document represents one **sample query** for an agent:

```json
{
  "id": "query-{AppId}-{query_index}",
  "type": "sample_query",
  "agent_id": "f72d7797-c6ee-4fd3-9454-028d0095068b",
  "agent_name": "Writing Coach",
  "query": "Critique my writing",
  "query_detail": "Provide detailed and constructive feedback on a piece of writing",
  "embedding": [0.052, -0.012, 0.015, ...],  // Embedding of the query
  "metadata": {
    "app_type": "GPT",
    "publisher": "Microsoft Corporation",
    "skill_id": "critique_writing"
  }
}
```

For **Writing Coach**, we'd have ~6 documents (one per sample query extracted from FBV_Sentence):
- "Critique my writing"
- "Change the tone of an email or message"
- "Translate a piece of text"
- "Teach me how to write instructions"
- "Professional blog post"
- "Write a whitepaper"

### Implementation Details

#### Indexing Phase

1. **Parse M365 TSV data**
   - Read `App_Entities-*.tsv`
   - Extract FBV_Sentence field

2. **Extract sample queries** (for GPT apps)
   ```python
   def extract_sample_queries(fbv_sentence: str) -> List[Dict]:
       """Parse FBV_Sentence to extract sample queries"""
       parts = [p.strip() for p in fbv_sentence.split('.') if p.strip()]
       queries = []

       # Skip first part (app name)
       parts = parts[1:]

       # Take every other part (short query, skip detailed description)
       for i in range(0, len(parts), 2):
           if i+1 < len(parts):
               queries.append({
                   "short": parts[i],      # "Critique my writing"
                   "detail": parts[i+1]    # "Provide detailed feedback..."
               })
       return queries
   ```

3. **Create query documents**
   - Multiple documents per agent (one per sample query)
   - Include minimal agent metadata (name, type, id)
   - Embed each query with text-embedding-3-small

4. **Index into Azure Search**
   - Vector field: `embedding` (of the query, not agent description)
   - Searchable text fields: `query`, `query_detail`, `agent_name`
   - Filterable fields: `agent_id`, `app_type`

#### Query Phase

1. **Embed user query**
   - Use text-embedding-3-small (same as indexing)

2. **Vector search**
   - Retrieve top-k query documents (e.g., k=100)
   - Use cosine similarity

3. **Aggregate by agent**
   ```python
   def aggregate_by_agent(query_docs: List[Dict]) -> List[Dict]:
       """Group query documents by agent_id"""
       agents = {}
       for doc in query_docs:
           agent_id = doc["agent_id"]
           if agent_id not in agents:
               agents[agent_id] = {
                   "agent_id": agent_id,
                   "agent_name": doc["agent_name"],
                   "matched_queries": [],
                   "metadata": doc.get("metadata", {})
               }
           agents[agent_id]["matched_queries"].append({
               "query": doc["query"],
               "detail": doc["query_detail"],
               "score": doc.get("@search.score", 0)
           })

       # Sort matched_queries by relevance score
       for agent in agents.values():
           agent["matched_queries"].sort(
               key=lambda q: q["score"],
               reverse=True
           )

       return list(agents.values())
   ```

4. **Rank aggregated agents with GPT-4o-mini**
   - For each aggregated agent:
     - Build context: agent name + top 3-5 matched queries
     - Call `llm_backend.rank_site(query, agent_context_json)`
   - Returns `{"score": 0-100, "description": "..."}`
   - Filter out agents with score < threshold

5. **Return top-N results**
   - Sort by score descending
   - Format as Who Protocol response

### Pros

✅ **Better semantic matching**: User queries match directly against sample queries
✅ **Higher recall**: More opportunities to match (multiple documents per agent)
✅ **Explainability**: Can show which sample queries matched user's query
✅ **Handles specific use cases well**: Query "translate text" strongly matches "Translate a piece of text" sample query
✅ **Fine-grained relevance**: Each capability is independently indexed
✅ **Leverages existing sample queries**: M365 data already contains these in FBV_Sentence

### Cons

❌ **Larger index size**: 5-10x more documents (multiple per agent)
❌ **More complex architecture**: Requires aggregation step
❌ **Higher indexing cost**: Must embed each sample query individually
❌ **Potential noise**: Low-quality sample queries may dilute retrieval
❌ **Aggregation overhead**: Additional processing step before ranking
❌ **Harder to debug**: Indirect mapping from retrieved queries → agents

### Cost Analysis

**Indexing (one-time):**
- Assume 1000 agents × 5 sample queries/agent = 5000 documents
- Each query ~20 tokens
- 5000 × 20 = 100K tokens
- text-embedding-3-small: $0.02/1M tokens = **~$0.002**

**Query (per request):**
- Embed query: ~20 tokens × $0.02/1M = **~$0.0000004**
- Rank with GPT-4o-mini: Assume 20 aggregated agents × 300 tokens = 6K tokens
  - (Less content per agent since we only include matched queries, not full descriptions)
  - Input: $0.15/1M tokens = **~$0.0009**
  - Output: ~100 tokens × $0.60/1M = **~$0.00006**
- **Total per query: ~$0.001** (0.1 cents)

**Actually cheaper than Strategy 1** because ranking context is smaller!

### Expected Performance

- **Precision**: High (query-to-query matching is very direct)
- **Recall**: High (multiple entry points per agent)
- **Latency**: Medium (~700ms for retrieval + aggregation + ranking)

---

## Comparison Matrix

| Dimension | Strategy 1: Agent-Level | Strategy 2: Query-Level |
|-----------|------------------------|------------------------|
| **Index Size** | Small (~1K docs) | Large (~5-10K docs) |
| **Retrieval Precision** | Medium | High |
| **Retrieval Recall** | Medium-High | High |
| **Semantic Matching** | Indirect (query → agent desc) | Direct (query → sample query) |
| **Indexing Cost** | $0 (pre-computed) or ~$0.01 | ~$0.002 |
| **Query Cost** | ~$0.004/query | ~$0.001/query |
| **Latency** | Low (~500ms) | Medium (~700ms) |
| **Explainability** | Low (why did this agent match?) | High (show matched queries) |
| **Complexity** | Low | Medium |
| **Debugging** | Easy | Harder |
| **Handles specific queries** | Poorly | Well |
| **Handles broad queries** | Well | Well |

---

## Recommendation

### Primary Recommendation: **Strategy 2 (Query-Level Retrieval)**

**Rationale:**

1. **Better user experience**: Users often have specific tasks in mind (e.g., "translate text", "critique my writing"). Strategy 2 matches these directly against sample queries, leading to more relevant results.

2. **Higher precision**: Query-to-query matching is semantically tighter than query-to-description matching.

3. **Lower query cost**: Despite having more documents, the ranking context is smaller ($0.001 vs $0.004 per query).

4. **Explainability**: We can show users *which sample queries* matched their request, improving transparency.

5. **Leverages M365 data structure**: The FBV_Sentence field already contains these sample queries; we're using the data as designed.

6. **Better for long-tail queries**: Specific queries like "create a whitepaper" will strongly match the "Write a whitepaper" sample query, even if the agent description is generic.

### Hybrid Approach (Optional)

Consider a **hybrid strategy** that combines both:

1. Index **both** agent-level documents AND query-level documents
2. Mark documents with `doc_type: "agent"` or `doc_type: "query"`
3. Retrieve from both, then aggregate and rank
4. This provides the best of both worlds but increases complexity

---

## Implementation Plan

### Phase 1: Implement Strategy 2 (Query-Level) - Week 1

**Tasks:**
1. Write `extract_sample_queries()` function to parse FBV_Sentence
2. Create indexing script:
   - Parse M365 TSV
   - Extract sample queries per agent
   - Embed each query with text-embedding-3-small
   - Index into Azure Search
3. Update `who_handler.py`:
   - Add aggregation logic after retrieval
   - Build aggregated agent context for ranking
4. Write tests for aggregation logic

### Phase 2: Test and Evaluate - Week 2

**Tasks:**
1. Index M365 Apps data (~1000 agents → ~5000 query docs)
2. Test with query sets from M365 team
3. Measure:
   - Precision@5, Recall@10
   - Latency (end-to-end)
   - User satisfaction (if possible)
4. Compare against baseline (if available)

### Phase 3: Optimize and Tune - Week 3

**Tasks:**
1. Tune aggregation parameters:
   - How many matched queries to include in ranking context?
   - Score threshold for filtering low-quality matches?
2. Experiment with embedding models:
   - text-embedding-3-small vs text-embedding-3-large
   - M365's pre-computed 384-dim embeddings
3. Add caching for popular queries

### Phase 4: Optional - Implement Strategy 1 for Comparison - Week 4

**Tasks:**
1. Implement Strategy 1 as a baseline
2. A/B test both strategies
3. Measure performance differences
4. Choose winner or implement hybrid approach

---

## Open Questions

1. **How many sample queries per agent?**
   - M365 GPT apps: ~3-10 queries in FBV_Sentence
   - Catalog apps: May need to synthesize queries from description

2. **What to do for Catalog apps (non-GPT)?**
   - Option A: Synthesize sample queries using LLM
   - Option B: Fall back to agent-level indexing
   - Option C: Skip catalog apps for now

3. **How to handle query quality variance?**
   - Some sample queries may be too generic ("Help me")
   - Consider filtering or rewriting low-quality queries

4. **Should we use M365's pre-computed embeddings?**
   - 384-dim embeddings already exist
   - But they may be of agent descriptions, not sample queries
   - Probably need to re-embed sample queries for Strategy 2

5. **Aggregation strategy details:**
   - How many matched queries to include in ranking context?
   - Should we weight queries by retrieval score?
   - Should we include agent description in ranking context?

---

## Appendix: Code Sketches

### Strategy 2: Sample Query Extraction

```python
def extract_sample_queries(fbv_sentence: str, app_type: str) -> List[Dict[str, str]]:
    """
    Extract sample queries from FBV_Sentence.

    Returns:
        List of {"short": "...", "detail": "..."} dicts
    """
    if app_type != "GPT":
        return []

    parts = [p.strip() for p in fbv_sentence.split('.') if p.strip()]

    # Skip app name (first part)
    if len(parts) > 0:
        parts = parts[1:]

    queries = []
    for i in range(0, len(parts), 2):
        if i+1 < len(parts):
            queries.append({
                "short": parts[i],
                "detail": parts[i+1]
            })
        elif i < len(parts):
            # Odd number of parts, last one has no detail
            queries.append({
                "short": parts[i],
                "detail": ""
            })

    return queries
```

### Strategy 2: Aggregation Logic

```python
def aggregate_query_results(query_docs: List[Dict], top_k: int = 20) -> List[Dict]:
    """
    Aggregate retrieved query documents by agent.

    Args:
        query_docs: List of retrieved query documents
        top_k: Max number of agents to return

    Returns:
        List of aggregated agent objects
    """
    agents = {}

    for doc in query_docs:
        agent_id = doc.get("agent_id")
        if not agent_id:
            continue

        if agent_id not in agents:
            agents[agent_id] = {
                "agent_id": agent_id,
                "agent_name": doc.get("agent_name", "Unknown"),
                "app_type": doc.get("metadata", {}).get("app_type", "Unknown"),
                "matched_queries": [],
                "max_score": 0,
                "metadata": doc.get("metadata", {})
            }

        # Add matched query
        query_score = doc.get("@search.score", 0)
        agents[agent_id]["matched_queries"].append({
            "query": doc.get("query", ""),
            "detail": doc.get("query_detail", ""),
            "score": query_score
        })

        # Track max score across all queries
        agents[agent_id]["max_score"] = max(
            agents[agent_id]["max_score"],
            query_score
        )

    # Sort agents by max score
    sorted_agents = sorted(
        agents.values(),
        key=lambda a: a["max_score"],
        reverse=True
    )

    # Return top-k agents
    return sorted_agents[:top_k]
```

### Strategy 2: Ranking Context Builder

```python
def build_ranking_context(aggregated_agent: Dict, max_queries: int = 5) -> str:
    """
    Build JSON context for LLM ranking.

    Args:
        aggregated_agent: Aggregated agent object
        max_queries: Max number of matched queries to include

    Returns:
        JSON string for ranking
    """
    # Take top N matched queries
    top_queries = aggregated_agent["matched_queries"][:max_queries]

    context = {
        "name": aggregated_agent["agent_name"],
        "type": aggregated_agent["app_type"],
        "matched_capabilities": [
            {
                "capability": q["query"],
                "description": q["detail"]
            }
            for q in top_queries
        ],
        "metadata": aggregated_agent.get("metadata", {})
    }

    return json.dumps(context, indent=2)
```

---

## Conclusion

**Strategy 2 (Query-Level Retrieval with Aggregation)** is recommended for implementation because it provides better semantic matching, higher precision, and better explainability at a lower per-query cost. The M365 Apps data structure (with sample queries embedded in FBV_Sentence) naturally supports this approach.

We should implement Strategy 2 first, evaluate its performance, and optionally implement Strategy 1 as a baseline for comparison.

---

**Next Steps:**
1. Get approval on Strategy 2 recommendation
2. Begin implementation (Phase 1)
3. Test with M365 query sets
4. Iterate based on results
