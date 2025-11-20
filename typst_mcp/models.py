"""Pydantic models for Typst MCP Server responses."""

from typing import Any
from pydantic import BaseModel, Field


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
