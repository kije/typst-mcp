# Typst MCP Server

Typst MCP Server is an [MCP (Model Context Protocol)](https://github.com/modelcontextprotocol) implementation that helps AI models interact with [Typst](https://github.com/typst/typst), a markup-based typesetting system. The server provides tools for converting between LaTeX and Typst, validating Typst syntax, and generating images from Typst code.

## Available Primitives

The server implements **multiple MCP primitives** for maximum compatibility and efficiency:

| Primitive | Claude Desktop | Claude Code | Cursor | VS Code |
|-----------|----------------|-------------|--------|---------|
| **Tools** | ✅ | ✅ | ✅ | ✅ |
| **Resources** | ✅ | ❌ | ❌ | ❌ |
| **Prompts** | ✅ | ❌ | ❌ | ❌ |

**Design Philosophy:** The server provides the same functionality via both **Tools** (universal compatibility) and **Resources** (efficient for documentation). Clients automatically use whichever primitives they support.

### Tools

All tools work in every MCP client:

1. **`list_docs_chapters()`**: Lists all chapters in the Typst documentation.
   - Lets the LLM get an overview of the documentation and select a chapter to read.
   - The LLM should select the relevant chapter to read based on the task at hand.

2. **`get_docs_chapter(route)`**: Retrieves a specific chapter from the Typst documentation.
   - Based on the chapter selected by the LLM, this tool retrieves the content of the chapter.
   - Also available as `get_docs_chapters(routes: list)` for retrieving multiple chapters at once.

3. **`latex_snippet_to_typst(latex_snippet)`**: Converts LaTeX code to Typst using Pandoc.
   - LLMs are better at writing LaTeX than Typst, so this tool helps convert LaTeX code to Typst.
   - Also available as `latex_snippets_to_typst(latex_snippets: list)` for converting multiple LaTeX snippets at once.

4. **`check_if_snippet_is_valid_typst_syntax(typst_snippet)`**: Validates Typst code.
   - Before sending Typst code to the user, the LLM should check if the code is valid.
   - Also available as `check_if_snippets_are_valid_typst_syntax(typst_snippets: list)` for validating multiple Typst snippets at once.

5. **`typst_snippet_to_image(typst_snippet)`**: Renders Typst code to a PNG image.
   - Before sending complex Typst illustrations to the user, the LLM should render the code to an image and check if it looks correct.
   - Only relevant for multi modal models.

6. **`search_packages(query, max_results=20)`**: Search for packages in Typst Universe.
   - Search through available Typst packages by name
   - Returns package info including names, URLs, and import statements
   - Useful for discovering packages before fetching their documentation

7. **`list_packages()`**: List all available packages in Typst Universe.
   - Returns a complete list of all packages (900+ packages)
   - Use `search_packages()` for filtered results

8. **`get_package_versions(package_name)`**: Get available versions for a package.
   - Fetches all published versions of a package
   - Versions returned in descending order (latest first)
   - Example: `get_package_versions("cetz")` → `["0.4.2", "0.4.1", "0.4.0", ...]`

9. **`get_package_docs(package_name, version=None, summary=False)`**: Fetch comprehensive documentation for a Typst Universe package.
   - **Summary mode** (`summary=True`): Lightweight discovery mode (~88% smaller)
     - Package metadata with homepage, repository, keywords
     - README preview (first 500 chars)
     - File listings for examples/ and docs/ (names and sizes only, no content)
     - Perfect for discovering package structure efficiently
   - **Full mode** (`summary=False`, default): Complete documentation
     - README, LICENSE, CHANGELOG (full content)
     - All .typ files from examples/ directory with full content
     - All files from docs/ directory (guides, tutorials, migration docs)
     - External links (homepage URL like polylux.dev, cetz-package.github.io)
   - Automatically fetches latest version if not specified
   - Results are cached locally for fast subsequent access
   - Example: `get_package_docs("polylux", summary=True)` → 1.8 KB vs 15.5 KB full

10. **`get_package_file(package_name, version, file_path)`**: Fetch a specific file from a package.
    - Granular access to individual files without fetching all documentation
    - Use after `get_package_docs(summary=True)` to discover available files
    - Perfect for targeted retrieval (e.g., fetch one example)
    - Example: `get_package_file("cetz", "0.2.2", "examples/plot.typ")` → Just that file
    - ~95% smaller than full package for single file access

### Resources

Available in **Claude Desktop** for efficient documentation access. Resources provide better caching and semantics for read-only data:

**Typst Core Documentation (RESTORED):**
- **`typst://docs/index`**: Index of all available documentation resources
- **`typst://docs/chapters`**: Complete list of all documentation chapters (same as `list_docs_chapters()` tool)
- **`typst://docs/chapter/{route}`**: Individual chapter content by route (same as `get_docs_chapter()` tool)
  - Lazy loading: First access may build docs (~1-2 min), subsequent instant (cached)
  - Example: `typst://docs/chapter/reference____layout____colbreak`

**Dynamic Package Resources (NEW!):**
- **`typst://packages/cached`**: List of all cached package documentation
  - Updates automatically as packages are fetched via tools
  - Shows package names, versions, and resource URIs

**Hierarchical Package Resources:**
- **`typst://package/{name}/{version}`** - Package summary (metadata + file listings)
- **`typst://package/{name}/{version}/readme`** - Full README content
- **`typst://package/{name}/{version}/examples`** - List all example files with URIs
- **`typst://package/{name}/{version}/examples/{filename}`** - Individual example file
- **`typst://package/{name}/{version}/docs`** - List all documentation files with URIs
- **`typst://package/{name}/{version}/docs/{filename}`** - Individual documentation file

**Resource Structure:**
```
typst://package/polylux/0.4.0              # Summary
├── readme                                 # Full README
├── examples                               # List examples
│   ├── demo.typ                          # Individual example
│   └── minimal.typ                       # Individual example
└── docs                                  # List docs
    └── guide.md                          # Individual doc
```

**Lazy Loading (WebDAV-like pattern):**
1. **Just access resources** - No tool call required!
   - `typst://package/polylux/0.4.0` - Auto-fetches if not cached (~3-5s first time)
   - Subsequent access instant (<10ms, cached)
2. **Or use tools** for explicit control
   - `get_package_docs()` for summary/full modes
   - `get_package_file()` for individual files
3. **Resources auto-fetch from GitHub** on first access
4. **Cached for efficiency** - fetch once, access forever
5. **Works like WebDAV MCP** - standard pattern for remote data

**Note:** If your client supports resources, they will be used automatically for better performance. Otherwise, the equivalent tools are used.

### Prompts (RESTORED)

Available in **Claude Desktop** for guided workflows. Prompts provide template-based interactions:

1. **`latex-to-typst-conversion`**: Guided workflow for converting LaTeX code to Typst with validation
   - Converts LaTeX to Typst using Pandoc
   - Validates generated Typst syntax
   - Provides error messages and suggestions

2. **`create-typst-document`**: Help create a new Typst document from scratch with proper structure
   - Guides through document type selection
   - Sets up proper structure and boilerplate
   - Includes best practices

3. **`fix-typst-syntax`**: Troubleshoot and fix Typst syntax errors with error analysis
   - Analyzes error messages
   - Suggests fixes
   - Validates corrected code

4. **`generate-typst-figure`**: Create figures, diagrams, or mathematical expressions in Typst
   - Can convert from LaTeX or create from description
   - Generates valid Typst code
   - Renders preview

5. **`typst-best-practices`**: Learn Typst best practices and common patterns for specific topics
   - Provides guidance on layout, styling, math, etc.
   - Shows idiomatic Typst code examples
   - Includes documentation references

## Installation

### Prerequisites

Before installing the MCP server, ensure you have the following tools installed:

- **Rust and Cargo** (for building Typst documentation): [Install Rust](https://rustup.rs/)
- **Typst CLI** (for syntax validation and image generation): [Install Typst](https://github.com/typst/typst)
- **Pandoc** (for LaTeX to Typst conversion): [Install Pandoc](https://pandoc.org/installing.html)

**Optional: Configure cache directory**
```bash
# Set custom cache location (optional)
export TYPST_MCP_CACHE_DIR="/path/to/custom/cache"
# Default locations:
# macOS: ~/Library/Caches/typst-mcp
# Linux: ~/.cache/typst-mcp
# Windows: %LOCALAPPDATA%\typst-mcp
```

**Quick install (macOS):**
```bash
brew install rust typst pandoc
```

**Quick install (Linux):**
```bash
# Install Rust
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# Install Typst and Pandoc (Ubuntu/Debian)
apt install typst pandoc

# Or use your distribution's package manager
```

### Option 1: Using uvx (Recommended for End Users)

The easiest way to use this MCP server is via `uvx`. **No manual setup required** - the server automatically builds documentation on first run:

```bash
# Just run the server - it handles everything automatically!
uvx typst-mcp
```

**Configure in Claude Desktop or other MCP clients:**

Add to your MCP configuration file (e.g., `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "typst": {
      "command": "uvx",
      "args": ["typst-mcp"]
    }
  }
}
```

**That's it!** The server uses **progressive disclosure** for maximum responsiveness:

**Immediate startup** (< 1 second):
- Server starts and responds to MCP initialization immediately
- **LaTeX conversion**, **syntax validation**, and **image rendering** tools are available instantly
- No timeout issues with MCP clients

**Background documentation loading**:
- Documentation loads/builds in a background thread
- Documentation query tools become available once loaded
- If docs need building (first run): takes 1-2 minutes in background
- If docs already exist: loads in ~1 second

This means **most tools work immediately** even on first run!

### Option 2: Development Installation

For development or contributing:

```bash
# Clone the repository
git clone https://github.com/johannesbrandenburger/typst-mcp.git
cd typst-mcp

# Initialize the typst submodule
git submodule update --init --depth=1

# Install dependencies
uv sync

# Run the server (builds docs automatically if needed)
uv run typst-mcp
```

**Or install locally with pip/uv:**

```bash
# Install in editable mode
uv pip install -e .

# Run server (builds docs automatically if needed)
typst-mcp
```

**Manual documentation build** (optional, for rebuilding):

```bash
# Force rebuild documentation
typst-mcp-build
```

## Running the Server

### Standalone Mode

Execute the server directly (automatically builds docs on first run):

```bash
# Using uvx (recommended)
uvx typst-mcp

# Or if installed locally
typst-mcp

# Or using Python module
python -m typst_mcp.server
```

**Note:** The server automatically checks for and builds documentation on startup. The first run takes 1-2 minutes for the build, subsequent runs start instantly.

### MCP Client Configuration

#### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "typst": {
      "command": "uvx",
      "args": ["typst-mcp"]
    }
  }
}
```

#### VS Code with Agent Mode

Configure in your VS Code MCP settings. See: [Agent mode in VS Code](https://code.visualstudio.com/blogs/2025/04/07/agentMode)

#### Using mcp CLI

```bash
mcp install typst-mcp
```

## Architecture

The project is structured as follows:

```
typst-mcp/
├── typst_mcp/              # Python package
│   ├── __init__.py         # Package initialization
│   ├── server.py           # MCP server implementation
│   └── build_docs.py       # Documentation build script
├── pyproject.toml          # Package configuration
├── README.md               # This file
└── LICENSE                 # MIT License

# Cache directory (created automatically):
~/.cache/typst-mcp/         # Linux
~/Library/Caches/typst-mcp/ # macOS
%LOCALAPPDATA%\typst-mcp\   # Windows
├── typst/                  # Cloned Typst repository
└── typst-docs/             # Generated documentation
    ├── main.json           # Documentation JSON
    └── assets/             # Documentation assets
```

## How It Works

1. **Progressive Tool Disclosure (Lazy Loading)**:
   - Server starts **immediately** (< 1 second) and responds to MCP initialization
   - **Non-doc tools available instantly**: LaTeX conversion, syntax validation, image rendering
   - **Doc tools load in background**: Documentation loading/building happens in a separate thread
   - **No client timeouts**: MCP clients never timeout waiting for initialization

2. **Automatic Documentation Setup**: In the background, the server checks if documentation exists in the cache directory. If not, it automatically:
   - Clones the Typst repository to the cache
   - Builds documentation from source
   - Stores everything in a persistent cache directory
   - **All while non-doc tools remain available**

3. **Documentation Generation**: Uses the Typst source via Cargo to generate comprehensive, up-to-date documentation in JSON format with all function signatures, examples, and descriptions.

4. **Smart Caching & Auto-Updates**:
   - Documentation is stored in platform-specific cache directories
   - Persists across `uvx` runs and system updates
   - **Automatic version tracking**: Tracks the Typst repository version used to build docs
   - **Auto-rebuild**: Automatically detects and rebuilds when a new Typst version is available
   - Subsequent server starts with same version are instant

5. **MCP Compatibility**: All build output goes to stderr, keeping stdout clean for JSON-RPC communication with MCP clients.

6. **Runtime Dependencies**: The server checks for required tools (`typst`, `pandoc`) on startup and provides helpful installation instructions if any are missing.

## Troubleshooting

### Documentation build issues

The server automatically builds documentation on first run. If the automatic build fails:

**Check Rust version** (requires Rust 1.89+):
```bash
rustc --version  # Should show 1.89 or higher
rustup update    # Update if needed
```

**Manually rebuild documentation:**
```bash
# Using uvx
uvx --from typst-mcp typst-mcp-build

# Or if installed locally
typst-mcp-build
```

### Missing external tools

If you see warnings about missing `typst` or `pandoc`:

- **macOS**: `brew install typst pandoc`
- **Linux**: `apt install typst pandoc` (or equivalent for your distribution)
- **Windows**: See installation links in Prerequisites section

### Updating to latest Typst documentation

The server automatically checks for Typst updates and rebuilds documentation when needed:

- On each server start, it checks if the Typst repository has been updated
- If a new version is detected, it automatically pulls the latest code and rebuilds
- Version tracking is stored in `~/.cache/typst-mcp/typst-docs/.version` (or platform equivalent)

**Manual cache clear** (if needed):

```bash
# macOS
rm -rf ~/Library/Caches/typst-mcp

# Linux
rm -rf ~/.cache/typst-mcp

# Windows
rmdir /s %LOCALAPPDATA%\typst-mcp
```

Then run the server again - it will automatically clone and build from scratch.

## JSON Schema of the Typst Documentation

>⚠️ The schema of the typst documentation is not stable and may change at any time. The schema is generated from the typst source code and is not guaranteed to be complete or correct. If the schema changes, this repository will need to be updated accordingly, so that the docs functionality works again.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- [Typst](https://github.com/typst/typst) - The amazing typesetting system
- [MCP](https://github.com/modelcontextprotocol) - Model Context Protocol
- [Pandoc](https://pandoc.org/) - Universal document converter
