# M365 Data Download Checklist

## Required Downloads

### 1. Apps Index (Binary)
- [ ] **URL:** https://aka.ms/esexplorer
- [ ] **Steps:**
  1. Go to User Data → Download Indexes
  2. Click Download next to "Apps Index"
  3. Save as `apps_index.bin` in this directory
- [ ] **Status:** Not started
- [ ] **File:** `apps_index.bin`

### 2. Apps Schema
- [ ] **URL:** https://o365exchange.visualstudio.com/DefaultCollection/O365%20Core/_git/EntityServe?path=/sources/dev/Schema/EntityDefinitions/EntitySchema/AppsEntitySchema.settings.ini
- [ ] **Steps:**
  1. Open URL in browser (Microsoft auth required)
  2. Save file
- [ ] **Status:** Not started
- [ ] **File:** `AppsEntitySchema.settings.ini`

### 3. Apps Types Documentation
- [ ] **URL:** https://o365exchange.visualstudio.com/O365%20Core/_wiki/wikis/O365%20Core.wiki/549848/Apps-Types
- [ ] **Steps:**
  1. Read documentation
  2. Note which app types to prioritize
- [ ] **Status:** Not started
- [ ] **Notes:** _______________

### 4. Query Sets
- [ ] **URL:** https://o365exchange.visualstudio.com/O365%20Core/_wiki/wikis/O365%20Core.wiki/622463/App-Platform-Feature-Experimentation-Guide?anchor=query-set-registry
- [ ] **Steps:**
  1. Download all TSV files
  2. Save to `query_sets/` subdirectory
- [ ] **Status:** Not started
- [ ] **Files saved to:** `query_sets/`

### 5. CIDebug Tool
- [ ] **URL:** https://msasg.visualstudio.com/Substrate/_git/SubstrateTools?path=/src/Tools/CIDebug
- [ ] **Steps:**
  1. Clone repository OR download as zip
  2. Compile with .NET 8
  3. Test by loading apps_index.bin
- [ ] **Status:** Not started
- [ ] **Compiled to:** _______________

## After Download

### Convert Binary Index
- [ ] Run CIDebug on `apps_index.bin`
- [ ] Export to `apps_index.json` or `apps_index.txt`
- [ ] Verify readable format

### Contact People
- [ ] **Loga Jegede** (loga.jegede@microsoft.com) - Get superset query set
- [ ] **Shu Cai** (caishu@microsoft.com) - Ask about data extraction tool
- [ ] **Paul Maree** (paulmaree@microsoft.com) - Report issues or ask questions

## Alternative: Have Paul Convert for You

If CIDebug doesn't work or is too complex:
- [ ] Send downloaded `apps_index.bin` to Paul Maree
- [ ] Ask him to convert to readable format
- [ ] He offered to do this in the email

## Directory Structure

```
data/m365/
├── DOWNLOAD_CHECKLIST.md (this file)
├── apps_index.bin (from ES Explorer)
├── apps_index.json (converted from binary)
├── AppsEntitySchema.settings.ini (schema definition)
└── query_sets/
    ├── query_set_1.tsv
    ├── query_set_2.tsv
    └── ... (more query sets)
```

## Notes

Date started: _____________
Date completed: _____________

Issues encountered:
-
-

Questions for Paul/team:
-
-
