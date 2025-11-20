"""Settings for Typst MCP Server."""

import os
import tempfile
from pathlib import Path
from typing import Annotated

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TypstSettings(BaseSettings):
    """Configuration settings for Typst MCP Server.

    Settings can be configured via:
    - Environment variables (TYPST_MCP_*)
    - .env file
    - Direct instantiation

    Examples:
        export TYPST_MCP_DOCS_BUILD_TIMEOUT=180
        export TYPST_MCP_MAX_PDF_SIZE_MB=100
    """

    model_config = SettingsConfigDict(
        env_prefix="TYPST_MCP_",
        env_file=".env",
        extra="ignore",
        env_nested_delimiter="__",
    )

    # Directory settings
    temp_dir: Annotated[
        Path,
        Field(
            description="Temporary directory for Typst compilation artifacts",
        ),
    ] = Path(tempfile.mkdtemp())

    cache_dir: Annotated[
        Path | None,
        Field(
            description="Cache directory for documentation and packages (uses platform default if not set)",
        ),
    ] = None

    # Documentation build settings
    docs_build_timeout: Annotated[
        int,
        Field(
            description="Timeout in seconds for documentation build process",
            ge=30,
            le=600,
        ),
    ] = 180

    enable_background_docs_build: Annotated[
        bool,
        Field(
            description="Enable background documentation building on server startup",
        ),
    ] = True

    docs_wait_timeout: Annotated[
        int,
        Field(
            description="Max seconds to wait for docs to build when accessed",
            ge=5,
            le=60,
        ),
    ] = 10

    # Package fetching settings
    package_fetch_timeout: Annotated[
        int,
        Field(
            description="Timeout in seconds for package documentation fetching",
            ge=5,
            le=120,
        ),
    ] = 30

    package_search_max_results: Annotated[
        int,
        Field(
            description="Maximum number of search results to return",
            ge=1,
            le=100,
        ),
    ] = 20

    # PDF generation settings
    max_pdf_size_mb: Annotated[
        int,
        Field(
            description="Maximum PDF file size in megabytes",
            ge=1,
            le=500,
        ),
    ] = 50

    pdf_cleanup_age_hours: Annotated[
        int,
        Field(
            description="Age in hours after which PDFs are cleaned up",
            ge=0,
            le=168,  # 1 week max
        ),
    ] = 24

    # Compilation timeouts (security)
    pandoc_timeout: Annotated[
        int,
        Field(
            description="Timeout in seconds for Pandoc LaTeX conversion",
            ge=5,
            le=120,
        ),
    ] = 30

    typst_compile_timeout: Annotated[
        int,
        Field(
            description="Timeout in seconds for Typst compilation",
            ge=5,
            le=120,
        ),
    ] = 60

    # Server behavior
    strict_validation: Annotated[
        bool,
        Field(
            description="Enable strict input validation for all tools",
        ),
    ] = True

    enable_progress_reporting: Annotated[
        bool,
        Field(
            description="Enable progress reporting for long-running operations",
        ),
    ] = True

    # Logging
    verbose_logging: Annotated[
        bool,
        Field(
            description="Enable verbose logging to client",
        ),
    ] = False

    def get_cache_dir(self) -> Path:
        """Get the cache directory, using platform default if not configured."""
        if self.cache_dir:
            cache_dir = self.cache_dir.expanduser()
            cache_dir.mkdir(parents=True, exist_ok=True)
            return cache_dir

        # Use platform-appropriate cache directory
        import sys

        if sys.platform == "darwin":
            cache_base = Path.home() / "Library" / "Caches"
        elif sys.platform == "win32":
            cache_base = Path(
                os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
            )
        else:  # Linux and others
            cache_base = Path.home() / ".cache"

        cache_dir = cache_base / "typst-mcp"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir


# Global settings instance
typst_settings = TypstSettings()
