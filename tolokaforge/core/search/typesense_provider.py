"""
TypeSense provider that bridges TolokaForge with mcp_core TypeSense implementation.

This module provides a standalone TypeSense feature that can be used by any adapter.
It integrates the existing mcp_core TypeSense infrastructure with TolokaForge adapters.

Thread-safe: Uses DomainStateManager to coordinate concurrent domain initializations.
Multiple callers for the same domain will properly coordinate - only one performs
initialization while others wait.
"""

from pathlib import Path
from typing import Any

from tolokaforge.core.logging import get_logger

from .domain_state import DomainState, DomainStateManager, DomainStatus
from .typesense import SearchResponse, SearchResult, create_typesense_client

logger = get_logger(__name__)

# Try to import mcp_core components
try:
    from mcp_core.search.typesense_client import TypesenseIndex
    from mcp_core.search.typesense_registry import (
        clear_registry,
        get_typesense_for_domain,
        initialize_typesense_for_domain,
    )

    MCP_CORE_AVAILABLE = True
    logger.debug("mcp_core TypeSense components available")
except ImportError as e:
    MCP_CORE_AVAILABLE = False
    logger.debug(f"mcp_core TypeSense components not available: {e}")
    initialize_typesense_for_domain = None
    get_typesense_for_domain = None
    clear_registry = None
    TypesenseIndex = None


class TypeSenseProviderConfig:
    """Configuration for TypeSense provider."""

    def __init__(
        self,
        enabled: bool = True,
        host: str = "127.0.0.1",
        port: int = 8108,
        api_key: str | None = None,
        timeout: float = 30.0,
        use_stub: bool = False,
    ):
        """
        Initialize TypeSense provider configuration.

        Args:
            enabled: Whether TypeSense is enabled
            host: TypeSense server host
            port: TypeSense server port
            api_key: TypeSense API key (if None, uses TYPESENSE_API_KEY env var)
            timeout: Connection timeout in seconds
            use_stub: Force use of stub implementation even if TypeSense is available
        """
        self.enabled = enabled
        self.host = host
        self.port = port
        if api_key is None:
            from tolokaforge.secrets import get_default

            api_key = get_default().get_secret("TYPESENSE_API_KEY")
        self.api_key = api_key
        self.timeout = timeout
        self.use_stub = use_stub


class TypeSenseProvider:
    """
    TypeSense provider that bridges TolokaForge with mcp_core TypeSense.

    This class provides a unified interface for TypeSense functionality that can
    work with or without mcp_core, and with or without an actual TypeSense server.

    Thread-safe: Uses DomainStateManager to coordinate concurrent domain initializations.
    Call ensure_domain_initialized() instead of initialize_for_domain() for proper
    coordination when multiple threads may initialize the same domain.
    """

    def __init__(self, config: TypeSenseProviderConfig):
        """
        Initialize TypeSense provider.

        Args:
            config: TypeSense provider configuration
        """
        self.config = config
        self._client_cache: dict[str, Any] = {}

        # Domain state management for coordinated initialization
        self._state_manager = DomainStateManager(
            max_concurrent_inits=2,
            default_timeout=config.timeout,
        )

        if not config.enabled:
            logger.info("TypeSense provider disabled by configuration")
        elif config.use_stub:
            logger.info("TypeSense provider configured to use stub implementation")
        elif not MCP_CORE_AVAILABLE:
            logger.warning(
                "mcp_core not available - TypeSense will use stub implementation. "
                "Install mcp_core for full TypeSense functionality."
            )
        else:
            logger.info(
                f"TypeSense provider initialized: host={config.host}, port={config.port}, timeout={config.timeout}"
            )

    def is_available(self) -> bool:
        """Check if TypeSense is available and enabled."""
        return self.config.enabled and MCP_CORE_AVAILABLE and not self.config.use_stub

    def ensure_domain_initialized(
        self,
        domain: str,
        docindex_path: Path,
        timeout: float | None = None,
    ) -> bool:
        """
        Ensure domain is initialized, waiting if initialization is in progress.

        Thread-safe: Multiple callers for same domain will coordinate properly.
        Uses atomic claim_initialization() to ensure exactly one thread initializes.

        Args:
            domain: Domain identifier
            docindex_path: Path to docindex directory
            timeout: Max time to wait (uses config default if None)

        Returns:
            True if domain is ready

        Raises:
            TimeoutError: If initialization times out
            RuntimeError: If initialization fails
        """
        if not self.config.enabled:
            logger.debug(f"TypeSense disabled, skipping initialization for domain '{domain}'")
            return False

        timeout = timeout or self._state_manager.default_timeout

        # Get or create domain state (atomic)
        state, _ = self._state_manager.get_or_create(domain)

        # Atomically claim initialization rights (only one thread succeeds)
        if state.claim_initialization():
            # We claimed initialization - perform it
            self._do_initialize(domain, docindex_path, state)

        # Wait for initialization to complete (or verify it's already done)
        # Note: wait_ready() checks predicate first, returns immediately if READY
        state.wait_ready(timeout=timeout)

        return state.status == DomainStatus.READY

    def _do_initialize(
        self,
        domain: str,
        docindex_path: Path,
        state: DomainState,
    ) -> None:
        """
        Perform actual initialization.

        Caller must have successfully called state.claim_initialization() first.
        State is already INITIALIZING when this is called.
        """
        try:
            documents = self.load_documents_from_directory(docindex_path)

            if not documents:
                state.set_ready(document_count=0)
                logger.info(f"TypeSense domain '{domain}' has no documents")
                return

            success = self.initialize_for_domain(domain, documents)

            if success:
                state.set_ready(document_count=len(documents))
                logger.info(
                    f"TypeSense initialized for domain '{domain}' with {len(documents)} documents",
                    host=self.config.host,
                    port=self.config.port,
                )
            else:
                error_msg = "initialize_for_domain returned False"
                state.set_failed(error_msg)
                logger.error(f"TypeSense initialization failed for domain '{domain}': {error_msg}")

        except Exception as e:
            state.set_failed(str(e))
            logger.error(f"TypeSense initialization error for domain '{domain}': {e}")
            raise

    def initialize_for_domain(
        self,
        domain: str,
        documents: list[str],
    ) -> bool:
        """
        Initialize TypeSense for a domain with documents.

        Args:
            domain: Domain identifier
            documents: List of document content strings to index

        Returns:
            True if initialization successful, False otherwise
        """
        if not self.config.enabled:
            logger.debug(f"TypeSense disabled, skipping initialization for domain '{domain}'")
            return False

        if self.config.use_stub:
            logger.info(
                f"Using stub TypeSense for domain '{domain}' - no actual indexing performed"
            )
            return True

        if not MCP_CORE_AVAILABLE:
            logger.info(f"Using stub TypeSense for domain '{domain}' - mcp_core not available")
            return True

        try:
            logger.info(
                f"Initializing TypeSense for domain '{domain}' with {len(documents)} documents"
            )

            client = initialize_typesense_for_domain(
                domain=domain,
                snippets=documents,
                host=self.config.host,
                port=self.config.port,
                timeout=self.config.timeout,
                api_key=self.config.api_key,
            )

            if client and client.is_available:
                logger.info(f"TypeSense successfully initialized for domain '{domain}'")
                return True
            else:
                logger.warning(
                    f"TypeSense initialization failed for domain '{domain}' - client not available"
                )
                return False

        except Exception as e:
            logger.warning(f"Failed to initialize TypeSense for domain '{domain}': {e}")
            return False

    def get_client_for_domain(self, domain: str) -> Any | None:
        """
        Get TypeSense client for a domain.

        Args:
            domain: Domain identifier

        Returns:
            TypeSense client instance or None if not available
        """
        if not self.config.enabled:
            return None

        if self.config.use_stub or not MCP_CORE_AVAILABLE:
            # Return stub client
            return create_typesense_client(use_stub=True)

        try:
            if get_typesense_for_domain is None:
                logger.warning("get_typesense_for_domain is not available")
                return None
            return get_typesense_for_domain(domain)
        except Exception as e:
            logger.warning(f"Failed to get TypeSense client for domain '{domain}': {e}")
            return None

    def search(
        self,
        domain: str,
        query: str,
        max_results: int = 10,
    ) -> SearchResponse:
        """
        Search documents in a domain.

        Args:
            domain: Domain to search in
            query: Search query
            max_results: Maximum number of results to return

        Returns:
            SearchResponse with results
        """
        if not self.config.enabled:
            logger.debug(f"TypeSense disabled, returning empty results for query '{query}'")
            return SearchResponse(
                hits=[],
                total_hits=0,
                query=query,
                search_time_ms=0.0,
            )

        client = self.get_client_for_domain(domain)
        if not client:
            logger.warning(f"No TypeSense client for domain '{domain}', returning empty results")
            return SearchResponse(
                hits=[],
                total_hits=0,
                query=query,
                search_time_ms=0.0,
            )

        try:
            # Handle different client types
            if hasattr(client, "universal_search_with_full_text"):
                # Real TypeSense client (mcp_core)
                results = client.universal_search_with_full_text(query, keywords=[])
                results = results[:max_results]

                # Convert to SearchResponse format
                hits = []
                for result in results:
                    hit = SearchResult(
                        document_id=result.get("source", ""),
                        score=float(result.get("score", 0.0)),
                        content={
                            "source": result.get("source", ""),
                            "text": result.get("text", ""),
                            "vector_distance": result.get("vector_distance", 2.0),
                        },
                        highlights={},
                    )
                    hits.append(hit)

                return SearchResponse(
                    hits=hits,
                    total_hits=len(hits),
                    query=query,
                    search_time_ms=1.0,  # Placeholder
                )
            else:
                # Stub client
                return client.search(
                    collection_name=domain,
                    query=query,
                    query_by=["text"],
                    limit=max_results,
                )

        except Exception as e:
            logger.error(f"TypeSense search failed for domain '{domain}', query '{query}': {e}")
            return SearchResponse(
                hits=[],
                total_hits=0,
                query=query,
                search_time_ms=0.0,
            )

    def load_documents_from_directory(self, docs_dir: Path) -> list[str]:
        """
        Load documents from a directory.

        Args:
            docs_dir: Directory containing .md files

        Returns:
            List of document contents
        """
        documents = []

        if not docs_dir.exists() or not docs_dir.is_dir():
            logger.warning(f"Documents directory not found: {docs_dir}")
            return documents

        # Find all .md files
        md_files = list(docs_dir.glob("*.md"))
        logger.info(f"Found {len(md_files)} markdown files in {docs_dir}")

        for md_file in md_files:
            try:
                content = md_file.read_text(encoding="utf-8")
                if content.strip():  # Only add non-empty documents
                    documents.append(content)
                    logger.debug(f"Loaded document: {md_file.name} ({len(content)} chars)")
            except Exception as e:
                logger.warning(f"Failed to load document {md_file}: {e}")

        logger.info(f"Loaded {len(documents)} documents from {docs_dir}")
        return documents

    def clear_domain_registry(self) -> None:
        """Clear the TypeSense domain registry (mainly for testing)."""
        if MCP_CORE_AVAILABLE and clear_registry:
            clear_registry()
            logger.info("TypeSense domain registry cleared")
        # Also clear our internal state manager
        self._state_manager.clear()

    def shutdown(self) -> None:
        """Shutdown the provider and release resources."""
        self._state_manager.shutdown(wait=True)
        logger.debug("TypeSense provider shut down")

    def get_domain_state(self, domain: str) -> DomainState | None:
        """Get domain state for inspection (mainly for testing)."""
        return self._state_manager.get(domain)


def create_typesense_provider(
    enabled: bool = True,
    host: str = "127.0.0.1",
    port: int = 8108,
    api_key: str | None = None,
    timeout: float = 30.0,
    use_stub: bool = False,
) -> TypeSenseProvider:
    """
    Factory function to create a TypeSense provider.

    Args:
        enabled: Whether TypeSense is enabled
        host: TypeSense server host
        port: TypeSense server port
        api_key: TypeSense API key (if None, uses TYPESENSE_API_KEY env var)
        timeout: Connection timeout in seconds
        use_stub: Force use of stub implementation

    Returns:
        TypeSenseProvider instance
    """
    config = TypeSenseProviderConfig(
        enabled=enabled,
        host=host,
        port=port,
        api_key=api_key,
        timeout=timeout,
        use_stub=use_stub,
    )

    return TypeSenseProvider(config)
