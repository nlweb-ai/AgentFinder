# ⚠️ CRITICAL: DO NOT BREAK THESE CORE FEATURES ⚠️

**READ THIS BEFORE MAKING ANY CHANGES TO THE CODEBASE**

This document exists because the system has been broken MULTIPLE times by well-intentioned refactoring that didn't understand the critical dependencies between code and Azure infrastructure.

## History of Breakages (Learn From These Mistakes)

### 2026-03-20: Terminology Rename Broke Everything

**What happened:**
- Code was renamed from "agent" → "augment" terminology
- Azure Search indices were NOT updated to match
- Result: **COMPLETE SYSTEM FAILURE** - 0 results returned

**Bugs introduced:**

1. **Index Name Mismatch (Bug #1)**
   - Code searched: `"augments-index"`
   - Actual Azure index: `"agents-index"`
   - Impact: Augment strategy returned 0 results
   - Fix: Changed line 101 in search_backend.py to use `"agents-index"`

2. **Field Name Mismatch (Bug #2)**
   - Code requested: `augment_id`, `augment_name`, `augment_url`, `augment_json_ld`
   - Actual Azure fields: `agent_id`, `agent_name`, `agent_url`, `agent_json_ld`
   - Impact: Query strategy retrieved documents but all metadata was empty/null
   - Fix: Lines 117 and 148-154 in search_backend.py now map `agent_*` → `augment_*`

**Lesson:** NEVER rename anything without checking Azure Search indices first!

### 2026-03-20: Streaming Was Removed

**What happened:**
- Refactoring unified the ranking pipeline
- Streaming support existed but was neutered
- Code collected ALL results before streaming them
- Impact: User had to wait for all rankings to complete before seeing ANY results

**The Problem:**
```python
# WRONG - This defeats streaming!
for completed_task in asyncio.as_completed(ranking_tasks):
    await completed_task  # Just wait...

# Then later, after ALL are done:
for result in final_results:
    await stream_callback(result)  # Too late!
```

**The Fix:**
```python
# CORRECT - Stream as each completes
for completed_task in asyncio.as_completed(ranking_tasks):
    await completed_task

    # IMMEDIATELY check if this completion produced a streamable result
    for doc in documents:
        if ranking_ready(doc) and not doc.get("_streamed"):
            result = build_result(doc)
            await stream_callback(result)  # Stream NOW!
            doc["_streamed"] = True
```

**Lesson:** Streaming means results arrive INCREMENTALLY, not "all at once then iterate"!

---

## Critical Infrastructure Dependencies

### Azure Search Indices (DO NOT RENAME WITHOUT UPDATING AZURE!)

**af1.search.windows.net** has TWO indices:

#### 1. agents-index (Augment Strategy)
```
Index Name: "agents-index"  ← NOT "augments-index"!
Document Count: 5,237
Vector Field: "embedding" (1536 dims)  ← NOT "descriptionVector"!

Schema:
  - id (key)
  - url
  - name
  - json_ld
  - description
  - embedding (vector field)
```

**Code Location:** `search_backend.py` line 101
```python
# CORRECT - matches Azure
index_name = "agents-index"

# WRONG - will break everything
index_name = "augments-index"  # ❌ This index doesn't exist!
```

#### 2. queries-index (Query Strategy)
```
Index Name: "queries-index"  ← Correct
Document Count: 1,317
Vector Field: "embedding" (1536 dims)  ← NOT "queryVector"!

Schema:
  - id (key)
  - url
  - name
  - agent_id      ← OLD terminology but this is what exists!
  - agent_name    ← OLD terminology but this is what exists!
  - agent_url     ← OLD terminology but this is what exists!
  - agent_json_ld ← OLD terminology but this is what exists!
  - query
  - query_detail
  - description
  - embedding (vector field)
```

**Code Location:** `search_backend.py` lines 117 and 148-154
```python
# CORRECT - Request OLD field names from Azure
select_fields = ["url", "name", "agent_id", "agent_name", "agent_url", ...]

# CORRECT - Map to NEW names for internal use
results.append({
    "augment_id": item.get("agent_id", ""),      # Map old → new
    "augment_name": item.get("agent_name", ""),
    "augment_url": item.get("agent_url", ""),
    "augment_json_ld": item.get("agent_json_ld", "{}"),
})

# WRONG - Request NEW field names that don't exist in Azure
select_fields = ["augment_id", "augment_name", ...]  # ❌ Azure doesn't have these!
```

---

## Critical Feature: Streaming Results

### What Streaming Is

**NOT streaming:** Collect all results → sort → send to user
```python
# ❌ WRONG - This is batch processing, not streaming!
for task in asyncio.as_completed(tasks):
    await task  # Just wait for all to finish

final_results = collect_all_results()
final_results.sort(key=lambda x: x["score"])  # Wait to sort
for result in final_results:
    await stream_callback(result)  # Finally send
```

**IS streaming:** Send each result as soon as it's ready
```python
# ✅ CORRECT - True streaming
for task in asyncio.as_completed(tasks):
    await task  # One task completed!

    # Check if this completion produced a ready result
    if result_is_ready and not already_streamed:
        await stream_callback(result)  # Send immediately!
```

### Where Streaming Lives

**File:** `code/who_handler.py`
**Function:** `_rank_and_build_results()`
**Lines:** ~487-540 (streaming mode) vs ~542-610 (non-streaming mode)

**The Pattern:**
```python
async def _rank_and_build_results(
    self,
    query: str,
    documents: List[Dict[str, Any]],
    cache_key: str,
    augment_type: Optional[str],
    result_limit: int,
    stream_callback: Optional[callable] = None  # ← If present, STREAM!
) -> Dict[str, Any]:

    if stream_callback and ranking_tasks:
        # STREAMING MODE: Send results as they complete
        for completed_task in asyncio.as_completed(ranking_tasks):
            await completed_task

            # Process newly completed rankings
            for doc in documents:
                rank_cache_key = (cache_key, doc["augment_id"])
                ranking = self.ranking_cache.get(rank_cache_key)

                # Skip if not ready or already streamed
                if not ranking or doc.get("_streamed"):
                    continue

                doc["_streamed"] = True  # Mark as streamed

                # Filter by score
                if ranking["score"] <= SETTINGS["score_threshold"]:
                    continue

                # Build and stream result IMMEDIATELY
                result = self._build_result_object(augment, ranking, schema_type)
                await stream_callback(result)  # ← SEND NOW!

        # Return empty - results already streamed
        return self._build_response([])

    else:
        # NON-STREAMING MODE: Collect, sort, return
        # ... (wait for all, collect all, sort all, return all)
```

### Testing Streaming

**Endpoint:** `POST /who-stream`

**Test:**
```bash
curl -X POST http://localhost:8000/who-stream \
  -H 'Content-Type: application/json' \
  -d '{"query": {"text": "test"}}' \
  --no-buffer
```

**Expected:** Results appear incrementally as rankings complete (not all at once)

**SSE Format:**
```
event: result
data: {"protocol": "http", "endpoint": "...", "score": 95}

event: result
data: {"protocol": "http", "endpoint": "...", "score": 87}

event: done
data: {"total_count": 2}
```

---

## Rules for Making Changes

### Rule 1: Check Azure BEFORE Renaming

**Before changing ANY field or index name:**

1. Check the actual Azure Search schema:
```bash
curl -X GET 'https://af1.search.windows.net/indexes/agents-index?api-version=2023-11-01' \
  -H 'api-key: YOUR_API_KEY' \
  | python3 -m json.tool
```

2. If the field doesn't exist in Azure, you have TWO options:
   - **Option A:** Update Azure index first, then update code
   - **Option B:** Add a mapping layer in code (like current `agent_*` → `augment_*`)

3. **NEVER** just rename and assume it will work!

### Rule 2: Always Test Both Strategies

**After ANY change to search_backend.py or who_handler.py:**

```bash
# Enable debug mode
export WHO_DEBUG=true

# Test augment strategy
curl -X POST 'http://localhost:8000/who' \
  -H 'Content-Type: application/json' \
  -d '{"query": {"text": "test"}, "meta": {"strategy": "augment"}}'

# Test query strategy
curl -X POST 'http://localhost:8000/who' \
  -H 'Content-Type: application/json' \
  -d '{"query": {"text": "test"}, "meta": {"strategy": "query"}}'

# Check debug logs for:
# - Search backend returned results | { count: >0 }
# - Normalized augments | { count: >0 }
# - Final results | { count: >0 }
```

**If you see `count: 0` at ANY stage, you broke something!**

### Rule 3: Preserve Streaming Behavior

**When refactoring `_rank_and_build_results()`:**

1. **MUST have TWO code paths:**
   - `if stream_callback:` → Stream results as they complete
   - `else:` → Collect all, sort, return

2. **Streaming path MUST:**
   - Use `asyncio.as_completed()` to process tasks as they finish
   - Check newly completed results inside the loop
   - Call `await stream_callback(result)` IMMEDIATELY when ready
   - NOT wait for all tasks to complete before streaming

3. **Non-streaming path MUST:**
   - Collect all results
   - Sort by score descending
   - Apply `result_limit`
   - Return sorted results

4. **Test streaming after changes:**
```bash
curl -X POST http://localhost:8000/who-stream \
  -H 'Content-Type: application/json' \
  -d '{"query": {"text": "test"}}' \
  --no-buffer
```

Watch for incremental results, not a batch at the end.

### Rule 4: Use Debug Mode

**ALWAYS enable debug mode when testing changes:**

```bash
export WHO_DEBUG=true
python3 code/agent_finder.py
```

Debug logs show:
- What index is being searched
- How many results came back from Azure
- Whether results are being streamed or batched
- Cache hits/misses
- Scores and filtering decisions

**If you don't see debug logs, something is wrong with your test setup!**

---

## Common Mistakes to Avoid

### ❌ Mistake 1: "I'll just rename this to be consistent"

**Example:**
```python
# Someone thinks: "Let's make this consistent with our new naming"
index_name = "augments-index"  # ❌ BREAKS EVERYTHING
```

**Why it breaks:** Azure index is still called `"agents-index"`

**Correct approach:** Check Azure first, add mapping layer if needed

### ❌ Mistake 2: "Streaming is just iterating over results"

**Example:**
```python
# Someone thinks: "I'll collect all results then stream them"
all_results = await gather_all_results()
for result in all_results:
    await stream_callback(result)  # ❌ NOT streaming!
```

**Why it breaks:** User still waits for ALL rankings to complete

**Correct approach:** Stream INSIDE the `asyncio.as_completed()` loop

### ❌ Mistake 3: "I'll simplify by removing the dual code path"

**Example:**
```python
# Someone thinks: "Why do we have two paths? Let's unify!"
# Removes the if/else for streaming vs non-streaming
# ❌ BREAKS streaming or sorting or both
```

**Why it breaks:** Streaming and non-streaming have different requirements
- Streaming: Send results immediately, no sorting
- Non-streaming: Collect all, sort, return top N

**Correct approach:** Keep BOTH paths

### ❌ Mistake 4: "The docs say queryVector, so I'll use that"

**Example:**
```python
# Someone reads RETRIEVAL_STRATEGIES_SPECIFICATION.md
# Doc says: "fields": "queryVector"
search_kwargs["vector_queries"] = [{
    "fields": "queryVector"  # ❌ BREAKS - field doesn't exist!
}]
```

**Why it breaks:** Docs describe the IDEAL schema, but Azure has `"embedding"`

**Correct approach:** Check CRITICAL_INDEX_MAPPING.md for ACTUAL schema

---

## Emergency Recovery

### If Search Returns 0 Results

1. **Check index name:**
```bash
grep "index_name" code/search_backend.py
# Should be "agents-index" NOT "augments-index"
```

2. **Check field names:**
```bash
grep "select_fields" code/search_backend.py
# For queries-index: should request "agent_id" not "augment_id"
```

3. **Verify Azure indices exist:**
```bash
curl -X GET 'https://af1.search.windows.net/indexes?api-version=2023-11-01' \
  -H 'api-key: YOUR_API_KEY'
# Should list: agents-index, queries-index
```

4. **Check document counts:**
```bash
curl -X GET 'https://af1.search.windows.net/indexes/agents-index/docs/$count?api-version=2023-11-01' \
  -H 'api-key: YOUR_API_KEY'
# Should return: 5237

curl -X GET 'https://af1.search.windows.net/indexes/queries-index/docs/$count?api-version=2023-11-01' \
  -H 'api-key: YOUR_API_KEY'
# Should return: 1317
```

### If Streaming Doesn't Work

1. **Check for stream_callback handling:**
```bash
grep -A 20 "if stream_callback and ranking_tasks:" code/who_handler.py
# Should show results being streamed INSIDE asyncio.as_completed loop
```

2. **Verify SSE endpoint exists:**
```bash
grep "who-stream" code/agent_finder.py
# Should have POST /who-stream endpoint
```

3. **Test with curl:**
```bash
curl -X POST http://localhost:8000/who-stream \
  -H 'Content-Type: application/json' \
  -d '{"query": {"text": "test"}}' \
  --no-buffer
```

---

## Summary: The Golden Rules

1. **NEVER rename fields/indices without checking Azure first**
2. **ALWAYS test both augment and query strategies after changes**
3. **PRESERVE streaming behavior - it's a feature, not a bug**
4. **USE debug mode when testing changes**
5. **READ this document before refactoring search or ranking code**

**When in doubt:** Check [CRITICAL_INDEX_MAPPING.md](CRITICAL_INDEX_MAPPING.md) for actual Azure schema

---

## Changelog

| Date | Issue | What Broke | How It Was Fixed |
|------|-------|------------|------------------|
| 2026-03-20 | Augment strategy 0 results | Index name `augments-index` vs `agents-index` | Changed to `agents-index` |
| 2026-03-20 | Query strategy empty metadata | Field names `augment_*` vs `agent_*` | Added mapping layer |
| 2026-03-20 | Streaming neutered | Results collected before streaming | Stream inside as_completed loop |

**LEARN FROM THESE MISTAKES. DON'T REPEAT THEM.**
