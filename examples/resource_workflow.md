# Dynamic Package Resources Workflow

## How It Works

### Step 1: Discovery
```
User/LLM uses tool → search_packages("plotting")
                    ↓
Returns: ["cetz-plot", "plotst", "simpleplot", ...]
```

### Step 2: Fetch & Cache
```
User/LLM uses tool → get_package_docs("cetz-plot")
                    ↓
Fetches from GitHub (first time)
                    ↓
Caches locally at:
  ~/.cache/typst-mcp/package-docs/cetz-plot_0.2.0.json
                    ↓
Returns: {package, version, readme, metadata, ...}
```

### Step 3: Resource Creation (Automatic!)
```
After caching completes
                    ↓
Resource automatically available:
  typst://package/cetz-plot/0.2.0
                    ↓
Listed in:
  typst://packages/cached
```

### Step 4: Efficient Access
```
Subsequent access (Claude Desktop only)
                    ↓
Use resource → typst://package/cetz-plot/0.2.0
                    ↓
Instant response (no network fetch!)
                    ↓
Client-side caching benefits
```

## Example Session

```python
# 1. Search for packages
>>> search_packages("plot")
[
  {"name": "cetz-plot", "import": "@preview/cetz-plot", ...},
  {"name": "plotst", "import": "@preview/plotst", ...}
]

# 2. Fetch a package (via tool)
>>> get_package_docs("cetz-plot")
{
  "package": "cetz-plot",
  "version": "0.2.0",
  "readme": "...",
  "metadata": {...},
  "import_statement": "#import \"@preview/cetz-plot:0.2.0\": *"
}

# 3. Check available resources
>>> # Access resource: typst://packages/cached
{
  "cached_packages": [
    {
      "package": "cetz-plot",
      "version": "0.2.0",
      "uri": "typst://package/cetz-plot/0.2.0"
    }
  ],
  "count": 1
}

# 4. Access package via resource (fast!)
>>> # Access resource: typst://package/cetz-plot/0.2.0
{
  "package": "cetz-plot",
  "version": "0.2.0",
  ... (same as tool, but instant access)
}
```

## Benefits

### For All Clients (Tools)
✅ Universal compatibility
✅ Full error handling
✅ Discovery and search
✅ On-demand fetching

### For Claude Desktop (Resources)
✅ All tool benefits, PLUS:
✅ Efficient client-side caching
✅ Instant access to cached packages
✅ Better semantic understanding (resource URIs)
✅ No repeated network requests

## Resource URIs

### Static Resource
- `typst://packages/cached` - List of all cached packages

### Dynamic Resource Template
- `typst://package/{package_name}/{version}` - Individual package docs
- Examples:
  - `typst://package/cetz/0.4.2`
  - `typst://package/polylux/0.4.0`
  - `typst://package/tidy/0.4.3`

## Cache Management

**Location:** `~/.cache/typst-mcp/package-docs/` (macOS/Linux)

**Structure:**
```
package-docs/
├── cetz_0.4.2.json
├── polylux_0.4.0.json
└── tidy_0.4.3.json
```

**Persistence:** Cache persists across server restarts

**Updates:** Resources automatically reflect cache contents
