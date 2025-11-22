#!/usr/bin/env python3
"""Module for fetching and caching Typst Universe package documentation."""

import json
import re
import sys
import time
from pathlib import Path
from typing import Optional, Dict, Any, List
import httpx
import toml
from .build_docs import get_cache_dir, eprint


# Package cache state
_package_cache: Dict[str, Dict[str, Any]] = {}


def validate_package_name(name: str) -> str:
    """Validate package name to prevent path traversal and injection attacks.

    Args:
        name: Package name to validate

    Returns:
        Validated package name

    Raises:
        ValueError: If package name is invalid or contains path traversal
    """
    # Allow only lowercase letters, numbers, hyphens (standard package naming)
    if not re.match(r'^[a-z0-9][a-z0-9-]*$', name):
        raise ValueError(
            f"Invalid package name format: '{name}'. "
            "Package names must start with a letter or number and contain only "
            "lowercase letters, numbers, and hyphens."
        )

    # Additional safety checks for path traversal
    if '..' in name or '/' in name or '\\' in name:
        raise ValueError(f"Path traversal detected in package name: '{name}'")

    # Reasonable length limit
    if len(name) > 100:
        raise ValueError(f"Package name too long (max 100 characters): '{name}'")

    return name


def validate_version(version: str) -> str:
    """Validate version string to prevent path traversal.

    Args:
        version: Version string to validate

    Returns:
        Validated version string

    Raises:
        ValueError: If version is invalid or contains path traversal
    """
    # Semantic versioning format: X.Y.Z or X.Y.Z-suffix
    if not re.match(r'^[0-9]+\.[0-9]+\.[0-9]+(-[a-z0-9.]+)?$', version):
        raise ValueError(
            f"Invalid version format: '{version}'. "
            "Version must follow semantic versioning (e.g., 1.0.0 or 1.0.0-beta.1)"
        )

    # Path traversal check
    if '..' in version or '/' in version or '\\' in version:
        raise ValueError(f"Path traversal detected in version: '{version}'")

    return version


def validate_file_path(path: str) -> str:
    """Validate file path within package to prevent path traversal.

    Args:
        path: File path to validate

    Returns:
        Validated file path

    Raises:
        ValueError: If path is invalid or contains path traversal
    """
    # Prevent absolute paths
    if path.startswith('/') or path.startswith('\\'):
        raise ValueError(f"Absolute paths not allowed: '{path}'")

    # Check for path traversal in normalized path
    normalized = path.replace('\\', '/')
    parts = normalized.split('/')
    if '..' in parts:
        raise ValueError(f"Path traversal detected in file path: '{path}'")

    # Prevent null bytes (can bypass some security checks)
    if '\x00' in path:
        raise ValueError("Null bytes in file path not allowed")

    # Reasonable length limit
    if len(path) > 500:
        raise ValueError(f"File path too long (max 500 characters): '{path}'")

    return path


def get_package_cache_dir() -> Path:
    """Get the cache directory for package documentation."""
    cache_dir = get_cache_dir() / "package-docs"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_package_versions(package_name: str, timeout: int = 10) -> list[str]:
    """
    Fetch available versions for a package from GitHub.

    Args:
        package_name: Name of the package (e.g., "cetz")
        timeout: Request timeout in seconds

    Returns:
        List of available versions

    Raises:
        RuntimeError: If package not found or request fails
        ValueError: If package_name contains invalid characters or path traversal
    """
    # SECURITY: Validate package name to prevent path traversal attacks
    package_name = validate_package_name(package_name)

    url = f"https://api.github.com/repos/typst/packages/contents/packages/preview/{package_name}"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, follow_redirects=True)

            if response.status_code == 404:
                raise RuntimeError(f"Package '{package_name}' not found in Typst Universe")

            response.raise_for_status()
            contents = response.json()

            # Extract version directories
            versions = [
                item["name"] for item in contents
                if item["type"] == "dir"
            ]

            return sorted(versions, reverse=True)  # Latest first

    except httpx.TimeoutException:
        raise RuntimeError(f"Timeout while fetching package versions for '{package_name}'")
    except httpx.HTTPError as e:
        raise RuntimeError(f"HTTP error while fetching package '{package_name}': {e}")
    except Exception as e:
        raise RuntimeError(f"Error fetching package '{package_name}': {e}")


def fetch_file_from_github(
    package_name: str,
    version: str,
    file_path: str,
    timeout: int = 10
) -> Optional[str]:
    """
    Fetch a file from a package's GitHub repository.

    Args:
        package_name: Package name
        version: Package version
        file_path: Path to file within package
        timeout: Request timeout in seconds

    Returns:
        File content as string, or None if not found

    Raises:
        ValueError: If inputs contain invalid characters or path traversal
    """
    # SECURITY: Validate all inputs to prevent path traversal attacks
    package_name = validate_package_name(package_name)
    version = validate_version(version)
    file_path = validate_file_path(file_path)

    url = f"https://raw.githubusercontent.com/typst/packages/main/packages/preview/{package_name}/{version}/{file_path}"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, follow_redirects=True)

            if response.status_code == 404:
                return None

            response.raise_for_status()
            return response.text

    except httpx.TimeoutException:
        eprint(f"Warning: Timeout fetching {file_path} from {package_name}@{version}")
        return None
    except Exception as e:
        eprint(f"Warning: Error fetching {file_path}: {e}")
        return None


def fetch_directory_listing(
    package_name: str,
    version: str,
    dir_path: str,
    timeout: int = 10
) -> Optional[List[Dict[str, str]]]:
    """
    Fetch directory listing from GitHub API.

    Args:
        package_name: Package name
        version: Package version
        dir_path: Directory path within package
        timeout: Request timeout

    Returns:
        List of file/directory entries with name, path, type

    Raises:
        ValueError: If inputs contain invalid characters or path traversal
    """
    # SECURITY: Validate all inputs to prevent path traversal attacks
    package_name = validate_package_name(package_name)
    version = validate_version(version)
    dir_path = validate_file_path(dir_path)

    url = f"https://api.github.com/repos/typst/packages/contents/packages/preview/{package_name}/{version}/{dir_path}"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, follow_redirects=True)

            if response.status_code == 404:
                return None

            response.raise_for_status()
            contents = response.json()

            # Return list of entries
            return [
                {
                    "name": item["name"],
                    "path": item["path"],
                    "type": item["type"],
                    "size": item.get("size", 0)
                }
                for item in contents
            ]

    except Exception as e:
        eprint(f"Warning: Error listing directory {dir_path}: {e}")
        return None


def fetch_examples_directory(package_name: str, version: str) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch all files from examples/ directory.

    Returns list of example files with their content.
    """
    listing = fetch_directory_listing(package_name, version, "examples")

    if not listing:
        return None

    examples = []
    for entry in listing:
        if entry["type"] == "file" and entry["name"].endswith(".typ"):
            content = fetch_file_from_github(package_name, version, f"examples/{entry['name']}")
            if content:
                examples.append({
                    "filename": entry["name"],
                    "content": content,
                    "size": entry["size"]
                })

    return examples if examples else None


def fetch_docs_directory(package_name: str, version: str) -> Optional[Dict[str, str]]:
    """
    Fetch all files from docs/ directory.

    Returns dictionary of filename -> content.
    """
    listing = fetch_directory_listing(package_name, version, "docs")

    if not listing:
        return None

    docs = {}
    for entry in listing:
        if entry["type"] == "file":
            # Fetch markdown, text, and typst files
            if entry["name"].endswith((".md", ".txt", ".typ")):
                content = fetch_file_from_github(package_name, version, f"docs/{entry['name']}")
                if content:
                    docs[entry["name"]] = content

    return docs if docs else None


def get_package_metadata(package_name: str, version: str) -> Dict[str, Any]:
    """
    Fetch package metadata from typst.toml.

    Args:
        package_name: Package name
        version: Package version

    Returns:
        Dictionary containing package metadata

    Raises:
        ValueError: If inputs contain invalid characters or path traversal
    """
    # SECURITY: Validation happens in fetch_file_from_github
    toml_content = fetch_file_from_github(package_name, version, "typst.toml")

    if not toml_content:
        return {
            "name": package_name,
            "version": version,
            "error": "Could not fetch package metadata"
        }

    # Proper TOML parsing
    try:
        parsed = toml.loads(toml_content)

        # Extract package section
        package_meta = parsed.get("package", {})

        # Add version info
        metadata = {
            "name": package_name,
            "version": version,
            **package_meta
        }

        return metadata

    except Exception as e:
        eprint(f"Error parsing TOML for {package_name}: {e}")
        # Fallback to basic metadata
        return {
            "name": package_name,
            "version": version,
            "error": f"TOML parsing failed: {str(e)}"
        }


def build_package_docs(
    package_name: str,
    version: Optional[str] = None,
    timeout: int = 30
) -> Dict[str, Any]:
    """
    Fetch and build documentation for a Typst Universe package.

    Args:
        package_name: Name of the package
        version: Specific version (defaults to latest)
        timeout: Total timeout in seconds

    Returns:
        Dictionary containing package documentation

    Raises:
        RuntimeError: If package cannot be fetched or built
        ValueError: If inputs contain invalid characters or path traversal
    """
    # SECURITY: Validate package name first
    package_name = validate_package_name(package_name)
    if version:
        version = validate_version(version)

    start_time = time.time()

    # Check cache first
    cache_key = f"{package_name}@{version if version else 'latest'}"
    if cache_key in _package_cache:
        eprint(f"✓ Using cached docs for {cache_key}")
        return _package_cache[cache_key]

    # Get available versions if version not specified
    if not version:
        eprint(f"Fetching versions for {package_name}...")
        versions = get_package_versions(package_name, timeout=10)

        if not versions:
            raise RuntimeError(f"No versions found for package '{package_name}'")

        version = versions[0]  # Use latest
        eprint(f"Using latest version: {version}")

    # Check for cached file
    cache_dir = get_package_cache_dir()
    package_cache_file = cache_dir / f"{package_name}_{version}.json"

    if package_cache_file.exists():
        eprint(f"✓ Loading cached package docs from {package_cache_file}")
        with open(package_cache_file, "r", encoding="utf-8") as f:
            docs = json.load(f)
            _package_cache[cache_key] = docs
            return docs

    # Fetch package documentation
    eprint(f"Fetching comprehensive documentation for {package_name}@{version}...")

    # Get metadata (includes homepage, repository, etc.)
    metadata = get_package_metadata(package_name, version)

    # Fetch README
    readme = None
    for readme_name in ["README.md", "readme.md", "Readme.md"]:
        readme = fetch_file_from_github(package_name, version, readme_name, timeout=10)
        if readme:
            break

    # Fetch LICENSE
    license_content = None
    for license_name in ["LICENSE", "LICENSE.md", "LICENSE.txt"]:
        license_content = fetch_file_from_github(package_name, version, license_name, timeout=10)
        if license_content:
            break

    # Fetch CHANGELOG (if exists)
    changelog = None
    for changelog_name in ["CHANGELOG.md", "CHANGELOG", "changelog.md", "HISTORY.md"]:
        changelog = fetch_file_from_github(package_name, version, changelog_name, timeout=10)
        if changelog:
            break

    # Check timeout before fetching additional content
    elapsed = time.time() - start_time
    if elapsed > timeout * 0.7:  # Leave 30% time for additional fetching
        eprint(f"Warning: Approaching timeout, skipping optional documentation")
        examples = None
        docs_dir = None
    else:
        # Fetch examples/ directory
        eprint(f"  Fetching examples directory...")
        examples = fetch_examples_directory(package_name, version)
        if examples:
            eprint(f"  ✓ Found {len(examples)} example files")

        # Fetch docs/ directory
        eprint(f"  Fetching docs directory...")
        docs_dir = fetch_docs_directory(package_name, version)
        if docs_dir:
            eprint(f"  ✓ Found {len(docs_dir)} documentation files")

    # Check final timeout
    elapsed = time.time() - start_time
    if elapsed > timeout:
        raise RuntimeError(f"Timeout exceeded while fetching package documentation ({elapsed:.1f}s > {timeout}s)")

    # Build documentation structure
    docs = {
        "package": package_name,
        "version": version,
        "metadata": metadata,
        "readme": readme,
        "license": license_content,
        "changelog": changelog,
        "examples": examples,  # NEW: Example .typ files
        "docs": docs_dir,  # NEW: Additional docs/ directory
        "universe_url": f"https://typst.app/universe/package/{package_name}/",
        "github_url": f"https://github.com/typst/packages/tree/main/packages/preview/{package_name}/{version}",
        "import_statement": f'#import "@preview/{package_name}:{version}": *',
        "fetched_at": time.time(),
    }

    # Add helpful links from metadata
    if metadata.get("homepage"):
        docs["homepage_url"] = metadata["homepage"]
    if metadata.get("repository"):
        docs["repository_url"] = metadata["repository"]

    # Cache to file
    with open(package_cache_file, "w", encoding="utf-8") as f:
        json.dump(docs, f, indent=2)

    # Cache in memory
    _package_cache[cache_key] = docs

    eprint(f"✓ Package documentation built and cached for {package_name}@{version}")
    return docs


def search_packages(query: str, max_results: int = 20) -> list[Dict[str, str]]:
    """
    Search for packages in Typst Universe.

    Args:
        query: Search query
        max_results: Maximum number of results to return

    Returns:
        List of package information dictionaries
    """
    # For now, we'll list all packages and filter
    # In the future, this could use a dedicated search API
    url = "https://api.github.com/repos/typst/packages/contents/packages/preview"

    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(url, follow_redirects=True)
            response.raise_for_status()
            contents = response.json()

            # Filter packages by query
            packages = []
            query_lower = query.lower()

            for item in contents:
                if item["type"] == "dir":
                    package_name = item["name"]
                    if query_lower in package_name.lower():
                        packages.append({
                            "name": package_name,
                            "url": f"https://typst.app/universe/package/{package_name}/",
                            "import": f'@preview/{package_name}',
                        })

                        if len(packages) >= max_results:
                            break

            return packages

    except Exception as e:
        eprint(f"Error searching packages: {e}")
        return []


def list_all_packages() -> list[str]:
    """
    List all available packages in Typst Universe.

    Returns:
        List of package names
    """
    url = "https://api.github.com/repos/typst/packages/contents/packages/preview"

    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(url, follow_redirects=True)
            response.raise_for_status()
            contents = response.json()

            return [
                item["name"] for item in contents
                if item["type"] == "dir"
            ]

    except Exception as e:
        eprint(f"Error listing packages: {e}")
        return []


def list_cached_packages() -> list[Dict[str, str]]:
    """
    List all locally cached packages.

    Returns:
        List of dictionaries with package info:
        [{"package": "name", "version": "x.y.z", "cache_file": "path"}]
    """
    cache_dir = get_package_cache_dir()
    cached = []

    for cache_file in cache_dir.glob("*.json"):
        # Parse filename: packagename_version.json
        stem = cache_file.stem
        if "_" in stem:
            parts = stem.rsplit("_", 1)
            if len(parts) == 2:
                package, version = parts
                cached.append({
                    "package": package,
                    "version": version,
                    "cache_file": str(cache_file),
                    "uri": f"typst://v1/packages/{package}/{version}",
                })

    return cached


def get_cached_package_docs(package_name: str, version: str) -> Optional[Dict[str, Any]]:
    """
    Get package documentation from cache only (no network fetch).

    Args:
        package_name: Package name
        version: Package version

    Returns:
        Cached documentation dict, or None if not cached
    """
    # Check memory cache first
    cache_key = f"{package_name}@{version}"
    if cache_key in _package_cache:
        return _package_cache[cache_key]

    # Check file cache
    cache_dir = get_package_cache_dir()
    cache_file = cache_dir / f"{package_name}_{version}.json"

    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                docs = json.load(f)
                _package_cache[cache_key] = docs
                return docs
        except Exception as e:
            eprint(f"Error reading cached docs: {e}")
            return None

    return None
