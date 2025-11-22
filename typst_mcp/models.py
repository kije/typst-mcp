"""Pydantic models for Typst MCP Server responses and tool parameters."""

from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator
import re

# ============================================================================
# MCP ERROR CODES
# ============================================================================


class MCPErrorCodes:
    """Standard MCP/JSON-RPC 2.0 error codes.

    Standard JSON-RPC 2.0 error codes:
    - -32700: Parse error
    - -32600: Invalid Request
    - -32601: Method not found
    - -32602: Invalid params
    - -32603: Internal error

    Application-specific codes (must be >= -32099 or custom range):
    """
    # Standard JSON-RPC 2.0 codes
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    # Application-specific codes (>= -32000)
    INPUT_TOO_LARGE = -32000
    OPERATION_FAILED = -32001
    RESOURCE_NOT_FOUND = -32002
    TIMEOUT = -32003


# ============================================================================
# VALIDATION CONSTANTS
# ============================================================================

# Maximum snippet sizes (to prevent DoS via memory exhaustion)
MAX_SNIPPET_LENGTH = 100_000  # 100KB for code snippets
MAX_LATEX_SNIPPET_LENGTH = 50_000  # 50KB for LaTeX (Pandoc can be slow)
MAX_QUERY_LENGTH = 500  # 500 chars for search queries
MAX_RESULTS = 1000  # Maximum search results

# Timeouts
DEFAULT_VALIDATION_TIMEOUT = 30  # seconds
DEFAULT_CONVERSION_TIMEOUT = 60  # seconds


class ChapterInfo(BaseModel):
    """Information about a documentation chapter."""

    route: str = Field(description="Chapter route path")
    content_length: int = Field(description="Size of chapter content in bytes")


class ChapterResponse(BaseModel):
    """Full chapter content or child routes if chapter is large."""

    route: str = Field(description="Chapter route path")
    title: str | None = Field(default=None, description="Chapter title")
    content_length: int | None = Field(default=None, description="Size of chapter content")
    content: dict[str, Any] | None = Field(
        default=None, description="Full chapter content (if not too large)"
    )
    child_routes: list[ChapterInfo] | None = Field(
        default=None,
        description="Child chapter routes (if chapter is too large to return in full)",
    )
    note: str | None = Field(default=None, description="Informational note")


class PackageMetadata(BaseModel):
    """Package metadata from typst.toml."""

    name: str | None = None
    version: str | None = None
    description: str | None = None
    authors: list[str] = Field(default_factory=list)
    license: str | None = None
    homepage: str | None = None
    repository: str | None = None
    keywords: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    entrypoint: str | None = None


class ExampleFileInfo(BaseModel):
    """Information about an example file."""

    filename: str
    size: int
    path: str


class DocsFileInfo(BaseModel):
    """Information about a documentation file."""

    filename: str
    size: int
    path: str


class PackageDocsSummary(BaseModel):
    """Lightweight package documentation summary."""

    package: str
    version: str
    metadata: PackageMetadata
    readme_preview: str | None = Field(
        default=None, description="First 500 characters of README"
    )
    readme_full_size: int = Field(default=0, description="Full README size in bytes")
    license_type: str | None = Field(default=None, description="First 100 chars of license")
    has_changelog: bool = Field(default=False, description="Whether package has a CHANGELOG")
    examples_list: list[ExampleFileInfo] = Field(
        default_factory=list, description="List of example files"
    )
    docs_list: list[DocsFileInfo] = Field(
        default_factory=list, description="List of documentation files"
    )
    import_statement: str = Field(description="How to import this package in Typst")
    universe_url: str = Field(description="Typst Universe URL")
    github_url: str | None = Field(default=None, description="GitHub repository URL")
    homepage_url: str | None = Field(default=None, description="Package homepage URL")
    repository_url: str | None = Field(default=None, description="Repository URL")
    note: str | None = Field(
        default="Use summary=false for full content, or get_package_file() for specific files"
    )


class PackageSearchResult(BaseModel):
    """Search result for a package."""

    name: str
    version: str | None = None
    description: str | None = None
    universe_url: str
    import_statement: str


class ValidationResult(BaseModel):
    """Result of Typst syntax validation."""

    valid: bool
    error_message: str | None = None


class ConversionResult(BaseModel):
    """Result of LaTeX to Typst conversion."""

    success: bool
    typst_code: str | None = None
    error_message: str | None = None


# ============================================================================
# TOOL PARAMETER MODELS
# ============================================================================


class LaTeXSnippetParams(BaseModel):
    """Parameters for latex_snippet_to_typst tool."""

    latex_snippet: str = Field(
        ...,
        description="LaTeX code to convert to Typst",
        min_length=1,
        max_length=MAX_LATEX_SNIPPET_LENGTH,
    )

    @field_validator("latex_snippet")
    @classmethod
    def validate_length(cls, v: str) -> str:
        """Validate snippet length to prevent DoS."""
        if len(v) > MAX_LATEX_SNIPPET_LENGTH:
            raise ValueError(
                f"LaTeX snippet too large: {len(v)} bytes (max {MAX_LATEX_SNIPPET_LENGTH} bytes)"
            )
        return v


class TypstSnippetParams(BaseModel):
    """Parameters for Typst snippet validation/rendering tools."""

    typst_snippet: str = Field(
        ...,
        description="Typst code to validate or render",
        min_length=1,
        max_length=MAX_SNIPPET_LENGTH,
    )

    @field_validator("typst_snippet")
    @classmethod
    def validate_length(cls, v: str) -> str:
        """Validate snippet length to prevent DoS."""
        if len(v) > MAX_SNIPPET_LENGTH:
            raise ValueError(
                f"Typst snippet too large: {len(v)} bytes (max {MAX_SNIPPET_LENGTH} bytes)"
            )
        return v


class TypstPDFParams(BaseModel):
    """Parameters for typst_snippet_to_pdf tool."""

    typst_snippet: str = Field(
        ...,
        description="Typst code to compile to PDF",
        min_length=1,
        max_length=MAX_SNIPPET_LENGTH,
    )
    output_mode: Literal["embedded", "path"] = Field(
        default="embedded",
        description='Output mode: "embedded" (return PDF data) or "path" (save to file)',
    )
    output_path: str | None = Field(
        default=None,
        description="Custom output path (only for path mode, must be in allowed directories)",
    )

    @field_validator("typst_snippet")
    @classmethod
    def validate_snippet_length(cls, v: str) -> str:
        """Validate snippet length."""
        if len(v) > MAX_SNIPPET_LENGTH:
            raise ValueError(
                f"Typst snippet too large: {len(v)} bytes (max {MAX_SNIPPET_LENGTH} bytes)"
            )
        return v


class PackageDocsParams(BaseModel):
    """Parameters for get_package_docs tool."""

    package_name: str = Field(
        ...,
        description="Package name (e.g., 'cetz', 'tidy')",
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    version: str | None = Field(
        None,
        description="Package version (semver, e.g., '0.2.2')",
        pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(-[a-z0-9.]+)?$",
    )
    summary: bool = Field(
        default=False, description="Return lightweight summary instead of full docs"
    )


class PackageFileParams(BaseModel):
    """Parameters for get_package_file tool."""

    package_name: str = Field(
        ...,
        description="Package name",
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    version: str = Field(
        ...,
        description="Package version (semver)",
        pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(-[a-z0-9.]+)?$",
    )
    file_path: str = Field(
        ..., description="File path within package (e.g., 'examples/basic.typ')"
    )

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, v: str) -> str:
        """Validate file path to prevent path traversal."""
        if ".." in v or v.startswith("/"):
            raise ValueError("Invalid file path: path traversal detected")
        return v


class SearchPackagesParams(BaseModel):
    """Parameters for search_packages tool."""

    query: str = Field(
        ...,
        description="Search query to match against package names",
        min_length=1,
        max_length=MAX_QUERY_LENGTH,
    )
    max_results: int = Field(
        default=20,
        description="Maximum number of results to return",
        ge=1,
        le=MAX_RESULTS,
    )
