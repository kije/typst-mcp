"""
Zero-setup sandboxing for typst-mcp using Anthropic Sandbox Runtime.

Uses npx to auto-download @anthropic-ai/sandbox-runtime with graceful degradation.
Environment variable TYPST_MCP_DISABLE_SANDBOX=1 to opt-out.
"""

import os
import sys
import shutil
import subprocess
import json
from pathlib import Path
from typing import Optional, List, Union


def eprint(*args, **kwargs):
    """Print to stderr to avoid breaking MCP JSON-RPC communication."""
    print(*args, file=sys.stderr, **kwargs)


class SandboxConfig:
    """Configuration for sandbox filesystem and network restrictions."""

    # SECURITY: These paths are ALWAYS blocked, even in whitelist mode
    # Prevents malicious code from setting READ_ALLOW_ONLY="/*"
    ALWAYS_DENY_READ = [
        "~/.ssh",
        "~/.aws",
        "~/.config/gcloud",
        "~/.config/gh",  # GitHub CLI credentials
        "~/.gnupg",
        "~/.kube",  # Kubernetes configs
        "~/.docker",  # Docker credentials
        ".env",
        ".git/config",
        "~/.netrc",  # Network credentials
        "~/.npmrc",  # npm credentials
        "~/.pypirc",  # PyPI credentials
    ]

    def __init__(self, temp_dir: str, current_dir: str = ".", read_allow_only: list = None):
        self.temp_dir = temp_dir
        self.current_dir = current_dir

        # SECURITY: Always block sensitive paths (even in whitelist mode)
        self.always_deny_read = [os.path.expanduser(p) for p in self.ALWAYS_DENY_READ]

        # Read mode: blacklist (default) or whitelist
        self.read_whitelist_mode = read_allow_only is not None
        if self.read_whitelist_mode:
            # Whitelist mode: Only allow specific paths
            self.allow_read = [os.path.expanduser(p) for p in read_allow_only]
            self.deny_read = self.always_deny_read  # Still block sensitive files
            eprint(f"🔒 Read whitelist mode: Only allowing {len(self.allow_read)} paths")
            eprint(f"   (Still blocking {len(self.always_deny_read)} sensitive paths)")
        else:
            # Blacklist mode (default): Allow most, block sensitive
            self.deny_read = self.always_deny_read.copy()
            self.allow_read = None

        # Filesystem: Allow-list for writes (deny most, allow specific)
        import tempfile
        system_temp_dir = tempfile.gettempdir()  # Cross-platform temp directory

        # SECURITY: Use system temp directory for cross-platform compatibility
        # - macOS: /var/folders/<hash>/<hash>/T (user-specific, mode 0700)
        # - Linux: /tmp (shared, mode 1777 with sticky bit)
        # - Windows: C:\Users\<user>\AppData\Local\Temp (user-specific)
        #
        # Why not hardcode /tmp?
        # - On macOS, /tmp (/private/tmp) is separate from the actual temp directory
        # - Python's tempfile module uses the system temp, not /tmp
        # - typst_snippet_to_pdf uses tempfile.TemporaryDirectory() which needs system temp
        #
        # Security notes:
        # - macOS /var/folders/<user>/ has restrictive permissions (0700, owner-only)
        # - Linux /tmp has sticky bit, so users can only delete their own files
        # - Windows temp is user-specific by default

        self.allow_write = [
            current_dir,
            system_temp_dir,
            temp_dir,  # Subdirectory of system_temp_dir (redundant but explicit)
        ]

        # Network: Allow package fetching only
        self.allowed_domains = [
            "packages.typst.org",
            "*.github.com",  # For package sources if needed
        ]

        # Load custom rules from environment variables
        self._load_custom_rules()

    def _load_custom_rules(self):
        """Load and apply custom sandbox rules from environment variables."""
        # Additional deny-read paths (only in blacklist mode)
        if not self.read_whitelist_mode:
            if custom_deny := os.getenv("TYPST_MCP_DENY_READ"):
                paths = [p.strip() for p in custom_deny.split(",") if p.strip()]
                expanded = [os.path.expanduser(p) for p in paths]
                self.deny_read.extend(expanded)
                eprint(f"Custom deny-read paths: {expanded}")

        # NOTE: TYPST_MCP_ALLOW_READ is intentionally NOT supported
        # Rationale: Environment variables can be easily set by malicious software
        # In whitelist mode, ALL paths must be specified via --read-allow-only flag
        # This ensures explicit, visible configuration that's harder to manipulate

        # Additional allow-write paths
        if custom_allow_write := os.getenv("TYPST_MCP_ALLOW_WRITE"):
            paths = [p.strip() for p in custom_allow_write.split(",") if p.strip()]
            expanded = [os.path.expanduser(p) for p in paths]
            self.allow_write.extend(expanded)
            eprint(f"Custom allow-write paths: {expanded}")

        # Additional allowed domains
        if custom_domains := os.getenv("TYPST_MCP_ALLOW_DOMAINS"):
            domains = [d.strip() for d in custom_domains.split(",") if d.strip()]
            self.allowed_domains.extend(domains)
            eprint(f"Custom allowed domains: {domains}")

    def to_srt_settings(self) -> dict:
        """Convert config to srt settings dictionary."""
        settings = {
            "filesystem": {},
            "network": {}
        }

        # Read restrictions
        if self.read_whitelist_mode:
            # Whitelist mode: Only allow specific paths
            settings["filesystem"]["allowRead"] = self.allow_read
            # SECURITY: Always deny sensitive paths even in whitelist mode
            settings["filesystem"]["denyRead"] = self.always_deny_read
        else:
            # Blacklist mode: Deny specific paths
            settings["filesystem"]["denyRead"] = self.deny_read

        # Write restrictions (always whitelist)
        settings["filesystem"]["allowWrite"] = self.allow_write

        # Network restrictions
        settings["network"]["allowedDomains"] = self.allowed_domains
        settings["network"]["deniedDomains"] = []  # Empty list (allow takes precedence)

        return settings


class TypestSandbox:
    """Platform-aware sandboxing with zero manual setup."""

    def __init__(self, temp_dir: str, read_allow_only: Optional[List[str]] = None, disable_sandbox: bool = False):
        self.temp_dir = Path(temp_dir)
        self.sandboxed = False
        self.sandbox_method = None
        self.config = SandboxConfig(str(self.temp_dir), os.getcwd(), read_allow_only)
        self.settings_file = None  # Path to temporary settings file
        self.settings_immutable = False  # Whether immutability flag is set
        self._lock_fd = None  # Linux: file descriptor for advisory lock

        # SECURITY: Sandbox disable is command-line flag only (not ENV variable)
        # Rationale: Malicious software could set TYPST_MCP_DISABLE_SANDBOX=1
        # and completely bypass all security protections
        self.disabled = disable_sandbox

    def initialize(self) -> bool:
        """
        Initialize sandboxing with auto-detection and graceful degradation.

        Returns:
            bool: True if sandboxing is active, False if using fallback.
        """
        if self.disabled:
            eprint("\n" + "="*60)
            eprint("ℹ️  Sandboxing Disabled")
            eprint("="*60)
            eprint("TYPST_MCP_DISABLE_SANDBOX is set.")
            eprint("Running without OS-level sandboxing.")
            eprint("="*60 + "\n")
            return False

        # Try 1: Check if srt is globally installed
        if shutil.which("srt"):
            self.sandboxed = True
            self.sandbox_method = "srt-installed"
            self._create_settings_file()
            eprint("✅ Sandboxing enabled (Anthropic Sandbox Runtime)")
            return True

        # Try 2: Check if we can use npx (Node.js installed)
        if shutil.which("npx"):
            # Test if npx can run srt (will auto-download on first use)
            try:
                result = subprocess.run(
                    ["npx", "-y", "@anthropic-ai/sandbox-runtime", "--version"],
                    capture_output=True,
                    timeout=30,  # First download may take time
                    text=True
                )
                if result.returncode == 0:
                    self.sandboxed = True
                    self.sandbox_method = "srt-npx"
                    self._create_settings_file()
                    eprint("✅ Sandboxing enabled (auto-downloaded via npx)")
                    return True
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
                pass

        # Fallback: No sandboxing available
        self._print_fallback_message()
        return False

    def _create_settings_file(self):
        """
        Create a temporary settings file for srt with secure permissions.

        SECURITY: Uses atomic operations to prevent TOCTOU attacks:
        - tempfile.mkstemp() creates file with O_EXCL (prevents race conditions)
        - os.fchmod() sets permissions on file descriptor (atomic, no symlink attack on Unix)
        - File is mode 0600 (owner read/write only) on Unix
        - On Windows, uses icacls for proper ACL restrictions

        Cross-platform: Works on Unix (Linux/macOS/BSD) and Windows
        """
        import tempfile
        import stat
        import platform

        is_windows = platform.system() == "Windows"

        # SECURITY: Verify parent directory has restricted permissions (Unix only)
        if not is_windows:
            parent_dir = Path(self.temp_dir)
            if parent_dir.exists():
                parent_stat = parent_dir.stat()
                # Check if directory is writable by others (potential security risk)
                if parent_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    eprint(f"⚠️  WARNING: Temp directory {parent_dir} is writable by group/others")
                    eprint("   This may allow settings file tampering")

        # SECURITY: Create settings file with O_EXCL (atomic, prevents race conditions)
        # tempfile.mkstemp() uses O_EXCL by default
        fd, path = tempfile.mkstemp(
            suffix=".json",
            prefix="srt-settings-",
            dir=self.temp_dir,
            text=False  # Binary mode for explicit encoding
        )

        try:
            # SECURITY: Set restrictive permissions atomically using file descriptor
            # Initially set to 0o600 for writing, will change to 0o400 (read-only) after
            if not is_windows:
                # Unix: Use fchmod on file descriptor (atomic, prevents symlink attacks)
                # Mode 0o600 = owner read/write only (no group/other access)
                os.fchmod(fd, 0o600)
            # Windows: Will set ACLs after closing file (see below)

            # SECURITY: Add settings file path to denyWrite to prevent tampering from within sandbox
            # This prevents TOCTOU attack where malicious code modifies sandbox config
            settings = self.config.to_srt_settings()

            # Add self-protection: deny writes to the settings file itself
            if "denyWrite" not in settings["filesystem"]:
                settings["filesystem"]["denyWrite"] = []
            settings["filesystem"]["denyWrite"].append(path)  # Protect settings file from modification

            settings_json = json.dumps(settings, indent=2).encode('utf-8')
            os.write(fd, settings_json)

            # Verify file was written correctly (sanity check)
            if os.fstat(fd).st_size == 0:
                raise RuntimeError("Failed to write settings file (0 bytes)")

            # SECURITY: Make file read-only after writing (prevents modification)
            # Mode 0o400 = owner read-only (srt needs to read it, but nobody should modify)
            # This prevents tampering even by the user running the process
            if not is_windows:
                os.fchmod(fd, 0o400)  # Atomic: change permissions before closing

                # SECURITY: Set immutability flag on file descriptor (before closing)
                # This is more secure than setting after close (eliminates timing window)
                if platform.system() == "Darwin":
                    # macOS: Use fchflags on file descriptor (atomic)
                    try:
                        import ctypes
                        import ctypes.util

                        # Load libc
                        libc = ctypes.CDLL(ctypes.util.find_library('c'))

                        # fchflags(int fd, unsigned int flags)
                        # UF_IMMUTABLE = 0x00000002 (user immutable flag)
                        UF_IMMUTABLE = 0x00000002

                        result = libc.fchflags(fd, UF_IMMUTABLE)
                        if result == 0:
                            self.settings_immutable = True
                            eprint("   Immutability: macOS uchg flag set atomically via fchflags()")
                        else:
                            eprint("   Immutability: fchflags() failed, will set after close")
                    except Exception:
                        eprint("   Immutability: fchflags() not available, will set after close")

                elif platform.system() == "Linux":
                    # Linux: Try advisory file locking (best effort without root)
                    # This provides some protection even without chattr +i
                    try:
                        import fcntl

                        # Duplicate fd before we close it (lock will remain on dup)
                        # This allows us to keep the lock alive while closing original fd
                        lock_fd = os.dup(fd)

                        # Set exclusive lock (prevents other processes from writing)
                        # LOCK_NB = non-blocking (fail immediately if can't lock)
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

                        # Store fd to keep lock alive (released when fd closed or process exits)
                        self._lock_fd = lock_fd
                        eprint("   Advisory lock: Linux exclusive lock set (best effort without root)")
                    except Exception:
                        eprint("   Advisory lock: Could not set, relying on denyWrite and chattr")

            # SECURITY NOTE: 0o400 prevents modification but NOT deletion (on Unix)
            # Deletion is a directory permission, not a file permission
            # Will set immutability flag after closing if not already set (Linux/Windows)

        except Exception as e:
            # Clean up on failure
            os.close(fd)
            try:
                os.unlink(path)
            except:
                pass
            raise RuntimeError(f"Failed to create secure settings file: {e}")
        finally:
            os.close(fd)

        # SECURITY: Set OS-level immutability to prevent deletion/renaming (if not already set)
        # This prevents TOCTOU attack where malicious code deletes settings and creates new one
        # Note: On macOS, this may already be set via fchflags() before closing
        if not is_windows and not self.settings_immutable:
            try:
                # macOS/BSD: Use chflags to set user immutable flag (fallback if fchflags failed)
                # Linux: Try chattr (requires root, so will likely fail)
                if platform.system() == "Darwin":
                    # macOS: uchg = user immutable (can be unset by owner for cleanup)
                    subprocess.run(
                        ["chflags", "uchg", path],
                        check=True,
                        capture_output=True,
                        timeout=5
                    )
                    self.settings_immutable = True
                    eprint("   Immutability: macOS uchg flag set via chflags (fallback)")
                elif platform.system() == "Linux":
                    # Linux: +i flag (requires root, so this will likely fail)
                    # We still try in case process has CAP_LINUX_IMMUTABLE capability
                    result = subprocess.run(
                        ["chattr", "+i", path],
                        capture_output=True,
                        timeout=5
                    )
                    if result.returncode == 0:
                        self.settings_immutable = True
                        eprint("   Immutability: Linux +i flag set (prevents deletion/rename)")
                    else:
                        eprint("   Immutability: Linux +i failed (requires root), relying on denyWrite + advisory lock")
            except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
                # Not critical - we still have denyWrite protection in sandbox config
                eprint("   Immutability: Could not set OS flag, relying on denyWrite protection")

        # SECURITY: Platform-specific permission verification and hardening
        if is_windows:
            # Windows: Use ctypes to set file attributes and deny DELETE permission
            try:
                import getpass
                username = getpass.getuser()

                # Step 1: Set read-only attribute (prevents modification)
                try:
                    kernel32 = ctypes.windll.kernel32
                    FILE_ATTRIBUTE_READONLY = 0x00000001
                    FILE_ATTRIBUTE_HIDDEN = 0x00000002

                    # Set as read-only + hidden for extra protection
                    result = kernel32.SetFileAttributesW(path, FILE_ATTRIBUTE_READONLY | FILE_ATTRIBUTE_HIDDEN)
                    if result:
                        eprint("   Immutability: Windows read-only + hidden attributes set")
                    else:
                        eprint("   WARNING: SetFileAttributes failed")
                except Exception as e:
                    eprint(f"   WARNING: Could not set file attributes: {e}")

                # Step 2: Set restrictive ACL with DENY DELETE (prevents deletion)
                try:
                    # Remove inherited permissions and grant only read to current user
                    # Also explicitly DENY delete permission
                    subprocess.run(
                        ["icacls", path, "/inheritance:r",
                         "/grant:r", f"{username}:(R)",  # Read-only (not R,W)
                         "/deny", f"{username}:(DE)"],    # Deny DELETE
                        check=True,
                        capture_output=True,
                        timeout=5
                    )
                    self.settings_immutable = True
                    eprint(f"   Settings file: {path} (Windows ACL: {username} R-only, DELETE denied)")
                except Exception as e:
                    eprint(f"   WARNING: Failed to set Windows ACLs: {e}")
                    eprint("   Settings file may be modifiable")
            except Exception as e:
                eprint(f"   WARNING: Windows security setup failed: {e}")
        else:
            # Unix: Verify final file permissions (defense in depth)
            file_stat = os.stat(path)
            expected_mode = stat.S_IRUSR  # 0o400 (read-only after immutability)
            actual_mode = stat.S_IMODE(file_stat.st_mode)

            if actual_mode != expected_mode:
                eprint("⚠️  WARNING: Settings file permissions unexpected")
                eprint(f"   Expected: {oct(expected_mode)}, Got: {oct(actual_mode)}")

            eprint(f"   Settings file: {path} (mode: {oct(actual_mode)})")

        self.settings_file = path

    def cleanup(self):
        """
        Clean up sandbox resources, removing immutability flags if needed.

        SECURITY: This must be called before deleting temp directory,
        otherwise immutable files cannot be removed by the OS.
        """
        if not self.settings_file or not self.settings_immutable:
            return

        try:
            import platform as platform_module
            import sys

            # Check if Python is shutting down
            if sys.meta_path is None or platform_module is None:
                return

            system = platform_module.system()
            if system is None:
                return

            if system == "Darwin":
                # macOS: Remove uchg flag
                subprocess.run(
                    ["chflags", "nouchg", self.settings_file],
                    capture_output=True,
                    timeout=5
                )
                eprint(f"   Cleanup: Removed macOS immutability flag from {self.settings_file}")
            elif system == "Linux":
                # Linux: Remove +i flag (requires root)
                subprocess.run(
                    ["chattr", "-i", self.settings_file],
                    capture_output=True,
                    timeout=5
                )
                # Release advisory lock if held
                if self._lock_fd:
                    try:
                        import fcntl
                        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                        os.close(self._lock_fd)
                        self._lock_fd = None
                    except Exception:
                        pass
                eprint(f"   Cleanup: Removed Linux immutability flag from {self.settings_file}")
            elif system == "Windows":
                # Windows: Remove read-only attribute and restore ACL
                try:
                    kernel32 = ctypes.windll.kernel32
                    FILE_ATTRIBUTE_NORMAL = 0x00000080
                    kernel32.SetFileAttributesW(self.settings_file, FILE_ATTRIBUTE_NORMAL)
                    eprint(f"   Cleanup: Removed Windows file attributes from {self.settings_file}")
                except Exception:
                    pass

            self.settings_immutable = False
        except Exception as e:
            # Suppress errors during shutdown
            import sys
            if sys.meta_path is not None:
                eprint(f"   Warning: Could not remove immutability flag: {e}")
                try:
                    import platform as pm
                    if pm and pm.system() == "Darwin":
                        eprint(f"   You may need to manually remove: chflags nouchg {self.settings_file}")
                    elif pm and pm.system() == "Linux":
                        eprint(f"   You may need to manually remove: sudo chattr -i {self.settings_file}")
                except:
                    pass

    def __del__(self):
        """Cleanup on garbage collection."""
        self.cleanup()

    def _print_fallback_message(self):
        """Print informative message about sandboxing unavailability."""
        eprint("\n" + "="*60)
        eprint("ℹ️  Enhanced Security Available (Optional)")
        eprint("="*60)
        eprint("Sandboxing tools not found. Server runs with basic security.")
        eprint("\nCurrent security measures:")
        eprint("  ✓ Pandoc --sandbox flag (prevents file writes)")
        eprint("  ✓ 30-60 second timeouts (prevents DoS)")
        eprint("  ✓ Isolated temp directory")
        eprint("\nFor enhanced security with OS-level sandboxing:")
        eprint("  Option 1: Install Node.js (npx auto-downloads sandbox)")
        eprint("            https://nodejs.org/")
        eprint("  Option 2: Install sandbox-runtime globally")
        eprint("            npm install -g @anthropic-ai/sandbox-runtime")
        eprint("\nTo disable this message:")
        eprint("  export TYPST_MCP_DISABLE_SANDBOX=1")
        eprint("="*60 + "\n")

    def wrap_command(
        self,
        command: Union[str, List[str]]
    ) -> Union[str, List[str]]:
        """
        Wrap command with sandboxing if available.

        Args:
            command: Command to execute (string or list)

        Returns:
            Wrapped command if sandboxing is active, original command otherwise
        """
        if not self.sandboxed or not self.settings_file:
            return command

        # Convert command to list if string
        if isinstance(command, str):
            import shlex
            cmd_list = shlex.split(command)
        else:
            cmd_list = list(command)

        # Build sandboxed command using settings file
        if self.sandbox_method == "srt-installed":
            # Use globally installed srt
            sandboxed = ["srt", "--settings", self.settings_file, "--"] + cmd_list
        elif self.sandbox_method == "srt-npx":
            # Use npx to run srt
            sandboxed = [
                "npx", "-y", "@anthropic-ai/sandbox-runtime",
                "--settings", self.settings_file,
                "--"
            ] + cmd_list
        else:
            return command

        # Return in same format as input
        if isinstance(command, str):
            import shlex
            return " ".join(shlex.quote(arg) for arg in sandboxed)
        else:
            return sandboxed

    def run_sandboxed(
        self,
        command: Union[str, List[str]],
        **kwargs
    ) -> subprocess.CompletedProcess:
        """
        Execute command with sandboxing if available.

        Args:
            command: Command to execute
            **kwargs: Additional subprocess arguments

        Returns:
            CompletedProcess result
        """
        wrapped = self.wrap_command(command)

        # Execute
        if isinstance(wrapped, list):
            return subprocess.run(wrapped, **kwargs)
        else:
            return subprocess.run(wrapped, shell=True, **kwargs)


# Global sandbox instance (initialized in main())
_sandbox: Optional[TypestSandbox] = None


def initialize_sandbox(temp_dir: str, argv: Optional[List[str]] = None) -> bool:
    """
    Initialize global sandbox instance.

    Args:
        temp_dir: Temporary directory for sandbox operations
        argv: Command-line arguments (defaults to sys.argv)

    Returns:
        bool: True if sandboxing is active
    """
    global _sandbox

    # Parse command-line arguments
    read_allow_only = None
    disable_sandbox = False

    if argv is None:
        argv = sys.argv

    # Look for --read-allow-only flag
    if "--read-allow-only" in argv:
        idx = argv.index("--read-allow-only")
        if idx + 1 < len(argv):
            # Paths are comma-separated
            paths_str = argv[idx + 1]
            read_allow_only = [p.strip() for p in paths_str.split(",") if p.strip()]
            eprint(f"⚠️  Read whitelist mode enabled via command-line")
            eprint(f"   Allowing reads only from: {read_allow_only}")

    # SECURITY: --disable-sandbox flag (command-line only, NOT env variable)
    if "--disable-sandbox" in argv:
        disable_sandbox = True
        eprint("\n" + "="*60)
        eprint("⚠️  SECURITY WARNING: Sandboxing DISABLED")
        eprint("="*60)
        eprint("Sandboxing has been explicitly disabled via --disable-sandbox flag.")
        eprint("This should ONLY be used for:")
        eprint("  • Debugging sandbox issues")
        eprint("  • Development/testing")
        eprint("  • Environments where sandboxing is not available")
        eprint("\nDO NOT disable sandboxing when processing untrusted code!")
        eprint("="*60 + "\n")

    _sandbox = TypestSandbox(temp_dir, read_allow_only, disable_sandbox)
    return _sandbox.initialize()


def get_sandbox() -> Optional[TypestSandbox]:
    """Get the global sandbox instance."""
    return _sandbox


def wrap_command(command: Union[str, List[str]]) -> Union[str, List[str]]:
    """
    Convenience function to wrap command with global sandbox.

    Args:
        command: Command to wrap

    Returns:
        Wrapped command if sandbox is active, original otherwise
    """
    if _sandbox is None:
        return command
    return _sandbox.wrap_command(command)


def run_sandboxed(
    command: Union[str, List[str]],
    **kwargs
) -> subprocess.CompletedProcess:
    """
    Convenience function to run command with global sandbox.

    Args:
        command: Command to execute
        **kwargs: subprocess arguments

    Returns:
        CompletedProcess result
    """
    if _sandbox is None:
        # Sandbox not initialized - run directly (shouldn't happen in practice)
        if isinstance(command, list):
            return subprocess.run(command, **kwargs)
        else:
            return subprocess.run(command, shell=True, **kwargs)

    return _sandbox.run_sandboxed(command, **kwargs)


def secure_copy_file(source: str, destination: str, timeout: int = 10) -> None:
    """
    Securely copy a file using sandboxed OS commands.

    SECURITY: This function uses the OS copy command (cp/copy) inside the sandbox,
    so filesystem restrictions are enforced by the sandbox runtime, not by Python.
    This is much safer than rolling our own path validation logic.

    Args:
        source: Source file path (absolute)
        destination: Destination file path (absolute)
        timeout: Command timeout in seconds (default: 10)

    Raises:
        subprocess.CalledProcessError: If copy fails (permission denied, path not allowed, etc.)
        FileNotFoundError: If source file doesn't exist
        RuntimeError: If destination file wasn't created

    Cross-platform:
        - Linux/macOS: Uses 'cp' command
        - Windows: Uses 'copy' command (falls back to 'cp' if Git Bash is available)
    """
    import platform

    # Verify source exists before attempting copy
    if not os.path.exists(source):
        raise FileNotFoundError(f"Source file does not exist: {source}")

    # Determine which copy command to use (cross-platform)
    system = platform.system()

    if system == "Windows":
        # Windows: Try 'copy' first, fall back to 'cp' (Git Bash / WSL)
        try:
            # Windows 'copy' uses backslashes
            src_win = source.replace("/", "\\")
            dst_win = destination.replace("/", "\\")

            run_sandboxed(
                ["cmd", "/c", "copy", "/Y", src_win, dst_win],
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Fall back to cp (Git Bash / WSL)
            run_sandboxed(
                ["cp", source, destination],
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout
            )
    else:
        # Linux/macOS/BSD: Use standard 'cp'
        run_sandboxed(
            ["cp", source, destination],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout
        )

    # Verify destination was created
    if not os.path.exists(destination):
        raise RuntimeError(f"Copy command succeeded but destination file not found: {destination}")