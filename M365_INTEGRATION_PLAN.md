# M365 Apps Integration Plan for AgentFinder

## Overview

This document outlines how to integrate Microsoft 365 Apps data with the AgentFinder Who Protocol implementation.

## Data Source Information

### M365 Apps Index
- **Location**: ES Explorer (https://aka.ms/esexplorer)
- **Access**: User Data > Download Indexes > Apps Index
- **Format**: Binary format requiring CIDebug tool to extract

### App Types Available
See: https://o365exchange.visualstudio.com/O365%20Core/_wiki/wikis/O365%20Core.wiki/549848/Apps-Types

Each app type is sourced from different data stores. Key app types likely include:
- M365 Copilot agents
- Teams apps
- Office Add-ins
- SharePoint apps
- Power Platform apps/flows

### Schema
**Schema location**: `https://o365exchange.visualstudio.com/DefaultCollection/O365 Core/_git/EntityServe?path=/sources/dev/Schema/EntityDefinitions/EntitySchema/AppsEntitySchema.settings.ini`

**Action needed**: Download and review schema to understand available fields.

### Query Sets (for Testing)
**Location**: https://o365exchange.visualstudio.com/O365%20Core/_wiki/wikis/O365%20Core.wiki/622463/App-Platform-Feature-Experimentation-Guide?anchor=query-set-registry

**Contacts**:
- **Loga Jegede** (loga.jegede@microsoft.com) - has superset covering main scenarios
- **Shu Cai** (caishu@microsoft.com) - has tool for getting raw data

---

## Integration Architecture

### Option 1: Direct ES Explorer Integration (Recommended)

```
┌──────────────────┐
│  M365 Apps Index │
│  (ES Explorer)   │
└────────┬─────────┘
         │
         ├─> Download & Convert (CIDebug tool)
         │
         v
┌──────────────────┐      ┌──────────────────┐
│  Indexing Script │─────>│  Azure Search /  │
│  (Python)        │      │  Vector Store    │
└──────────────────┘      └────────┬─────────┘
                                   │
                                   v
                          ┌──────────────────┐
                          │  WHO Handler     │
                          │  (AgentFinder)   │
                          └──────────────────┘
```

**Pros**:
- Uses existing Who Protocol implementation
- Leverages current search/ranking infrastructure
- No dependency on M365 runtime systems

**Cons**:
- Requires periodic updates from ES Explorer
- Not real-time

### Option 2: Live Query Integration

Query ES Explorer APIs directly when needed.

**Pros**:
- Always up-to-date
- No data duplication

**Cons**:
- Latency from additional API call
- Dependency on M365 availability

---

## Implementation Tasks

### Phase 1: Data Extraction & Understanding (Week 1)

#### 1.1 Extract Apps Index Data
```bash
# Steps:
1. Go to https://aka.ms/esexplorer
2. User Data > Download Indexes > Apps Index
3. Save to local disk
4. Use CIDebug tool to convert:
   - Pull: https://msasg.visualstudio.com/Substrate/_git/SubstrateTools?path=/src/Tools/CIDebug
   - Compile and run CIDebug.exe
   - Load index file > "Get Index Content"
```

**Deliverable**: Raw M365 Apps data in text/JSON format

#### 1.2 Analyze Schema
- Download AppsEntitySchema.settings.ini
- Document all available fields
- Identify fields relevant to Who Protocol:
  - App name/description
  - Capabilities/skills
  - Category/domain
  - Invocation details
  - Authentication requirements

**Deliverable**: Schema mapping document

#### 1.3 Map to Who Protocol
Create mapping from M365 Apps schema to Who Protocol formats:

| M365 Field | Who Protocol Field | Notes |
|------------|-------------------|-------|
| TBD | protocol | Likely "http" or custom |
| TBD | endpoint | App invocation URL |
| TBD | definition.name | App name |
| TBD | definition.description | App description |
| TBD | definition.capabilities | App capabilities |

**Deliverable**: `code/m365_schema_mapping.py`

### Phase 2: Data Transformation (Week 2)

#### 2.1 Create M365 Apps Converter

Create script to convert M365 Apps index to Who Protocol format:

```python
# code/convert_m365_apps.py

import json
from typing import Dict, Any, List

def convert_m365_app_to_who_protocol(app: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert M365 app to Who Protocol augment format.

    Returns augment with protocol, endpoint, and definition.
    """
    # Determine protocol based on app type
    protocol = determine_protocol(app)

    # Build definition based on protocol
    definition = build_definition(protocol, app)

    return {
        "protocol": protocol,
        "endpoint": app.get("endpoint_url", ""),
        "definition": definition,
        "source": "m365-apps-index"
    }

def determine_protocol(app: Dict[str, Any]) -> str:
    """Determine protocol type from M365 app metadata"""
    app_type = app.get("type", "").lower()

    # Map M365 app types to Who Protocol types
    if "copilot" in app_type or "agent" in app_type:
        return "a2a"  # Treat as A2A agent
    elif "api" in app_type:
        return "openapi"
    else:
        return "http"  # Default to custom HTTP

def build_definition(protocol: str, app: Dict[str, Any]) -> Dict[str, Any]:
    """Build protocol-specific definition"""
    base_def = {
        "name": app.get("name", ""),
        "description": app.get("description", ""),
    }

    if protocol == "a2a":
        # Build A2A Agent Card
        base_def.update({
            "url": app.get("endpoint_url", ""),
            "version": app.get("version", "1.0.0"),
            "skills": extract_skills(app)
        })

    # Add more protocol-specific fields...

    return base_def

def extract_skills(app: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract skills/capabilities from M365 app"""
    # Implementation depends on schema
    return []
```

**Deliverable**: Working converter script

#### 2.2 Create Indexing Pipeline

```python
# code/index_m365_apps.py

import asyncio
import json
from search_backend import get_search_backend
from llm_backend import get_llm_backend
from convert_m365_apps import convert_m365_app_to_who_protocol

async def index_m365_apps(apps_file: str):
    """
    Index M365 apps into search backend.

    Args:
        apps_file: Path to converted M365 apps JSON
    """
    # Initialize backends
    search = get_search_backend()
    llm = get_llm_backend()

    await search.initialize()
    await llm.initialize()

    # Load apps
    with open(apps_file) as f:
        apps = json.load(f)

    print(f"Indexing {len(apps)} M365 apps...")

    for app in apps:
        # Convert to Who Protocol format
        augment = convert_m365_app_to_who_protocol(app)

        # Generate embedding for description
        embedding = await llm.get_embedding(
            augment["definition"]["description"]
        )

        # Create search document
        doc = {
            "url": augment["endpoint"],
            "name": augment["definition"]["name"],
            "json_ld": json.dumps({
                "@type": get_schema_type(augment["protocol"]),
                **augment["definition"]
            }),
            "embedding": embedding,
            "site": "m365-apps"
        }

        # Index in search backend
        await index_document(search, doc)

    print("Indexing complete!")

def get_schema_type(protocol: str) -> str:
    """Map protocol to @type"""
    mapping = {
        "a2a": "A2AAgent",
        "mcp": "MCPServer",
        "openapi": "OpenAPIService",
        "skill": "Skill",
        "http": "CustomEndpoint"
    }
    return mapping.get(protocol, "CustomEndpoint")
```

**Deliverable**: Automated indexing pipeline

### Phase 3: Testing with Query Sets (Week 3)

#### 3.1 Download Query Sets
- Download TSV files from wiki
- Convert to test format
- Coordinate with Loga for superset

#### 3.2 Create Test Suite

```python
# code/test_m365_integration.py

import pytest
import json
from who_handler import who_query

class TestM365Integration:
    """Test M365 apps integration with query sets"""

    @pytest.mark.asyncio
    async def test_query_set_1(self):
        """Test first query set"""
        with open("query_sets/set1.json") as f:
            queries = json.load(f)

        for query_item in queries:
            result = await who_query(query_item["query"])

            # Check expected app is in results
            expected_app = query_item["expected_app"]
            found = any(
                r["definition"]["name"] == expected_app
                for r in result["results"]
            )

            assert found, f"Expected app '{expected_app}' not found"
```

**Deliverable**: Test results against labeled query sets

### Phase 4: Integration & Deployment (Week 4)

#### 4.1 Update Search Backend Configuration
Add M365 apps site to search filter:

```python
# In search_backend.py
SEARCH_CONFIG = {
    "site": os.getenv("SEARCH_SITE", "m365-apps"),  # Updated
}
```

#### 4.2 Create Update Script
Script to periodically refresh M365 apps index:

```bash
#!/bin/bash
# scripts/update_m365_apps.sh

# Download latest index from ES Explorer
# (May need to automate via API if available)

# Convert with CIDebug
./CIDebug/CIDebug.exe convert --input apps_index.bin --output apps.json

# Run indexing
python code/index_m365_apps.py apps.json

echo "M365 apps index updated!"
```

---

## Who Protocol Extensions for M365

### M365-Specific Fields

The Who Protocol supports source attribution (Section 11.3). We can add M365-specific metadata:

```json
{
  "protocol": "a2a",
  "endpoint": "https://apps.microsoft.com/agent/...",
  "score": 95,
  "source": "https://who.microsoft.com",
  "definition": {
    "name": "Sales Copilot",
    "description": "AI assistant for sales teams",
    "url": "https://apps.microsoft.com/agent/sales",
    "version": "1.0.0",
    "capabilities": {
      "streaming": true,
      "m365Integration": true
    },
    "skills": [...],
    "metadata": {
      "publisher": "Microsoft",
      "categories": ["Sales", "CRM"],
      "permissions": ["Dynamics365", "Teams"]
    }
  }
}
```

---

## Next Steps & Questions

### Immediate Actions (This Week)
1. **Download sample apps index** from ES Explorer
2. **Review schema** - understand available fields
3. **Download query sets** - get labeled test data
4. **Meet with Loga** - get superset query set
5. **Meet with Shu** - understand her data extraction tool

### Questions to Answer
1. What app types should we prioritize?
   - Copilot agents?
   - Teams apps?
   - All M365 apps?

2. How frequently should we update the index?
   - Daily?
   - Weekly?
   - Real-time?

3. What authentication is needed for app invocation?
   - OAuth tokens?
   - API keys?
   - M365 SSO?

4. Should we filter apps by user permissions?
   - Only show apps user can access?
   - Show all and filter at invocation time?

### Contacts
- **Paul Maree** (paulmaree@microsoft.com) - Data source owner
- **Gregory Filbrandt** (gfilbrandt@microsoft.com) - CC'd
- **Loga Jegede** (loga.jegede@microsoft.com) - Query sets
- **Shu Cai** (caishu@microsoft.com) - Data extraction tool

---

## Timeline

| Week | Tasks | Deliverables |
|------|-------|--------------|
| 1 | Data extraction & schema analysis | Raw data, schema mapping |
| 2 | Converter & indexing pipeline | Working code |
| 3 | Testing with query sets | Test results, metrics |
| 4 | Integration & deployment | Production-ready system |

---

## Success Metrics

1. **Coverage**: % of M365 apps indexed
2. **Accuracy**: % of labeled queries returning correct app
3. **Latency**: Query response time
4. **Relevance**: Average score of correct app in results

---

## Technical Considerations

### Schema Translation Challenges
- M365 schema → Who Protocol mapping may be lossy
- Some M365 metadata may not fit Who Protocol
- Solution: Use `metadata` field in definition for M365-specific data

### Performance
- How many M365 apps are there? (affects index size)
- What's acceptable query latency?
- Should we cache embeddings?

### Security
- Apps may have permission requirements
- Need to pass user context to WHO handler
- May need to filter results by user access

---

## Resources

### Code Templates
All code templates above can be implemented in the existing AgentFinder structure:
- Converters go in `code/convert_m365_apps.py`
- Indexing in `code/index_m365_apps.py`
- Tests in `code/test_m365_integration.py`

### Tools Needed
1. **CIDebug** - Convert binary index to text
2. **ES Explorer** - Download apps index
3. **Query set files** - Testing data

### Documentation
- Who Protocol: [who_protocol.txt](who_protocol.txt)
- Test Results: [TEST_RESULTS.md](TEST_RESULTS.md)
- M365 Apps Types: [Wiki link provided]
