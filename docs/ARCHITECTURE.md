# AgentFinder Architecture Documentation

**Version:** 1.1
**Date:** 2026-03-18
**WHO Protocol Version:** 0.1

## Table of Contents

1. [Overview](#overview)
2. [System Architecture](#system-architecture)
3. [Module Breakdown](#module-breakdown)
4. [Unified Strategy Architecture](#unified-strategy-architecture)
5. [Data Flow](#data-flow)
6. [Design Patterns](#design-patterns)
7. [Performance Characteristics](#performance-characteristics)
8. [Configuration](#configuration)
9. [Deployment](#deployment)

---

## Overview

AgentFinder is a WHO Protocol v0.1 implementation that discovers relevant augments based on natural language queries. It implements a multi-layer architecture separating transport, business logic, and data access concerns.

**What is an Augment?**

An "augment" is any capability that can extend what a user can do. This includes:
- **Traditional agents** (HTTP endpoints that provide services)
- **MCP tools** (Model Context Protocol tools and services)
- **A2A skills** (Agent-to-Agent skills)
- **Anthropic skills** (Claude-specific capabilities)
- **Any future capability type** that might be developed

All an augment needs is an endpoint, a description with metadata, and a few example queries that it can answer.

**Key Features:**
- Dual retrieval strategies (augment-level and query-level)
- **Unified post-retrieval processing** - strategies differ ONLY in retrieval
- LLM-based ranking with multiple model support
- Multi-level caching (embeddings, search, rankings)
- REST and MCP protocol endpoints
- 70% top-1 accuracy with query strategy

**Architectural Principle:**
The system follows a strict separation where **retrieval strategies differ ONLY in how they fetch documents**. All post-retrieval processing (normalization, ranking, filtering, sorting) uses identical unified code, ensuring consistency and eliminating duplication.

---

## System Architecture

### Layer Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Transport Layer                          │
│              (agent_finder.py - 566 lines)                  │
│    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│    │ REST/who     │  │ MCP endpoint │  │ Admin APIs   │   │
│    │ endpoint     │  │              │  │ /health /stats│   │
│    └──────────────┘  └──────────────┘  └──────────────┘   │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────┴──────────────────────────────────┐
│                    Business Logic Layer                     │
│              (who_handler.py - 952 lines)                   │
│    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│    │ Agent        │  │ Query        │  │ UNIFIED      │   │
│    │ Retrieval    │  │ Retrieval +  │  │ Ranking &    │   │
│    │              │  │ Aggregation  │  │ Filtering    │   │
│    └──────────────┘  └──────────────┘  └──────────────┘   │
│                    ↓                 ↓           ↓          │
│              ┌─────────────────────────────────────┐       │
│              │   UNIFIED POST-RETRIEVAL PIPELINE   │       │
│              │  - Normalize documents              │       │
│              │  - Rank in parallel                 │       │
│              │  - Filter by threshold              │       │
│              │  - Sort by score                    │       │
│              └─────────────────────────────────────┘       │
└──────────────────────────┬──────────────────────────────────┘
                           │
           ┌───────────────┴───────────────┐
           ↓                               ↓
┌──────────────────────┐      ┌──────────────────────┐
│   Data Access Layer  │      │  External Services   │
│  search_backend.py   │      │   llm_backend.py     │
└──────────┬───────────┘      └──────────┬───────────┘
           ↓                              ↓
┌──────────────────────┐      ┌──────────────────────┐
│   Azure AI Search    │      │   Azure OpenAI       │
│  - augments-index      │      │  - Embeddings        │
│  - queries-index     │      │  - Chat (ranking)    │
└──────────────────────┘      └──────────────────────┘
```

---

## Module Breakdown

### 1. agent_finder.py (Transport Layer)

**Purpose:** HTTP/Web server exposing WHO handler via REST and MCP endpoints.

**Size:** 566 lines

**Key Components:**
- REST endpoint (`/who`) - WHO Protocol v0.1 implementation
- MCP endpoint (`/mcp`) - Model Context Protocol support
- Static file serving (Web UI, documentation)
- Admin endpoints (`/health`, `/stats`, `/clear_cache`)
- CORS middleware
- Error handling

**What it does NOT do:**
- No business logic
- No caching
- No search or LLM interactions
- No strategy implementation

---

### 2. who_handler.py (Business Logic Layer)

**Purpose:** Core agent discovery logic implementing WHO Protocol business rules with unified post-retrieval processing.

**Size:** 952 lines

**Key Architectural Change (2026-03-18):**
Implemented unified post-retrieval pipeline. Both augment and query strategies now use **identical** code for ranking, filtering, and sorting. Only the retrieval phase differs between strategies.

**Key Components:**

#### TTLCache Class (Lines 41-77)
```python
class TTLCache:
    def __init__(self, max_size: int = 10000, ttl: int = 3600)
    def get(self, key: Any) -> Optional[Any]
    def set(self, key: Any, value: Any)
```

**Features:**
- LRU eviction when at capacity
- Time-to-live expiration
- Move-to-end for cache hits (LRU tracking)

#### WHOHandler Class (Lines 78-840)

**Initialization:**
```python
def __init__(self):
    # Three-tier caching strategy
    self.embedding_cache = {}  # Never expires
    self.search_cache = TTLCache(max_size=10000, ttl=3600)
    self.ranking_cache = TTLCache(max_size=100000, ttl=7200)
```

**Main Query Processing:**
```python
async def process_query(
    self,
    query: str,
    retrieval_strategy: str = "augment",
    ranking_model: Optional[str] = None,
    ...
) -> Dict[str, Any]
```

**Strategy Router:**
```python
if retrieval_strategy == "augment":
    return await self._process_augment_strategy(...)
elif retrieval_strategy == "query":
    return await self._process_query_strategy(...)
```

---

## Unified Strategy Architecture

### Core Principle

**ONLY RETRIEVAL DIFFERS. EVERYTHING ELSE IS UNIFIED.**

Both strategies follow this flow:

```
1. RETRIEVAL (Strategy-specific)
   ├─ Augment Strategy: Search augments-index
   └─ Query Strategy: Search queries-index + Aggregate by augment

2. NORMALIZATION (Unified)
   └─ Convert to standard document format

3. RANKING (Unified)
   └─ Parallel LLM ranking with unified _rank_document()

4. FILTERING (Unified)
   └─ Score threshold filtering

5. SORTING (Unified)
   └─ Sort by score descending

6. RESPONSE (Unified)
   └─ Build WHO Protocol response
```

### Augment Strategy

```python
async def _process_augment_strategy(...) -> Dict[str, Any]:
    # 1. Get embedding (with cache)
    vector = await self.llm_backend.get_embedding(query)

    # 2. Search for augment documents
    raw_augments = await self.search_backend.search(
        query, vector, k=20, strategy="augment"
    )

    # 3. Normalize to standard format
    documents = self._normalize_augment_documents(raw_augments)

    # 4. UNIFIED POST-RETRIEVAL PROCESSING
    return await self._rank_and_build_results(
        query=query,
        documents=documents,
        cache_key=cache_key,
        ...
    )
```

**Normalization Function:**
```python
def _normalize_augment_documents(
    self,
    agent_docs: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Convert augment search results to standard format."""
    normalized = []
    for doc in agent_docs:
        normalized.append({
            "augment_id": doc.get("url"),
            "augment_name": doc.get("name"),
            "augment_url": doc.get("url"),
            "augment_json_ld": doc.get("json_ld", "{}"),
            "augment_description": doc.get("description", ""),
            "matched_queries": []  # Empty for augment strategy
        })
    return normalized
```

### Query Strategy

```python
async def _process_query_strategy(...) -> Dict[str, Any]:
    # 1. Get embedding (with cache)
    vector = await self.llm_backend.get_embedding(query)

    # 2. Search for query documents
    raw_query_docs = await self.search_backend.search(
        query, vector, k=40, strategy="query"
    )

    # 3. Aggregate by augment (produces standard format)
    documents = self._aggregate_by_augment(raw_query_docs)

    # 4. UNIFIED POST-RETRIEVAL PROCESSING (same as augment strategy)
    return await self._rank_and_build_results(
        query=query,
        documents=documents,
        cache_key=cache_key,
        ...
    )
```

**Aggregation Function:**
```python
def _aggregate_by_augment(
    self,
    query_docs: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Group query documents by augment_id.
    Produces same format as _normalize_augment_documents().
    """
    augments = {}
    for doc in query_docs:
        augment_id = doc.get("augment_id")
        if augment_id not in augments:
            augments[augment_id] = {
                "augment_id": augment_id,
                "augment_name": doc.get("augment_name"),
                "augment_url": doc.get("augment_url"),
                "augment_json_ld": doc.get("augment_json_ld", "{}"),
                "augment_description": doc.get("augment_description", ""),
                "matched_queries": []
            }

        # Add matched query
        augments[augment_id]["matched_queries"].append({
            "query": doc.get("query"),
            "detail": doc.get("query_detail", ""),
            "score": doc.get("@search.score", 0)
        })

    return sorted(augments.values(), key=lambda a: a["max_score"], reverse=True)
```

### Unified Post-Retrieval Processing

**The _rank_and_build_results() function handles ALL post-retrieval logic for BOTH strategies:**

```python
async def _rank_and_build_results(
    self,
    query: str,
    documents: List[Dict[str, Any]],  # Normalized format from either strategy
    cache_key: str,
    augment_type: Optional[str],
    result_limit: int,
    stream_callback: Optional[callable] = None
) -> Dict[str, Any]:
    """
    Unified ranking, filtering, and result building logic.
    Used by BOTH augment and query strategies.
    """
    # 1. Rank documents in parallel
    ranking_tasks = []
    for doc in documents:
        rank_cache_key = (cache_key, doc["augment_id"])
        cached_ranking = self.ranking_cache.get(rank_cache_key)
        if cached_ranking is None:
            ranking_tasks.append(self._rank_document(query, doc, rank_cache_key))

    # Execute ranking tasks
    if ranking_tasks:
        for completed_task in asyncio.as_completed(ranking_tasks):
            await completed_task

    # 2. Collect and filter results
    final_results = []
    for doc in documents:
        rank_cache_key = (cache_key, doc["augment_id"])
        ranking = self.ranking_cache.get(rank_cache_key)

        if ranking and ranking["score"] > SETTINGS["score_threshold"]:
            # Build augment object
            augment = {
                "url": doc.get("augment_url"),
                "name": doc["augment_name"],
                "json_ld": doc.get("augment_json_ld", "{}"),
                "description": doc.get("augment_description", "")
            }

            result = self._build_result_object(augment, ranking, schema_type)

            # Add matched queries if present (query strategy only)
            if doc.get("matched_queries"):
                result["matched_queries"] = [
                    {"query": mq["query"], "score": mq.get("score", 0)}
                    for mq in doc["matched_queries"][:3]
                ]

            final_results.append(result)

    # 3. Sort by score and return top results
    final_results.sort(key=lambda x: x["score"], reverse=True)
    top_results = final_results[:result_limit]

    return self._build_response(top_results)
```

### Unified Ranking Function

**The _rank_document() function handles ranking for BOTH strategies:**

```python
async def _rank_document(
    self,
    query: str,
    doc: Dict[str, Any],
    cache_key: Tuple
) -> bool:
    """
    Unified ranking function for both strategies.
    Automatically detects whether to use augment description or matched queries.
    """
    try:
        # Build ranking context based on available information
        if doc.get("matched_queries"):
            # Query strategy: use matched queries as context
            context = {
                "name": doc["augment_name"],
                "matched_capabilities": [
                    {
                        "capability": mq["query"],
                        "description": mq.get("detail", "")
                    }
                    for mq in doc["matched_queries"][:5]
                ]
            }
            ranking_input = json.dumps(context, indent=2)
        else:
            # Augment strategy: use description as context
            ranking_input = doc.get("augment_description", "") or doc.get("augment_json_ld", "{}")

        # Get ranking from LLM
        ranking = await self.llm_backend.rank_augment(query, ranking_input)

        # Cache the result
        self.ranking_cache.set(cache_key, ranking)

        return True
    except Exception as e:
        return False
```

**Key Design Points:**
1. Single ranking function for both strategies
2. Automatically detects document type (matched_queries vs description)
3. Uses same LLM prompt structure for both
4. Unified caching mechanism
5. Consistent error handling

---

## Data Flow

### Complete Request Flow (Query Strategy)

```
1. HTTP Request
   POST /who
   {"query": "help me write better", "meta": {"strategy": "query"}}

   ↓

2. agent_finder.py → who_handler.who_query()

   ↓

3. WHO Handler (Business Logic)
   - Generate embedding
   - Cache key: hash("help me write better_query_strategy")

   ↓

4. Check search_cache → MISS

   ↓

5. search_backend.search(strategy="query")
   - Vector search queries-index (k=40)

   ↓

6. Azure AI Search returns 40 query documents

   ↓

7. Cache search results (TTL: 3600s)

   ↓

8. _aggregate_by_augment()
   - Groups 40 queries by augment_id
   - Produces ~10 unique augments with matched_queries

   ↓

9. _rank_and_build_results() [UNIFIED PIPELINE]
   - Creates ranking tasks for each augment
   - Calls _rank_document() in parallel

   ↓

10. _rank_document() for each augment [UNIFIED RANKING]
    - Detects matched_queries present
    - Builds context from matched queries
    - Calls Azure OpenAI for ranking
    - Caches result

    ↓

11. Filter by score threshold (>= 64)

    ↓

12. Sort by score descending

    ↓

13. Build WHO Protocol response

    ↓

14. Return to agent_finder.py

    ↓

15. HTTP Response
```

### Cache Hit Scenario

```
Second request with same query:

1-3. Same as above

   ↓

4. Check search_cache → HIT!
   - Skip Azure Search call
   - Return cached 40 query documents

   ↓

7. _aggregate_by_augment() (same 10 augments)

   ↓

8. _rank_and_build_results()
   - Check ranking_cache for each augment
   - ALL HIT! Skip Azure OpenAI calls

   ↓

9-13. Same as above

Total latency: ~10-50ms (cache-only)
```

---

## Design Patterns

### 1. Unified Processing Pattern (NEW)

**Implementation:**
```python
# Both strategies converge to same processing
documents = normalize_or_aggregate(raw_results)
return await _rank_and_build_results(documents)
```

**Benefits:**
- No code duplication
- Consistent behavior across strategies
- Single source of truth for ranking logic
- Easier testing and maintenance
- Bug fixes apply to both strategies

### 2. Strategy Pattern

**Implementation:**
```python
if retrieval_strategy == "augment":
    return await self._process_augment_strategy(...)
elif retrieval_strategy == "query":
    return await self._process_query_strategy(...)
```

**Benefits:**
- Clear separation of retrieval logic
- Easy to add new retrieval strategies
- Strategies share unified infrastructure

### 3. Document Normalization Pattern (NEW)

**Implementation:**
Both strategies produce documents in identical format:
```python
{
    "augment_id": str,
    "augment_name": str,
    "augment_url": str,
    "augment_json_ld": str,
    "augment_description": str,
    "matched_queries": List[Dict]  # Empty for augment strategy
}
```

**Benefits:**
- Unified pipeline can process any document
- Type-safe downstream processing
- Easy to add new document sources

### 4. Three-Tier Caching

**Implementation:**
```python
# Tier 1: Embeddings (never expires)
self.embedding_cache = {}

# Tier 2: Search results (1 hour TTL)
self.search_cache = TTLCache(max_size=10000, ttl=3600)

# Tier 3: Rankings (2 hour TTL)
self.ranking_cache = TTLCache(max_size=100000, ttl=7200)
```

**Benefits:**
- Different TTLs for different volatility
- Larger cache for most granular data (rankings)
- Embeddings cached forever (deterministic)

### 5. Connection Pooling

**Implementation:**
```python
# 100 clients for parallel ranking
for i in range(100):
    client = AsyncAzureOpenAI(...)
    self.clients.append(client)

# Round-robin selection
client = next(self.client_cycle)
```

**Benefits:**
- Support 1000+ concurrent calls
- Distribute load
- Handle timeouts gracefully

---

## Performance Characteristics

### Latency Breakdown

**Cold Cache (First Query):**
```
Total: ~1500-2000ms
- Embedding generation: 100-200ms
- Vector search: 50-100ms
- Aggregation: 1-5ms
- LLM ranking (parallel 10 augments): 1000-1500ms
- Response building: 1-5ms
```

**Warm Cache (Repeat Query):**
```
Total: ~10-50ms
- Cache lookup: 1-5ms
- Response building: 1-5ms
```

### Accuracy Metrics

**Augment Strategy:**
- Top-1: 15%
- Top-3: 35%

**Query Strategy:**
- Top-1: 70%
- Top-3: 70%

---

## Configuration

### Environment Variables

**Server:**
```bash
WHO_SERVER_PORT=8080
WHO_SERVER_HOST=0.0.0.0
```

**Search Backend:**
```bash
SEARCH_PROVIDER=azure
AZURE_SEARCH_ENDPOINT=https://yoursearch.search.windows.net
AZURE_SEARCH_API_KEY=your_key
```

**LLM Backend:**
```bash
LLM_PROVIDER=azure_openai
LLM_ENDPOINT=https://yourinstance.openai.azure.com
LLM_API_KEY=your_key
LLM_MODEL=gpt-4.1
LLM_EMBEDDING_MODEL=text-embedding-3-large
```

**WHO Handler:**
```bash
WHO_SCORE_THRESHOLD=64
WHO_MAX_RESULTS=10
WHO_SEARCH_TOP_K=30
```

---

## Deployment

### Azure Web App

```bash
# 1. Create deployment package
zip -r deploy.zip code/ data/ -x "*.pyc" "*__pycache__*" "*.log"

# 2. Deploy
az webapp deployment source config-zip \
  --resource-group NLWeb_v0.1 \
  --name agentfinder2 \
  --src deploy.zip
```

---

## Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-03-17 | System | Initial architecture documentation |
| 1.1 | 2026-03-18 | System | Updated for unified post-retrieval architecture |

