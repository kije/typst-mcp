#!/usr/bin/env python3
"""Build script to generate Typst documentation from the typst source repository."""

import subprocess
import sys
import shutil
import os
from pathlib import Path


def eprint(*args, **kwargs):
    """Print to stderr to avoid breaking MCP JSON-RPC communication."""
    print(*args, file=sys.stderr, **kwargs)


def get_cache_dir():
    """Get the cache directory for typst-mcp data.

    Can be configured via TYPST_MCP_CACHE_DIR environment variable.
    """
    # Check for environment variable first
    env_cache = os.environ.get("TYPST_MCP_CACHE_DIR")
    if env_cache:
        cache_dir = Path(env_cache).expanduser()
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir

    # Use platform-appropriate cache directory
    if sys.platform == "darwin":
        cache_base = Path.home() / "Library" / "Caches"
    elif sys.platform == "win32":
        cache_base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:  # Linux and others
        cache_base = Path.home() / ".cache"

    cache_dir = cache_base / "typst-mcp"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def check_cargo_installed():
    """Check if cargo is installed and meets minimum version requirements."""
    if shutil.which("cargo") is None:
        eprint("ERROR: cargo is not installed. Please install Rust toolchain from https://rustup.rs/")
        return False

    # Check Rust version
    try:
        result = subprocess.run(
            ["rustc", "--version"],
            capture_output=True,
            text=True,
            check=True
        )
        version_output = result.stdout.strip()
        eprint(f"Detected {version_output}")

        # Extract version number (e.g., "rustc 1.86.0" -> "1.86.0")
        if "rustc" in version_output:
            version_str = version_output.split()[1]
            major, minor, _ = version_str.split('.')
            version_num = int(major) * 100 + int(minor)

            # Typst requires Rust 1.89+
            if version_num < 189:
                eprint(f"\nERROR: Rust version {version_str} is too old.")
                eprint("Typst requires Rust 1.89 or later.")
                eprint("\nTo update Rust, run:")
                eprint("  rustup update")
                return False

    except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as e:
        eprint(f"WARNING: Could not check Rust version: {e}")
        eprint("Proceeding anyway, but build may fail if Rust is outdated.")

    return True


def get_repo_commit_hash(repo_path: Path) -> str:
    """Get the current git commit hash of a repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def needs_rebuild(cache_dir: Path, typst_repo: Path) -> bool:
    """Check if documentation needs to be rebuilt due to version changes."""
    version_file = cache_dir / "typst-docs" / ".version"
    docs_json = cache_dir / "typst-docs" / "main.json"

    # If docs don't exist, rebuild needed
    if not docs_json.exists():
        return True

    # If version file doesn't exist (old installation), rebuild
    if not version_file.exists():
        eprint("ℹ️  No version tracking found - will rebuild to track versions")
        return True

    # Check if typst repo exists
    if not (typst_repo / ".git").exists():
        return True

    # Read stored version
    try:
        with open(version_file, 'r') as f:
            stored_commit = f.read().strip()
    except:
        return True

    # Get current commit from repo
    current_commit = get_repo_commit_hash(typst_repo)
    if not current_commit:
        return True

    # Compare versions
    if stored_commit != current_commit:
        eprint(f"ℹ️  New Typst version detected")
        eprint(f"   Old: {stored_commit[:8]}")
        eprint(f"   New: {current_commit[:8]}")
        return True

    return False


def save_version(cache_dir: Path, typst_repo: Path):
    """Save the current typst repo commit hash to version file."""
    commit_hash = get_repo_commit_hash(typst_repo)
    if commit_hash:
        version_file = cache_dir / "typst-docs" / ".version"
        with open(version_file, 'w') as f:
            f.write(commit_hash)
        eprint(f"✓ Saved version: {commit_hash[:8]}")


def update_typst_repo(typst_repo: Path) -> bool:
    """Update the typst repository to the latest version."""
    if (typst_repo / ".git").exists():
        eprint("Checking for Typst updates...")
        try:
            # Fetch latest changes
            subprocess.run(
                ["git", "fetch", "origin", "main"],
                cwd=typst_repo,
                capture_output=True,
                check=True
            )
            # Reset to latest
            subprocess.run(
                ["git", "reset", "--hard", "origin/main"],
                cwd=typst_repo,
                capture_output=True,
                check=True
            )
            eprint("✓ Typst repository updated to latest version")
            return True
        except subprocess.CalledProcessError as e:
            eprint(f"Warning: Could not update typst repo: {e.stderr}")
            return False
    return True


def build_typst_docs():
    """Generate Typst documentation by running cargo in the typst repository."""
    # Get cache directory for persistent storage across uvx runs
    cache_dir = get_cache_dir()
    docs_dir = cache_dir / "typst-docs"
    docs_json = docs_dir / "main.json"
    typst_repo = cache_dir / "typst"

    eprint(f"Cache directory: {cache_dir}")
    eprint(f"Docs directory: {docs_dir}")
    eprint(f"Typst repo: {typst_repo}")

    # Check if rebuild is needed
    if not needs_rebuild(cache_dir, typst_repo):
        eprint("✓ Typst docs are up to date")
        eprint(f"  Location: {docs_json}")
        return True

    # If docs exist but version changed, we'll rebuild
    if docs_json.exists():
        eprint("ℹ️  Rebuilding documentation with new version...")

    # Check if cargo is installed
    if not check_cargo_installed():
        return False

    # Ensure typst repository is cloned
    if not (typst_repo / "Cargo.toml").exists():
        eprint("Cloning typst repository (this may take a moment)...")
        try:
            subprocess.run(
                ["git", "clone", "--depth=1", "https://github.com/typst/typst.git", str(typst_repo)],
                check=True,
                capture_output=True,
                text=True
            )
            eprint("✓ Typst repository cloned")
        except subprocess.CalledProcessError as e:
            eprint(f"ERROR: Failed to clone repository: {e.stderr}")
            return False
        except FileNotFoundError:
            eprint("ERROR: git command not found. Please install git.")
            return False
    else:
        # Update existing repository to latest version
        update_typst_repo(typst_repo)

    # Create docs directory
    docs_dir.mkdir(parents=True, exist_ok=True)

    # Run cargo command to generate docs
    eprint("Generating Typst documentation (this may take 30-60 seconds)...")
    eprint("Running: cargo run --package typst-docs ...")

    try:
        result = subprocess.run(
            [
                "cargo", "run",
                "--manifest-path", str(typst_repo / "Cargo.toml"),
                "--package", "typst-docs",
                "--",
                "--assets-dir", str(docs_dir),
                "--out-file", str(docs_json)
            ],
            check=True,
            capture_output=True,
            text=True
        )
        eprint("✓ Typst documentation generated successfully")
        eprint(f"  Output: {docs_json}")

        # Save version for future checks
        save_version(cache_dir, typst_repo)

        return True

    except subprocess.CalledProcessError as e:
        eprint(f"ERROR: Failed to generate docs: {e.stderr}")
        return False


def main():
    """Entry point for the build script."""
    eprint("=" * 60)
    eprint("Typst MCP Server - Documentation Build Script")
    eprint("=" * 60)

    success = build_typst_docs()

    if success:
        eprint("\n" + "=" * 60)
        eprint("Build completed successfully!")
        eprint("=" * 60)
        sys.exit(0)
    else:
        eprint("\n" + "=" * 60)
        eprint("Build failed. Please check the errors above.")
        eprint("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()