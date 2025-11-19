# Typst MCP Server

Typst MCP Server is an [MCP (Model Context Protocol)](https://github.com/modelcontextprotocol) implementation that helps AI models interact with [Typst](https://github.com/typst/typst), a markup-based typesetting system. The server provides tools for converting between LaTeX and Typst, validating Typst syntax, and generating images from Typst code.

## Available Tools

>⚠️ Currently all the functionality is implemented as `tools`, because Cursor and VS Code are not able to handle the other primitives yet.

The server provides the following tools:

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

5. **`typst_to_image(typst_snippet)`**: Renders Typst code to a PNG image.
   - Before sending complex Typst illustrations to the user, the LLM should render the code to an image and check if it looks correct.
   - Only relevant for multi modal models.

## Installation

### Prerequisites

Before installing the MCP server, ensure you have the following tools installed:

- **Rust and Cargo** (for building Typst documentation): [Install Rust](https://rustup.rs/)
- **Typst CLI** (for syntax validation and image generation): [Install Typst](https://github.com/typst/typst)
- **Pandoc** (for LaTeX to Typst conversion): [Install Pandoc](https://pandoc.org/installing.html)

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

**That's it!** The first time you run it, the server will:
1. Check if documentation exists
2. If not, automatically build it (takes 1-2 minutes, one-time only)
3. Start the MCP server

On subsequent runs, it starts instantly since docs are already built.

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
├── vendor/                 # Git submodules
│   └── typst/              # Typst source repository (submodule)
├── typst-docs/             # Generated documentation (gitignored)
│   ├── main.json           # Documentation JSON
│   └── assets/             # Documentation assets
├── pyproject.toml          # Package configuration
├── README.md               # This file
└── LICENSE                 # MIT License
```

## How It Works

1. **Automatic Setup**: On first run, the server checks if documentation exists. If not, it automatically builds it from the Typst source repository (included as a submodule). This is a one-time process.

2. **Documentation Generation**: Uses the Typst source via Cargo to generate comprehensive, up-to-date documentation in JSON format with all function signatures, examples, and descriptions.

3. **Smart Caching**: When using `uvx`, both the package and generated documentation are cached in the uvx environment. Subsequent runs start instantly without rebuilding.

4. **Runtime Dependencies**: The server checks for required tools (`typst`, `pandoc`) on startup and provides helpful installation instructions if any are missing.

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

### Submodule not initialized

If the typst submodule is missing:

```bash
git submodule update --init --depth=1 vendor/typst
```

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
