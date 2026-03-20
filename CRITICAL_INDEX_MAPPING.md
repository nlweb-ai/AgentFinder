# CRITICAL: Azure Search Index Field Mapping

**⚠️ DO NOT RENAME THESE WITHOUT UPDATING THE AZURE SEARCH INDICES ⚠️**

## The Problem

During the "agent" → "augment" terminology rename, the code was updated to use new field names (`augment_id`, `augment_name`, etc.) but the **Azure Search indices were NOT updated**. This caused complete system failures with 0 results.

## Current State (2026-03-20)

### agents-index (Strategy 1: Augment-Level)
- **Index Name**: `agents-index` (NOT `augments-index`)
- **Vector Field**: `embedding` (1536 dimensions)
- **Document Count**: 5,237 documents
- **Field Names**:
  - `id` (key)
  - `url`
  - `name`
  - `json_ld`
  - `description`
  - `embedding` (vector field)

### queries-index (Strategy 2: Query-Level)
- **Index Name**: `queries-index` (correct)
- **Vector Field**: `embedding` (NOT `queryVector` as in docs!)
- **Document Count**: 1,317 documents
- **Field Names**:
  - `id` (key)
  - `url`
  - `name`
  - `agent_id` ⚠️ (OLD terminology, but this is what exists in Azure!)
  - `agent_name` ⚠️
  - `agent_url` ⚠️
  - `agent_json_ld` ⚠️
  - `query`
  - `query_detail`
  - `description`
  - `embedding` (vector field)

## Code Mapping (search_backend.py)

The code MUST map between:
- **Internal code**: Uses `augment_*` terminology
- **Azure indices**: Use `agent_*` field names

```python
# Line 117: Request agent_* fields from Azure
select_fields = ["url", "name", "agent_id", "agent_name", "agent_url", ...]

# Lines 148-154: Map to augment_* for internal use
results.append({
    "augment_id": item.get("agent_id", ""),      # Map agent_id → augment_id
    "augment_name": item.get("agent_name", ""),  # Map agent_name → augment_name
    "augment_url": item.get("agent_url", ""),    # Map agent_url → augment_url
    "augment_json_ld": item.get("agent_json_ld", "{}"),  # Map agent_json_ld → augment_json_ld
    ...
})
```

## What Broke (2026-03-20)

### Bug #1: Index Name Mismatch (Augment Strategy)
- **Code requested**: `augments-index`
- **Actual index**: `agents-index`
- **Impact**: Augment strategy returned 0 results
- **Fix**: Changed line 101 to use `"agents-index"`

### Bug #2: Field Name Mismatch (Query Strategy)
- **Code requested**: `augment_id`, `augment_name`, `augment_url`, `augment_json_ld`
- **Actual fields**: `agent_id`, `agent_name`, `agent_url`, `agent_json_ld`
- **Impact**: Query strategy retrieved documents but all augment fields were empty/null
- **Fix**: Lines 117 and 148-154 now request `agent_*` fields and map to `augment_*`

## Prevention

**BEFORE renaming anything:**
1. Check Azure Search index schemas with:
   ```bash
   curl -X GET 'https://af1.search.windows.net/indexes/agents-index?api-version=2023-11-01' \
     -H 'api-key: YOUR_API_KEY'
   ```

2. If renaming fields:
   - Update Azure Search indices FIRST
   - Then update code to match
   - Test immediately with debug mode enabled

3. If unsure, add field mapping layer (like current solution) instead of direct rename

## Debug Commands

### Check Index Schemas
```bash
# agents-index
curl -X GET 'https://af1.search.windows.net/indexes/agents-index?api-version=2023-11-01' \
  -H 'api-key: YOUR_API_KEY' | python3 -m json.tool

# queries-index
curl -X GET 'https://af1.search.windows.net/indexes/queries-index?api-version=2023-11-01' \
  -H 'api-key: YOUR_API_KEY' | python3 -m json.tool
```

### Check Document Counts
```bash
curl -X GET 'https://af1.search.windows.net/indexes/agents-index/docs/$count?api-version=2023-11-01' \
  -H 'api-key: YOUR_API_KEY'

curl -X GET 'https://af1.search.windows.net/indexes/queries-index/docs/$count?api-version=2023-11-01' \
  -H 'api-key: YOUR_API_KEY'
```

### Test with Debug Mode
```bash
export WHO_DEBUG=true
python3 code/agent_finder.py
```

## Summary

**The Azure Search indices use OLD terminology (`agent_*`) and MUST NOT be changed in code without updating the indices first.**

The current solution adds a mapping layer in `search_backend.py` to translate between Azure's `agent_*` fields and the code's internal `augment_*` terminology.
