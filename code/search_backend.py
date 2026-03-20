"""
Swappable search backend interface.
Implement SearchBackend class for your provider (Azure, Elasticsearch, etc.)
"""
import os
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
import asyncio

# Configuration from environment variables
SEARCH_CONFIG = {
    "provider": os.getenv("SEARCH_PROVIDER", "azure"),  # azure, elasticsearch, qdrant
    "endpoint": os.getenv("SEARCH_ENDPOINT"),  # Must be set via environment variable
    "api_key": os.getenv("SEARCH_API_KEY"),  # Must be set via environment variable
    "index": os.getenv("SEARCH_INDEX", "augments-collection"),  # Default to augment-level collection
}


class SearchBackend(ABC):
    """Abstract base for search backends"""

    @abstractmethod
    async def initialize(self):
        """Initialize connection pools"""
        pass

    @abstractmethod
    async def search(self, query: str, vector: List[float], top_k: int = 30, strategy: str = "agent") -> List[Dict[str, Any]]:
        """
        Search for augments.
        Args:
            query: Search query text
            vector: Query embedding vector
            top_k: Number of results to return
            strategy: "agent" for augment-level or "query" for query-level retrieval
        Returns: List of {"url": str, "json_ld": str, "name": str, "augment": str}
        """
        pass

    @abstractmethod
    async def close(self):
        """Cleanup connections"""
        pass


class AzureSearchBackend(SearchBackend):
    """Azure AI Search implementation"""

    def __init__(self):
        self.session = None
        self.endpoint = None
        self.api_key = None
        self.clients = {}  # Cache clients for different indices

    async def initialize(self):
        """Initialize Azure Search client with connection pooling"""
        import aiohttp
        from azure.core.credentials import AzureKeyCredential

        # Validate required configuration
        if not SEARCH_CONFIG["endpoint"]:
            raise ValueError(
                "SEARCH_ENDPOINT environment variable is required. "
                "Please set it to your Azure Search endpoint URL (e.g., https://your-search.search.windows.net)"
            )

        if not SEARCH_CONFIG["api_key"]:
            raise ValueError(
                "SEARCH_API_KEY environment variable is required. "
                "Please set it to your Azure Search API key"
            )

        # Create session with connection pooling
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(
                limit=50,
                limit_per_host=50,
                force_close=False,
                enable_cleanup_closed=True
            ),
            timeout=aiohttp.ClientTimeout(total=10)
        )

        # Store credentials for creating clients on demand
        self.endpoint = SEARCH_CONFIG["endpoint"]
        self.api_key = SEARCH_CONFIG["api_key"]


    async def search(self, query: str, vector: List[float], top_k: int = 30, strategy: str = "agent") -> List[Dict[str, Any]]:
        """Search Azure AI Search with vector search"""
        from azure.search.documents.aio import SearchClient
        from azure.core.credentials import AzureKeyCredential

        results = []

        try:
            # Determine index based on strategy
            if strategy == "query":
                index_name = "queries-index"
            else:
                index_name = "agents-index"

            # Get or create client for this index
            if index_name not in self.clients:
                self.clients[index_name] = SearchClient(
                    endpoint=self.endpoint,
                    index_name=index_name,
                    credential=AzureKeyCredential(self.api_key),
                    session=self.session
                )

            client = self.clients[index_name]

            # Configure search request for vector search
            # Select fields based on strategy
            if strategy == "query":
                select_fields = ["url", "name", "agent_id", "agent_name", "agent_url", "query", "query_detail", "agent_json_ld", "description"]
            else:
                select_fields = ["url", "json_ld", "name", "description"]

            search_kwargs = {
                "search_text": None,  # Use None for vector search
                "select": select_fields,
                "top": top_k,
            }

            # Add vector search if vector is provided
            if vector:
                search_kwargs["vector_queries"] = [{
                    "kind": "vector",
                    "vector": vector,
                    "fields": "embedding",
                    "k": top_k  # Use "k" instead of "k_nearest_neighbors"
                }]

            # Print search configuration for debugging

            # Execute search
            response = await client.search(**search_kwargs)

            # Collect results
            async for item in response:
                if strategy == "query":
                    # Query document fields - map agent_* fields from index to augment_* for internal use
                    results.append({
                        "url": item.get("url", ""),
                        "name": item.get("name", "Unknown"),
                        "augment_id": item.get("agent_id", ""),
                        "augment_name": item.get("agent_name", ""),
                        "augment_url": item.get("agent_url", ""),
                        "query": item.get("query", ""),
                        "query_detail": item.get("query_detail", ""),
                        "augment_json_ld": item.get("agent_json_ld", "{}"),
                        "description": item.get("description", "")
                    })
                else:
                    # Agent document fields
                    results.append({
                        "url": item.get("url", ""),
                        "json_ld": item.get("json_ld", "{}"),
                        "name": item.get("name", "Unknown"),
                        "description": item.get("description", "")
                    })

        except Exception as e:
            # Return empty results on error rather than crashing
            return []

        return results

    async def close(self):
        """Cleanup Azure Search connections"""
        if self.session:
            await self.session.close()


class ElasticsearchBackend(SearchBackend):
    """Elasticsearch implementation (placeholder for future implementation)"""

    def __init__(self):
        self.client = None

    async def initialize(self):
        """Initialize Elasticsearch client"""
        # Example implementation
        # from elasticsearch import AsyncElasticsearch
        # self.client = AsyncElasticsearch(
        #     hosts=[SEARCH_CONFIG["endpoint"]],
        #     api_key=SEARCH_CONFIG["api_key"]
        # )
        raise NotImplementedError("Elasticsearch backend not yet implemented")

    async def search(self, query: str, vector: List[float], top_k: int = 30, strategy: str = "agent") -> List[Dict[str, Any]]:
        """Search Elasticsearch"""
        raise NotImplementedError("Elasticsearch backend not yet implemented")

    async def close(self):
        """Cleanup Elasticsearch connections"""
        if self.client:
            await self.client.close()


class QdrantBackend(SearchBackend):
    """Qdrant implementation"""

    def __init__(self):
        self.client = None
        self.collection_name = None

    async def initialize(self):
        """Initialize Qdrant client"""
        from qdrant_client import QdrantClient
        from pathlib import Path

        # Use local Qdrant storage
        qdrant_path = SEARCH_CONFIG.get("endpoint") or str(Path.home() / ".qdrant" / "agentfinder")

        # Determine collection based on index config
        index_name = SEARCH_CONFIG.get("index", "augments-collection")
        if "query" in index_name.lower() or index_name == "queries-index":
            self.collection_name = "queries-collection"
        else:
            self.collection_name = "augments-collection"

        self.client = QdrantClient(path=qdrant_path)


    async def search(self, query: str, vector: List[float], top_k: int = 30, strategy: str = "agent") -> List[Dict[str, Any]]:
        """Search Qdrant"""
        results = []

        try:
            # Determine collection based on strategy
            collection_name = "queries-collection" if strategy == "query" else "augments-collection"

            # Perform vector search
            search_result = self.client.search(
                collection_name=collection_name,
                query_vector=vector,
                limit=top_k
            )

            # Convert to expected format
            for hit in search_result:
                payload = hit.payload

                # Map Qdrant payload to expected format
                result = {
                    "url": payload.get("url", ""),
                    "json_ld": payload.get("json_ld") or payload.get("augment_json_ld", "{}"),
                    "name": payload.get("name", "Unknown"),
                    "augment": payload.get("augment", "m365")
                }

                # For query-level strategy, include agent metadata
                if collection_name == "queries-collection":
                    result.update({
                        "augment_id": payload.get("augment_id", ""),
                        "augment_name": payload.get("augment_name", ""),
                        "augment_url": payload.get("augment_url", ""),
                        "augment_json_ld": payload.get("augment_json_ld", "{}"),
                        "query": payload.get("query", ""),
                        "query_detail": payload.get("query_detail", ""),
                        "description": payload.get("description", ""),
                        "@search.score": hit.score  # Include search score
                    })

                results.append(result)

        except Exception as e:
            import traceback
            traceback.print_exc()
            return []

        return results

    async def close(self):
        """Cleanup Qdrant connections"""
        # Qdrant client doesn't require explicit closing for local storage
        pass


# Factory function
def get_search_backend() -> SearchBackend:
    """Get the configured search backend"""
    provider = SEARCH_CONFIG["provider"].lower()

    if provider == "azure":
        return AzureSearchBackend()
    elif provider == "elasticsearch":
        return ElasticsearchBackend()
    elif provider == "qdrant":
        return QdrantBackend()
    else:
        raise ValueError(f"Unknown search provider: {SEARCH_CONFIG['provider']}")