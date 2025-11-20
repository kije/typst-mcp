# Security

## Overview

The typst-mcp server implements multiple security layers to safely execute external commands (Typst compiler, Pandoc converter) on user-provided code.

## Security Measures

### 1. OS-Level Sandboxing (Automatic, Zero-Setup)

The server uses [Anthropic Sandbox Runtime](https://github.com/anthropic-experimental/sandbox-runtime) to provide OS-level isolation for all subprocess execution.

**Platforms:**
- ✅ **macOS**: Full sandboxing via `sandbox-exec` (built-in)
- ✅ **Linux**: Full sandboxing via `bubblewrap` (auto-downloaded via npx)
- ⚠️ **Windows**: No native support - **Use WSL2 or Docker** for sandboxing (see below)

**Installation:**
- **Zero-setup**: If Node.js/npx is installed, sandboxing is automatically enabled on first run (downloads via `npx`)
- **Manual install**: `npm install -g @anthropic-ai/sandbox-runtime`
- **Fallback**: Server works without sandboxing (with clear warnings)

**How it Works:**
```bash
# With npx (automatic) - using settings file for security
npx -y @anthropic-ai/sandbox-runtime --settings /tmp/srt-settings-xyz.json -- typst compile file.typ

# Or if globally installed
srt --settings /tmp/srt-settings-xyz.json -- typst compile file.typ

# Settings file example (JSON):
{
  "filesystem": {
    "denyRead": ["~/.ssh", "~/.aws", ...],
    "allowWrite": [".", "/tmp"]
  },
  "network": {
    "allowedDomains": ["packages.typst.org", "*.github.com"]
  }
}
```

**Security Note:** Settings are passed via a temporary JSON file (not command-line args) to prevent shell injection and ensure proper quoting of paths with special characters.

### 2. Secure Settings File Creation

**Cross-Platform Security (NEW!):**

The server creates sandbox settings files with strict permissions to prevent tampering:

**Unix/Linux/macOS:**
- ✅ **Atomic creation**: `tempfile.mkstemp()` with `O_EXCL` flag (prevents race conditions)
- ✅ **File descriptor permissions**: `os.fchmod(fd, 0o600)` before writing (prevents symlink attacks)
- ✅ **Mode 0600**: Owner read/write only, no group/other access
- ✅ **Directory verification**: Checks parent directory is not writable by group/others

**Windows:**
- ✅ **Atomic creation**: Same `tempfile.mkstemp()` guarantees
- ✅ **ACL restrictions**: Uses `icacls` to grant access only to current user
- ✅ **Command**: `icacls <file> /inheritance:r /grant:r <username>:(R,W)`
- ✅ **Removes inherited permissions**: Prevents privilege escalation

**Attack Prevention:**
- 🛡️ **TOCTOU (Time-of-Check-Time-of-Use)**: Atomic operations prevent race conditions
- 🛡️ **Symlink attacks**: Operating on file descriptor (not path) prevents symlink substitution
- 🛡️ **Privilege escalation**: Restrictive permissions prevent other users from modifying sandbox config
- 🛡️ **Settings tampering**: File marked immutable before closing (no timing window)
- 🛡️ **Settings deletion**: OS-level immutability prevents malicious code from deleting and recreating settings
- 🛡️ **Defense in depth**: Multiple verification layers (creation, fchmod, fchflags/chattr, denyWrite)

**Security Layers (Defense in Depth):**

| Layer | Protection | macOS | Linux | Windows |
|-------|-----------|-------|-------|---------|
| **1** | Atomic creation (`O_EXCL`) | ✅ | ✅ | ✅ |
| **2** | Read-only permissions (0o400/R-only) | ✅ | ✅ | ✅ |
| **3** | Self-protection in config (`denyWrite`) | ✅ | ✅ | ✅ |
| **4** | OS immutability (atomic on fd) | ✅ fchflags | ✅ flock | ✅ FILE_ATTRIBUTE_READONLY |
| **5** | Deletion prevention | ✅ UF_IMMUTABLE | ⚠️ chattr +i (root) | ✅ ACL DENY DELETE |

**Platform-Specific Details:**

**macOS:**
- `fchflags(fd, UF_IMMUTABLE)` - Set immutability **before** closing (atomic, no timing window)
- `chflags uchg` - Fallback if fchflags fails
- User immutable flag can be removed by owner for cleanup

**Linux:**
- `os.dup(fd)` + `fcntl.flock(LOCK_EX)` - Advisory lock (best effort without root)
- `chattr +i` - Immutable flag (requires root or CAP_LINUX_IMMUTABLE capability)
- Advisory lock released on process exit (automatic cleanup)

**Windows:**
- `SetFileAttributesW()` - Set FILE_ATTRIBUTE_READONLY + FILE_ATTRIBUTE_HIDDEN
- `icacls` - Set ACL with READ-only permission + DENY DELETE
- Combined attributes + ACL provide strong protection

**Atomic Operation Sequences:**

**macOS:**
```python
fd = tempfile.mkstemp()              # Create with O_EXCL
os.fchmod(fd, 0o600)                 # Writable (for data write)
os.write(fd, settings_json)          # Write configuration with self-protection
os.fchmod(fd, 0o400)                 # Read-only (atomic on fd)
libc.fchflags(fd, UF_IMMUTABLE)      # Immutable (atomic on fd, BEFORE close!)
os.close(fd)                         # Fully protected, no timing window
```

**Linux (without root):**
```python
fd = tempfile.mkstemp()              # Create with O_EXCL
os.fchmod(fd, 0o600)                 # Writable (for data write)
os.write(fd, settings_json)          # Write configuration with self-protection
os.fchmod(fd, 0o400)                 # Read-only (atomic on fd)
lock_fd = os.dup(fd)                 # Duplicate fd for lock
fcntl.flock(lock_fd, LOCK_EX)        # Exclusive lock (held until process exit)
os.close(fd)                         # Original closed, lock_fd keeps lock alive
# Note: chattr +i would require root
```

**Windows:**
```python
fd = tempfile.mkstemp()              # Create with O_EXCL
os.write(fd, settings_json)          # Write configuration with self-protection
os.close(fd)
kernel32.SetFileAttributesW(path, READONLY | HIDDEN)  # File attributes
subprocess.run(["icacls", path, "/grant:r", "user:(R)", "/deny", "user:(DE)"])  # ACL
```

**No Timing Window (macOS/Linux)**: All critical protections set on file descriptor **before** closing, preventing any modification or deletion attempts during the transition.

**Example:**
```python
# Creates /tmp/xyz/srt-settings-abc123.json with:
# - Mode 0400 (read-only, owner only)
# - OS immutability flag (cannot be deleted or renamed)
# - Listed in its own denyWrite config (sandbox enforced)
#
# Result: Complete protection against tampering
# - Malicious code cannot modify settings
# - Malicious code cannot delete and recreate settings
# - Malicious code cannot weaken sandbox restrictions
```

### 3. Filesystem Restrictions

**Reads (Deny-List):**
- ❌ `~/.ssh` - SSH keys
- ❌ `~/.aws` - AWS credentials
- ❌ `~/.config` - User configuration files
- ❌ `~/.gnupg` - GPG keys
- ❌ `.env` - Environment files
- ✅ Everything else readable (allows `#image("../assets/logo.png")` in Typst)

**Writes (Allow-List):**
- ✅ Current directory (`.`) - User's project files
- ✅ System temp directory - Cross-platform (macOS: `/var/folders/.../T`, Linux: `/tmp`, Windows: `C:\Users\...\Temp`)
- ✅ Server temp directory - Internal operations (subdirectory of system temp)
- ❌ Everything else blocked

**Note**: The server uses `tempfile.gettempdir()` to automatically detect the correct temp directory for each platform, ensuring cross-platform compatibility.

### 4. Network Restrictions

**Allowed Domains:**
- ✅ `packages.typst.org` - Typst package repository
- ✅ `*.github.com` - Package sources (if needed)
- ❌ Everything else blocked

### 5. Process Timeouts

All external command executions have aggressive timeouts:
- **Pandoc**: 30 seconds
- **Typst compile (validation)**: 30 seconds
- **Typst compile (image)**: 60 seconds
- **Typst compile (PDF)**: 60 seconds

Prevents denial-of-service from infinite loops or resource-intensive operations.

### 6. Pandoc Sandboxing

Pandoc's built-in `--sandbox` flag is always enabled:
- Prevents arbitrary file inclusion (`\include{/etc/passwd}`)
- Blocks file write operations
- Mitigates CVE-2024-XXXX (arbitrary file write vulnerability in Pandoc 3.1.4+)

**Requirement:** Pandoc version ≥ 3.1.4

## Configuration

### Opt-Out

To disable OS-level sandboxing (not recommended):

```bash
uvx typst-mcp --disable-sandbox
```

**SECURITY NOTE:** Sandbox disable is a command-line flag ONLY (not an environment variable). This prevents malicious software from disabling security by setting `TYPST_MCP_DISABLE_SANDBOX=1`.

**When to opt-out:**
- Windows (no sandboxing available anyway)
- Debugging sandbox-related issues
- Development/testing environments

**Warning:** DO NOT disable sandboxing when processing untrusted code!

**Note:** Pandoc `--sandbox` and timeouts remain active even when opted out.

### Custom Sandbox Rules

You can customize sandbox behavior via environment variables and command-line flags:

#### Read Whitelist Mode (Advanced)

**SECURITY NOTE:** This mode flips from blacklist to whitelist for reads. Use with caution.

Enable via command-line flag (not environment variable for security):

```bash
uvx typst-mcp --read-allow-only "/home/user/projects,/tmp"
```

**How it works:**
- Only allows reading from specified paths
- **Still blocks sensitive files** (~/.ssh, ~/.aws, etc.) even in whitelist mode
- Useful for strict security policies or containerized environments

**Example with Claude Desktop:**
```json
{
  "mcpServers": {
    "typst": {
      "command": "uvx",
      "args": ["typst-mcp", "--read-allow-only", "/Users/me/typst-projects"]
    }
  }
}
```

**Security guarantees:**
- ✅ Sensitive paths ALWAYS blocked (even if in whitelist)
- ✅ Cannot be set via ENV variable (prevents malicious code)
- ✅ Must be explicit command-line flag
- ✅ All paths must be specified in flag (no ENV variable additions)

**Adding multiple paths:**
```bash
# Comma-separated paths in the flag itself
uvx typst-mcp --read-allow-only "/base/path,~/additional-path,/tmp"
```

**Important:** There is NO `TYPST_MCP_ALLOW_READ` environment variable for security reasons. All read-allow paths must be explicitly specified in the `--read-allow-only` flag.

#### Additional Deny-Read Paths (Blacklist Mode)

Block additional paths from being read (only in default blacklist mode):

```bash
export TYPST_MCP_DENY_READ="~/.custom-secrets,/company/confidential"
uvx typst-mcp
```

**Use case:** Block custom credential locations, proprietary data directories

#### Additional Allow-Write Paths

Allow writing to specific locations:

```bash
export TYPST_MCP_ALLOW_WRITE="/var/log/typst,~/build-artifacts"
uvx typst-mcp
```

**Use case:** Build systems that need to write to specific output directories

#### Additional Allowed Domains

Allow network access to additional domains:

```bash
export TYPST_MCP_ALLOW_DOMAINS="fonts.googleapis.com,cdn.mycompany.com"
uvx typst-mcp
```

**Use case:** Custom package repositories, private CDNs, font services

#### Combine Multiple Rules

```bash
export TYPST_MCP_DENY_READ="~/.private,/secret"
export TYPST_MCP_ALLOW_WRITE="~/typst-output,/build"
export TYPST_MCP_ALLOW_DOMAINS="mycdn.com,packages.internal"
uvx typst-mcp
```

#### Claude Desktop Configuration with Custom Rules

```json
{
  "mcpServers": {
    "typst": {
      "command": "uvx",
      "args": ["typst-mcp"],
      "env": {
        "TYPST_MCP_ALLOW_DOMAINS": "fonts.googleapis.com,mycdn.com",
        "TYPST_MCP_ALLOW_WRITE": "/Users/me/typst-projects/output",
        "TYPST_MCP_DENY_READ": "~/.private-keys"
      }
    }
  }
}
```

**Note:** Custom rules are **additive** - they extend the default rules, not replace them. Default protections (like blocking `~/.ssh`) remain active.

## Security Model

### What is Protected

✅ **Sensitive files** cannot be read by malicious Typst/LaTeX code
✅ **System files** cannot be written
✅ **Credentials** cannot be exfiltrated
✅ **DoS attacks** are prevented via timeouts
✅ **Pandoc exploits** are mitigated

### What is NOT Protected

❌ **Local file references** - User can still read project files (by design)
❌ **CPU/Memory exhaustion** - Monitored but not hard-limited (platform-dependent)
❌ **Side-channel attacks** - Not addressed

### Threat Model

**Assumptions:**
1. User is compiling potentially untrusted Typst/LaTeX code
2. User's local files should remain accessible (for `#image()`, `#include()`)
3. User's credentials should NOT be accessible
4. Network access should be limited to package repositories

**Not Protected Against:**
- Physical access attacks
- Compromised Node.js/Python runtime
- Kernel-level vulnerabilities
- Social engineering

## Windows Users: Security Recommendations

Anthropic Sandbox Runtime does **not support Windows** natively. For full sandboxing on Windows:

### Recommended: WSL2 (Best for Development)

**Install WSL2:**
```powershell
wsl --install -d Ubuntu
```

**Inside WSL2:**
```bash
sudo apt install nodejs npm typst pandoc
pip install typst-mcp
typst-mcp  # ✅ Full sandboxing
```

**Configure Claude Desktop:**
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

### Alternative: Docker (Best for Production)

See Docker deployment section in `docs/ADVANCED_SETUP.md`

### Windows Without WSL2/Docker

If you cannot use WSL2 or Docker, the server provides basic security:
- ✅ Pandoc `--sandbox` flag
- ✅ Process timeouts
- ✅ Isolated temp directory
- ❌ No filesystem/network isolation

**Not recommended for untrusted code.**

## Verifying Security

### Check Sandbox Status

On startup, the server displays:

```
✅ Sandboxing enabled (Anthropic Sandbox Runtime)
```

Or:

```
ℹ️  Enhanced Security Available (Optional)
Sandboxing tools not found. Server runs with basic security.
```

### Test Sandbox

```bash
# Should fail with permission denied
echo '#include("~/.ssh/id_rsa")' | uvx typst-mcp

# Should succeed
echo '#include("./myfile.typ")' | uvx typst-mcp
```

## Reporting Security Issues

**DO NOT** open public issues for security vulnerabilities.

Please report security issues privately to: [Your Contact Method]

Include:
- Description of the vulnerability
- Steps to reproduce
- Impact assessment
- Suggested fix (if any)

## Security Updates

Stay updated with security patches:

```bash
# Update via uvx (recommended)
uvx --upgrade typst-mcp

# Or update sandbox runtime
npm update -g @anthropic-ai/sandbox-runtime
```

## References

- [Anthropic Sandbox Runtime](https://github.com/anthropic-experimental/sandbox-runtime)
- [MCP Security Best Practices](https://modelcontextprotocol.io/specification/draft/basic/security_best_practices)
- [Pandoc Security](https://pandoc.org/MANUAL.html#option--sandbox)
- [OWASP Gen AI Security - MCP CheatSheet](https://genai.owasp.org/resource/cheatsheet-a-practical-guide-for-securely-using-third-party-mcp-servers-1-0/)

## License

Security implementations based on Anthropic's Sandbox Runtime (Apache 2.0).
