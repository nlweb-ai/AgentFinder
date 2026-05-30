# AgentFinder Architecture Documentation

**Version:** 1.0
**Date:** 2026-03-17
**WHO Protocol Version:** 0.1

## Table of Contents

1. [Overview](#overview)
2. [System Architecture](#system-architecture)
3. [Module Breakdown](#module-breakdown)
4. [Data Flow](#data-flow)
5. [Design Patterns](#design-patterns)
6. [Performance Characteristics](#performance-characteristics)
7. [Configuration](#configuration)
8. [Deployment](#deployment)

---

## Overview

AgentFinder is a WHO Protocol v0.1 implementation that discovers relevant agents based on natural language queries. It implements a multi-layer architecture separating transport, business logic, and data access concerns.

**Key Features:**
- Dual retrieval strategies (agent-level and query-level)
- LLM-based ranking with multiple model support
- Multi-level caching (embeddings, search, rankings)
- REST and MCP protocol endpoints
- 70% top-1 accuracy with query strategy

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
│              (who_handler.py - 882 lines)                   │
│    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│    │ Agent        │  │ Query        │  │ Caching      │   │
│    │ Strategy     │  │ Strategy     │  │ LRU + TTL    │   │
│    └──────────────┘  └──────────────┘  └──────────────┘   │
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
│  - agents-index      │      │  - Embeddings        │
│  - queries-index     │      │  - Chat (ranking)    │
└──────────────────────┘      └──────────────────────┘
```

---

## Module Breakdown

### 1. agent_finder.py (Transport Layer)

**Purpose:** HTTP/Web server exposing WHO handler via REST and MCP endpoints.

**Size:** 566 lines

**Key Components:**

#### REST Endpoint (Lines 27-152)
```python
async def who_endpoint(request: web.Request) -> web.Response
```

**Responsibilities:**
- Parse HTTP requests (GET/POST)
- Extract query parameters (query, strategy, model)
- Support both WHO Protocol v0.1 and legacy format
- Call `who_handler.who_query()`
- Return JSON responses per WHO Protocol spec

**Request Format:**
```json
{
  "query": "natural language query",
  "meta": {
    "strategy": "query",
    "model": "gpt-5-nano",
    "max_results": 10
  }
}
```

**Response Format:**
```json
{
  "_meta": {
    "response_type": "answer",
    "version": "0.1",
    "result_count": 3
  },
  "results": [...]
}
```

#### MCP Endpoint (Lines 153-377)
```python
async def mcp_endpoint(request: web.Request) -> web.Response
```

**Responsibilities:**
- Implement Model Context Protocol (MCP) 2024-11-05
- Expose WHO handler as MCP tool
- Handle MCP tool call format
- Wrap WHO Protocol responses in MCP format

**MCP Tool Definition:**
```json
{
  "name": "who",
  "description": "Find agents that can answer a query",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {"type": "string"},
      "strategy": {"type": "string", "enum": ["agent", "query"]}
    }
  }
}
```

#### Static File Serving (Lines 378-417)
- `serve_html_file()` - Generic HTML file server
- `index_page()` - Serves [code/index.html](code/index.html)
- `evaluation_report()` - Serves [code/evaluation_report.html](code/evaluation_report.html)

#### Admin Endpoints (Lines 420-456)
- `/health` - Server health check with statistics
- `/stats` - Query processing statistics
- `/clear_cache` - Cache management endpoint

#### Middleware (Lines 458-487)
- CORS support for cross-origin requests
- Global error handling with graceful degradation

#### Server Lifecycle (Lines 489-566)
- `startup()` - Initialize WHO handler
- `cleanup()` - Graceful shutdown
- `create_app()` - Configure aiohttp application
- `main()` - Entry point

**Dependencies:**
- `aiohttp` for web server
- `who_handler` module (business logic)

**What it does NOT do:**
- No business logic
- No caching
- No search or LLM interactions
- No strategy implementation

---

### 2. who_handler.py (Business Logic Layer)

**Purpose:** Core agent discovery logic implementing WHO Protocol business rules.

**Size:** 882 lines

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

#### WHOHandler Class (Lines 78-810)

**Initialization (Lines 81-112):**
```python
def __init__(self):
    # Three-tier caching strategy
    self.embedding_cache = {}  # Never expires
    self.search_cache = TTLCache(max_size=10000, ttl=3600)
    self.ranking_cache = TTLCache(max_size=100000, ttl=7200)

    # Statistics tracking
    self.stats = {
        "queries_processed": 0,
        "cache_hits": 0,
        "cache_misses": 0,
        "avg_latency_ms": 0
    }
```

**Main Query Processing (Lines 115-148):**
```python
async def process_query(
    self,
    query: str,
    retrieval_strategy: str = "agent",
    ranking_model: Optional[str] = None,
    ...
) -> Dict[str, Any]
```

**Responsibilities:**
- Validate input parameters
- Route to appropriate strategy
- Handle errors gracefully
- Return WHO Protocol response

**Strategy Router:**
```python
if retrieval_strategy == "agent":
    return await self._process_agent_strategy(...)
elif retrieval_strategy == "query":
    return await self._process_query_strategy(...)
```

#### Agent Strategy (Lines 150-270)
```python
async def _process_agent_strategy(
    self,
    query: str,
    vector: List[float],
    cache_key: str,
    max_results: int,
    ranking_model: Optional[str]
) -> Dict[str, Any]
```

**Algorithm:**
1. Generate query embedding
2. Check search cache
3. Vector search against `agents-index` (k=20)
4. Cache search results
5. Rank each agent with LLM (parallel)
6. Filter by score threshold (default: 64)
7. Sort by score descending
8. Return top N results

**Performance:**
- Top-1 Accuracy: 15%
- Top-3 Accuracy: 35%
- Latency: ~1-2 seconds

**Why Lower Accuracy:**
- Semantic mismatch between agent descriptions and queries
- Agent descriptions don't capture all use cases
- Poor ranking (correct agent found but ranked low)

#### Query Strategy (Lines 272-417)
```python
async def _process_query_strategy(
    self,
    query: str,
    vector: List[float],
    cache_key: str,
    max_results: int,
    ranking_model: Optional[str]
) -> Dict[str, Any]
```

**Algorithm:**
1. Generate query embedding
2. Check search cache
3. Vector search against `queries-index` (k=40, 2x agent strategy)
4. Cache search results
5. **Aggregate query documents by agent_id**
6. Rank aggregated agents with LLM (parallel)
7. Filter by score threshold (default: 64)
8. Sort by score descending
9. Return top N results with matched queries

**Aggregation Step (Lines 419-480):**
```python
def _aggregate_by_agent(
    self,
    query_docs: List[Dict[str, Any]]
) -> List[Dict[str, Any]]
```

**Critical Logic:**
- Groups multiple query examples by `agent_id`
- Creates single agent object per unique agent
- Collects all matched queries in `matched_queries` array
- Preserves agent metadata (name, description, endpoint)

**Example:**
```python
Input: [
    {query: "write email", agent_id: "A1", agent_name: "Writer"},
    {query: "check grammar", agent_id: "A1", agent_name: "Writer"},
    {query: "schedule meeting", agent_id: "A2", agent_name: "Calendar"}
]

Output: [
    {
        agent_id: "A1",
        agent_name: "Writer",
        matched_queries: [
            {query: "write email"},
            {query: "check grammar"}
        ]
    },
    {
        agent_id: "A2",
        agent_name: "Calendar",
        matched_queries: [
            {query: "schedule meeting"}
        ]
    }
]
```

**Performance:**
- Top-1 Accuracy: 70%
- Top-3 Accuracy: 70%
- Latency: ~1-2 seconds

**Why Better Accuracy:**
- Example queries better represent agent capabilities
- Query-to-query similarity more effective than query-to-description
- Perfect ranking: when right agent found, always at position 1

#### Ranking (Lines 482-526, 748-780)
```python
async def _rank_aggregated_agent(
    self,
    query: str,
    agent: Dict[str, Any],
    cache_key: Tuple
) -> bool
```

**Responsibilities:**
- Check ranking cache first
- Call LLM backend with agent description
- Parse JSON response (score 0-100, description)
- Handle errors gracefully (return score 0)
- Cache ranking results

**LLM Prompt:**
```
Assign a score between 0 and 100 to the following agent based on
the likelihood that the agent will contain an answer to the user's question.

First think about the kind of thing the user is seeking and then verify
that the agent is primarily focused on that kind of thing.

The user's question is: {query}

The agent's description is:
{agent_description}

Return JSON only with this exact format:
{"score": <integer 0-100>, "description": "<one sentence explanation>"}
```

**Ranking Configuration:**
- Temperature: 0 (deterministic)
- Max tokens: 100
- Response format: JSON object
- Timeout: 8 seconds
- Retry: 1 attempt

#### Response Building (Lines 553-725)

**Result Object (Lines 553-594):**
```python
def _build_result_object(
    self,
    site: Dict[str, Any],
    ranking: Dict[str, Any],
    schema_type: str
) -> Dict[str, Any]
```

Returns WHO Protocol result structure:
```json
{
  "protocol": "http",
  "endpoint": "https://agent.example.com/endpoint",
  "score": 85,
  "definition": {
    "name": "Agent Name",
    "description": "LLM ranking description",
    "method": "POST",
    "contentType": "application/json"
  },
  "matched_queries": [
    {"query": "example query 1"},
    {"query": "example query 2"}
  ]
}
```

**WHO Protocol Response (Lines 700-725):**
```python
def _build_response(
    self,
    results: List[Dict[str, Any]],
    referrals: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]
```

Returns:
```json
{
  "_meta": {
    "response_type": "answer",
    "version": "0.1",
    "result_count": 3
  },
  "results": [...],
  "referrals": [...]  // Optional
}
```

#### Module-Level API (Lines 813-882)

**Singleton Handler:**
```python
async def get_handler() -> WHOHandler
```
- Lazy initialization
- Ensures single handler instance
- Thread-safe with global variable

**Public API:**
```python
async def who_query(
    query: str,
    augment_type: Optional[str] = None,
    domain: Optional[str] = None,
    max_results: Optional[int] = None,
    retrieval_strategy: str = "agent",
    ranking_model: Optional[str] = None
) -> Dict[str, Any]
```

**Utility Functions:**
```python
async def get_stats() -> Dict[str, Any]
async def clear_caches()
async def cleanup()
```

**What it does NOT do:**
- No HTTP/web server code
- No request parsing or endpoint routing
- No direct Azure SDK calls (delegates to backends)

**Dependencies:**
- `search_backend` for Azure AI Search
- `llm_backend` for Azure OpenAI

---

### 3. search_backend.py (Data Access Layer)

**Purpose:** Abstract interface to vector search backends (Azure AI Search, Qdrant).

**Key Functions:**
```python
async def search(
    query: str,
    vector: List[float],
    top_k: int,
    strategy: str = "agent"
) -> List[Dict[str, Any]]
```

**Responsibilities:**
- Switch between `agents-index` and `queries-index` based on strategy
- Execute Azure AI Search vector queries
- Handle errors and timeouts
- Return normalized results

**Azure Search Configuration:**
- API Version: 2024-05-01-preview
- Vector field: `descriptionVector` (agent) or `queryVector` (query)
- Similarity: Cosine
- Dimensions: 1536 (text-embedding-3-large)

---

### 4. llm_backend.py (External Services Layer)

**Purpose:** Abstract interface to LLM providers (Azure OpenAI, OpenAI, Anthropic).

**Key Functions:**
```python
async def get_embedding(text: str) -> List[float]

async def rank_agent(
    query: str,
    agent_description: str,
    model: Optional[str] = None
) -> Dict[str, Any]
```

**Connection Pooling:**
- 100 AsyncAzureOpenAI clients
- Round-robin distribution
- Max 1000 concurrent calls
- 8 second timeout per call

**Model Support:**
- gpt-4.1
- gpt-4.1-mini
- gpt-4.1-nano
- gpt-4o-mini
- gpt-5-nano

**Model Override:**
```python
# Use provided model or fall back to configured model
model_to_use = model if model else LLM_CONFIG["model"]
```

**Performance Finding:** All models show identical accuracy (15%/70%), so use cheapest (gpt-5-nano).

---

## Data Flow

### Complete Request Flow

```
1. HTTP Request arrives
   POST /who
   {"query": "help me write better", "meta": {"strategy": "query"}}

   ↓

2. agent_finder.py (Transport Layer)
   - Parse JSON request
   - Extract query="help me write better"
   - Extract strategy="query"
   - Extract model=None (use default)

   ↓

3. Call who_handler.who_query()

   ↓

4. who_handler.py (Business Logic)
   - Generate embedding via llm_backend
   - Cache key: hash("help me write better", "query")

   ↓

5. Check search_cache
   - MISS: Cache empty for this query

   ↓

6. search_backend.search()
   - strategy="query" → use queries-index
   - Vector search with k=40

   ↓

7. Azure AI Search
   POST /indexes/queries-index/docs/search
   {
     "vectorQueries": [{
       "vector": [0.123, -0.456, ...],  // 1536 dims
       "k": 40,
       "fields": "queryVector"
     }]
   }

   Returns 40 query documents:
   [
     {query: "improve emails", agent_id: "writer-001", agent_name: "Writing Coach"},
     {query: "check grammar", agent_id: "writer-001", agent_name: "Writing Coach"},
     {query: "write report", agent_id: "writer-001", agent_name: "Writing Coach"},
     {query: "schedule meeting", agent_id: "calendar-002", agent_name: "Calendar Bot"},
     ...
   ]

   ↓

8. Cache search results
   - search_cache.set(cache_key, results)
   - TTL: 3600 seconds

   ↓

9. Aggregate by agent_id
   - writer-001: 3 matched queries
   - calendar-002: 1 matched query
   - Result: 2 unique agents

   ↓

10. Parallel LLM Ranking
    For each agent:
    - Check ranking_cache with key (cache_key, agent_id)
    - MISS: Call Azure OpenAI

    Task 1: Rank writer-001
    Task 2: Rank calendar-002

    ↓

11. Azure OpenAI (2 parallel calls)
    POST /chat/completions
    {
      "model": "gpt-4.1",
      "messages": [{
        "role": "user",
        "content": "Assign a score... [prompt] ...Writing Coach description..."
      }],
      "response_format": {"type": "json_object"},
      "temperature": 0,
      "max_tokens": 100
    }

    Response 1: {"score": 95, "description": "Highly relevant for writing improvement"}
    Response 2: {"score": 25, "description": "Not relevant, focuses on scheduling"}

    ↓

12. Cache rankings
    - ranking_cache.set((cache_key, "writer-001"), {"score": 95, ...})
    - ranking_cache.set((cache_key, "calendar-002"), {"score": 25, ...})
    - TTL: 7200 seconds

    ↓

13. Filter by threshold
    - writer-001: score 95 > threshold 64 ✓
    - calendar-002: score 25 ≤ threshold 64 ✗

    Result: [writer-001]

    ↓

14. Sort by score (already sorted in this case)

    ↓

15. Build WHO Protocol response
    {
      "_meta": {
        "response_type": "answer",
        "version": "0.1",
        "result_count": 1
      },
      "results": [{
        "protocol": "http",
        "endpoint": "https://agents.example.com/writing-coach",
        "score": 95,
        "definition": {
          "name": "Writing Coach",
          "description": "Highly relevant for writing improvement",
          "method": "POST",
          "contentType": "application/json"
        },
        "matched_queries": [
          {"query": "improve emails"},
          {"query": "check grammar"},
          {"query": "write report"}
        ]
      }]
    }

    ↓

16. Return to agent_finder.py

    ↓

17. Format HTTP response
    HTTP 200 OK
    Content-Type: application/json

    {response body}

    ↓

18. Send to client
```

### Cache Hit Scenario

```
Second request with same query:

1. HTTP Request: {"query": "help me write better", "meta": {"strategy": "query"}}

   ↓

2-4. Same as above

   ↓

5. Check search_cache
   - HIT! Return cached 40 query documents
   - Skip steps 6-7 (no Azure Search call)

   ↓

9. Aggregate by agent_id (same 2 agents)

   ↓

10. Check ranking_cache for each agent
    - writer-001: HIT! Return cached {"score": 95, ...}
    - calendar-002: HIT! Return cached {"score": 25, ...}
    - Skip step 11 (no Azure OpenAI calls)

    ↓

13-18. Same as above

Total latency: ~10-50ms (cache-only, no external calls)
```

---

## Design Patterns

### 1. Layered Architecture

**Separation of Concerns:**
- Transport Layer: HTTP/MCP protocol handling
- Business Logic: Agent discovery algorithms
- Data Access: Backend abstraction
- External Services: LLM and search providers

**Benefits:**
- Independent testing of each layer
- Easy to swap implementations (e.g., Qdrant instead of Azure Search)
- Protocol-agnostic business logic
- Clear dependencies

### 2. Strategy Pattern

**Implementation:**
```python
if retrieval_strategy == "agent":
    return await self._process_agent_strategy(...)
elif retrieval_strategy == "query":
    return await self._process_query_strategy(...)
```

**Benefits:**
- Easy to add new strategies
- Strategies share common infrastructure (caching, ranking)
- Can A/B test strategies
- Clear separation of algorithm logic

### 3. Singleton Pattern

**Handler Instance:**
```python
_handler_instance = None

async def get_handler() -> WHOHandler:
    global _handler_instance
    if _handler_instance is None:
        _handler_instance = WHOHandler()
        await _handler_instance.initialize()
    return _handler_instance
```

**Benefits:**
- Single connection pool across requests
- Shared caches
- Reduced initialization overhead

### 4. Caching Strategy

**Three-Tier Cache:**
```python
# Tier 1: Embeddings (never expires)
self.embedding_cache = {}

# Tier 2: Search results (1 hour TTL)
self.search_cache = TTLCache(max_size=10000, ttl=3600)

# Tier 3: Rankings (2 hour TTL, largest)
self.ranking_cache = TTLCache(max_size=100000, ttl=7200)
```

**Benefits:**
- Embeddings are stable and expensive to generate
- Search results change less frequently than rankings
- Rankings are query+agent specific, need larger cache
- Different TTLs balance freshness vs. performance

### 5. Connection Pooling

**LLM Backend:**
```python
# Create 100 clients for parallel ranking
for i in range(100):
    client = AsyncAzureOpenAI(...)
    self.clients.append(client)

# Round-robin selection
self.client_cycle = itertools.cycle(self.clients)
client = next(self.client_cycle)
```

**Benefits:**
- Support 1000+ concurrent ranking calls
- Avoid connection overhead
- Distribute load across clients
- Handle timeouts gracefully

### 6. Parallel Processing

**Ranking All Agents:**
```python
# Create ranking tasks
tasks = []
for agent in agents:
    task = self._rank_agent(query, agent)
    tasks.append(task)

# Execute in parallel
results = await asyncio.gather(*tasks, return_exceptions=True)
```

**Benefits:**
- Rank 10-20 agents in same time as 1 agent
- Reduces total latency from 10-20s to 1-2s
- Handles failures gracefully (exceptions caught)

---

## Performance Characteristics

### Latency Breakdown

**Cold Cache (First Query):**
```
Total: ~1500-2000ms
- Embedding generation: 100-200ms
- Vector search: 50-100ms
- Aggregation: 1-5ms
- LLM ranking (parallel 10 agents): 1000-1500ms
- Response building: 1-5ms
```

**Warm Cache (Repeat Query):**
```
Total: ~10-50ms
- Cache lookup: 1-5ms
- Response building: 1-5ms
```

**Partial Cache (New query, some agents seen):**
```
Total: 500-1000ms
- Embedding generation: 100-200ms
- Vector search (cached): 1-5ms
- Aggregation: 1-5ms
- LLM ranking (5 new agents): 500-800ms
- Response building: 1-5ms
```

### Cache Hit Rates

**After Warmup (100 queries):**
- Embedding cache: ~60% hits
- Search cache: ~65% hits
- Ranking cache: ~70% hits

**Combined Cache Effect:**
- Cold: 0% (all miss)
- Warm: 95%+ (all hit)
- Typical: 70-80% (mixed)

### Accuracy Metrics

**Agent Strategy:**
- Top-1: 15%
- Top-3: 35%
- Improvement: 2.33x from top-1 to top-3

**Query Strategy:**
- Top-1: 70%
- Top-3: 70%
- Improvement: None (perfect initial ranking)

**Interpretation:**
- Agent strategy finds correct agents but ranks poorly
- Query strategy has excellent ranking when agent is found
- Query strategy 30% failure is retrieval issue, not ranking

### Resource Usage

**Memory:**
- Embedding cache: ~100MB (10K embeddings × 6KB each)
- Search cache: ~50MB (10K results × 5KB each)
- Ranking cache: ~200MB (100K rankings × 2KB each)
- Total: ~350MB cache overhead

**Network:**
- Per query (cold): 2-3 KB upload, 10-20 KB download
- Per query (warm): <1 KB upload, <5 KB download

**Compute:**
- CPU: Low (mostly I/O bound)
- Connections: 100 HTTP clients to Azure OpenAI

---

## Configuration

### Environment Variables

**Server Configuration:**
```bash
WHO_SERVER_PORT=8080           # Default: 8080
WHO_SERVER_HOST=0.0.0.0        # Default: 0.0.0.0
```

**Search Backend:**
```bash
SEARCH_PROVIDER=azure          # azure or qdrant
AZURE_SEARCH_ENDPOINT=https://yoursearch.search.windows.net
AZURE_SEARCH_API_KEY=your_key
SEARCH_INDEX=agents-index      # Default index name
```

**LLM Backend:**
```bash
LLM_PROVIDER=azure_openai      # azure_openai, openai, anthropic
LLM_ENDPOINT=https://yourinstance.openai.azure.com
LLM_API_KEY=your_key
LLM_MODEL=gpt-4.1              # Default ranking model
LLM_EMBEDDING_MODEL=text-embedding-3-large
LLM_MAX_CONCURRENT=1000        # Max parallel ranking calls
LLM_API_VERSION=2024-02-01
```

**WHO Handler Settings:**
```bash
WHO_SCORE_THRESHOLD=64         # Min score to include (0-100)
WHO_MAX_RESULTS=10             # Default max results
WHO_SEARCH_TOP_K=30            # Docs to retrieve from search
WHO_CACHE_TTL=3600             # Search cache TTL (seconds)
WHO_MAX_CACHE_ENTRIES=10000    # Search cache size
WHO_RANKING_CACHE_ENTRIES=100000  # Ranking cache size
```

### Index Configuration

**agents-index Schema:**
```json
{
  "fields": [
    {"name": "agent_id", "type": "Edm.String", "key": true},
    {"name": "agent_name", "type": "Edm.String"},
    {"name": "description", "type": "Edm.String"},
    {"name": "endpoint", "type": "Edm.String"},
    {"name": "protocol", "type": "Edm.String"},
    {"name": "method", "type": "Edm.String"},
    {"name": "contentType", "type": "Edm.String"},
    {
      "name": "descriptionVector",
      "type": "Collection(Edm.Single)",
      "dimensions": 1536,
      "vectorSearchProfile": "vector-profile"
    }
  ]
}
```

**queries-index Schema:**
```json
{
  "fields": [
    {"name": "query_id", "type": "Edm.String", "key": true},
    {"name": "query", "type": "Edm.String"},
    {"name": "agent_id", "type": "Edm.String"},
    {"name": "agent_name", "type": "Edm.String"},
    {"name": "agent_description", "type": "Edm.String"},
    {"name": "endpoint", "type": "Edm.String"},
    {"name": "protocol", "type": "Edm.String"},
    {"name": "method", "type": "Edm.String"},
    {"name": "contentType", "type": "Edm.String"},
    {
      "name": "queryVector",
      "type": "Collection(Edm.Single)",
      "dimensions": 1536,
      "vectorSearchProfile": "vector-profile"
    }
  ]
}
```

---

## Deployment

### Local Development

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set environment variables
source set_keys.sh

# 3. Run server
python3 code/agent_finder.py
```

**Endpoints:**
- REST: http://localhost:8080/who
- MCP: http://localhost:8080/mcp
- Health: http://localhost:8080/health
- Web UI: http://localhost:8080/

### Azure Web App

```bash
# 1. Create app service plan
az appservice plan create \
  --name agentfinder-plan \
  --resource-group your-rg \
  --sku B1 \
  --is-linux

# 2. Create web app
az webapp create \
  --name agentfinder2 \
  --resource-group your-rg \
  --plan agentfinder-plan \
  --runtime "PYTHON:3.11"

# 3. Configure app settings
az webapp config appsettings set \
  --name agentfinder2 \
  --resource-group your-rg \
  --settings \
    LLM_ENDPOINT="https://yourinstance.openai.azure.com" \
    LLM_API_KEY="your_key" \
    AZURE_SEARCH_ENDPOINT="https://yoursearch.search.windows.net" \
    AZURE_SEARCH_API_KEY="your_key"

# 4. Deploy code
zip -r deploy.zip code/ requirements.txt
az webapp deploy \
  --name agentfinder2 \
  --resource-group your-rg \
  --src-path deploy.zip \
  --type zip
```

### Production Checklist

- [ ] Set `WHO_SCORE_THRESHOLD` appropriately (64 recommended)
- [ ] Configure Application Insights for monitoring
- [ ] Set up health check endpoint monitoring
- [ ] Enable Azure CDN for static assets
- [ ] Configure auto-scaling rules
- [ ] Set up log analytics workspace
- [ ] Enable CORS for allowed origins
- [ ] Configure Azure Key Vault for secrets
- [ ] Set up backup search service (failover)
- [ ] Monitor cache hit rates and adjust sizes
- [ ] Set up alerts for error rates
- [ ] Configure rate limiting if needed

---

## Related Documentation

- [RETRIEVAL_STRATEGIES_SPECIFICATION.md](RETRIEVAL_STRATEGIES_SPECIFICATION.md) - Detailed strategy algorithms
- [MULTI_MODEL_EVALUATION_REPORT.md](MULTI_MODEL_EVALUATION_REPORT.md) - Performance evaluation results
- [README.md](README.md) - Getting started guide
- [who_protocol.md](who_protocol.md) - WHO Protocol v0.1 specification

---

## Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-03-17 | System | Initial architecture documentation |
