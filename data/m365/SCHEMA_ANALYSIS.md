# M365 Apps Entity Schema Analysis

## File Format
- **Type**: Tab-separated values (TSV)
- **Encoding**: Fields use `key:type=value` format
- **Binary Fields**: Base64 encoded embeddings and definitions

## Field Analysis

Based on analysis of sample records (Writing Coach GPT, Azure Boards Catalog app):

### Core Identity Fields

| Field # | Field Name | Type | Example | Description |
|---------|-----------|------|---------|-------------|
| 1 | **Score/Rank** | float | `0.000244` | Relevance score or ranking weight |
| 2 | **Entity ID** | string | `P_e3d64609...declarativeAgent` | Unique identifier for the entity |
| 3 | **AppId** | string | `f72d7797-c6ee-4fd3-9454-028d0095068b` | GUID identifier for the app |
| 4 | **TitleId** | string | `P_e3d64609-7a28-6de6-3093-402c20bb96ce` | Title identifier (may match entity ID) |

### Display Information

| Field # | Field Name | Type | Example | Description |
|---------|-----------|------|---------|-------------|
| 5 | **Title** | string | `"Writing Coach"` | **Primary display name** |
| 6 | **AppName** | string | `"Writing Coach"` | Application name (often same as Title) |
| 7 | **AppType** | string | `"GPT"`, `"Catalog"` | **Type of app** (see App Types below) |
| 8 | **CatalogName** | string | `"MOS3"` | Catalog/store name |
| 10 | **Description** | string | `"Writing Coach to help you..."` | **Full text description** |
| 11 | **IconAnonymousUrl** | string | `https://res.cdn.office.net/...` | **Icon/image URL** |

### Version & Provenance

| Field # | Field Name | Type | Example | Description |
|---------|-----------|------|---------|-------------|
| 12 | **Version** | string | `"1.0.0"` | App version |
| 13 | **AppVersion** | string | `"1.0.0"` | Application version (may duplicate Version) |
| 14 | **AcquisitionContext** | string | `"Tenant"` | How the app was acquired |
| 15 | **DeveloperName** | string | `"Microsoft Corporation"` | **Publisher/developer name** |
| 16 | **AppProvenance** | string | `"FirstParty"` | First-party vs third-party |

### Permissions & Availability

| Field # | Field Name | Type | Example | Description |
|---------|-----------|------|---------|-------------|
| 17 | **IsOwner** | boolean | `"False"` | Whether user owns this app |
| 18 | **IsShareable** | boolean | `"True"` | Whether app can be shared |
| 19 | **PrePinned** | boolean | `"False"` | Whether pre-pinned in UI |
| 22 | **ExposeToolsToCopilot** | boolean | `"False"` | Copilot extensibility flag |
| 23 | **ApplicationStateFlags** | string | `"None"` | State flags |

### Semantic Content (For Search/Ranking)

| Field # | Field Name | Type | Example | Description |
|---------|-----------|------|---------|-------------|
| 20 | **FBV_Sentence** | string | Long description | **Full semantic description** - appears to be concatenated capabilities/prompts |
| 21 | **Atlas_Sentence** | string | Long description | Alternative semantic description |
| 25 | **FBV_Embedding** | binary | Base64 blob | **Vector embedding for FBV sentence** |
| 26 | **Atlas_Embedding** | binary | Base64 blob | **Vector embedding for Atlas sentence** |

### Type-Specific Fields

#### For GPT Apps:
| Field # | Field Name | Type | Example | Description |
|---------|-----------|------|---------|-------------|
| 9 | **GptId** | string | `P_e3d64609...declarativeAgent` | GPT-specific identifier |
| 24 | **GptDefinition** | binary | Base64 blob | **GPT configuration/definition** |

#### For Catalog Apps (like Azure Boards):
| Field # | Field Name | Type | Example | Description |
|---------|-----------|------|---------|-------------|
| - | **Sources** | string | `"Teams"` | Source platform |
| - | **SmallImageUrl** | string | URL | Small icon URL |
| - | **Categories** | string[] | `["ITAdmin", "Productivity"]` | **App categories** (multiple values) |
| - | **CapabilityDetails** | string[] | `["bots", "messageExtensions"]` | **Capabilities** (multiple values) |

## App Types Observed

Based on the data:

1. **GPT** - Copilot GPT agents (declarative agents)
   - Examples: Writing Coach, Files, Word, Excel
   - Have `GptId` and `GptDefinition` fields

2. **Catalog** - Traditional M365 apps from catalog
   - Examples: Azure Boards
   - Have `Sources`, `Categories`, `CapabilityDetails`

## Key Insights for Who Protocol Mapping

### 1. **Multiple App Types** → Protocol Mapping
- **GPT apps** → `protocol: "a2a"` (treat as A2A agents)
- **Catalog apps with bots** → `protocol: "http"` or `"a2a"` depending on capabilities
- **Catalog apps with APIs** → `protocol: "openapi"`

### 2. **Rich Semantic Content Available**
- `FBV_Sentence`: Contains detailed capability descriptions
- `Description`: Human-readable summary
- `Categories`: Domain classification
- `CapabilityDetails`: Technical capabilities

### 3. **Embeddings Already Computed**
- `FBV_Embedding` and `Atlas_Embedding` are pre-computed
- Can be used directly for vector search
- No need to regenerate embeddings

### 4. **Invocation Information**
For GPT apps:
- `GptDefinition` likely contains invocation details
- Would need to decode binary blob

For Catalog apps:
- `CapabilityDetails` tells us how to invoke (bots, messageExtensions, etc.)
- May need additional metadata for actual endpoints

## Example Records

### GPT App (Writing Coach)
```
Title: Writing Coach
AppType: GPT
Description: Writing Coach to help you in your writing.
DeveloperName: Microsoft Corporation
FBV_Sentence: Writing Coach. Critique my writing. Provide detailed and constructive feedback on a piece of writing.. Change the tone of an email or message. Help change the tone of an email or message from professional language to a more casual tone.. Translate a piece of text. Translate a piece of text into another language.. Teach me how to write instructions. Help me write instructions to explain a complex process.. Professional blog post. Help me write a blog post for my company.. Write a whitepaper. Help me write a whitepaper.
```

**Maps to Who Protocol:**
```json
{
  "protocol": "a2a",
  "endpoint": "https://m365.com/gpt/P_e3d64609-7a28-6de6-3093-402c20bb96ce",
  "score": 95,
  "definition": {
    "name": "Writing Coach",
    "description": "Writing Coach to help you in your writing.",
    "version": "1.0.0",
    "skills": [
      {
        "id": "critique_writing",
        "name": "Critique Writing",
        "description": "Provide detailed and constructive feedback on a piece of writing"
      },
      {
        "id": "change_tone",
        "name": "Change Tone",
        "description": "Help change the tone of an email or message from professional language to a more casual tone"
      }
    ],
    "metadata": {
      "publisher": "Microsoft Corporation",
      "categories": ["productivity", "writing"],
      "provenance": "FirstParty"
    }
  }
}
```

### Catalog App (Azure Boards)
```
Title: Azure Boards
AppType: Catalog
Description: Monitor and collaborate on existing work items and create new work items.
Categories: ITAdmin, Productivity, ProjectManagement
CapabilityDetails: bots, messageExtensions
DeveloperName: Microsoft Corporation
```

**Maps to Who Protocol:**
```json
{
  "protocol": "http",
  "endpoint": "https://teams.microsoft.com/app/7299542a-1697-4ec1-812b-6b70065c0795",
  "score": 88,
  "definition": {
    "name": "Azure Boards",
    "description": "Monitor and collaborate on existing work items and create new work items.",
    "capabilities": ["bots", "messageExtensions"],
    "metadata": {
      "publisher": "Microsoft Corporation",
      "categories": ["ITAdmin", "Productivity", "ProjectManagement"],
      "sources": ["Teams"],
      "provenance": "FirstParty"
    }
  }
}
```

## Recommended Field Mapping to Who Protocol

| M365 Field | Who Protocol Field | Notes |
|------------|-------------------|-------|
| `Title` | `definition.name` | Primary name |
| `Description` | `definition.description` | Short description |
| `FBV_Sentence` | *(for ranking)* | Use for enhanced search/ranking |
| `AppType` | `protocol` | GPT→a2a, Catalog→http/openapi |
| `Version` | `definition.version` | Version string |
| `DeveloperName` | `definition.metadata.publisher` | Publisher info |
| `Categories` | `definition.metadata.categories` | Domain tags |
| `CapabilityDetails` | `definition.capabilities` | Technical capabilities |
| `IconAnonymousUrl` | `definition.metadata.iconUrl` | Icon for display |
| `FBV_Embedding` | *(for search)* | Use for vector search |
| `AppId` | *(endpoint construction)* | Build endpoint URL |
| `GptId` | *(for GPT apps)* | Unique GPT identifier |

## Next Steps

1. **Parse Binary Fields**
   - Decode `GptDefinition` to understand invocation format
   - Decode embeddings if needed for search

2. **Build Converter**
   - Implement `convert_m365_app_to_who_protocol()` function
   - Handle GPT vs Catalog app differences

3. **Extract Skills from FBV_Sentence**
   - Parse the concatenated skill descriptions
   - Convert to structured skills array

4. **Index Embeddings**
   - Use pre-computed `FBV_Embedding` for vector search
   - Store in Azure Search or vector database

5. **Test with Query Sets**
   - Validate against labeled queries
   - Measure relevance scores
