from mcp.server.fastmcp import FastMCP, Image
import json
import subprocess
import os
import tempfile
import shutil
import sys
import threading
import time
from pathlib import Path
from PIL import Image as PILImage
import io
import numpy as np
from typing import Optional

temp_dir = tempfile.mkdtemp()

mcp = FastMCP("Typst MCP Server")

# Global state for lazy-loaded docs
_docs_state = {
    "loaded": False,
    "building": False,
    "error": None,
    "docs": None,
    "lock": threading.Lock(),
}


def eprint(*args, **kwargs):
    """Print to stderr to avoid breaking MCP JSON-RPC communication."""
    print(*args, file=sys.stderr, **kwargs)


def check_dependencies():
    """Check if required external tools are available."""
    required = {
        "typst": "Typst CLI (https://github.com/typst/typst)",
        "pandoc": "Pandoc (https://pandoc.org/installing.html)",
    }
    missing = []

    for tool, info in required.items():
        if shutil.which(tool) is None:
            missing.append(f"  - {tool}: {info}")

    if missing:
        eprint("\n" + "=" * 60)
        eprint("WARNING: Missing required external tools:")
        eprint("=" * 60)
        for msg in missing:
            eprint(msg)
        eprint("\nInstallation instructions:")
        eprint("  macOS:   brew install typst pandoc")
        eprint("  Linux:   apt install typst pandoc  # or your package manager")
        eprint("  Windows: See installation links above")
        eprint("=" * 60 + "\n")


def build_docs_background():
    """Build documentation in background thread."""
    from .build_docs import build_typst_docs, get_cache_dir
    from . import package_docs  # Import package_docs module to initialize it

    with _docs_state["lock"]:
        if _docs_state["loaded"] or _docs_state["building"]:
            return
        _docs_state["building"] = True

    try:
        cache_dir = get_cache_dir()
        docs_dir = cache_dir / "typst-docs"
        docs_json = docs_dir / "main.json"

        # Check if docs already exist
        if docs_json.exists():
            eprint("✓ Loading existing Typst documentation...")
            with open(docs_json, "r", encoding="utf-8") as f:
                docs_data = json.loads(f.read())

            with _docs_state["lock"]:
                _docs_state["docs"] = docs_data
                _docs_state["loaded"] = True
                _docs_state["building"] = False
            eprint("✓ Documentation loaded successfully")
            return

        # Build docs if they don't exist
        eprint("\n" + "=" * 60)
        eprint("Typst documentation not found - building in background...")
        eprint("=" * 60)
        eprint("(This is a one-time process that may take 1-2 minutes)")
        eprint("Note: Non-doc tools are available immediately!\n")

        success = build_typst_docs()

        if not success:
            with _docs_state["lock"]:
                _docs_state["error"] = "Failed to build documentation"
                _docs_state["building"] = False
            eprint("\n" + "=" * 60)
            eprint("ERROR: Failed to build documentation automatically.")
            eprint("Documentation-related tools will not be available.")
            eprint("=" * 60 + "\n")
            return

        # Load the built docs
        with open(docs_json, "r", encoding="utf-8") as f:
            docs_data = json.loads(f.read())

        with _docs_state["lock"]:
            _docs_state["docs"] = docs_data
            _docs_state["loaded"] = True
            _docs_state["building"] = False

        eprint("\n" + "=" * 60)
        eprint("Documentation built and loaded successfully!")
        eprint("All tools are now available.")
        eprint("=" * 60 + "\n")

    except Exception as e:
        with _docs_state["lock"]:
            _docs_state["error"] = str(e)
            _docs_state["building"] = False
        eprint(f"\n ERROR building docs: {e}\n")


def get_docs(wait_seconds=5):
    """
    Get the typst docs, waiting if they're being built.
    Returns docs or raises error if not available.
    """
    # If already loaded, return immediately
    if _docs_state["loaded"]:
        return _docs_state["docs"]

    # If building, wait briefly
    if _docs_state["building"]:
        eprint(f"Waiting for documentation to finish building (max {wait_seconds}s)...")
        start = time.time()
        while time.time() - start < wait_seconds:
            time.sleep(0.5)
            if _docs_state["loaded"]:
                return _docs_state["docs"]

        # Still building after wait
        raise RuntimeError(
            "Documentation is still building. This typically takes 1-2 minutes on first run. "
            "Please try again in a moment. Non-documentation tools (LaTeX conversion, "
            "syntax validation, image rendering) are available immediately."
        )

    # Not loaded and not building - check for error
    if _docs_state["error"]:
        raise RuntimeError(f"Documentation failed to build: {_docs_state['error']}")

    # Should not reach here, but handle gracefully
    raise RuntimeError("Documentation not available. Please restart the server.")


def list_child_routes(chapter: dict) -> list[dict]:
    """
    Lists all child routes of a chapter.
    """
    if "children" not in chapter:
        return []
    child_routes = []  # { "route": str, content_length: int }[]
    for child in chapter["children"]:
        if "route" in child:
            child_routes.append(
                {"route": child["route"], "content_length": len(json.dumps(child))}
            )
        child_routes += list_child_routes(child)
    return child_routes


@mcp.tool()
def list_docs_chapters() -> str:
    """
    Lists all chapters in the typst docs.
    The LLM should use this in the beginning to get the list of chapters and then decide which chapter to read.
    """
    eprint("mcp.tool('list_docs_chapters') called")

    try:
        typst_docs = get_docs(wait_seconds=10)
    except RuntimeError as e:
        return json.dumps({"error": str(e)})

    chapters = []
    for chapter in typst_docs:
        chapters.append(
            {"route": chapter["route"], "content_length": len(json.dumps(chapter))}
        )
        chapters += list_child_routes(chapter)
    return json.dumps(chapters)


@mcp.tool()
def get_docs_chapter(route: str) -> str:
    """
    Gets a chapter by route.
    The route is the path to the chapter in the typst docs.
    For example, the route "____reference____layout____colbreak" corresponds to the chapter "reference/layout/colbreak".
    The route is a string with underscores ("____") instead of slashes (because MCP uses slashes to separate the input parameters).

    If a chapter has children and its content length is over 1000, this will only return the child routes
    instead of the full content to avoid overwhelming responses.
    """
    eprint(f"mcp.tool('get_docs_chapter') called with route: {route}")

    try:
        typst_docs = get_docs(wait_seconds=10)
    except RuntimeError as e:
        return json.dumps({"error": str(e)})

    # the rout could also be in the form of "____reference____layout____colbreak" -> "/reference/layout/colbreak"
    # replace all underscores with slashes
    route = route.replace("____", "/")

    def route_matches(chapter_route: str, input_route: str) -> bool:
        return chapter_route.strip("/") == input_route.strip("/")

    def get_child(chapter: dict, route: str) -> dict:
        """
        Gets a child chapter by route.
        """
        if "children" not in chapter:
            return {}
        for child in chapter["children"]:
            if route_matches(child["route"], route):
                return child
            child = get_child(child, route)
            if child:
                return child
        return {}

    # Find the requested chapter
    found_chapter = None
    for chapter in typst_docs:
        if route_matches(chapter["route"], route):
            found_chapter = chapter
            break
        child = get_child(chapter, route)
        if child:
            found_chapter = child
            break

    if not found_chapter:
        return json.dumps({})

    # Check if chapter has children and is large
    content_length = len(json.dumps(found_chapter))
    if (
        "children" in found_chapter
        and len(found_chapter["children"]) > 0
        and content_length > 1000
    ):
        # Return just the child routes instead of full content
        child_routes = []
        for child in found_chapter["children"]:
            if "route" in child:
                child_routes.append(
                    {"route": child["route"], "content_length": len(json.dumps(child))}
                )

        # Create simplified chapter with only essential info and child routes
        simplified_chapter = {
            "route": found_chapter["route"],
            "title": found_chapter.get("title", ""),
            "content_length": content_length,
            "note": "This chapter is large. Only child routes are shown. Request specific child routes for detailed content.",
            "child_routes": child_routes,
        }
        return json.dumps(simplified_chapter)

    return json.dumps(found_chapter)


@mcp.tool()
def get_docs_chapters(routes: list) -> str:
    """
    Gets multiple chapters by their routes.
    Takes a list of routes and returns a JSON stringified list of results.

    Example:
    Input: ["____reference____layout____colbreak", "____reference____text____text"]
    Output: JSON stringified list containing the content of both chapters
    """
    results = []
    for route in routes:
        results.append(json.loads(get_docs_chapter(route)))
    return json.dumps(results)


@mcp.tool()
def latex_snippet_to_typst(latex_snippet) -> str:
    r"""
    Converts a latex to typst using pandoc.

    LLMs are way better at writing latex than typst.
    So the LLM should write the wanted output in latex and use this tool to convert it to typst.

    If it was not valid latex, the tool returns "ERROR: in latex_to_typst. Failed to convert latex to typst. Error message from pandoc: {error_message}".

    This should be used primarily for converting small snippets of latex to typst but it can also be used for larger snippets.

    Example 1:
    ```latex
    "$ f\in K ( t^ { H } , \beta ) _ { \delta } $"
    ```
    gets converted to:
    ```typst
    $f in K \( t^H \, beta \)_delta$
    ```

    Example 2:
    ```latex
    \begin{figure}[t]
        \includegraphics[width=8cm]{"placeholder.png"}
        \caption{Placeholder image}
        \label{fig:placeholder}
        \centering
    \end{figure}
    ```
    gets converted to:
    ```typst
    #figure(image("placeholder.png", width: 8cm),
        caption: [
            Placeholder image
        ]
    )
    <fig:placeholder>
    ```
    """
    # create a main.tex file with the latex_snippet
    with open(os.path.join(temp_dir, "main.tex"), "w") as f:
        f.write(latex_snippet)

    # run the pandoc command line tool and capture error output
    try:
        result = subprocess.run(
            [
                "pandoc",
                os.path.join(temp_dir, "main.tex"),
                "--from=latex",
                "--to=typst",
                "--output",
                os.path.join(temp_dir, "main.typ"),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        error_message = e.stderr.strip() if e.stderr else "Unknown error"
        return f"ERROR: in latex_to_typst. Failed to convert latex to typst. Error message from pandoc: {error_message}"

    # read the typst file
    with open(os.path.join(temp_dir, "main.typ"), "r") as f:
        typst = f.read()
        typst = typst.strip()

    return typst


@mcp.tool()
def latex_snippets_to_typst(latex_snippets: list) -> str:
    r"""
    Converts multiple latex snippets to typst.
    Takes a list of LaTeX snippets and returns a JSON stringified list of results.

    Example:
    Input: ["$f\in K ( t^ { H } , \beta ) _ { \delta }$", "\\begin{align} a &= b \\\\ c &= d \\end{align}"]
    Output: JSON stringified list containing the converted typst for each snippet
    """
    results = []
    # Ensure latex_snippets is actually a list
    if not isinstance(latex_snippets, list):
        try:
            latex_snippets = json.loads(latex_snippets)
        except:
            pass

    for snippet in latex_snippets:
        results.append(latex_snippet_to_typst(snippet))
    return json.dumps(results)


@mcp.tool()
def check_if_snippet_is_valid_typst_syntax(typst_snippet) -> str:
    r"""
    Checks if the given typst text is valid typst syntax.
    Returns "VALID" if it is valid, otherwise returns "INVALID! Error message: {error_message}".

    The LLM should use this to check if the typst syntax it generated is valid.
    If not valid, the LLM should try to fix it and check again.
    This should be used primarily for checking small snippets of typst syntax but it can also be used for larger snippets.

    Example 1:
    ```typst
    "$f in K \( t^H \, beta \)_delta$"
    ```
    returns: VALID

    Example 2:
    ```typst
    $a = \frac{1}{2}$ // not valid typst syntax (\frac is a latex command and not a typst command)
    ```
    returns: INVALID! Error message: {error: unknown variable: rac
        ┌─ temp.typ:1:7
        │
        1 │ $a = \frac{1}{2}$
        │        ^^^
        │
        = hint: if you meant to display multiple letters as is, try adding spaces between each letter: `r a c`
        = hint: or if you meant to display this as text, try placing it in quotes: `"rac"`}

    """

    # create a main.typ file with the typst
    with open(os.path.join(temp_dir, "main.typ"), "w") as f:
        f.write(typst_snippet)
    # run the typst command line tool and capture the result
    try:
        subprocess.run(
            ["typst", "compile", os.path.join(temp_dir, "main.typ")],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return "VALID"
    except subprocess.CalledProcessError as e:
        error_message = e.stderr.strip() if e.stderr else "Unknown error"
        return f"INVALID! Error message: {error_message}"


@mcp.tool()
def check_if_snippets_are_valid_typst_syntax(typst_snippets: list) -> str:
    r"""
    Checks if multiple typst snippets have valid syntax.
    Takes a list of typst snippets and returns a JSON stringified list of results.

    The LLM should use this for example to check every single typst snippet it generated.

    Example:
    Input: ["$f in K \( t^H \, beta \)_delta$", "#let x = 1\n#x"]
    Output: JSON stringified list containing validation results ("VALID" or error messages)
    """
    results = []
    # Ensure typst_snippets is actually a list
    if not isinstance(typst_snippets, list):
        try:
            typst_snippets = json.loads(typst_snippets)
        except:
            pass

    for snippet in typst_snippets:
        results.append(check_if_snippet_is_valid_typst_syntax(snippet))
    return json.dumps(results)


@mcp.tool()
def typst_snippet_to_image(typst_snippet) -> Image | str:
    r"""
    Converts a typst text to an image using the typst command line tool.
    It is capable of converting multiple pages to a single png image.
    The image gets cropped to the content and padded with 10px on each side.

    The LLM should use this to convert typst to an image and then evaluate if the image is what it wanted.
    If not valid, the LLM should try to fix it and check again.
    This should be used primarily for converting small snippets of typst to images but it can also be used for larger snippets.

    Example 1:
    ```typst
    "$f in K \( t^H \, beta \)_delta$"
    ```
    gets converted to:
    ```image
    <image object>
    ```

    Example 2:
    ```typst
    #figure(image("placeholder.png", width: 8cm),
        caption: [
            Placeholder image
        ]
    )
    <fig:placeholder>
    ```
    gets converted to:
    ```image
    <image object>
    ```

    """

    # create a main.typ file with the typst
    with open(os.path.join(temp_dir, "main.typ"), "w") as f:
        f.write(typst_snippet)

    # run the typst command line tool and capture the result
    try:
        subprocess.run(
            [
                "typst",
                "compile",
                os.path.join(temp_dir, "main.typ"),
                "--format",
                "png",
                "--ppi",
                "500",
                os.path.join(temp_dir, "page{0p}.png"),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Find all generated pages
        page_files = []
        page_num = 1
        while os.path.exists(os.path.join(temp_dir, f"page{page_num}.png")):
            page_files.append(os.path.join(temp_dir, f"page{page_num}.png"))
            page_num += 1

        if not page_files:
            return "ERROR: in typst_to_image. No pages were generated."

        # Load all pages using PIL and crop to content
        pages = []
        for page_file in page_files:
            img = PILImage.open(page_file)

            # Convert to numpy array for easier processing
            img_array = np.array(img)

            # Check if the image is RGB
            if len(img_array.shape) == 3 and img_array.shape[2] == 3:
                # Find non-white pixels (R,G,B not all 255)
                non_white = np.where(~np.all(img_array == 255, axis=2))
            else:
                # For grayscale images
                non_white = np.where(img_array < 255)

            if len(non_white[0]) > 0:  # If there are non-white pixels
                # Find bounding box
                top = non_white[0].min()
                bottom = non_white[0].max()
                left = non_white[1].min()
                right = non_white[1].max()

                # Add some padding (10px on each side)
                padding = 10
                top = max(0, top - padding)
                bottom = min(img.height - 1, bottom + padding)
                left = max(0, left - padding)
                right = min(img.width - 1, right + padding)

                # Crop image to bounding box
                cropped_img = img.crop((left, top, right + 1, bottom + 1))
                pages.append(cropped_img)
            else:
                # If image is completely white, keep it as is
                pages.append(img)

        if not pages:
            return "ERROR: in typst_to_image. Failed to process page images."

        # Calculate total height
        total_width = max(page.width for page in pages)
        total_height = sum(page.height for page in pages)

        # Create a new image with the combined dimensions
        combined_image = PILImage.new(
            "RGB", (total_width, total_height), (255, 255, 255)
        )

        # Paste all pages vertically
        y_offset = 0
        for page in pages:
            # Center horizontally if page is narrower than the combined image
            x_offset = (total_width - page.width) // 2
            combined_image.paste(page, (x_offset, y_offset))
            y_offset += page.height

        # Save combined image to bytes
        img_bytes_io = io.BytesIO()
        combined_image.save(img_bytes_io, format="PNG")
        img_bytes = img_bytes_io.getvalue()

        # Clean up temp files
        os.remove(os.path.join(temp_dir, "main.typ"))
        for page_file in page_files:
            os.remove(page_file)

        return Image(data=img_bytes, format="png")

    except subprocess.CalledProcessError as e:
        error_message = e.stderr.strip() if e.stderr else "Unknown error"
        return f"ERROR: in typst_to_image. Failed to convert typst to image. Error message from typst: {error_message}"


# ============================================================================
# CORE TYPST DOCUMENTATION RESOURCES
# ============================================================================


@mcp.resource("typst://docs/index")
def docs_index_resource() -> str:
    """
    Index of all available Typst documentation resources.
    Useful for discovering what documentation is available.
    """
    return json.dumps(
        {
            "resources": [
                {
                    "uri": "typst://docs/chapters",
                    "name": "Documentation Chapters List",
                    "description": "Complete list of all documentation chapters with routes and sizes",
                    "mimeType": "application/json",
                },
                {
                    "uri": "typst://docs/chapter/{route}",
                    "name": "Documentation Chapter",
                    "description": "Individual chapter content by route (use ____ as path separator)",
                    "mimeType": "application/json",
                },
            ],
            "note": "If your client doesn't support resources, use the equivalent tools instead.",
        },
        indent=2,
    )


@mcp.resource("typst://docs/chapters")
def list_docs_chapters_resource() -> str:
    """
    Lists all chapters in the Typst documentation.

    Returns a JSON array of all available documentation chapters with their routes and content sizes.

    Lazy loading: First access may build docs (~1-2 min first time),
    subsequent accesses are instant (cached).
    """
    try:
        typst_docs = get_docs(wait_seconds=10)
    except RuntimeError as e:
        return json.dumps({"error": str(e)}, indent=2)

    chapters = []
    for chapter in typst_docs:
        chapters.append(
            {"route": chapter["route"], "content_length": len(json.dumps(chapter))}
        )
        chapters += list_child_routes(chapter)
    return json.dumps(chapters, indent=2)


@mcp.resource("typst://docs/chapter/{route}")
def get_docs_chapter_resource(route: str) -> str:
    """
    Gets a specific chapter from the Typst documentation by route.

    The route uses underscores (____) as path separators instead of slashes.

    Lazy loading: First access may build docs (~1-2 min first time),
    subsequent accesses are instant (cached).

    Example URIs:
    - typst://docs/chapter/reference____layout____colbreak
    - typst://docs/chapter/reference____text____text
    """
    # Reuse the existing tool implementation
    return get_docs_chapter(route)


# ============================================================================
# PACKAGE DOCUMENTATION RESOURCES
# ============================================================================


@mcp.resource("typst://packages/cached")
def list_cached_package_resources() -> str:
    """
    List all locally cached package documentation.

    Returns a JSON array of all packages that have been downloaded and cached.
    Each entry includes the package name, version, and URI for accessing the docs.

    This resource updates dynamically as packages are fetched via tools.
    """
    from .package_docs import list_cached_packages

    cached = list_cached_packages()
    return json.dumps(
        {
            "cached_packages": cached,
            "count": len(cached),
            "note": "These packages are available as resources at typst://package/{name}/{version}",
        },
        indent=2,
    )


@mcp.resource("typst://package/{package_name}/{version}")
def get_cached_package_resource(package_name: str, version: str) -> str:
    """
    Get package documentation (auto-fetches if not cached).

    Returns lightweight summary including:
    - Metadata and README preview
    - File listings for examples/docs directories
    - Links and import statement

    Lazy loading: First access fetches from GitHub (~3-5s),
    subsequent accesses are instant (cached).

    For full content, use:
    - get_package_docs(package_name, version, summary=False) tool
    - get_package_file(package_name, version, file_path) tool

    URI format: typst://package/{package_name}/{version}
    Example: typst://package/cetz/0.4.2
    """
    from .package_docs import get_cached_package_docs, build_package_docs

    # Try cache first
    docs = get_cached_package_docs(package_name, version)

    # Auto-fetch if not cached (WebDAV-like pattern)
    if docs is None:
        eprint(f"Resource: Auto-fetching {package_name}@{version} (not cached)")
        try:
            docs = build_package_docs(package_name, version, timeout=30)
        except Exception as e:
            return json.dumps(
                {
                    "error": f"Failed to fetch package '{package_name}@{version}': {str(e)}",
                    "package": package_name,
                    "version": version,
                },
                indent=2,
            )

    # Return summary by default (resources are for browsing)
    summary = {
        "package": docs["package"],
        "version": docs["version"],
        "metadata": docs["metadata"],
        "readme_preview": docs["readme"][:500] + "..."
        if docs.get("readme") and len(docs["readme"]) > 500
        else docs.get("readme"),
        "examples_count": len(docs.get("examples") or []),
        "docs_count": len(docs.get("docs") or {}),
        "examples_list": [
            {
                "filename": ex["filename"],
                "size": ex["size"],
                "path": f"examples/{ex['filename']}",
            }
            for ex in (docs.get("examples") or [])
        ]
        if docs.get("examples")
        else [],
        "docs_list": [
            {"filename": name, "size": len(content), "path": f"docs/{name}"}
            for name, content in (docs.get("docs") or {}).items()
        ]
        if docs.get("docs")
        else [],
        "import_statement": docs["import_statement"],
        "universe_url": docs["universe_url"],
        "homepage_url": docs.get("homepage_url"),
        "note": "Use get_package_docs() or get_package_file() tools for full content",
    }

    return json.dumps(summary, indent=2)


@mcp.resource("typst://package/{package_name}/{version}/readme")
def get_package_readme_resource(package_name: str, version: str) -> str:
    """
    Get full README content (auto-fetches if not cached).

    Lazy loading: First access fetches package data (~3-5s),
    subsequent accesses are instant (cached).

    URI format: typst://package/{package_name}/{version}/readme
    Example: typst://package/cetz/0.4.2/readme
    """
    from .package_docs import get_cached_package_docs, build_package_docs

    # Try cache first
    docs = get_cached_package_docs(package_name, version)

    # Auto-fetch if not cached
    if docs is None:
        eprint(f"Resource: Auto-fetching {package_name}@{version} for README")
        try:
            docs = build_package_docs(package_name, version, timeout=30)
        except Exception as e:
            return json.dumps(
                {
                    "error": f"Failed to fetch package: {str(e)}",
                    "package": package_name,
                    "version": version,
                },
                indent=2,
            )

    if not docs.get("readme"):
        return json.dumps(
            {"error": f"README not available for {package_name}@{version}"}, indent=2
        )

    return json.dumps(
        {
            "package": package_name,
            "version": version,
            "readme": docs["readme"],
            "size": len(docs["readme"]),
        },
        indent=2,
    )


@mcp.resource("typst://package/{package_name}/{version}/examples")
def list_package_examples_resource(package_name: str, version: str) -> str:
    """
    List all example files (auto-fetches if not cached).

    Returns list of examples with URIs for individual access.

    Lazy loading: First access fetches package data (~3-5s),
    subsequent accesses are instant (cached).

    URI format: typst://package/{package_name}/{version}/examples
    Example: typst://package/polylux/0.4.0/examples
    """
    from .package_docs import get_cached_package_docs, build_package_docs

    # Try cache first
    docs = get_cached_package_docs(package_name, version)

    # Auto-fetch if not cached
    if docs is None:
        eprint(f"Resource: Auto-fetching {package_name}@{version} for examples")
        try:
            docs = build_package_docs(package_name, version, timeout=30)
        except Exception as e:
            return json.dumps(
                {
                    "error": f"Failed to fetch package: {str(e)}",
                    "package": package_name,
                    "version": version,
                },
                indent=2,
            )

    examples = docs.get("examples", [])

    if not examples:
        return json.dumps(
            {
                "package": package_name,
                "version": version,
                "examples": [],
                "note": "This package has no examples directory",
            },
            indent=2,
        )

    return json.dumps(
        {
            "package": package_name,
            "version": version,
            "examples": [
                {
                    "filename": ex["filename"],
                    "size": ex["size"],
                    "uri": f"typst://package/{package_name}/{version}/examples/{ex['filename']}",
                }
                for ex in examples
            ],
            "count": len(examples),
        },
        indent=2,
    )


@mcp.resource("typst://package/{package_name}/{version}/examples/{filename}")
def get_package_example_resource(package_name: str, version: str, filename: str) -> str:
    """
    Get specific example file content (auto-fetches if not cached).

    Lazy loading: First access fetches package data (~3-5s),
    subsequent accesses are instant (cached).

    URI format: typst://package/{package_name}/{version}/examples/{filename}
    Example: typst://package/polylux/0.4.0/examples/demo.typ
    """
    from .package_docs import get_cached_package_docs, build_package_docs

    # Try cache first
    docs = get_cached_package_docs(package_name, version)

    # Auto-fetch if not cached
    if docs is None:
        eprint(f"Resource: Auto-fetching {package_name}@{version} for example file")
        try:
            docs = build_package_docs(package_name, version, timeout=30)
        except Exception as e:
            return json.dumps(
                {
                    "error": f"Failed to fetch package: {str(e)}",
                    "package": package_name,
                    "version": version,
                },
                indent=2,
            )

    examples = docs.get("examples", [])

    for ex in examples:
        if ex["filename"] == filename:
            return json.dumps(
                {
                    "package": package_name,
                    "version": version,
                    "filename": filename,
                    "content": ex["content"],
                    "size": ex["size"],
                },
                indent=2,
            )

    return json.dumps(
        {
            "error": f"Example '{filename}' not found in {package_name}@{version}",
            "available_examples": [ex["filename"] for ex in examples],
        },
        indent=2,
    )


@mcp.resource("typst://package/{package_name}/{version}/docs")
def list_package_docs_resource(package_name: str, version: str) -> str:
    """
    List all documentation files (auto-fetches if not cached).

    Returns list of docs with URIs for individual access.

    Lazy loading: First access fetches package data (~3-5s),
    subsequent accesses are instant (cached).

    URI format: typst://package/{package_name}/{version}/docs
    Example: typst://package/tidy/0.4.3/docs
    """
    from .package_docs import get_cached_package_docs, build_package_docs

    # Try cache first
    docs_data = get_cached_package_docs(package_name, version)

    # Auto-fetch if not cached
    if docs_data is None:
        eprint(f"Resource: Auto-fetching {package_name}@{version} for docs")
        try:
            docs_data = build_package_docs(package_name, version, timeout=30)
        except Exception as e:
            return json.dumps(
                {
                    "error": f"Failed to fetch package: {str(e)}",
                    "package": package_name,
                    "version": version,
                },
                indent=2,
            )

    docs_files = docs_data.get("docs", {})

    if not docs_files:
        return json.dumps(
            {
                "package": package_name,
                "version": version,
                "docs": [],
                "note": "This package has no docs directory",
            },
            indent=2,
        )

    return json.dumps(
        {
            "package": package_name,
            "version": version,
            "docs": [
                {
                    "filename": filename,
                    "size": len(content),
                    "uri": f"typst://package/{package_name}/{version}/docs/{filename}",
                }
                for filename, content in docs_files.items()
            ],
            "count": len(docs_files),
        },
        indent=2,
    )


@mcp.resource("typst://package/{package_name}/{version}/docs/{filename}")
def get_package_doc_file_resource(
    package_name: str, version: str, filename: str
) -> str:
    """
    Get specific documentation file content (auto-fetches if not cached).

    Lazy loading: First access fetches package data (~3-5s),
    subsequent accesses are instant (cached).

    URI format: typst://package/{package_name}/{version}/docs/{filename}
    Example: typst://package/tidy/0.4.3/docs/migration.md
    """
    from .package_docs import get_cached_package_docs, build_package_docs

    # Try cache first
    docs_data = get_cached_package_docs(package_name, version)

    # Auto-fetch if not cached
    if docs_data is None:
        eprint(f"Resource: Auto-fetching {package_name}@{version} for doc file")
        try:
            docs_data = build_package_docs(package_name, version, timeout=30)
        except Exception as e:
            return json.dumps(
                {
                    "error": f"Failed to fetch package: {str(e)}",
                    "package": package_name,
                    "version": version,
                },
                indent=2,
            )

    docs_files = docs_data.get("docs", {})

    if filename in docs_files:
        return json.dumps(
            {
                "package": package_name,
                "version": version,
                "filename": filename,
                "content": docs_files[filename],
                "size": len(docs_files[filename]),
            },
            indent=2,
        )

    return json.dumps(
        {
            "error": f"Documentation file '{filename}' not found in {package_name}@{version}",
            "available_docs": list(docs_files.keys()),
        },
        indent=2,
    )


# ============================================================================
# PACKAGE DOCUMENTATION TOOLS
# ============================================================================


@mcp.tool()
def search_packages(query: str, max_results: int = 20) -> str:
    """
    Search for packages in Typst Universe.

    Searches through available Typst packages and returns matching results.
    Useful for discovering packages before fetching their documentation.

    Args:
        query: Search query to match against package names
        max_results: Maximum number of results to return (default: 20)

    Returns:
        JSON string containing list of matching packages with their names,
        universe URLs, and import statements.

    Example:
        Input: "cetz"
        Output: JSON list with package info including import statement
    """
    eprint(f"mcp.tool('search_packages') called with query: {query}")

    try:
        from .package_docs import search_packages as _search_packages

        results = _search_packages(query, max_results)
        return json.dumps(results, indent=2)

    except Exception as e:
        eprint(f"Error in search_packages: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_packages() -> str:
    """
    List all available packages in Typst Universe.

    Returns a complete list of all packages available in the Typst Universe.
    Use search_packages() for filtered results.

    Returns:
        JSON string containing list of all package names

    Example output: ["cetz", "tidy", "mantys", "polylux", ...]
    """
    eprint("mcp.tool('list_packages') called")

    try:
        from .package_docs import list_all_packages

        packages = list_all_packages()
        return json.dumps(packages, indent=2)

    except Exception as e:
        eprint(f"Error in list_packages: {e}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_package_versions(package_name: str) -> str:
    """
    Get available versions for a Typst package.

    Fetches all published versions of a package from Typst Universe.
    Versions are returned in descending order (latest first).

    Args:
        package_name: Name of the package (e.g., "cetz", "tidy")

    Returns:
        JSON string containing list of available versions

    Example:
        Input: "cetz"
        Output: ["0.2.2", "0.2.1", "0.2.0", ...]
    """
    eprint(f"mcp.tool('get_package_versions') called with package: {package_name}")

    try:
        from .package_docs import get_package_versions as _get_versions

        versions = _get_versions(package_name, timeout=15)
        return json.dumps(versions, indent=2)

    except RuntimeError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        eprint(f"Error in get_package_versions: {e}")
        return json.dumps({"error": f"Unexpected error: {str(e)}"})


@mcp.tool()
def get_package_docs(
    package_name: str, version: Optional[str] = None, summary: bool = False
) -> str:
    """
    Fetch documentation for a Typst Universe package.

    Args:
        package_name: Package name (e.g., "cetz", "tidy", "polylux")
        version: Version (defaults to latest)
        summary: If True, returns only metadata and file listings without full content

    Summary mode (summary=True):
        - Package metadata from typst.toml
        - README preview (first 500 characters)
        - File listings for examples/ and docs/ (names and sizes only)
        - Links and import statement
        - ~90% smaller response for large packages

    Full mode (summary=False, default):
        - Complete package documentation
        - Full README, LICENSE, CHANGELOG content
        - All example files with complete content
        - All docs files with complete content

    Recommended workflow for large packages:
        1. Call with summary=True to discover package structure
        2. Use get_package_file() to fetch specific files as needed

    Returns:
        JSON string with package documentation or summary

    Error handling:
        - Package not found: Returns {"error": "Package 'name' not found..."}
        - Network timeout: Returns {"error": "Timeout while fetching..."}
        - Invalid version: Returns {"error": "Version 'x.y.z' not found..."}

    Example:
        Input: package_name="cetz", summary=True
        Output: Lightweight summary with file listings (~5KB vs 50KB+)
    """
    eprint(
        f"mcp.tool('get_package_docs') called: {package_name}@{version}, summary={summary}"
    )

    try:
        from .package_docs import build_package_docs

        # Fetch package docs (with timeout handling built-in)
        docs = build_package_docs(package_name, version, timeout=30)

        if summary:
            # Return lightweight summary
            summary_data = {
                "package": docs["package"],
                "version": docs["version"],
                "metadata": docs["metadata"],
                "readme_preview": docs["readme"][:500] + "..."
                if docs.get("readme") and len(docs["readme"]) > 500
                else docs.get("readme"),
                "readme_full_size": len(docs["readme"]) if docs.get("readme") else 0,
                "license_type": docs["license"][:100] if docs.get("license") else None,
                "has_changelog": docs.get("changelog") is not None,
                "examples_list": [
                    {
                        "filename": ex["filename"],
                        "size": ex["size"],
                        "path": f"examples/{ex['filename']}",
                    }
                    for ex in (docs.get("examples") or [])
                ],
                "docs_list": [
                    {"filename": name, "size": len(content), "path": f"docs/{name}"}
                    for name, content in (docs.get("docs") or {}).items()
                ],
                "import_statement": docs["import_statement"],
                "universe_url": docs["universe_url"],
                "github_url": docs["github_url"],
                "homepage_url": docs.get("homepage_url"),
                "repository_url": docs.get("repository_url"),
                "note": "Use summary=false for full content, or get_package_file() for specific files",
            }
            return json.dumps(summary_data, indent=2)

        return json.dumps(docs, indent=2)

    except RuntimeError as e:
        # Handle expected errors (package not found, timeout, etc.)
        eprint(f"Error fetching package docs: {e}")
        return json.dumps({"error": str(e)})

    except Exception as e:
        # Handle unexpected errors
        eprint(f"Unexpected error in get_package_docs: {e}")
        return json.dumps(
            {
                "error": f"Unexpected error while fetching package documentation: {str(e)}",
                "package": package_name,
                "version": version,
            }
        )


@mcp.tool()
def get_package_file(package_name: str, version: str, file_path: str) -> str:
    """
    Fetch a specific file from a Typst package.

    Enables granular access to individual files within a package without
    fetching all documentation. Use after get_package_docs(summary=True)
    to discover available files.

    Args:
        package_name: Package name (e.g., "cetz")
        version: Package version (e.g., "0.2.2")
        file_path: Path within package (e.g., "examples/basic.typ", "docs/guide.md")

    Returns:
        JSON with file content or error message

    Error handling:
        - File not found: Returns {"error": "File 'path' not found..."}
        - Network error: Returns {"error": "..."}

    Example:
        Input: package_name="cetz", version="0.2.2", file_path="examples/plot.typ"
        Output: {"package": "cetz", "version": "0.2.2", "file_path": "...", "content": "..."}
    """
    eprint(f"mcp.tool('get_package_file') called: {package_name}@{version}/{file_path}")

    try:
        from .package_docs import fetch_file_from_github

        content = fetch_file_from_github(package_name, version, file_path, timeout=10)

        if content is None:
            return json.dumps(
                {
                    "error": f"File '{file_path}' not found in package '{package_name}@{version}'",
                    "package": package_name,
                    "version": version,
                    "file_path": file_path,
                    "note": "Check that the file path is correct. Use get_package_docs(summary=True) to see available files.",
                }
            )

        return json.dumps(
            {
                "package": package_name,
                "version": version,
                "file_path": file_path,
                "content": content,
                "size": len(content),
            },
            indent=2,
        )

    except Exception as e:
        eprint(f"Error in get_package_file: {e}")
        return json.dumps(
            {
                "error": str(e),
                "package": package_name,
                "version": version,
                "file_path": file_path,
            }
        )


@mcp.prompt()
def create_typst_document_prompt(document_type: str, requirements: str = ""):
    """
    Helps create a new Typst document from scratch.
    Provides guidance on document structure and common patterns.
    """
    requirements_text = (
        f"\n\nAdditional requirements:\n{requirements}" if requirements else ""
    )

    return {
        "name": "create-typst-document",
        "description": "Create a new Typst document with proper structure",
        "arguments": [
            {
                "name": "document_type",
                "description": "Type of document (article, report, presentation, etc.)",
                "required": True,
            },
            {
                "name": "requirements",
                "description": "Specific requirements or content to include",
                "required": False,
            },
        ],
        "messages": [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": f"""I need to create a new Typst {document_type}.{requirements_text}

Please help me create this document step by step:

1. First, check the Typst documentation for {document_type}-related features using `list_docs_chapters()` to find relevant chapters
2. Design an appropriate structure for a {document_type} including:
   - Document metadata (title, author, date)
   - Proper styling and layout
   - Section organization
   - Any special requirements for this document type
3. Create the Typst code with:
   - Clear comments explaining each section
   - Best practices for Typst
   - Proper use of Typst functions and markup
4. Validate the syntax using `check_if_snippet_is_valid_typst_syntax()`
5. Show me the complete document code
6. Optionally render a preview with `typst_snippet_to_image()`

Let's start by exploring the relevant Typst documentation!""",
                },
            }
        ],
    }


@mcp.prompt()
def fix_typst_syntax_prompt(typst_code: str, error_message: str = ""):
    """
    Helps troubleshoot and fix Typst syntax errors.
    Analyzes error messages and suggests corrections.
    """
    error_context = (
        f"\n\nError message:\n```\n{error_message}\n```" if error_message else ""
    )

    return {
        "name": "fix-typst-syntax",
        "description": "Troubleshoot and fix Typst syntax errors",
        "arguments": [
            {
                "name": "typst_code",
                "description": "The Typst code with syntax errors",
                "required": True,
            },
            {
                "name": "error_message",
                "description": "The error message from Typst compiler (if available)",
                "required": False,
            },
        ],
        "messages": [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": f"""I have Typst code with syntax errors that I need help fixing:

```typst
{typst_code}
```{error_context}

Please help me fix these errors step by step:

1. First, validate the code using `check_if_snippet_is_valid_typst_syntax()` to see the exact error
2. Analyze the error message to identify the problem:
   - Is it a syntax error (missing brackets, wrong function name)?
   - Is it using LaTeX syntax instead of Typst?
   - Is it a logic error?
3. Check relevant Typst documentation using `get_docs_chapter()` if needed for the correct syntax
4. Fix the code based on the error analysis
5. Validate the fixed code again
6. Repeat steps 4-5 until all errors are resolved
7. Show me the corrected code with explanations of what was wrong
8. Optionally render the corrected code with `typst_snippet_to_image()` to verify it works

Let's start by validating the code!""",
                },
            }
        ],
    }


@mcp.prompt()
def generate_typst_figure_prompt(description: str, latex_reference: str = ""):
    """
    Helps create figures, diagrams, or mathematical expressions in Typst.
    Can convert from LaTeX or create from description.
    """
    latex_context = (
        f"\n\nLaTeX reference code:\n```latex\n{latex_reference}\n```\n\nPlease convert this LaTeX as a starting point."
        if latex_reference
        else ""
    )

    return {
        "name": "generate-typst-figure",
        "description": "Create a figure, diagram, or mathematical expression in Typst",
        "arguments": [
            {
                "name": "description",
                "description": "Description of what to create",
                "required": True,
            },
            {
                "name": "latex_reference",
                "description": "Optional LaTeX code as reference",
                "required": False,
            },
        ],
        "messages": [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": f"""I need to create the following in Typst:

{description}{latex_context}

Please help me create this step by step:

1. {"If LaTeX reference provided, convert it using `latex_snippet_to_typst()`" if latex_reference else "Search for relevant examples and documentation using `get_docs_chapter()` or `search_packages()` if specialized packages are needed"}
2. Design the figure/diagram/expression structure in Typst
3. Write the Typst code with:
   - Proper syntax for the figure type (math, image, diagram, etc.)
   - Good formatting and readability
   - Comments explaining complex parts
4. Validate the syntax using `check_if_snippet_is_valid_typst_syntax()`
5. Fix any errors and validate again
6. Render the result using `typst_snippet_to_image()` so I can see how it looks
7. If needed, iterate on the design based on the preview
8. Show me the final Typst code

Let's create this figure!""",
                },
            }
        ],
    }


@mcp.prompt()
def typst_best_practices_prompt(topic: str = "general"):
    """
    Provides guidance on Typst best practices and common patterns.
    Useful for learning how to write idiomatic Typst code.
    """
    return {
        "name": "typst-best-practices",
        "description": "Learn Typst best practices and common patterns",
        "arguments": [
            {
                "name": "topic",
                "description": "Specific topic or feature to learn about (layout, styling, math, etc.)",
                "required": False,
            }
        ],
        "messages": [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": f"""I want to learn Typst best practices{f" for {topic}" if topic != "general" else ""}.

Please teach me step by step:

1. First, search the Typst documentation for relevant chapters about {topic} using `list_docs_chapters()` and `get_docs_chapter()`
2. Explain the key concepts and patterns for {topic} in Typst
3. Show me common patterns and idiomatic Typst code with examples:
   - Bad/non-idiomatic approach (what to avoid)
   - Good/idiomatic approach (best practice)
   - Explanation of why the good approach is better
4. For each example:
   - Validate the code using `check_if_snippet_is_valid_typst_syntax()`
   - Optionally render with `typst_snippet_to_image()` to show the result
5. If relevant, check if there are helpful packages using `search_packages()` that provide better ways to accomplish common tasks
6. Summarize the key takeaways and best practices

Let's explore Typst best practices for {topic}!""",
                },
            }
        ],
    }


def main():
    """Entry point for the MCP server."""
    # Check dependencies on startup
    check_dependencies()

    # Start background thread to build/load docs
    eprint("Starting Typst MCP Server...")
    eprint("\nTools available immediately:")
    eprint("  - LaTeX conversion: latex_snippet_to_typst, latex_snippets_to_typst")
    eprint(
        "  - Syntax validation: check_if_snippet_is_valid_typst_syntax, check_if_snippets_are_valid_typst_syntax"
    )
    eprint("  - Image rendering: typst_snippet_to_image")
    eprint("  - Package search: search_packages, list_packages, get_package_versions")
    eprint("  - Package docs: get_package_docs (fetches on-demand)")
    eprint("\nResources available:")
    eprint("  - typst://packages/cached - List of all cached package docs")
    eprint("  - typst://package/{name}/{version} - Cached package documentation")
    eprint(
        "\nCore documentation tools will be available after docs are loaded/built...\n"
    )

    doc_thread = threading.Thread(target=build_docs_background, daemon=True)
    doc_thread.start()

    # Run the server
    mcp.run()


if __name__ == "__main__":
    main()
