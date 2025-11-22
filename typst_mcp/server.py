from collections import Counter
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Literal
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import anyio
import numpy as np
from PIL import Image as PILImage

from fastmcp import FastMCP, Context
from fastmcp.exceptions import ToolError, ResourceError
from fastmcp.utilities.logging import get_logger
from fastmcp.utilities.types import Image, File

# Import local modules
from . import sandbox
from .settings import typst_settings
from .models import (
    ChapterInfo,
    ChapterResponse,
    PackageDocsSummary,
    PackageSearchResult,
    ValidationResult,
    ConversionResult,
    MCPErrorCodes,
    MAX_SNIPPET_LENGTH,
    MAX_LATEX_SNIPPET_LENGTH,
    MAX_QUERY_LENGTH,
)

# Use configured temp directory
temp_dir = str(typst_settings.temp_dir)

# Create logger instance
logger = get_logger("typst-mcp")

# Server version and metadata
__version__ = "0.2.0"
_server_start_time = time.time()

# Create FastMCP server instance
# Note: FastMCP only accepts name parameter in current version
mcp = FastMCP("Typst MCP Server")

# Global state for lazy-loaded docs
_docs_state = {
    "loaded": False,
    "building": False,
    "error": None,
    "docs": None,
    "lock": None,  # Lazy initialize to avoid race condition at module import
}


async def _get_docs_lock() -> anyio.Lock:
    """Get or create the docs state lock (lazy initialization).

    This avoids creating anyio.Lock() at module import time before
    any event loop exists, which would cause a race condition.
    """
    if _docs_state["lock"] is None:
        _docs_state["lock"] = anyio.Lock()
    return _docs_state["lock"]

# Privacy-preserving telemetry (no user data)
_telemetry = {
    "tool_calls": Counter(),
    "resource_accesses": Counter(),
    "errors": Counter(),
}

# Import package cache from package_docs module (single source of truth)
from .package_docs import _package_cache

# Maximum results for list/search operations
MAX_RESULTS = 1000


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
        logger.warning("=" * 60)
        logger.warning("Missing required external tools:")
        logger.warning("=" * 60)
        for msg in missing:
            logger.warning(msg)
        logger.warning("Installation instructions:")
        logger.warning("  macOS:   brew install typst pandoc")
        logger.warning("  Linux:   apt install typst pandoc  # or your package manager")
        logger.warning("  Windows: See installation links above")
        logger.warning("=" * 60)


async def build_docs_background(ctx: Context | None = None):
    """Build documentation in background with optional progress reporting."""
    from .build_docs import build_typst_docs
    from . import package_docs  # Import package_docs module to initialize it

    lock = await _get_docs_lock()
    async with lock:
        if _docs_state["loaded"] or _docs_state["building"]:
            return
        _docs_state["building"] = True

    try:
        cache_dir = typst_settings.get_cache_dir()
        docs_dir = cache_dir / "typst-docs"
        docs_json = docs_dir / "main.json"

        # Check if docs already exist
        if docs_json.exists():
            if ctx:
                await ctx.debug("Loading existing Typst documentation...")
            logger.info("✓ Loading existing Typst documentation...")

            # Async file read
            async with await anyio.open_file(docs_json, "r", encoding="utf-8") as f:
                content = await f.read()
                docs_data = json.loads(content)

            async with lock:
                _docs_state["docs"] = docs_data
                _docs_state["loaded"] = True
                _docs_state["building"] = False

            if ctx:
                await ctx.info("Documentation loaded successfully")
            logger.info("✓ Documentation loaded successfully")
            return

        # Build docs if they don't exist
        if ctx:
            await ctx.info("Building Typst documentation (first run, 1-2 minutes)...")
            if typst_settings.enable_progress_reporting:
                await ctx.report_progress(0, 100, "Starting documentation build")

        logger.info("=" * 60)
        logger.info("Typst documentation not found - building in background...")
        logger.info("=" * 60)
        logger.info("(This is a one-time process that may take 1-2 minutes)")
        logger.info("Note: Non-doc tools are available immediately!")

        # Run build in thread pool (build_typst_docs is synchronous)
        success = await anyio.to_thread.run_sync(build_typst_docs)

        if not success:
            async with lock:
                _docs_state["error"] = "Failed to build documentation"
                _docs_state["building"] = False

            error_msg = "Failed to build documentation automatically"
            if ctx:
                await ctx.error(error_msg)
            logger.error("=" * 60)
            logger.error(f"ERROR: {error_msg}")
            logger.error("Documentation-related tools will not be available.")
            logger.error("=" * 60)
            return

        # Load the built docs
        async with await anyio.open_file(docs_json, "r", encoding="utf-8") as f:
            content = await f.read()
            docs_data = json.loads(content)

        async with lock:
            _docs_state["docs"] = docs_data
            _docs_state["loaded"] = True
            _docs_state["building"] = False

        if ctx:
            await ctx.info("Documentation built and loaded successfully!")
            if typst_settings.enable_progress_reporting:
                await ctx.report_progress(100, 100, "Complete")

        logger.info("=" * 60)
        logger.info("Documentation built and loaded successfully!")
        logger.info("All tools are now available.")
        logger.info("=" * 60)

    except Exception as e:
        async with lock:
            _docs_state["error"] = str(e)
            _docs_state["building"] = False

        error_msg = f"Error building docs: {e}"
        if ctx:
            await ctx.error(error_msg)
        logger.error(error_msg)


async def get_docs(wait_seconds: int | None = None) -> dict:
    """
    Get the typst docs, waiting if they're being built.
    Returns docs or raises error if not available.
    """
    if wait_seconds is None:
        wait_seconds = typst_settings.docs_wait_timeout

    # If already loaded, return immediately
    if _docs_state["loaded"]:
        return _docs_state["docs"]

    # If building, wait briefly
    if _docs_state["building"]:
        logger.info(f"Waiting for documentation to finish building (max {wait_seconds}s)...")
        start = time.time()
        while time.time() - start < wait_seconds:
            await anyio.sleep(0.5)
            if _docs_state["loaded"]:
                return _docs_state["docs"]

        # Still building after wait
        raise ResourceError(
            "Documentation is still building. This typically takes 1-2 minutes on first run. "
            "Please try again in a moment. Non-documentation tools (LaTeX conversion, "
            "syntax validation, image rendering) are available immediately."
        )

    # Not loaded and not building - check for error
    if _docs_state["error"]:
        raise ResourceError(f"Documentation failed to build: {_docs_state['error']}")

    # Should not reach here, but handle gracefully
    raise ResourceError("Documentation not available. Please restart the server.")


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


# Removed create_pdf_resource - now using File type from fastmcp.utilities.types


def cleanup_old_pdfs(directory: Path, max_age_hours: int = 24):
    """Remove PDF files older than max_age_hours.

    Args:
        directory: Directory containing PDFs
        max_age_hours: Maximum age in hours before deletion (0 = delete all)
    """
    import time

    if not directory.exists():
        return

    cutoff_time = time.time() - (max_age_hours * 3600)
    cleaned_count = 0

    try:
        for pdf_file in directory.glob("*.pdf"):
            try:
                if pdf_file.stat().st_mtime < cutoff_time:
                    pdf_file.unlink()
                    cleaned_count += 1
            except Exception as e:
                logger.warning(f"Could not delete {pdf_file.name}: {e}")

        if cleaned_count > 0:
            logger.debug(f"Cleaned up {cleaned_count} old PDF(s)")
    except Exception as e:
        logger.warning(f"PDF cleanup failed: {e}")


def get_pdf_output_dir() -> Path:
    """
    Get the output directory for PDF files in path mode.

    PDFs older than 24 hours are automatically cleaned up.

    Returns:
        Path to the PDF output directory (in /tmp or platform equivalent)
    """
    # Use system temp directory
    pdf_dir = Path(tempfile.gettempdir()) / "typst-mcp-pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    # SECURITY: Clean up PDFs older than 24 hours on each access
    cleanup_old_pdfs(pdf_dir, max_age_hours=24)

    return pdf_dir


@mcp.tool()
async def list_docs_chapters(ctx: Context) -> str:
    """Lists all chapters in the Typst documentation.

    The LLM should use this to get an overview of available documentation chapters,
    then decide which specific chapter to read.

    Args:
        ctx: MCP context for logging and progress reporting

    Returns:
        JSON string containing list of chapters with routes and sizes

    Raises:
        ResourceError: If documentation is not available
    """
    await ctx.debug("Listing documentation chapters")

    try:
        typst_docs = await get_docs(wait_seconds=10)
    except ResourceError as e:
        await ctx.error(f"Documentation not available: {e}")
        return json.dumps({"error": str(e)})

    chapters = []
    for chapter in typst_docs:
        chapters.append(
            {"route": chapter["route"], "content_length": len(json.dumps(chapter))}
        )
        chapters += list_child_routes(chapter)

    await ctx.info(f"Found {len(chapters)} documentation chapters")
    return json.dumps(chapters)


@mcp.tool()
async def get_docs_chapter(route: str, ctx: Context) -> str:
    """Gets a chapter by route.

    The route is the path to the chapter in the typst docs.
    For example, the route "____reference____layout____colbreak" corresponds to
    the chapter "reference/layout/colbreak".
    The route uses underscores ("____") as path separators.

    If a chapter has children and its content length is over 1000, this will only
    return the child routes instead of the full content to avoid overwhelming responses.

    Args:
        route: Chapter route with ____ as path separator
        ctx: MCP context for logging

    Returns:
        JSON string containing chapter content or child routes

    Raises:
        ResourceError: If documentation is not available or chapter not found
    """
    _telemetry["tool_calls"]["get_docs_chapter"] += 1
    await ctx.debug(f"Fetching chapter: {route}")

    try:
        typst_docs = await get_docs(wait_seconds=10)
    except ResourceError as e:
        _telemetry["errors"]["get_docs_chapter"] += 1
        await ctx.error(f"Documentation not available: {e}")
        raise

    # Convert underscores to slashes
    route = route.replace("____", "/")

    def route_matches(chapter_route: str, input_route: str) -> bool:
        return chapter_route.strip("/") == input_route.strip("/")

    def get_child(chapter: dict, route: str) -> dict:
        """Gets a child chapter by route."""
        if "children" not in chapter:
            return {}
        for child in chapter["children"]:
            if route_matches(child["route"], route):
                return child
            child_result = get_child(child, route)
            if child_result:
                return child_result
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
        await ctx.warning(f"Chapter not found: {route}")
        raise ResourceError(f"Chapter not found: {route}")

    # Check if chapter has children and is large
    content_length = len(json.dumps(found_chapter))
    await ctx.debug(f"Chapter size: {content_length} bytes")

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

        await ctx.info(f"Large chapter with {len(child_routes)} children, returning routes only")

        # Create simplified chapter with only essential info and child routes
        simplified_chapter = {
            "route": found_chapter["route"],
            "title": found_chapter.get("title", ""),
            "content_length": content_length,
            "note": "This chapter is large. Only child routes are shown. Request specific child routes for detailed content.",
            "child_routes": child_routes,
        }
        return json.dumps(simplified_chapter)

    await ctx.info(f"Returning chapter content ({content_length} bytes)")
    return json.dumps(found_chapter)


@mcp.tool()
async def get_docs_chapters(routes: list[str], ctx: Context) -> str:
    """Gets multiple chapters by their routes.

    Takes a list of routes and returns a JSON stringified list of results.

    Args:
        routes: List of chapter routes
        ctx: MCP context for logging

    Returns:
        JSON string containing list of chapter contents

    Example:
        Input: ["____reference____layout____colbreak", "____reference____text____text"]
        Output: JSON stringified list containing the content of both chapters
    """
    _telemetry["tool_calls"]["get_docs_chapters"] += 1
    await ctx.debug(f"Fetching {len(routes)} chapters")

    results = []
    for route in routes:
        try:
            chapter_json = await get_docs_chapter(route, ctx)
            results.append(json.loads(chapter_json))
        except ResourceError as e:
            await ctx.warning(f"Failed to fetch chapter {route}: {e}")
            results.append({"error": str(e), "route": route})

    await ctx.info(f"Successfully fetched {len(results)} chapters")
    return json.dumps(results)


@mcp.tool()
async def latex_snippet_to_typst(latex_snippet: str, ctx: Context) -> str:
    r"""Converts LaTeX to Typst using Pandoc.

    LLMs are often better at writing LaTeX than Typst, so this tool enables
    writing in LaTeX and converting to Typst automatically.

    Args:
        latex_snippet: LaTeX code to convert (max 50KB)
        ctx: MCP context for logging and progress reporting

    Returns:
        Converted Typst code (stripped of leading/trailing whitespace)

    Raises:
        ToolError: If conversion fails due to invalid LaTeX, Pandoc error, or input too large

    Examples:
        Math expression:
        ```latex
        $ f\in K ( t^ { H } , \beta ) _ { \delta } $
        ```
        Converts to:
        ```typst
        $f in K \( t^H \, beta \)_delta$
        ```

        Figure:
        ```latex
        \begin{figure}[t]
            \includegraphics[width=8cm]{"placeholder.png"}
            \caption{Placeholder image}
            \label{fig:placeholder}
        \end{figure}
        ```
        Converts to:
        ```typst
        #figure(image("placeholder.png", width: 8cm),
            caption: [Placeholder image]
        )
        <fig:placeholder>
        ```
    """
    _telemetry["tool_calls"]["latex_snippet_to_typst"] += 1

    # Input validation
    if len(latex_snippet) > MAX_LATEX_SNIPPET_LENGTH:
        _telemetry["errors"]["latex_snippet_to_typst"] += 1
        await ctx.error(f"LaTeX snippet too large: {len(latex_snippet)} bytes")
        raise ToolError(
            f"LaTeX snippet too large: {len(latex_snippet)} bytes (max {MAX_LATEX_SNIPPET_LENGTH} bytes)",
            code=MCPErrorCodes.TIMEOUT,
        )

    await ctx.debug(f"Converting LaTeX snippet ({len(latex_snippet)} chars)")

    # Write LaTeX to temp file (async)
    tex_file = Path(temp_dir) / "main.tex"
    typ_file = Path(temp_dir) / "main.typ"

    await anyio.Path(tex_file).write_text(latex_snippet, encoding="utf-8")

    # Run Pandoc conversion in thread pool (sandboxed)
    try:
        await anyio.to_thread.run_sync(
            lambda: sandbox.run_sandboxed(
                [
                    "pandoc",
                    "--sandbox",  # SECURITY: Prevent arbitrary file operations
                    str(tex_file),
                    "--from=latex",
                    "--to=typst",
                    "--output",
                    str(typ_file),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=typst_settings.pandoc_timeout,
            )
        )
    except subprocess.CalledProcessError as e:
        _telemetry["errors"]["latex_snippet_to_typst"] += 1
        error_message = e.stderr.strip() if e.stderr else "Unknown error"
        await ctx.error(f"Pandoc conversion failed: {error_message}")
        raise ToolError(
            f"Failed to convert LaTeX to Typst. Pandoc error: {error_message}",
            code=MCPErrorCodes.OPERATION_FAILED,
            details={"snippet_length": len(latex_snippet), "stderr": error_message},
        ) from e

    # Read converted Typst code (async)
    typst_code = await anyio.Path(typ_file).read_text(encoding="utf-8")
    typst_code = typst_code.strip()

    await ctx.info(f"Successfully converted to Typst ({len(typst_code)} chars)")
    return typst_code


@mcp.tool()
async def latex_snippets_to_typst(latex_snippets: list[str], ctx: Context) -> str:
    r"""Converts multiple LaTeX snippets to Typst.

    Takes a list of LaTeX snippets and returns a JSON stringified list of results.

    Args:
        latex_snippets: List of LaTeX code strings
        ctx: MCP context for logging

    Returns:
        JSON string containing list of converted Typst code

    Example:
        Input: ["$f\in K ( t^ { H } , \beta ) _ { \delta }$", "\\begin{align} a &= b \\\\ c &= d \\end{align}"]
        Output: JSON stringified list containing the converted typst for each snippet
    """
    _telemetry["tool_calls"]["latex_snippets_to_typst"] += 1
    await ctx.debug(f"Converting {len(latex_snippets)} LaTeX snippets")

    results = []
    for i, snippet in enumerate(latex_snippets):
        try:
            converted = await latex_snippet_to_typst(snippet, ctx)
            results.append(converted)
        except ToolError as e:
            await ctx.warning(f"Failed to convert snippet {i+1}: {e}")
            results.append(f"ERROR: {e}")

    await ctx.info(f"Converted {len(results)} snippets")
    return json.dumps(results)


@mcp.tool()
async def check_if_snippet_is_valid_typst_syntax(typst_snippet: str, ctx: Context) -> str:
    r"""Checks if the given Typst text has valid syntax.

    Returns "VALID" if valid, otherwise returns "INVALID! Error message: {error_message}".

    The LLM should use this to check if the typst syntax it generated is valid.
    If not valid, the LLM should try to fix it and check again.

    Args:
        typst_snippet: Typst code to validate (max 100KB)
        ctx: MCP context for logging

    Returns:
        "VALID" if syntax is valid, or "INVALID! Error message: ..." with details

    Example 1:
        Input: "$f in K \( t^H \, beta \)_delta$"
        Output: "VALID"

    Example 2:
        Input: "$a = \frac{1}{2}$"
        Output: "INVALID! Error message: {error: unknown variable: rac ...}"
    """
    _telemetry["tool_calls"]["check_if_snippet_is_valid_typst_syntax"] += 1

    # Input validation
    if len(typst_snippet) > MAX_SNIPPET_LENGTH:
        await ctx.error(f"Snippet too large: {len(typst_snippet)} bytes")
        return f"INVALID! Error message: Snippet too large ({len(typst_snippet)} bytes, max {MAX_SNIPPET_LENGTH} bytes)"

    await ctx.debug(f"Validating Typst snippet ({len(typst_snippet)} chars)")

    # Write to temp file (async)
    typ_file = anyio.Path(temp_dir) / "main.typ"
    await typ_file.write_text(typst_snippet, encoding="utf-8")

    # Run validation in thread pool
    try:
        await anyio.to_thread.run_sync(
            lambda: sandbox.run_sandboxed(
                ["typst", "compile", str(typ_file)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,  # SECURITY: Prevent DoS from malicious code
            )
        )
        await ctx.info("Syntax validation: VALID")
        return "VALID"
    except subprocess.CalledProcessError as e:
        error_message = e.stderr.strip() if e.stderr else "Unknown error"
        await ctx.debug(f"Syntax validation failed: {error_message[:100]}...")
        return f"INVALID! Error message: {error_message}"


@mcp.tool()
async def check_if_snippets_are_valid_typst_syntax(typst_snippets: list[str], ctx: Context) -> str:
    r"""Checks if multiple Typst snippets have valid syntax.

    Takes a list of typst snippets and returns a JSON stringified list of results.

    Args:
        typst_snippets: List of Typst code strings to validate
        ctx: MCP context for logging

    Returns:
        JSON string containing list of validation results ("VALID" or error messages)

    Example:
        Input: ["$f in K \( t^H \, beta \)_delta$", "#let x = 1\n#x"]
        Output: JSON list containing validation results
    """
    _telemetry["tool_calls"]["check_if_snippets_are_valid_typst_syntax"] += 1
    await ctx.debug(f"Validating {len(typst_snippets)} Typst snippets")

    results = []
    for i, snippet in enumerate(typst_snippets):
        result = await check_if_snippet_is_valid_typst_syntax(snippet, ctx)
        results.append(result)

    valid_count = sum(1 for r in results if r == "VALID")
    await ctx.info(f"Validated {len(results)} snippets: {valid_count} valid, {len(results) - valid_count} invalid")
    return json.dumps(results)


@mcp.tool()
async def typst_snippet_to_image(typst_snippet: str, ctx: Context) -> Image:
    r"""Converts Typst code to an image using the typst command line tool.

    It is capable of converting multiple pages to a single PNG image.
    The image gets cropped to the content and padded with 10px on each side.

    The LLM should use this to convert typst to an image and then evaluate if the image
    is what it wanted. If not valid, the LLM should try to fix it and check again.

    Args:
        typst_snippet: Typst code to render (max 100KB)
        ctx: MCP context for logging

    Returns:
        Image object containing PNG data

    Raises:
        ToolError: If rendering fails or no pages generated

    Example 1:
        Input: "$f in K \( t^H \, beta \)_delta$"
        Output: Image object with rendered math

    Example 2:
        Input: "#figure(...)"
        Output: Image object with rendered figure
    """
    _telemetry["tool_calls"]["typst_snippet_to_image"] += 1

    # Input validation
    if len(typst_snippet) > MAX_SNIPPET_LENGTH:
        _telemetry["errors"]["typst_snippet_to_image"] += 1
        await ctx.error(f"Snippet too large: {len(typst_snippet)} bytes")
        raise ToolError(
            f"Typst snippet too large: {len(typst_snippet)} bytes (max {MAX_SNIPPET_LENGTH} bytes)",
            code=MCPErrorCodes.TIMEOUT,
        )

    await ctx.debug(f"Rendering Typst to image ({len(typst_snippet)} chars)")

    # Write to temp file (async)
    typ_file = anyio.Path(temp_dir) / "main.typ"
    await typ_file.write_text(typst_snippet, encoding="utf-8")

    # Run Typst compiler in thread pool
    try:
        await anyio.to_thread.run_sync(
            lambda: sandbox.run_sandboxed(
                [
                    "typst",
                    "compile",
                    str(typ_file),
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
                timeout=60,  # SECURITY: Prevent DoS (longer for image generation)
            )
        )
    except subprocess.CalledProcessError as e:
        _telemetry["errors"]["typst_snippet_to_image"] += 1
        error_message = e.stderr.strip() if e.stderr else "Unknown error"
        await ctx.error(f"Typst compilation failed: {error_message[:100]}...")
        raise ToolError(
            f"Failed to convert Typst to image: {error_message}",
            code=MCPErrorCodes.OPERATION_FAILED,
            details={"snippet_length": len(typst_snippet), "stderr": error_message},
        ) from e

    # Find all generated pages (use async path checking)
    page_files = []
    page_num = 1
    while await anyio.Path(os.path.join(temp_dir, f"page{page_num}.png")).exists():
        page_files.append(os.path.join(temp_dir, f"page{page_num}.png"))
        page_num += 1

    if not page_files:
        _telemetry["errors"]["typst_snippet_to_image"] += 1
        await ctx.error("No pages generated")
        raise ToolError("No pages were generated by Typst compiler")

    await ctx.debug(f"Processing {len(page_files)} page(s)")

    # Process images in thread pool (CPU-intensive)
    def process_images():
        """Process and combine page images (runs in thread pool)."""
        pages = []
        for page_file in page_files:
            # Use context manager to ensure PIL images are properly closed
            with PILImage.open(page_file) as img:
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

                    # Crop image to bounding box and copy to avoid reference to closed image
                    cropped_img = img.crop((left, top, right + 1, bottom + 1)).copy()
                    pages.append(cropped_img)
                else:
                    # If image is completely white, copy it to avoid reference to closed image
                    pages.append(img.copy())

        if not pages:
            raise ValueError("Failed to process page images")

        # Calculate total dimensions
        total_width = max(page.width for page in pages)
        total_height = sum(page.height for page in pages)

        # Create combined image
        combined_image = PILImage.new("RGB", (total_width, total_height), (255, 255, 255))

        # Paste all pages vertically
        y_offset = 0
        for page in pages:
            x_offset = (total_width - page.width) // 2
            combined_image.paste(page, (x_offset, y_offset))
            y_offset += page.height
            # Close the copied page image after pasting
            page.close()

        # Save to bytes
        img_bytes_io = io.BytesIO()
        combined_image.save(img_bytes_io, format="PNG")
        combined_image.close()
        return img_bytes_io.getvalue()

    try:
        img_bytes = await anyio.to_thread.run_sync(process_images)
    except Exception as e:
        _telemetry["errors"]["typst_snippet_to_image"] += 1
        await ctx.error(f"Image processing failed: {e}")
        raise ToolError(f"Failed to process page images: {e}") from e
    finally:
        # Clean up temp files (async)
        try:
            await typ_file.unlink()
            for page_file in page_files:
                await anyio.Path(page_file).unlink()
        except Exception:
            pass  # Ignore cleanup errors

    await ctx.info(f"Generated image ({len(img_bytes)} bytes, {len(page_files)} page(s))")
    return Image(data=img_bytes, format="png")


# NOTE: This tool is registered manually in main() with dynamic description
# Do not add @mcp.tool() decorator here - it needs runtime sandbox info
async def typst_snippet_to_pdf(
    typst_snippet: str,
    ctx: Context,
    output_mode: Literal["embedded", "path"] = "embedded",
    output_path: str | None = None,
) -> File | str:
    r"""
    Converts Typst code to a PDF document using the typst command line tool.
    Handles multi-page documents naturally (PDF format supports multiple pages).

    The LLM should use this to generate PDF documents from Typst code.
    Useful for creating complete documents, reports, or presentations.

    Args:
        typst_snippet: Typst code to compile to PDF
        output_mode: How to return the PDF:
            - "embedded" (default): Returns Image object with PDF data
            - "path": Saves PDF to disk and returns absolute file path
        output_path: Optional custom filepath for saving PDF (only used with output_mode="path").
                     If not provided, generates automatic filename in temp directory.

                     SECURITY: For security reasons, output_path must be within allowed directories:
                     - Current working directory (where MCP server was started)
                     - System temp directory (platform-specific)
                     - Any custom directories set via TYPST_MCP_ALLOW_WRITE environment variable

                     If you need to write outside these directories, use output_mode="embedded" instead.

    Returns:
        - If output_mode="embedded": File object containing PDF binary data
        - If output_mode="path": Absolute path to saved PDF file (string)

    Raises:
        ToolError: If PDF compilation fails

    Output modes:
        - "embedded": Best for small-medium PDFs, returns File object with PDF data.
                      FastMCP automatically converts to EmbeddedResource with base64 encoding.
                      No filesystem restrictions - PDF data returned directly.
        - "path": Best for large PDFs or file-based workflows, returns file path string.
                  Subject to filesystem security restrictions (see output_path docs).

    Example 1 - Simple document (embedded mode - recommended):
    ```typst
    #set page(paper: "a4")
    #set text(font: "New Computer Modern")

    = Introduction
    This is a sample PDF document.
    ```

    Example 2 - Path mode with auto-generated filename (always allowed):
    ```python
    typst_snippet_to_pdf(typst_code, output_mode="path")
    # Returns: "/tmp/typst-mcp-pdf/document_20250120_143022_abc123.pdf"
    ```

    Example 3 - Path mode with custom filepath (must be in allowed directory):
    ```python
    # ✅ Works if current directory is /home/user/project
    typst_snippet_to_pdf(typst_code, output_mode="path", output_path="/home/user/project/my_doc.pdf")

    # ❌ Fails if /home/user is NOT an allowed directory
    typst_snippet_to_pdf(typst_code, output_mode="path", output_path="/home/user/my_doc.pdf")
    # ERROR: Output path not in allowed directories
    ```

    Example 4 - Large document (use path mode with auto-generated name):
    ```typst
    #set page(paper: "us-letter")

    = Chapter 1
    Content for page 1...

    #pagebreak()

    = Chapter 2
    Content for page 2...
    ```

    Security Notes:
    - Typst compilation runs in a sandbox (cannot access sensitive files)
    - PDF output is restricted to allowed directories for additional protection
    - Use embedded mode if you need maximum flexibility (no filesystem access)

    {{SANDBOX_PATHS_PLACEHOLDER}}
    """
    _telemetry["tool_calls"]["typst_snippet_to_pdf"] += 1

    # Input validation
    if len(typst_snippet) > MAX_SNIPPET_LENGTH:
        _telemetry["errors"]["typst_snippet_to_pdf"] += 1
        await ctx.error(f"Snippet too large: {len(typst_snippet)} bytes")
        raise ToolError(
            f"Typst snippet too large: {len(typst_snippet)} bytes (max {MAX_SNIPPET_LENGTH} bytes)",
            code=MCPErrorCodes.TIMEOUT,
        )

    await ctx.debug(f"Compiling Typst to PDF (mode: {output_mode}, {len(typst_snippet)} chars)")

    # Progress reporting for large documents
    if typst_settings.enable_progress_reporting:
        await ctx.report_progress(0, 100, "Starting PDF compilation")

    # Create unique temp files using uuid instead of task group ID hack
    unique_id = uuid.uuid4().hex[:8]
    typ_file = Path(temp_dir) / f"main_{unique_id}.typ"
    pdf_file = Path(temp_dir) / f"output_{unique_id}.pdf"

    try:
        # Write snippet (async)
        await anyio.Path(typ_file).write_text(typst_snippet, encoding="utf-8")

        # Compile to PDF (run in thread pool)
        # Note: anyio.to_thread.run_sync doesn't accept kwargs, so we use a lambda
        await anyio.to_thread.run_sync(
            lambda: sandbox.run_sandboxed(
                ["typst", "compile", str(typ_file), str(pdf_file)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=typst_settings.typst_compile_timeout,
            )
        )

        # Read PDF bytes (async)
        pdf_bytes = await anyio.Path(pdf_file).read_bytes()

        await ctx.debug(f"Generated PDF ({len(pdf_bytes)} bytes)")

        # Progress reporting
        if typst_settings.enable_progress_reporting:
            await ctx.report_progress(80, 100, "PDF generated, preparing output")

        # Return based on output mode
        if output_mode == "path":
            # Determine output path
            if output_path:
                # Use custom filepath (must be absolute)
                final_path = Path(output_path).absolute()

                # SECURITY: Use sandboxed copy instead of Python file I/O
                # This ensures sandbox restrictions are enforced by the OS, not our code
                # Avoids vulnerabilities: symlinks, path traversal, race conditions, etc.

                try:
                    # Use secure_copy_file (cross-platform, sandbox-enforced, async)
                    await anyio.to_thread.run_sync(
                        partial(
                            sandbox.secure_copy_file,
                            str(pdf_file),
                            str(final_path),
                            timeout=10,
                        )
                    )

                    # SECURITY: Restrict file permissions to owner-only (0600)
                    try:
                        await anyio.to_thread.run_sync(os.chmod, final_path, 0o600)
                    except Exception as chmod_err:
                        logger.warning(f"Could not restrict PDF permissions: {chmod_err}")

                except (subprocess.CalledProcessError, FileNotFoundError, RuntimeError) as e:
                    # Sandbox blocked the operation or copy failed
                    if isinstance(e, subprocess.CalledProcessError):
                        error_msg = e.stderr.strip() if e.stderr else "Permission denied or path not allowed"
                    else:
                        error_msg = str(e)

                    # Get allowed directories for helpful error message
                    sb = sandbox.get_sandbox()
                    allowed_dirs = sb.config.allow_write if sb and sb.config else []

                    error_details = (
                        f"Could not write PDF to requested location.\n"
                        f"Requested: {final_path}\n"
                        f"Error: {error_msg}\n\n"
                        f"Allowed write directories (enforced by OS sandbox):\n" +
                        "\n".join(f"  - {d}" for d in allowed_dirs) +
                        "\n\nFor security reasons, PDFs can only be written to:\n"
                        "  1. Current working directory (where server started)\n"
                        "  2. System temp directory\n"
                        "  3. Custom directories via TYPST_MCP_ALLOW_WRITE environment variable\n"
                        "  4. Use output_mode='embedded' to get PDF data directly (no restrictions)"
                    )

                    await ctx.error(f"PDF write failed: {error_msg}")
                    raise ToolError(error_details) from e

            else:
                # Generate automatic filename in temp directory (always allowed)
                output_dir = get_pdf_output_dir()
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                unique_name = f"document_{timestamp}_{os.urandom(3).hex()}.pdf"
                final_path = output_dir / unique_name

                # Copy using sandboxed copy (async)
                try:
                    await anyio.to_thread.run_sync(
                        partial(
                            sandbox.secure_copy_file,
                            str(pdf_file),
                            str(final_path),
                            timeout=10,
                        )
                    )
                except Exception as e:
                    await ctx.error(f"Failed to copy PDF: {e}")
                    raise ToolError(f"Failed to copy PDF to temp directory: {e}") from e

            await ctx.info(f"PDF saved to: {final_path}")

            if typst_settings.enable_progress_reporting:
                await ctx.report_progress(100, 100, "Complete")

            return str(final_path)

        else:  # embedded mode (default)
            # Return File object - FastMCP automatically converts to EmbeddedResource
            await ctx.info(f"Returning embedded PDF ({len(pdf_bytes)} bytes)")

            if typst_settings.enable_progress_reporting:
                await ctx.report_progress(100, 100, "Complete")

            return File(
                data=pdf_bytes,
                format="pdf",
                name="document.pdf",
            )

    except subprocess.CalledProcessError as e:
        _telemetry["errors"]["typst_snippet_to_pdf"] += 1
        error_message = e.stderr.strip() if e.stderr else "Unknown error"
        await ctx.error(f"Typst compilation failed: {error_message}")
        raise ToolError(
            f"Failed to compile Typst to PDF: {error_message}",
            code=MCPErrorCodes.OPERATION_FAILED,
            details={"snippet_length": len(typst_snippet), "stderr": error_message},
        ) from e
    finally:
        # Cleanup temp files (async)
        for temp_file in [typ_file, pdf_file]:
            try:
                if await anyio.Path(temp_file).exists():
                    await anyio.Path(temp_file).unlink()
            except Exception:
                pass  # Ignore cleanup errors


# ============================================================================
# SERVER HEALTH AND TELEMETRY TOOLS
# ============================================================================


@mcp.tool()
async def server_health(ctx: Context) -> dict:
    """Health check endpoint for monitoring.

    Returns server health status, version, and operational state.

    Args:
        ctx: MCP context for logging

    Returns:
        Dictionary containing health status and server information
    """
    _telemetry["tool_calls"]["server_health"] += 1
    await ctx.debug("Health check requested")

    health = {
        "status": "healthy",
        "version": __version__,
        "server_name": "Typst MCP Server",
        "docs_status": {
            "loaded": _docs_state["loaded"],
            "building": _docs_state["building"],
            "error": _docs_state["error"],
        },
        "sandbox_enabled": sandbox.get_sandbox().sandboxed if sandbox.get_sandbox() else False,
        "uptime_seconds": round(time.time() - _server_start_time, 2),
    }

    await ctx.info(f"Health check: {health['status']}")
    return health


@mcp.tool()
async def server_stats(ctx: Context) -> dict:
    """Get server usage statistics (privacy-preserving).

    Returns telemetry data about tool usage, errors, and performance.
    No user data is collected - only aggregate counts.

    Args:
        ctx: MCP context for logging

    Returns:
        Dictionary containing usage statistics and performance metrics
    """
    _telemetry["tool_calls"]["server_stats"] += 1
    await ctx.debug("Statistics requested")

    uptime = time.time() - _server_start_time
    total_tool_calls = sum(_telemetry["tool_calls"].values())
    total_errors = sum(_telemetry["errors"].values())

    stats = {
        "uptime": {
            "seconds": round(uptime, 2),
            "hours": round(uptime / 3600, 2),
            "human": f"{int(uptime // 3600)}h {int((uptime % 3600) // 60)}m",
        },
        "tool_calls": {
            "total": total_tool_calls,
            "by_tool": dict(_telemetry["tool_calls"]),
            "top_5": _telemetry["tool_calls"].most_common(5),
        },
        "errors": {
            "total": total_errors,
            "by_tool": dict(_telemetry["errors"]),
        },
        "resource_accesses": {
            "total": sum(_telemetry["resource_accesses"].values()),
            "by_resource": dict(_telemetry["resource_accesses"]),
        },
        "performance": {
            "requests_per_hour": round((total_tool_calls / uptime) * 3600, 2) if uptime > 0 else 0,
            "error_rate": round((total_errors / total_tool_calls) * 100, 2) if total_tool_calls > 0 else 0,
        },
        "cache": {
            "cached_packages": len(_package_cache),
        },
    }

    await ctx.info(f"Serving stats: {total_tool_calls} calls, {total_errors} errors, {uptime/3600:.1f}h uptime")
    return stats


# ============================================================================
# CORE TYPST DOCUMENTATION RESOURCES
# ============================================================================


@mcp.resource("typst://v1/", mime_type="application/json")
async def root_index_resource(ctx: Context) -> str:
    """Root index of all available resource namespaces.

    Provides discovery for all available resources in the Typst MCP server.
    """
    _telemetry["resource_accesses"]["root_index"] += 1
    await ctx.debug("Accessing root resource index")

    return json.dumps({
        "version": "v1",
        "server": "Typst MCP Server",
        "server_version": __version__,
        "namespaces": [
            {
                "name": "docs",
                "uri": "typst://v1/docs/",
                "description": "Typst core documentation chapters"
            },
            {
                "name": "packages",
                "uri": "typst://v1/packages/",
                "description": "Typst Universe package registry and documentation"
            }
        ],
        "note": "Use namespace URIs to discover available resources"
    }, indent=2)


@mcp.resource("typst://v1/docs/", mime_type="application/json")
async def docs_namespace_index(ctx: Context) -> str:
    """Documentation namespace index."""
    _telemetry["resource_accesses"]["docs_namespace"] += 1
    await ctx.debug("Accessing docs namespace index")

    return json.dumps({
        "namespace": "docs",
        "resources": [
            {
                "uri": "typst://v1/docs/chapters",
                "name": "All documentation chapters",
                "description": "Complete list of available chapters with routes"
            },
            {
                "uri": "typst://v1/docs/chapters/{route}",
                "name": "Specific chapter by route",
                "description": "Get chapter content using ____ as path separator"
            }
        ]
    }, indent=2)


@mcp.resource("typst://v1/packages/", mime_type="application/json")
async def packages_namespace_index(ctx: Context) -> str:
    """Packages namespace index."""
    _telemetry["resource_accesses"]["packages_namespace"] += 1
    await ctx.debug("Accessing packages namespace index")

    return json.dumps({
        "namespace": "packages",
        "resources": [
            {
                "uri": "typst://v1/packages/cached",
                "name": "Cached packages",
                "description": "List of all locally cached package docs"
            },
            {
                "uri": "typst://v1/packages/{name}/{version}",
                "name": "Package documentation",
                "description": "Get package docs (auto-fetches if not cached)"
            },
            {
                "uri": "typst://v1/packages/{name}/{version}/readme",
                "name": "Package README",
                "description": "Get full README content"
            },
            {
                "uri": "typst://v1/packages/{name}/{version}/examples",
                "name": "Package examples list",
                "description": "List all example files"
            },
            {
                "uri": "typst://v1/packages/{name}/{version}/examples/{filename}",
                "name": "Package example file",
                "description": "Get specific example file content"
            },
            {
                "uri": "typst://v1/packages/{name}/{version}/docs",
                "name": "Package docs list",
                "description": "List all documentation files"
            },
            {
                "uri": "typst://v1/packages/{name}/{version}/docs/{filename}",
                "name": "Package doc file",
                "description": "Get specific documentation file content"
            }
        ]
    }, indent=2)


@mcp.resource("typst://v1/docs/chapters", mime_type="application/json")
async def list_docs_chapters_resource(ctx: Context) -> str:
    """Lists all chapters in the Typst documentation.

    Returns a JSON array of all available documentation chapters with their routes and content sizes.

    Lazy loading: First access may build docs (~1-2 min first time),
    subsequent accesses are instant (cached).
    """
    _telemetry["resource_accesses"]["docs_chapters"] += 1
    await ctx.debug("Accessing docs chapters resource")

    try:
        typst_docs = await get_docs(wait_seconds=10)
    except ResourceError as e:
        await ctx.error(f"Documentation not available: {e}")
        raise  # Raise instead of returning JSON error

    chapters = []
    for chapter in typst_docs:
        chapters.append(
            {"route": chapter["route"], "content_length": len(json.dumps(chapter))}
        )
        chapters += list_child_routes(chapter)

    await ctx.info(f"Returning {len(chapters)} chapters")
    return json.dumps(chapters, indent=2)


@mcp.resource("typst://v1/docs/chapters/{route}", mime_type="application/json")
async def get_docs_chapter_resource(route: str, ctx: Context) -> str:
    """Gets a specific chapter from the Typst documentation by route.

    The route uses underscores (____) as path separators instead of slashes.

    Lazy loading: First access may build docs (~1-2 min first time),
    subsequent accesses are instant (cached).

    Example URIs:
    - typst://v1/docs/chapters/reference____layout____colbreak
    - typst://v1/docs/chapters/reference____text____text
    """
    _telemetry["resource_accesses"]["docs_chapter"] += 1
    await ctx.debug(f"Accessing docs chapter resource: {route}")

    # Call the async tool implementation
    result = await get_docs_chapter(route, ctx)
    return result


# ============================================================================
# PACKAGE DOCUMENTATION RESOURCES
# ============================================================================


@mcp.resource("typst://v1/packages/cached", mime_type="application/json")
async def list_cached_package_resources(ctx: Context) -> str:
    """List all locally cached package documentation.

    Returns a JSON array of all packages that have been downloaded and cached.
    Each entry includes the package name, version, and URI for accessing the docs.

    This resource updates dynamically as packages are fetched via tools.
    """
    _telemetry["resource_accesses"]["packages_cached"] += 1
    await ctx.debug("Accessing cached packages resource")

    try:
        from .package_docs import list_cached_packages

        # Run in thread pool (may do disk I/O)
        cached = await anyio.to_thread.run_sync(list_cached_packages)

        await ctx.info(f"Returning {len(cached)} cached packages")
        return json.dumps(
            {
                "cached_packages": cached,
                "count": len(cached),
                "note": "These packages are available as resources at typst://v1/packages/{name}/{version}",
            },
            indent=2,
        )
    except Exception as e:
        await ctx.error(f"Failed to list cached packages: {e}")
        raise ResourceError(f"Failed to list cached packages: {e}") from e


@mcp.resource("typst://v1/packages/{package_name}/{version}", mime_type="application/json")
async def get_cached_package_resource(package_name: str, version: str, ctx: Context) -> str:
    """Get package documentation (auto-fetches if not cached).

    Returns lightweight summary including:
    - Metadata and README preview
    - File listings for examples/docs directories
    - Links and import statement

    Lazy loading: First access fetches from GitHub (~3-5s),
    subsequent accesses are instant (cached).

    For full content, use:
    - get_package_docs(package_name, version, summary=False) tool
    - get_package_file(package_name, version, file_path) tool
    """
    _telemetry["resource_accesses"]["package"] += 1
    await ctx.debug(f"Accessing package resource: {package_name}@{version}")

    try:
        from .package_docs import get_cached_package_docs, build_package_docs

        # Try cache first
        docs = get_cached_package_docs(package_name, version)

        # Auto-fetch if not cached (WebDAV-like pattern)
        if docs is None:
            await ctx.info(f"Auto-fetching {package_name}@{version} (not cached)")

            # Run in thread pool (network I/O)
            docs = await anyio.to_thread.run_sync(
                lambda: build_package_docs(package_name, version, timeout=30)
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

        await ctx.info(f"Returning package summary")
        return json.dumps(summary, indent=2)

    except Exception as e:
        await ctx.error(f"Failed to fetch package: {e}")
        raise ResourceError(f"Failed to fetch package '{package_name}@{version}': {e}") from e


@mcp.resource("typst://v1/packages/{package_name}/{version}/readme", mime_type="application/json")
async def get_package_readme_resource(package_name: str, version: str, ctx: Context) -> str:
    """Get full README content (auto-fetches if not cached).

    Lazy loading: First access fetches package data (~3-5s),
    subsequent accesses are instant (cached).
    """
    _telemetry["resource_accesses"]["package_readme"] += 1
    await ctx.debug(f"Accessing README resource: {package_name}@{version}")

    try:
        from .package_docs import get_cached_package_docs, build_package_docs

        # Try cache first
        docs = get_cached_package_docs(package_name, version)

        # Auto-fetch if not cached
        if docs is None:
            await ctx.info(f"Auto-fetching {package_name}@{version} for README")

            # Run in thread pool (network I/O)
            docs = await anyio.to_thread.run_sync(
                lambda: build_package_docs(package_name, version, timeout=30)
            )

        if not docs.get("readme"):
            await ctx.warning(f"README not available for {package_name}@{version}")
            raise ResourceError(f"README not available for {package_name}@{version}")

        await ctx.info(f"Returning README ({len(docs['readme'])} bytes)")
        return json.dumps(
            {
                "package": package_name,
                "version": version,
                "readme": docs["readme"],
                "size": len(docs["readme"]),
            },
            indent=2,
        )

    except ResourceError:
        raise
    except Exception as e:
        await ctx.error(f"Failed to fetch README: {e}")
        raise ResourceError(f"Failed to fetch README for '{package_name}@{version}': {e}") from e


@mcp.resource("typst://v1/packages/{package_name}/{version}/examples", mime_type="application/json")
async def list_package_examples_resource(package_name: str, version: str, ctx: Context) -> str:
    """List all example files (auto-fetches if not cached).

    Returns list of examples with URIs for individual access.

    Lazy loading: First access fetches package data (~3-5s),
    subsequent accesses are instant (cached).
    """
    _telemetry["resource_accesses"]["package_examples"] += 1
    await ctx.debug(f"Accessing examples list resource: {package_name}@{version}")

    try:
        from .package_docs import get_cached_package_docs, build_package_docs

        # Try cache first
        docs = get_cached_package_docs(package_name, version)

        # Auto-fetch if not cached
        if docs is None:
            await ctx.info(f"Auto-fetching {package_name}@{version} for examples")

            # Run in thread pool (network I/O)
            docs = await anyio.to_thread.run_sync(
                lambda: build_package_docs(package_name, version, timeout=30)
            )

        examples = docs.get("examples", [])

        if not examples:
            await ctx.info(f"No examples available for {package_name}@{version}")
            return json.dumps(
                {
                    "package": package_name,
                    "version": version,
                    "examples": [],
                    "note": "This package has no examples directory",
                },
                indent=2,
            )

        await ctx.info(f"Returning {len(examples)} examples")
        return json.dumps(
            {
                "package": package_name,
                "version": version,
                "examples": [
                    {
                        "filename": ex["filename"],
                        "size": ex["size"],
                        "uri": f"typst://v1/packages/{package_name}/{version}/examples/{ex['filename']}",
                    }
                    for ex in examples
                ],
                "count": len(examples),
            },
            indent=2,
        )

    except Exception as e:
        await ctx.error(f"Failed to list examples: {e}")
        raise ResourceError(f"Failed to list examples for '{package_name}@{version}': {e}") from e


@mcp.resource("typst://v1/packages/{package_name}/{version}/examples/{filename}", mime_type="application/json")
async def get_package_example_resource(package_name: str, version: str, filename: str, ctx: Context) -> str:
    """Get specific example file content (auto-fetches if not cached).

    Lazy loading: First access fetches package data (~3-5s),
    subsequent accesses are instant (cached).
    """
    _telemetry["resource_accesses"]["package_example_file"] += 1
    await ctx.debug(f"Accessing example file resource: {package_name}@{version}/{filename}")

    try:
        from .package_docs import get_cached_package_docs, build_package_docs

        # Try cache first
        docs = get_cached_package_docs(package_name, version)

        # Auto-fetch if not cached
        if docs is None:
            await ctx.info(f"Auto-fetching {package_name}@{version} for example file")

            # Run in thread pool (network I/O)
            docs = await anyio.to_thread.run_sync(
                lambda: build_package_docs(package_name, version, timeout=30)
            )

        examples = docs.get("examples", [])

        for ex in examples:
            if ex["filename"] == filename:
                await ctx.info(f"Returning example file ({ex['size']} bytes)")
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

        # File not found
        available = [ex["filename"] for ex in examples]
        await ctx.warning(f"Example '{filename}' not found")
        raise ResourceError(
            f"Example '{filename}' not found in {package_name}@{version}. "
            f"Available examples: {', '.join(available) if available else 'none'}"
        )

    except ResourceError:
        raise
    except Exception as e:
        await ctx.error(f"Failed to fetch example file: {e}")
        raise ResourceError(f"Failed to fetch example file '{filename}' from '{package_name}@{version}': {e}") from e


@mcp.resource("typst://v1/packages/{package_name}/{version}/docs", mime_type="application/json")
async def list_package_docs_resource(package_name: str, version: str, ctx: Context) -> str:
    """List all documentation files (auto-fetches if not cached).

    Returns list of docs with URIs for individual access.

    Lazy loading: First access fetches package data (~3-5s),
    subsequent accesses are instant (cached).
    """
    _telemetry["resource_accesses"]["package_docs_list"] += 1
    await ctx.debug(f"Accessing docs list resource: {package_name}@{version}")

    try:
        from .package_docs import get_cached_package_docs, build_package_docs

        # Try cache first
        docs_data = get_cached_package_docs(package_name, version)

        # Auto-fetch if not cached
        if docs_data is None:
            await ctx.info(f"Auto-fetching {package_name}@{version} for docs")

            # Run in thread pool (network I/O)
            docs_data = await anyio.to_thread.run_sync(
                lambda: build_package_docs(package_name, version, timeout=30)
            )

        docs_files = docs_data.get("docs", {})

        if not docs_files:
            await ctx.info(f"No docs available for {package_name}@{version}")
            return json.dumps(
                {
                    "package": package_name,
                    "version": version,
                    "docs": [],
                    "note": "This package has no docs directory",
                },
                indent=2,
            )

        await ctx.info(f"Returning {len(docs_files)} documentation files")
        return json.dumps(
            {
                "package": package_name,
                "version": version,
                "docs": [
                    {
                        "filename": filename,
                        "size": len(content),
                        "uri": f"typst://v1/packages/{package_name}/{version}/docs/{filename}",
                    }
                    for filename, content in docs_files.items()
                ],
                "count": len(docs_files),
            },
            indent=2,
        )

    except Exception as e:
        await ctx.error(f"Failed to list docs: {e}")
        raise ResourceError(f"Failed to list docs for '{package_name}@{version}': {e}") from e


@mcp.resource("typst://v1/packages/{package_name}/{version}/docs/{filename}", mime_type="application/json")
async def get_package_doc_file_resource(
    package_name: str, version: str, filename: str, ctx: Context
) -> str:
    """Get specific documentation file content (auto-fetches if not cached).

    Lazy loading: First access fetches package data (~3-5s),
    subsequent accesses are instant (cached).
    """
    _telemetry["resource_accesses"]["package_doc_file"] += 1
    await ctx.debug(f"Accessing doc file resource: {package_name}@{version}/{filename}")

    try:
        from .package_docs import get_cached_package_docs, build_package_docs

        # Try cache first
        docs_data = get_cached_package_docs(package_name, version)

        # Auto-fetch if not cached
        if docs_data is None:
            await ctx.info(f"Auto-fetching {package_name}@{version} for doc file")

            # Run in thread pool (network I/O)
            docs_data = await anyio.to_thread.run_sync(
                lambda: build_package_docs(package_name, version, timeout=30)
            )

        docs_files = docs_data.get("docs", {})

        if filename in docs_files:
            await ctx.info(f"Returning doc file ({len(docs_files[filename])} bytes)")
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

        # File not found
        available = list(docs_files.keys())
        await ctx.warning(f"Doc file '{filename}' not found")
        raise ResourceError(
            f"Documentation file '{filename}' not found in {package_name}@{version}. "
            f"Available docs: {', '.join(available) if available else 'none'}"
        )

    except ResourceError:
        raise
    except Exception as e:
        await ctx.error(f"Failed to fetch doc file: {e}")
        raise ResourceError(f"Failed to fetch doc file '{filename}' from '{package_name}@{version}': {e}") from e


# ============================================================================
# PACKAGE DOCUMENTATION TOOLS
# ============================================================================


@mcp.tool()
async def search_packages(query: str, ctx: Context, max_results: int = 20) -> list[dict]:
    """Search for packages in Typst Universe.

    Searches through available Typst packages and returns matching results.
    Useful for discovering packages before fetching their documentation.

    Args:
        query: Search query to match against package names (max 500 chars)
        ctx: MCP context for logging
        max_results: Maximum number of results to return (1-1000, default: 20)

    Returns:
        List of matching packages with names, URLs, and import statements

    Example:
        Input: query="cetz"
        Output: List with package info including import statement
    """
    _telemetry["tool_calls"]["search_packages"] += 1

    # Input validation
    if len(query) > MAX_QUERY_LENGTH:
        await ctx.error(f"Query too long: {len(query)} chars")
        raise ToolError(f"Query too long: {len(query)} chars (max {MAX_QUERY_LENGTH} chars)", code=MCPErrorCodes.TIMEOUT)

    if max_results < 1 or max_results > MAX_RESULTS:
        await ctx.warning(f"max_results out of range, clamping to 1-{MAX_RESULTS}")
        max_results = max(1, min(max_results, MAX_RESULTS))

    await ctx.debug(f"Searching packages: '{query}' (max {max_results} results)")

    try:
        from .package_docs import search_packages as _search_packages

        # Run in thread pool (may do I/O)
        results = await anyio.to_thread.run_sync(lambda: _search_packages(query, max_results))

        await ctx.info(f"Found {len(results)} matching packages")
        return results

    except Exception as e:
        _telemetry["errors"]["search_packages"] += 1
        await ctx.error(f"Package search failed: {e}")
        raise ToolError(f"Failed to search packages: {e}", code=MCPErrorCodes.OPERATION_FAILED) from e


@mcp.tool()
async def list_packages(ctx: Context, offset: int = 0, limit: int = 100) -> dict:
    """List all available packages in Typst Universe with pagination.

    Returns packages available in the Typst Universe with pagination support.
    Use search_packages() for filtered results.

    Args:
        ctx: MCP context for logging
        offset: Number of packages to skip (default: 0)
        limit: Maximum number of packages to return (1-1000, default: 100)

    Returns:
        Dictionary containing:
        - packages: List of package names
        - total: Total number of packages
        - offset: Current offset
        - limit: Current limit
        - has_more: Whether there are more packages

    Example output:
        {
            "packages": ["cetz", "tidy", ...],
            "total": 900,
            "offset": 0,
            "limit": 100,
            "has_more": true
        }
    """
    _telemetry["tool_calls"]["list_packages"] += 1

    # Validate pagination parameters
    if offset < 0:
        offset = 0
    if limit < 1 or limit > MAX_RESULTS:
        await ctx.warning(f"limit out of range, clamping to 1-{MAX_RESULTS}")
        limit = max(1, min(limit, MAX_RESULTS))

    await ctx.debug(f"Listing packages (offset={offset}, limit={limit})")

    try:
        from .package_docs import list_all_packages

        # Run in thread pool
        all_packages = await anyio.to_thread.run_sync(list_all_packages)
        total = len(all_packages)

        # Apply pagination
        end = offset + limit
        page_packages = all_packages[offset:end]
        has_more = end < total

        await ctx.info(f"Returning {len(page_packages)} of {total} packages (offset={offset})")

        return {
            "packages": page_packages,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
        }

    except Exception as e:
        _telemetry["errors"]["list_packages"] += 1
        await ctx.error(f"Failed to list packages: {e}")
        raise ToolError(f"Failed to list packages: {e}", code=MCPErrorCodes.OPERATION_FAILED) from e


@mcp.tool()
async def get_package_versions(package_name: str, ctx: Context) -> list[str]:
    """Get available versions for a Typst package.

    Fetches all published versions of a package from Typst Universe.
    Versions are returned in descending order (latest first).

    Args:
        package_name: Name of the package (e.g., "cetz", "tidy")
        ctx: MCP context for logging

    Returns:
        List of available versions (e.g., ["0.2.2", "0.2.1", "0.2.0", ...])

    Example:
        Input: package_name="cetz"
        Output: ["0.2.2", "0.2.1", "0.2.0", ...]
    """
    _telemetry["tool_calls"]["get_package_versions"] += 1
    await ctx.debug(f"Fetching versions for: {package_name}")

    try:
        from .package_docs import get_package_versions as _get_versions

        # Run in thread pool (network I/O)
        versions = await anyio.to_thread.run_sync(lambda: _get_versions(package_name, timeout=15))

        await ctx.info(f"Found {len(versions)} versions for {package_name}")
        return versions

    except RuntimeError as e:
        _telemetry["errors"]["get_package_versions"] += 1
        await ctx.error(f"Failed to get versions: {e}")
        raise ToolError(f"Failed to get package versions: {e}", code=MCPErrorCodes.OPERATION_FAILED) from e
    except Exception as e:
        _telemetry["errors"]["get_package_versions"] += 1
        await ctx.error(f"Unexpected error: {e}")
        raise ToolError(f"Unexpected error while fetching versions: {e}", code=MCPErrorCodes.OPERATION_FAILED) from e


@mcp.tool()
async def get_package_docs(
    package_name: str, ctx: Context, version: str | None = None, summary: bool = False
) -> dict:
    """Fetch documentation for a Typst Universe package.

    Args:
        package_name: Package name (e.g., "cetz", "tidy", "polylux")
        ctx: MCP context for logging
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
        Dictionary with package documentation or summary

    Raises:
        ToolError: If package not found, network timeout, or invalid version

    Example:
        Input: package_name="cetz", summary=True
        Output: Lightweight summary dict with file listings (~5KB vs 50KB+)
    """
    _telemetry["tool_calls"]["get_package_docs"] += 1
    await ctx.debug(f"Fetching docs: {package_name}@{version}, summary={summary}")

    try:
        from .package_docs import build_package_docs

        # Run in thread pool (network I/O)
        docs = await anyio.to_thread.run_sync(
            lambda: build_package_docs(package_name, version, timeout=30)
        )

        if summary:
            # Return lightweight summary
            await ctx.info(f"Returning summary for {package_name}@{docs['version']}")
            return {
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

        await ctx.info(f"Returning full docs for {package_name}@{docs['version']}")
        return docs

    except RuntimeError as e:
        _telemetry["errors"]["get_package_docs"] += 1
        await ctx.error(f"Failed to fetch docs: {e}")
        raise ToolError(f"Failed to fetch package docs: {e}", code=MCPErrorCodes.OPERATION_FAILED) from e
    except Exception as e:
        _telemetry["errors"]["get_package_docs"] += 1
        await ctx.error(f"Unexpected error: {e}")
        raise ToolError(
            f"Unexpected error while fetching package documentation: {e}",
            code=MCPErrorCodes.OPERATION_FAILED,
        ) from e


@mcp.tool()
async def get_package_file(package_name: str, version: str, file_path: str, ctx: Context) -> dict:
    """Fetch a specific file from a Typst package.

    Enables granular access to individual files within a package without
    fetching all documentation. Use after get_package_docs(summary=True)
    to discover available files.

    Args:
        package_name: Package name (e.g., "cetz")
        version: Package version (e.g., "0.2.2")
        file_path: Path within package (e.g., "examples/basic.typ", "docs/guide.md")
        ctx: MCP context for logging

    Returns:
        Dictionary with file content

    Raises:
        ToolError: If file not found or network error

    Example:
        Input: package_name="cetz", version="0.2.2", file_path="examples/plot.typ"
        Output: {"package": "cetz", "version": "0.2.2", "file_path": "...", "content": "..."}
    """
    _telemetry["tool_calls"]["get_package_file"] += 1
    await ctx.debug(f"Fetching file: {package_name}@{version}/{file_path}")

    try:
        from .package_docs import fetch_file_from_github

        # Run in thread pool (network I/O)
        content = await anyio.to_thread.run_sync(
            lambda: fetch_file_from_github(package_name, version, file_path, timeout=10)
        )

        if content is None:
            raise ToolError(
                f"File '{file_path}' not found in package '{package_name}@{version}'. "
                f"Use get_package_docs(summary=True) to see available files.",
                code=MCPErrorCodes.OPERATION_FAILED,
            )

        await ctx.info(f"Fetched file ({len(content)} bytes)")
        return {
            "package": package_name,
            "version": version,
            "file_path": file_path,
            "content": content,
            "size": len(content),
        }

    except ToolError:
        _telemetry["errors"]["get_package_file"] += 1
        raise
    except Exception as e:
        _telemetry["errors"]["get_package_file"] += 1
        await ctx.error(f"Failed to fetch file: {e}")
        raise ToolError(f"Failed to fetch package file: {e}", code=MCPErrorCodes.OPERATION_FAILED) from e


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


def _get_pdf_tool_description():
    """Generate dynamic tool description with actual sandbox paths."""
    sb = sandbox.get_sandbox()
    allowed_dirs = []

    if sb and sb.sandboxed:
        # Sandbox is enabled - show actual allowed directories
        allowed_dirs = sb.config.allow_write
        paths_list = "\n".join(f"       - {d}" for d in allowed_dirs)

        sandbox_section = f"""
    Sandbox Configuration (ENABLED):
    The following write locations are currently allowed by the OS-level sandbox:
{paths_list}

    All other write attempts will be blocked by the sandbox runtime.
    These paths are enforced at the OS level for maximum security.
    """
    elif sb and sb.disabled:
        # Sandbox explicitly disabled
        sandbox_section = """
    Sandbox Configuration (DISABLED):
    ⚠️  WARNING: Sandbox has been disabled via --disable-sandbox flag.
    Filesystem restrictions are NOT enforced.
    This should only be used for debugging or in trusted environments.
    """
    else:
        # Sandbox initialization failed or not available
        sandbox_section = """
    Sandbox Configuration (NOT AVAILABLE):
    Sandboxing could not be initialized on this platform.
    Basic security measures are in place (timeouts, pandoc --sandbox flag)
    but filesystem restrictions are NOT enforced.
    For production use, please use WSL2, Docker, or a supported platform.
    """

    # Generate the full description by replacing placeholder in docstring
    base_description = typst_snippet_to_pdf.__doc__ or ""
    full_description = base_description.replace(
        "{{SANDBOX_PATHS_PLACEHOLDER}}",
        sandbox_section
    )

    if sb and sb.sandboxed:
        logger.debug(f"✓ Generated PDF tool description with {len(allowed_dirs)} sandbox paths")

    return full_description


async def async_main():
    """Async entry point for the MCP server."""
    import atexit
    import asyncio

    # Check dependencies on startup
    check_dependencies()

    # Initialize sandboxing
    logger.info("Initializing security sandbox...")
    sandbox.initialize_sandbox(temp_dir)

    # Manually register typst_snippet_to_pdf with dynamic description
    pdf_description = _get_pdf_tool_description()
    mcp.tool(typst_snippet_to_pdf, description=pdf_description)

    # SECURITY: Register cleanup handler for temp_dir
    def cleanup_temp_dir():
        """Clean up temporary directory on exit."""
        try:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
                logger.info(f"✓ Cleaned up temporary directory: {temp_dir}")
        except Exception as e:
            logger.warning(f"Failed to clean up temp directory: {e}")

    atexit.register(cleanup_temp_dir)

    # SECURITY: Register cleanup handler for PDF directory on exit
    def cleanup_all_pdfs():
        """Clean up all PDFs on server shutdown."""
        try:
            pdf_dir = Path(tempfile.gettempdir()) / "typst-mcp-pdf"
            cleanup_old_pdfs(pdf_dir, max_age_hours=0)  # Delete all on shutdown
        except Exception as e:
            logger.warning(f"Failed to clean up PDFs on exit: {e}")

    atexit.register(cleanup_all_pdfs)

    # Log startup info
    logger.info("Starting Typst MCP Server...")
    logger.info("")
    logger.info("Tools available immediately:")
    logger.info("  - LaTeX conversion: latex_snippet_to_typst, latex_snippets_to_typst")
    logger.info(
        "  - Syntax validation: check_if_snippet_is_valid_typst_syntax, check_if_snippets_are_valid_typst_syntax"
    )
    logger.info("  - Image rendering: typst_snippet_to_image")
    logger.info("  - PDF generation: typst_snippet_to_pdf")
    logger.info("  - Package search: search_packages, list_packages, get_package_versions")
    logger.info("  - Package docs: get_package_docs (fetches on-demand)")
    logger.info("")
    logger.info("Resources available:")
    logger.info("  - typst://v1/ - Root resource index")
    logger.info("  - typst://v1/packages/cached - List of all cached package docs")
    logger.info("  - typst://v1/packages/{name}/{version} - Cached package documentation")
    logger.info("")
    logger.info("Core documentation tools will be available after docs are loaded/built...")
    logger.info("")

    # Start docs build as background task (proper async, no threading)
    async def build_docs_task():
        """Build docs in background without blocking server startup."""
        try:
            await build_docs_background(None)
        except Exception as e:
            logger.error(f"Background docs build failed: {e}")

    asyncio.create_task(build_docs_task())

    # Run the server asynchronously
    await mcp.run_async()


def main():
    """Entry point for the MCP server."""
    import asyncio
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
