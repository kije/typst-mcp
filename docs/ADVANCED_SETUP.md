# Advanced Setup Options

## Windows: WSL2 or Docker (Recommended)

Anthropic Sandbox Runtime **does not support Windows** natively. Windows users have two options for full sandboxing:

### Option 1: WSL2 (Recommended for Development)

Install Windows Subsystem for Linux 2 for native Linux sandboxing:

**1. Install WSL2:**
```powershell
# PowerShell (Administrator)
wsl --install
```

**2. Install Ubuntu (or your preferred distro):**
```powershell
wsl --install -d Ubuntu
```

**3. Inside WSL2, install dependencies:**
```bash
# Install Node.js, Python, Typst, Pandoc
sudo apt update
sudo apt install nodejs npm python3 python3-pip typst pandoc

# Install typst-mcp
pip install typst-mcp

# Run with full sandboxing
typst-mcp
# ✅ Sandboxing enabled (auto-downloaded via npx)
```

**4. Configure Claude Desktop to use WSL2:**
```json
{
  "mcpServers": {
    "typst": {
      "command": "wsl",
      "args": ["uvx", "typst-mcp"]
    }
  }
}
```

**Benefits:**
- ✅ Full OS-level sandboxing
- ✅ Native Linux performance
- ✅ Access to Windows files via `/mnt/c/`
- ✅ Better development experience

### Option 2: Docker (Recommended for Production)

Use Docker for containerized, isolated execution:

See "Docker Deployment" section below.

### Windows Without WSL2/Docker

If you cannot use WSL2 or Docker, the server runs with basic security:
- ✅ Pandoc `--sandbox` flag (CVE mitigation)
- ✅ 30-60 second timeouts
- ✅ Isolated temp directory
- ❌ No OS-level sandboxing

**To explicitly disable sandboxing (if needed):**
```powershell
uvx typst-mcp --disable-sandbox
```

**Note:** On Windows, sandboxing is automatically disabled with a warning. The `--disable-sandbox` flag is mainly useful for development/testing.

## Docker Deployment (Maximum Security)

For maximum isolation, especially on Windows:

**Dockerfile:**

```dockerfile
FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    nodejs \
    npm \
    typst \
    pandoc \
    && rm -rf /var/lib/apt/lists/*

# Install sandbox-runtime globally
RUN npm install -g @anthropic-ai/sandbox-runtime

# Create non-root user
RUN useradd -m -u 1000 typstuser
USER typstuser

# Install typst-mcp
RUN pip install --user typst-mcp

# Run server
CMD ["python", "-m", "typst_mcp.server"]
```

**Build and run:**

```bash
docker build -t typst-mcp .
docker run --rm -i typst-mcp
```

**With custom rules:**

```bash
docker run --rm -i \
  -e TYPST_MCP_ALLOW_DOMAINS="fonts.googleapis.com" \
  typst-mcp
```

## Air-Gapped / Offline Environments

For environments without internet access:

**1. Download dependencies on a machine with internet:**

```bash
# Download npm package
npm pack @anthropic-ai/sandbox-runtime

# Download Python package
pip download typst-mcp -d ./packages
```

**2. Transfer to air-gapped machine:**

```bash
# Install npm package from tarball
npm install -g ./anthropic-ai-sandbox-runtime-0.0.14.tgz

# Install Python package from directory
pip install --no-index --find-links=./packages typst-mcp
```

**3. Verify:**

```bash
srt --version
typst-mcp  # Should use local srt
```

## Performance Benchmarking

Compare startup times:

```bash
# npx (auto-download)
time (echo '{"jsonrpc":"2.0","id":1,"method":"initialize"}' | uvx typst-mcp)

# Global install
npm install -g @anthropic-ai/sandbox-runtime
time (echo '{"jsonrpc":"2.0","id":1,"method":"initialize"}' | uvx typst-mcp)

# Typical difference: ~50-100ms faster with global install
```

## Recommendation

**For most users:** Default npx approach is fine (zero-setup, automatic)

**For power users:** Global install (`npm install -g`) for faster startup

**For enterprises:** Local package.json with version pinning

**For maximum security:** Docker deployment with locked-down runtime
