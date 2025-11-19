#!/usr/bin/env python3
"""Build script to generate Typst documentation from the typst source repository."""

import subprocess
import sys
import shutil
from pathlib import Path


def check_cargo_installed():
    """Check if cargo is installed and meets minimum version requirements."""
    if shutil.which("cargo") is None:
        print("ERROR: cargo is not installed. Please install Rust toolchain from https://rustup.rs/")
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
        print(f"Detected {version_output}")

        # Extract version number (e.g., "rustc 1.86.0" -> "1.86.0")
        if "rustc" in version_output:
            version_str = version_output.split()[1]
            major, minor, _ = version_str.split('.')
            version_num = int(major) * 100 + int(minor)

            # Typst requires Rust 1.89+
            if version_num < 189:
                print(f"\nERROR: Rust version {version_str} is too old.")
                print("Typst requires Rust 1.89 or later.")
                print("\nTo update Rust, run:")
                print("  rustup update")
                return False

    except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as e:
        print(f"WARNING: Could not check Rust version: {e}")
        print("Proceeding anyway, but build may fail if Rust is outdated.")

    return True


def build_typst_docs():
    """Generate Typst documentation by running cargo in the vendor/typst submodule."""
    # Find the package root (where this script's parent directory is)
    script_dir = Path(__file__).parent
    root = script_dir.parent

    docs_dir = root / "typst-docs"
    docs_json = docs_dir / "main.json"
    typst_repo = root / "vendor" / "typst"

    print(f"Root directory: {root}")
    print(f"Docs directory: {docs_dir}")
    print(f"Typst repo: {typst_repo}")

    # Check if docs already exist and are valid
    if docs_json.exists():
        print("✓ Typst docs already generated at typst-docs/main.json")
        print("  To regenerate, delete typst-docs/main.json and run this command again.")
        return True

    # Check if cargo is installed
    if not check_cargo_installed():
        return False

    # Ensure submodule is initialized
    if not (typst_repo / "Cargo.toml").exists():
        print("Initializing typst submodule (this may take a moment)...")
        try:
            subprocess.run(
                ["git", "submodule", "update", "--init", "--depth=1", "vendor/typst"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True
            )
            print("✓ Typst submodule initialized")
        except subprocess.CalledProcessError as e:
            print(f"ERROR: Failed to initialize submodule: {e.stderr}")
            return False
        except FileNotFoundError:
            print("ERROR: git command not found. Please install git.")
            return False

    # Create docs directory
    docs_dir.mkdir(exist_ok=True)

    # Run cargo command to generate docs
    print("Generating Typst documentation (this may take 30-60 seconds)...")
    print("Running: cargo run --package typst-docs ...")

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
        print("✓ Typst documentation generated successfully")
        print(f"  Output: {docs_json}")
        return True

    except subprocess.CalledProcessError as e:
        print(f"ERROR: Failed to generate docs: {e.stderr}")
        return False


def main():
    """Entry point for the build script."""
    print("=" * 60)
    print("Typst MCP Server - Documentation Build Script")
    print("=" * 60)

    success = build_typst_docs()

    if success:
        print("\n" + "=" * 60)
        print("Build completed successfully!")
        print("=" * 60)
        sys.exit(0)
    else:
        print("\n" + "=" * 60)
        print("Build failed. Please check the errors above.")
        print("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()
