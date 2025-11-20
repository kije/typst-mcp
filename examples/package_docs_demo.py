#!/usr/bin/env python3
"""
Demonstration of Typst Universe package documentation tools.

This script shows how to use the package documentation features
to discover, explore, and fetch documentation for Typst packages.
"""

import json
from typst_mcp.package_docs import (
    search_packages,
    get_package_versions,
    build_package_docs,
)


def demo_search():
    """Demo: Search for packages related to plotting."""
    print("\n" + "=" * 60)
    print("DEMO 1: Search for plotting packages")
    print("=" * 60)

    results = search_packages("plot", max_results=5)
    print(f"\nFound {len(results)} packages matching 'plot':")
    for pkg in results:
        print(f"\n  📦 {pkg['name']}")
        print(f"     Import: {pkg['import']}")
        print(f"     URL: {pkg['url']}")


def demo_versions():
    """Demo: Get versions for a specific package."""
    print("\n" + "=" * 60)
    print("DEMO 2: Get versions for 'cetz' package")
    print("=" * 60)

    versions = get_package_versions("cetz")
    print(f"\n📋 Available versions ({len(versions)} total):")
    print(f"   Latest: {versions[0]}")
    print(f"   Oldest: {versions[-1]}")
    print(f"   Recent releases: {', '.join(versions[:5])}")


def demo_package_docs():
    """Demo: Fetch complete documentation for a package."""
    print("\n" + "=" * 60)
    print("DEMO 3: Fetch documentation for 'cetz' package")
    print("=" * 60)

    docs = build_package_docs("cetz")

    print(f"\n📖 Package Documentation:")
    print(f"   Name: {docs['package']}")
    print(f"   Version: {docs['version']}")
    print(f"   Import: {docs['import_statement']}")
    print(f"\n🔗 Links:")
    print(f"   Universe: {docs['universe_url']}")
    print(f"   GitHub: {docs['github_url']}")

    if docs.get('readme'):
        readme_preview = docs['readme'][:200].replace('\n', ' ')
        print(f"\n📝 README Preview:")
        print(f"   {readme_preview}...")
        print(f"   (Total length: {len(docs['readme'])} characters)")

    if docs.get('metadata'):
        print(f"\n⚙️  Metadata:")
        metadata = docs['metadata']
        for key in ['description', 'license', 'repository', 'homepage']:
            if key in metadata:
                print(f"   {key.capitalize()}: {metadata[key]}")


def demo_error_handling():
    """Demo: Graceful error handling."""
    print("\n" + "=" * 60)
    print("DEMO 4: Error handling for non-existent package")
    print("=" * 60)

    try:
        build_package_docs("nonexistent-package-xyz")
    except RuntimeError as e:
        print(f"\n❌ Error (as expected):")
        print(f"   {e}")
        print(f"\n✅ Error was handled gracefully!")


if __name__ == "__main__":
    print("\n" + "=" * 70)
    print(" " * 15 + "TYPST PACKAGE DOCUMENTATION DEMO")
    print("=" * 70)

    demo_search()
    demo_versions()
    demo_package_docs()
    demo_error_handling()

    print("\n" + "=" * 70)
    print(" " * 20 + "DEMO COMPLETED ✓")
    print("=" * 70)
    print("\nKey Features:")
    print("  ✓ Fast package search across 900+ packages")
    print("  ✓ Version lookup for any package")
    print("  ✓ Comprehensive documentation fetching")
    print("  ✓ Automatic caching for performance")
    print("  ✓ Graceful error handling")
    print("  ✓ Timeout protection (30s default)")
    print()
