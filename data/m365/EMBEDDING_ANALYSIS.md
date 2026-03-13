# M365 Apps Embedding Analysis

## What Are The Embeddings Of?

The **FBV_Embedding** and **Atlas_Embedding** fields contain vector embeddings of the **FBV_Sentence** and **Atlas_Sentence** fields respectively.

### FBV_Sentence Content

The `FBV_Sentence` field contains a **concatenated list of capabilities and sample use cases** for each app. It appears to be optimized for search and discovery.

#### Structure:

**For GPT Apps (like Writing Coach):**
```
FBV_Sentence = "App Name. Capability 1. Description 1. Capability 2. Description 2. ..."
```

**Example - Writing Coach:**
```
Writing Coach.
Critique my writing. Provide detailed and constructive feedback on a piece of writing.
Change the tone of an email or message. Help change the tone of an email or message from professional language to a more casual tone.
Translate a piece of text. Translate a piece of text into another language.
Teach me how to write instructions. Help me write instructions to explain a complex process.
Professional blog post. Help me write a blog post for my company.
Write a whitepaper. Help me write a whitepaper.
```

**Pattern:** Each capability appears twice:
1. **Short form** (e.g., "Critique my writing")
2. **Detailed form** (e.g., "Provide detailed and constructive feedback on a piece of writing")

**For Catalog Apps (like Azure Boards):**
```
FBV_Sentence = "Description of what the app does"
```

**Example - Azure Boards:**
```
Monitor and collaborate on existing work items and create new work items.
```

### Embedding Format

**Technical Details:**
- **Type**: Binary (Base64 encoded)
- **Raw size**: 1,536 bytes
- **Format**: float32 (4 bytes per dimension)
- **Dimensions**: **384** (1536 bytes ÷ 4 bytes/float)
- **Model**: Likely a 384-dimensional embedding model (possibly sentence-transformers or similar)

**Example values (first 4 dimensions):**
```
[0.0627, -0.0085, 0.0103, 0.0493, ...]
```

### Are There Sample Queries?

**Short answer: Sort of, but not explicitly labeled.**

The `FBV_Sentence` field contains **example use cases that function as implicit sample queries**:

#### For GPT Apps:
The short forms act as sample queries:
- ✅ "Critique my writing"
- ✅ "Change the tone of an email or message"
- ✅ "Translate a piece of text"
- ✅ "Professional blog post"
- ✅ "Write a whitepaper"

These are user-facing prompts that demonstrate what the agent can do.

#### For Catalog Apps:
No explicit sample queries - just the description.

### Key Insights

1. **Pre-computed Embeddings Save Time & Cost**
   - No need to call OpenAI/Azure OpenAI API
   - Can index directly into vector database
   - Embeddings already optimized for search

2. **FBV_Sentence is Rich with Search Content**
   - Contains multiple phrasings of each capability
   - Includes both short and detailed descriptions
   - Optimized for matching user queries

3. **Sample Queries Are Embedded**
   - The short capability phrases are essentially sample queries
   - Format: imperative mood (e.g., "Critique my writing")
   - Can be extracted and structured for testing

4. **Two Sentence Types**
   - **FBV_Sentence**: Appears more detailed for GPT apps
   - **Atlas_Sentence**: May be an alternative representation (need to investigate)
   - Both have corresponding embeddings

## Extracting Sample Queries from FBV_Sentence

For **GPT apps**, we can parse the FBV_Sentence to extract sample queries:

```python
def extract_sample_queries(fbv_sentence: str, app_type: str) -> list[str]:
    """
    Extract sample queries from FBV_Sentence.

    For GPT apps, extracts the short capability phrases.
    """
    if app_type != "GPT":
        return []

    # Split by periods
    parts = [p.strip() for p in fbv_sentence.split('.') if p.strip()]

    # Skip the first part (app name)
    parts = parts[1:]

    # Take every other part (the short forms, skip detailed descriptions)
    sample_queries = []
    for i in range(0, len(parts), 2):
        if parts[i]:
            sample_queries.append(parts[i])

    return sample_queries
```

**Example output for Writing Coach:**
```python
[
    "Critique my writing",
    "Change the tone of an email or message",
    "Translate a piece of text",
    "Teach me how to write instructions",
    "Professional blog post",
    "Write a whitepaper"
]
```

## Using Embeddings for Search

### Option 1: Use Pre-computed Embeddings Directly

Since embeddings are already computed, we can:

1. **Extract FBV_Embedding** from TSV
2. **Decode Base64** to get float array
3. **Index directly** into Azure Search or vector database
4. **Use for similarity search**

**Pros:**
- ✅ No API calls needed
- ✅ Fast indexing
- ✅ No cost for embedding generation

**Cons:**
- ⚠️ Tied to specific embedding model (384-dim)
- ⚠️ Can't change embedding strategy

### Option 2: Re-embed with Different Model

Could re-generate embeddings using:
- OpenAI text-embedding-3-large (3072-dim)
- OpenAI text-embedding-3-small (1536-dim)
- Azure OpenAI ada-002 (1536-dim)

**Pros:**
- ✅ Control over embedding model
- ✅ Can use larger/better models
- ✅ Consistent with other data sources

**Cons:**
- ❌ Costs money (API calls)
- ❌ Takes time to generate
- ❌ May not be better than pre-computed

## Recommendation

**Use pre-computed embeddings** because:
1. They're already optimized for app discovery
2. Zero cost and instant availability
3. The 384-dim model appears purpose-built for this use case
4. FBV_Sentence content is rich enough for good retrieval

## Next Steps

1. ✅ **Confirmed**: Embeddings are of FBV_Sentence content
2. ✅ **Confirmed**: Dimensions are 384 (float32)
3. ✅ **Found**: Sample queries embedded in FBV_Sentence (for GPT apps)
4. ⏭️ **TODO**: Parse FBV_Sentence to extract structured skills/capabilities
5. ⏭️ **TODO**: Test pre-computed embeddings for search quality
6. ⏭️ **TODO**: Compare FBV_Embedding vs Atlas_Embedding
