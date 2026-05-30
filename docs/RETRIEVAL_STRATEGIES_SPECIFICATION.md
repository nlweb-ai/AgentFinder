# Retrieval Strategies Specification

**Document Version:** 1.0
**Date:** 2026-03-17
**System:** WHO Protocol v0.1 Agent Discovery System

## Overview

This document provides complete specifications for implementing the two retrieval strategies used in the WHO Protocol agent discovery system. Each strategy uses different data sources and ranking approaches to find relevant agents for user queries.

Both strategies share common infrastructure (Azure AI Search vector search, Azure OpenAI embeddings, LLM-based ranking) but differ fundamentally in their approach to matching queries with agents.

---

## Common Infrastructure

### Vector Search Backend

**Service:** Azure AI Search
**Embedding Model:** text-embedding-3-large (1536 dimensions)
**Similarity Metric:** Cosine similarity

**Configuration:**
- Search endpoint: Configurable via `AZURE_SEARCH_ENDPOINT`
- API key: Secure key management
- API version: 2024-05-01-preview

### LLM Ranking Backend

**Service:** Azure OpenAI
**Models Tested:** gpt-4.1, gpt-4.1-mini, gpt-4.1-nano, gpt-4o-mini, gpt-5-nano
**Connection Pool:** 100 clients for parallel ranking
**Max Concurrent Calls:** 1000
**Timeout:** 8 seconds per ranking call

**Ranking Prompt Template:**
```
Assign a score between 0 and 100 to the following augment based on the likelihood that the augment will contain an answer to the user's question.

First think about the kind of thing the user is seeking and then verify that the augment is primarily focused on that kind of thing.

The user's question is: {query}

The augment's description is:
{augment_description}

Return JSON only with this exact format: {"score": <integer 0-100>, "description": "<one sentence explanation>"}
```

**Ranking Configuration:**
- Response format: JSON object
- Temperature: 0 (deterministic)
- Max tokens: 100
- Retry policy: 1 retry on failure
- Fallback: Score of 0 on timeout/error

### Caching

**LRU Cache Implementation:**
- Search cache: 1000 entries
- Ranking cache: 10000 entries
- Cache key format: Hash of (query text, strategy, optional model)

### Score Filtering

**Default Threshold:** 50
**Configurable via:** `WHO_SCORE_THRESHOLD` environment variable
**Behavior:** Agents with LLM ranking scores ≤ threshold are filtered out

---

## Strategy 1: Augment-Level Retrieval

### Concept

Search directly against augment descriptions, rank the retrieved augments using LLM scoring, and return the top-ranked results.

### Data Requirements

**Azure AI Search Index:** `augments-index`

**Document Schema:**
```json
{
  "augment_id": "string (unique identifier)",
  "augment_name": "string (display name)",
  "description": "string (detailed augment description)",
  "endpoint": "string (augment endpoint URL)",
  "protocol": "string (e.g., 'http', 'mcp')",
  "method": "string (HTTP method, e.g., 'POST')",
  "contentType": "string (e.g., 'application/json')",
  "descriptionVector": [1536 floats] (embedding of description)
}
```

**Index Size:** ~100-200 augment documents

### Implementation Steps

#### 1. Query Embedding Generation

```python
def generate_query_embedding(query_text: str) -> List[float]:
    """
    Generate 1536-dimensional embedding for the user's query.

    Args:
        query_text: User's natural language query (max 8000 chars)

    Returns:
        List of 1536 floats representing the query embedding
    """
    # Use Azure OpenAI embeddings API
    # Model: text-embedding-3-large
    # Truncate input to 8000 characters if longer
    # On error: return zero vector [0.0] * 1536
```

#### 2. Vector Search

```python
def search_agents_index(query_vector: List[float], top_k: int = 20) -> List[dict]:
    """
    Search the augments-index using vector similarity.

    Args:
        query_vector: 1536-dimensional query embedding
        top_k: Number of results to retrieve (default: 20)

    Returns:
        List of augment documents sorted by similarity score

    Azure AI Search API Call:
        POST https://{endpoint}/indexes/augments-index/docs/search?api-version=2024-05-01-preview

        Request body:
        {
            "vectorQueries": [{
                "vector": query_vector,
                "k": top_k,
                "fields": "descriptionVector",
                "kind": "vector"
            }],
            "select": "augment_id,augment_name,description,endpoint,protocol,method,contentType",
            "top": top_k
        }
    """
```

**Key Parameters:**
- `k`: Number of nearest neighbors to retrieve (20 for augment strategy)
- `fields`: Vector field to search (`descriptionVector`)
- `select`: Fields to return in results

#### 3. LLM Ranking

```python
def rank_augments(query: str, augments: List[dict]) -> List[dict]:
    """
    Rank each augment using LLM scoring.

    Args:
        query: Original user query
        augments: List of augment documents from vector search

    Returns:
        List of augments with added 'ranking_score' and 'ranking_description' fields

    Process:
        1. For each augment in parallel:
            a. Check ranking cache with key: (query_hash, augment_id)
            b. If cached, use cached score
            c. If not cached:
                - Call LLM with ranking prompt
                - Parse JSON response to extract score and description
                - Ensure score is integer 0-100
                - Cache the result

        2. Add ranking results to each augment document:
            augment['ranking_score'] = score
            augment['ranking_description'] = description
    """
```

**Parallel Execution:**
- Use asyncio to rank all augments concurrently
- Maximum 1000 concurrent LLM calls
- Round-robin distribution across 100 client pool
- Continue on individual failures (assign score of 0)

#### 4. Filtering and Sorting

```python
def filter_and_sort(augments: List[dict], threshold: int = 50) -> List[dict]:
    """
    Filter by score threshold and sort by ranking score.

    Args:
        augments: List of augments with ranking_score
        threshold: Minimum score required (default: 50)

    Returns:
        Sorted list of augments with score > threshold

    Process:
        1. Filter: Keep only augments where ranking_score > threshold
        2. Sort: Order by ranking_score descending
        3. Return sorted list
    """
```

#### 5. Response Formatting

```python
def format_response(augments: List[dict]) -> dict:
    """
    Format augments into WHO Protocol v0.1 response.

    Returns:
        {
            "_meta": {
                "response_type": "answer",
                "version": "0.1",
                "result_count": len(augments)
            },
            "results": [
                {
                    "protocol": augment['protocol'],
                    "endpoint": augment['endpoint'],
                    "score": augment['ranking_score'],
                    "definition": {
                        "name": augment['augment_name'],
                        "description": augment['ranking_description'],
                        "method": augment['method'],
                        "contentType": augment['contentType']
                    }
                }
                for augment in augments
            ]
        }
    """
```

### Complete Augment Strategy Pseudocode

```python
async def process_agent_strategy(query: str, max_results: int = None) -> dict:
    # Step 1: Generate query embedding
    query_vector = await generate_query_embedding(query)

    # Step 2: Check search cache
    cache_key = hash((query, "augment"))
    search_results = search_cache.get(cache_key)

    if not search_results:
        # Perform vector search
        search_results = await search_agents_index(
            query_vector=query_vector,
            top_k=20  # Retrieve 20 augments
        )
        search_cache.set(cache_key, search_results)

    if not search_results:
        return format_response([])  # Empty result

    # Step 3: Rank augments with LLM (parallel)
    ranking_tasks = []
    for augment in search_results:
        rank_cache_key = (cache_key, augment['augment_id'])
        cached_ranking = ranking_cache.get(rank_cache_key)

        if not cached_ranking:
            task = rank_agent_with_llm(query, augment['description'])
            ranking_tasks.append((augment, task, rank_cache_key))
        else:
            augment['ranking_score'] = cached_ranking['score']
            augment['ranking_description'] = cached_ranking['description']

    # Execute all ranking tasks in parallel
    if ranking_tasks:
        results = await asyncio.gather(
            *[task for _, task, _ in ranking_tasks],
            return_exceptions=True
        )

        for (augment, _, cache_key), result in zip(ranking_tasks, results):
            if isinstance(result, Exception):
                result = {"score": 0, "description": "Ranking failed"}

            augment['ranking_score'] = result['score']
            augment['ranking_description'] = result['description']
            ranking_cache.set(cache_key, result)

    # Step 4: Filter by threshold and sort
    filtered_augments = [
        augment for augment in search_results
        if augment.get('ranking_score', 0) > SCORE_THRESHOLD
    ]

    sorted_augments = sorted(
        filtered_augments,
        key=lambda x: x['ranking_score'],
        reverse=True
    )

    # Step 5: Apply max_results limit
    if max_results:
        sorted_augments = sorted_augments[:max_results]

    # Step 6: Format response
    return format_response(sorted_augments)
```

### Performance Characteristics

**Measured on 50-query test set:**
- Top-1 Accuracy: 15%
- Top-3 Accuracy: 35%
- Average latency: ~1-2 seconds (with caching)
- Cache hit rate: ~60-70% after warmup

**Strengths:**
- Simple, direct approach
- Fast vector search
- Lower infrastructure complexity (single index)

**Weaknesses:**
- Poor ranking accuracy (15% top-1)
- Semantic mismatch between augment descriptions and user queries
- Agent descriptions don't capture all use cases

---

## Strategy 2: Query-Level Retrieval (Recommended)

### Concept

Search against example queries that augments can answer, aggregate results by augment, rank the aggregated augments using LLM scoring, and return the top-ranked results.

### Data Requirements

**Azure AI Search Index:** `queries-index`

**Document Schema:**
```json
{
  "query_id": "string (unique identifier for this query example)",
  "query": "string (example user query this augment can answer)",
  "augment_id": "string (foreign key to augment)",
  "augment_name": "string (augment display name)",
  "augment_description": "string (detailed augment description)",
  "endpoint": "string (augment endpoint URL)",
  "protocol": "string (e.g., 'http', 'mcp')",
  "method": "string (HTTP method)",
  "contentType": "string (MIME type)",
  "queryVector": [1536 floats] (embedding of the query field)
}
```

**Index Size:** ~1300 query documents (multiple queries per augment)

**Example Documents:**
```json
[
  {
    "query_id": "q1",
    "query": "How do I write better emails?",
    "augment_id": "writing-coach-001",
    "augment_name": "Writing Coach",
    "augment_description": "Provides feedback on writing style, grammar, and clarity",
    "queryVector": [0.123, -0.456, ...]
  },
  {
    "query_id": "q2",
    "query": "Improve my presentation skills",
    "augment_id": "writing-coach-001",
    "augment_name": "Writing Coach",
    "augment_description": "Provides feedback on writing style, grammar, and clarity",
    "queryVector": [0.789, -0.234, ...]
  },
  {
    "query_id": "q3",
    "query": "Check my document for errors",
    "augment_id": "writing-coach-001",
    "augment_name": "Writing Coach",
    "augment_description": "Provides feedback on writing style, grammar, and clarity",
    "queryVector": [-0.111, 0.222, ...]
  }
]
```

### Implementation Steps

#### 1. Query Embedding Generation

Same as augment strategy - generate 1536-dimensional embedding for user query.

#### 2. Vector Search Against Queries

```python
def search_queries_index(query_vector: List[float], top_k: int = 40) -> List[dict]:
    """
    Search the queries-index using vector similarity.

    Args:
        query_vector: 1536-dimensional query embedding
        top_k: Number of query documents to retrieve (default: 40, 2x augment strategy)

    Returns:
        List of query documents sorted by similarity score

    Azure AI Search API Call:
        POST https://{endpoint}/indexes/queries-index/docs/search?api-version=2024-05-01-preview

        Request body:
        {
            "vectorQueries": [{
                "vector": query_vector,
                "k": top_k,
                "fields": "queryVector",  # Different field name!
                "kind": "vector"
            }],
            "select": "query_id,query,augment_id,augment_name,augment_description,endpoint,protocol,method,contentType",
            "top": top_k
        }
    """
```

**Key Differences from Augment Strategy:**
- Search field: `queryVector` instead of `descriptionVector`
- Higher k value: 40 instead of 20 (because we'll aggregate multiple queries per augment)
- Returns query documents, not augment documents

#### 3. Aggregation by Agent

This is the critical step that distinguishes query strategy from augment strategy.

```python
def aggregate_by_augment(query_documents: List[dict]) -> List[dict]:
    """
    Aggregate query documents by augment, collecting matched queries for each.

    Args:
        query_documents: List of query documents from vector search

    Returns:
        List of unique augment documents with matched_queries field

    Algorithm:
        1. Create empty dict: agents_map = {}

        2. For each query_doc in query_documents:
            augment_id = query_doc['augment_id']

            if augment_id not in agents_map:
                # First time seeing this augment
                agents_map[augment_id] = {
                    'augment_id': query_doc['augment_id'],
                    'augment_name': query_doc['augment_name'],
                    'description': query_doc['augment_description'],
                    'endpoint': query_doc['endpoint'],
                    'protocol': query_doc['protocol'],
                    'method': query_doc['method'],
                    'contentType': query_doc['contentType'],
                    'matched_queries': []
                }

            # Add this query example to the augment's matched queries
            agents_map[augment_id]['matched_queries'].append({
                'query': query_doc['query'],
                'query_id': query_doc['query_id']
            })

        3. Return list(agents_map.values())

    Example:
        Input: [
            {query: "write email", augment_id: "A1", augment_name: "Writer"},
            {query: "check grammar", augment_id: "A1", augment_name: "Writer"},
            {query: "schedule meeting", augment_id: "A2", augment_name: "Calendar"}
        ]

        Output: [
            {
                augment_id: "A1",
                augment_name: "Writer",
                matched_queries: [
                    {query: "write email"},
                    {query: "check grammar"}
                ]
            },
            {
                augment_id: "A2",
                augment_name: "Calendar",
                matched_queries: [
                    {query: "schedule meeting"}
                ]
            }
        ]
    """
```

**Why This Matters:**
- Multiple query examples for the same augment get consolidated
- Agents appear once in the ranking phase (not once per matched query)
- Matched queries are preserved for response formatting

#### 4. LLM Ranking

Same ranking process as augment strategy, but applied to aggregated augments.

```python
async def rank_aggregated_augments(query: str, augments: List[dict]) -> List[dict]:
    """
    Rank each aggregated augment using LLM scoring.

    Args:
        query: Original user query
        augments: List of aggregated augment documents (with matched_queries)

    Returns:
        List of augments with added ranking_score and ranking_description

    Process: Identical to augment strategy ranking
        - Use same ranking prompt template
        - Use augment['description'] for ranking (not matched queries)
        - Cache results with key: (query_hash, augment_id)
        - Execute rankings in parallel
        - Handle failures gracefully (score of 0)
    """
```

**Important Notes:**
- The LLM ranking prompt receives the augment's description, NOT the matched queries
- Matched queries are used only for vector search; ranking is still description-based
- This is why query strategy has better initial retrieval but same ranking approach

#### 5. Filtering, Sorting, and Response Formatting

Same as augment strategy:
1. Filter by score threshold (default: 50)
2. Sort by ranking_score descending
3. Apply max_results limit if specified
4. Format into WHO Protocol v0.1 response

**Response Format Enhancement:**

Query strategy includes matched queries in the response:

```python
def format_query_strategy_response(augments: List[dict]) -> dict:
    """
    Format augments with matched queries into WHO Protocol response.

    Returns:
        {
            "_meta": {
                "response_type": "answer",
                "version": "0.1",
                "result_count": len(augments)
            },
            "results": [
                {
                    "protocol": augment['protocol'],
                    "endpoint": augment['endpoint'],
                    "score": augment['ranking_score'],
                    "definition": {
                        "name": augment['augment_name'],
                        "description": augment['ranking_description'],
                        "method": augment['method'],
                        "contentType": augment['contentType']
                    },
                    "matched_queries": [  # Additional field!
                        {"query": q['query']}
                        for q in augment['matched_queries'][:3]  # Limit to top 3
                    ]
                }
                for augment in augments
            ]
        }
    """
```

### Complete Query Strategy Pseudocode

```python
async def process_query_strategy(query: str, max_results: int = None) -> dict:
    # Step 1: Generate query embedding
    query_vector = await generate_query_embedding(query)

    # Step 2: Check search cache
    cache_key = hash((query, "query"))
    search_results = search_cache.get(cache_key)

    if not search_results:
        # Perform vector search against queries-index
        search_results = await search_queries_index(
            query_vector=query_vector,
            top_k=40  # Retrieve more documents (2x augment strategy)
        )
        search_cache.set(cache_key, search_results)

    if not search_results:
        return format_query_strategy_response([])

    # Step 3: Aggregate query documents by augment
    aggregated_augments = aggregate_by_augment(search_results)

    if not aggregated_augments:
        return format_query_strategy_response([])

    # Step 4: Rank aggregated augments with LLM (parallel)
    ranking_tasks = []
    for augment in aggregated_augments:
        rank_cache_key = (cache_key, augment['augment_id'])
        cached_ranking = ranking_cache.get(rank_cache_key)

        if not cached_ranking:
            task = rank_agent_with_llm(query, augment['description'])
            ranking_tasks.append((augment, task, rank_cache_key))
        else:
            augment['ranking_score'] = cached_ranking['score']
            augment['ranking_description'] = cached_ranking['description']

    # Execute all ranking tasks in parallel
    if ranking_tasks:
        results = await asyncio.gather(
            *[task for _, task, _ in ranking_tasks],
            return_exceptions=True
        )

        for (augment, _, cache_key), result in zip(ranking_tasks, results):
            if isinstance(result, Exception):
                result = {"score": 0, "description": "Ranking failed"}

            augment['ranking_score'] = result['score']
            augment['ranking_description'] = result['description']
            ranking_cache.set(cache_key, result)

    # Step 5: Filter by threshold and sort
    filtered_augments = [
        augment for augment in aggregated_augments
        if augment.get('ranking_score', 0) > SCORE_THRESHOLD
    ]

    sorted_augments = sorted(
        filtered_augments,
        key=lambda x: x['ranking_score'],
        reverse=True
    )

    # Step 6: Apply max_results limit
    if max_results:
        sorted_augments = sorted_augments[:max_results]

    # Step 7: Format response (including matched_queries)
    return format_query_strategy_response(sorted_augments)
```

### Performance Characteristics

**Measured on 50-query test set:**
- Top-1 Accuracy: 70%
- Top-3 Accuracy: 70%
- Average latency: ~1-2 seconds (with caching)
- Cache hit rate: ~60-70% after warmup

**Strengths:**
- Excellent ranking accuracy (70% top-1)
- 4.67x better than augment strategy
- Perfect ranking: when right augment is found, it's always #1
- Leverages example queries which better capture augment capabilities

**Weaknesses:**
- 30% retrieval failures (expected augment not in results at all)
- More complex infrastructure (two indices)
- Requires maintaining query examples for each augment

---

## Strategy Comparison

### Architectural Differences

| Aspect | Augment Strategy | Query Strategy |
|--------|---------------|----------------|
| Search Index | augments-index | queries-index |
| Search Field | descriptionVector | queryVector |
| Search Target | Agent descriptions | Example queries |
| Documents Retrieved | 20 augments | 40 queries |
| Aggregation Step | None | Group by augment_id |
| Ranking Input | Agent description | Agent description (same) |
| Response Extra | None | matched_queries field |

### Performance Comparison

| Metric | Augment Strategy | Query Strategy | Winner |
|--------|---------------|----------------|---------|
| Top-1 Accuracy | 15% | 70% | Query (4.67x) |
| Top-3 Accuracy | 35% | 70% | Query (2x) |
| Top-1 to Top-3 Growth | +20pp (2.33x) | 0pp (1x) | Different patterns |
| Latency | ~1-2s | ~1-2s | Tied |
| Infrastructure | Simple (1 index) | Complex (2 indices) | Agent |
| Maintenance | Low | Medium (query curation) | Agent |

### Accuracy Analysis

**Augment Strategy (15% → 35%):**
- Retrieval is working (finds relevant augments)
- Ranking is failing (places correct augment at positions 2-3)
- Problem: Semantic mismatch between descriptions and queries

**Query Strategy (70% → 70%):**
- Retrieval + ranking working together
- When correct augment found, always ranked #1
- 30% failures are retrieval issues (augment not in results at all)
- Problem: Vector search not finding the right queries

### When to Use Each Strategy

**Use Augment Strategy When:**
- Minimal infrastructure preferred
- Agent descriptions are comprehensive and query-aligned
- Lower accuracy is acceptable (15-35%)
- Fewer augments to search (<50)

**Use Query Strategy When:**
- Maximum accuracy required (70%)
- Resources available to maintain query examples
- Agent descriptions alone don't capture all use cases
- Larger augment corpus (>100 augments)

---

## Implementation Checklist

### Prerequisites
- [ ] Azure AI Search service provisioned
- [ ] Azure OpenAI service with embedding and chat models
- [ ] Python 3.8+ with asyncio support
- [ ] Required packages: azure-search-documents, openai, aiohttp

### Augment Strategy Setup
1. [ ] Create augments-index in Azure AI Search
2. [ ] Define schema with descriptionVector field (1536 dims)
3. [ ] Populate index with augment documents
4. [ ] Generate embeddings for all augment descriptions
5. [ ] Configure search endpoint and API key
6. [ ] Implement LRU caches (search: 1000, ranking: 10000)
7. [ ] Set up Azure OpenAI client pool (100 clients)
8. [ ] Configure score threshold (default: 50)
9. [ ] Implement the 6-step pipeline
10. [ ] Test with sample queries

### Query Strategy Setup
1. [ ] Create queries-index in Azure AI Search
2. [ ] Define schema with queryVector field (1536 dims)
3. [ ] Collect/generate query examples for each augment
4. [ ] Populate index with query documents
5. [ ] Generate embeddings for all query examples
6. [ ] Configure search endpoint and API key
7. [ ] Implement LRU caches (search: 1000, ranking: 10000)
8. [ ] Set up Azure OpenAI client pool (100 clients)
9. [ ] Configure score threshold (default: 50)
10. [ ] Implement the 7-step pipeline (including aggregation)
11. [ ] Test with sample queries
12. [ ] Establish query example maintenance process

### Testing and Validation
- [ ] Verify vector search returns expected results
- [ ] Validate LLM ranking produces 0-100 scores
- [ ] Confirm caching reduces latency
- [ ] Test parallel ranking performance
- [ ] Measure accuracy on test set
- [ ] Compare strategies side-by-side
- [ ] Load test with concurrent requests

---

## Optimization Opportunities

### For Augment Strategy

1. **Improve Agent Descriptions**
   - Include common query patterns in descriptions
   - Add use case examples directly in description text
   - Optimize description length (not too short, not too long)

2. **Better Ranking Prompt**
   - Add few-shot examples to ranking prompt
   - Include augment capabilities checklist
   - Emphasize query-description semantic matching

3. **Hybrid Approach**
   - Combine augment descriptions with top query examples
   - Use query examples in ranking prompt
   - Weighted combination of description and query embeddings

### For Query Strategy

1. **Improve Query Coverage**
   - Add more diverse query examples per augment
   - Cover edge cases and niche use cases
   - Include negative examples (queries augment can't handle)

2. **Increase Search Depth**
   - Retrieve more than 40 queries (e.g., 60-80)
   - Test impact on recall vs. precision tradeoff
   - Monitor impact on latency

3. **Query Quality Scoring**
   - Weight matched queries by similarity score
   - Prefer augments with multiple high-scoring matches
   - Incorporate match count into ranking

4. **Automatic Query Generation**
   - Use LLM to generate synthetic queries from descriptions
   - Augment index with GPT-generated examples
   - Periodically refresh query examples

---

## Model Selection Notes

**Finding:** All tested models show identical performance (15%/35% for augment, 70%/70% for query).

**Tested Models:**
- gpt-4.1
- gpt-4.1-mini
- gpt-4.1-nano
- gpt-4o-mini
- gpt-5-nano

**Implication:** Use the smallest/cheapest model (gpt-5-nano or gpt-4.1-nano) for ranking without sacrificing accuracy.

**Implementation:**
```python
# Configure via environment variable or request parameter
RANKING_MODEL = os.getenv("LLM_MODEL", "gpt-5-nano")

# Or allow per-request override via meta.model parameter
ranking_model = request_meta.get("model", RANKING_MODEL)
```

---

## References

- WHO Protocol v0.1 Specification
- Azure AI Search Vector Search Documentation
- Azure OpenAI Embeddings API Reference
- Multi-Model Evaluation Report (MULTI_MODEL_EVALUATION_REPORT.md)

---

## Appendix: Example Request/Response

### Request Format
```json
{
  "query": "help me write better emails",
  "meta": {
    "strategy": "query",
    "model": "gpt-5-nano",
    "max_results": 5
  }
}
```

### Augment Strategy Response
```json
{
  "_meta": {
    "response_type": "answer",
    "version": "0.1",
    "result_count": 2
  },
  "results": [
    {
      "protocol": "http",
      "endpoint": "https://augments.example.com/writing-coach",
      "score": 85,
      "definition": {
        "name": "Writing Coach",
        "description": "Focuses on email writing and professional communication",
        "method": "POST",
        "contentType": "application/json"
      }
    },
    {
      "protocol": "http",
      "endpoint": "https://augments.example.com/grammar-checker",
      "score": 72,
      "definition": {
        "name": "Grammar Checker",
        "description": "Checks grammar and spelling in written content",
        "method": "POST",
        "contentType": "application/json"
      }
    }
  ]
}
```

### Query Strategy Response
```json
{
  "_meta": {
    "response_type": "answer",
    "version": "0.1",
    "result_count": 2
  },
  "results": [
    {
      "protocol": "http",
      "endpoint": "https://augments.example.com/writing-coach",
      "score": 95,
      "definition": {
        "name": "Writing Coach",
        "description": "Specializes in email writing and professional communication skills",
        "method": "POST",
        "contentType": "application/json"
      },
      "matched_queries": [
        {"query": "improve my email writing"},
        {"query": "make my emails more professional"},
        {"query": "help with business correspondence"}
      ]
    },
    {
      "protocol": "http",
      "endpoint": "https://augments.example.com/communication-assistant",
      "score": 78,
      "definition": {
        "name": "Communication Assistant",
        "description": "Assists with various forms of professional communication",
        "method": "POST",
        "contentType": "application/json"
      },
      "matched_queries": [
        {"query": "write professional emails"},
        {"query": "communication tips"}
      ]
    }
  ]
}
```

Note the additional `matched_queries` field in the query strategy response, showing which example queries matched the user's query.
