"""
Swappable search backend interface.
Implement SearchBackend class for your provider (Azure, Elasticsearch, etc.)
"""
import os
import json
import math
import pickle
import hashlib
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Tuple
import asyncio

# Configuration from environment variables
SEARCH_CONFIG = {
    "provider": os.getenv("SEARCH_PROVIDER", "memory"),  # memory (default), azure, elasticsearch, qdrant, github
    "endpoint": os.getenv("SEARCH_ENDPOINT"),  # Must be set via environment variable
    "api_key": os.getenv("SEARCH_API_KEY"),  # Must be set via environment variable
    "index": os.getenv("SEARCH_INDEX", "augments-collection"),  # Default to augment-level collection
}

# In-memory / GitHub static-discovery ingestion config (§6).
# Defaults point at the catalog committed in this repo, so a fresh clone (or a
# fork) running `SEARCH_PROVIDER=memory` with no local dir auto-loads it from
# GitHub at startup. A forker overrides AGENT_FINDER_GITHUB_REPO with their fork.
GITHUB_CONFIG = {
    "repo": os.getenv("AGENT_FINDER_GITHUB_REPO", "nlweb-ai/AgentFinder"),  # "owner/repo"
    "path": os.getenv("AGENT_FINDER_GITHUB_PATH", "catalog"),  # directory within the repo
    "ref": os.getenv("AGENT_FINDER_GITHUB_REF", "main"),
    "token": os.getenv("GITHUB_TOKEN"),
    "local_dir": os.getenv("AGENT_FINDER_CATALOG_DIR"),  # read local files instead of GitHub
    "max_collection_depth": int(os.getenv("AGENT_FINDER_MAX_COLLECTION_DEPTH", "3")),
}


class SearchBackend(ABC):
    """Abstract base for search backends"""

    @abstractmethod
    async def initialize(self, embedder: Optional[Any] = None):
        """Initialize connection pools.

        `embedder` (optional) exposes `get_embedding(text) -> List[float]` and is
        used by backends that build their own vectors at load time (in-memory store).
        """
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
    async def list_all(self, top_k: int = 200) -> List[Dict[str, Any]]:
        """
        Scan the augment index without relevance ranking (for deterministic browsing).
        Returns: List of {"url": str, "json_ld": str, "name": str, "description": str}
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

    async def initialize(self, embedder: Optional[Any] = None):
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

    async def list_all(self, top_k: int = 200) -> List[Dict[str, Any]]:
        """Scan the agents index using a wildcard text search (no vector ranking)."""
        from azure.search.documents.aio import SearchClient
        from azure.core.credentials import AzureKeyCredential

        index_name = "agents-index"
        if index_name not in self.clients:
            self.clients[index_name] = SearchClient(
                endpoint=self.endpoint,
                index_name=index_name,
                credential=AzureKeyCredential(self.api_key),
                session=self.session,
            )
        client = self.clients[index_name]

        results = []
        try:
            response = await client.search(
                search_text="*",
                select=["url", "json_ld", "name", "description"],
                top=top_k,
            )
            async for item in response:
                results.append({
                    "url": item.get("url", ""),
                    "json_ld": item.get("json_ld", "{}"),
                    "name": item.get("name", "Unknown"),
                    "description": item.get("description", ""),
                })
        except Exception:
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

    async def initialize(self, embedder: Optional[Any] = None):
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

    async def list_all(self, top_k: int = 200) -> List[Dict[str, Any]]:
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

    async def initialize(self, embedder: Optional[Any] = None):
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

    async def list_all(self, top_k: int = 200) -> List[Dict[str, Any]]:
        """Scroll the augments collection (no vector ranking)."""
        results = []
        try:
            points, _ = self.client.scroll(
                collection_name="augments-collection",
                limit=top_k,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                payload = point.payload or {}
                results.append({
                    "url": payload.get("url", ""),
                    "json_ld": payload.get("json_ld") or payload.get("augment_json_ld", "{}"),
                    "name": payload.get("name", "Unknown"),
                    "description": payload.get("description", ""),
                })
        except Exception:
            import traceback
            traceback.print_exc()
            return []
        return results

    async def close(self):
        """Cleanup Qdrant connections"""
        # Qdrant client doesn't require explicit closing for local storage
        pass


def _l2_normalize(vec: List[float]) -> List[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def _dot(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class InMemorySearchBackend(SearchBackend):
    """In-memory store populated from ai-catalog.json manifests (§6.2 web ingestion).

    Reads manifest files from a GitHub directory (contents API) or a local directory,
    parses entries, follows `collections` links and inline catalog bundles, and holds
    the resulting catalog entries in memory. At load time each entry's text is embedded
    and L2-normalized; candidate retrieval is a brute-force cosine-similarity scan over
    the in-memory vectors (mirrors the Go reference). The handler's LLM stage performs
    the final relevance ranking.
    """

    def __init__(self):
        self.session = None
        self.embedder = None
        self.docs: List[Dict[str, Any]] = []     # internal docs (url/name/json_ld/description)
        self.texts: List[str] = []               # per-doc embedding text (displayName\ndescription)
        self.vectors: List[List[float]] = []     # L2-normalized entry embeddings

    async def initialize(self, embedder: Optional[Any] = None):
        import aiohttp
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self.embedder = embedder

        entries: List[Dict[str, Any]] = []
        # `collection_links` are (url, depth) crawl edges discovered during parsing (§4.6).
        collection_links: List[Tuple[str, int]] = []

        if GITHUB_CONFIG["local_dir"]:
            entries, collection_links = self._load_local(GITHUB_CONFIG["local_dir"])
        elif GITHUB_CONFIG["repo"]:
            entries, collection_links = await self._load_github()
        else:
            print("Warning: in-memory backend has no source "
                  "(set AGENT_FINDER_GITHUB_REPO or AGENT_FINDER_CATALOG_DIR)")

        entries.extend(await self._crawl_collections(collection_links))
        self._build_index(entries)
        await self._embed_index()
        print(f"In-memory backend loaded {len(self.docs)} catalog entries")

    # ----- ingestion -----

    def _load_local(self, directory: str):
        from pathlib import Path
        entries: List[Dict[str, Any]] = []
        links: List[Tuple[str, int]] = []
        for fp in sorted(Path(directory).rglob("*.json")):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    e, l = self._parse_manifest(json.load(f), 0)
                    entries.extend(e)
                    links.extend(l)
            except (json.JSONDecodeError, OSError) as e:
                print(f"Skipping {fp}: {e}")
        return entries, links

    async def _load_github(self):
        owner_repo = GITHUB_CONFIG["repo"]
        path = GITHUB_CONFIG["path"].strip("/")
        ref = GITHUB_CONFIG["ref"]
        api = f"https://api.github.com/repos/{owner_repo}/contents/{path}"
        entries: List[Dict[str, Any]] = []
        links: List[Tuple[str, int]] = []
        for raw in await self._fetch_github_dir(api, ref):
            try:
                e, l = self._parse_manifest(json.loads(raw), 0)
                entries.extend(e)
                links.extend(l)
            except json.JSONDecodeError as e:
                print(f"Skipping malformed manifest: {e}")
        return entries, links

    async def _crawl_collections(self, links: List[Tuple[str, int]]) -> List[Dict[str, Any]]:
        """Follow `collections` URLs transitively (§6.2), depth- and cycle-guarded."""
        entries: List[Dict[str, Any]] = []
        queue = list(links)
        visited = set()
        while queue:
            url, depth = queue.pop(0)
            if url in visited or depth > GITHUB_CONFIG["max_collection_depth"]:
                continue
            visited.add(url)
            try:
                async with self.session.get(url, headers=self._github_headers()) as resp:
                    if resp.status != 200:
                        continue
                    obj = json.loads(await resp.text())
            except Exception as e:
                print(f"Skipping collection {url}: {e}")
                continue
            child_entries, child_links = self._parse_manifest(obj, depth)
            entries.extend(child_entries)
            queue.extend(child_links)
        return entries

    def _github_headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if GITHUB_CONFIG["token"]:
            headers["Authorization"] = f"Bearer {GITHUB_CONFIG['token']}"
        return headers

    async def _fetch_github_dir(self, api_url: str, ref: str) -> List[str]:
        """Return the raw text of every .json file under a GitHub directory (recursive)."""
        contents: List[str] = []
        try:
            async with self.session.get(api_url, params={"ref": ref}, headers=self._github_headers()) as resp:
                if resp.status != 200:
                    print(f"GitHub listing failed ({resp.status}) for {api_url}")
                    return contents
                listing = await resp.json()
        except Exception as e:
            print(f"GitHub listing error: {e}")
            return contents

        if isinstance(listing, dict):  # a single file was addressed directly
            listing = [listing]

        for item in listing:
            if item.get("type") == "dir":
                contents.extend(await self._fetch_github_dir(item["url"], ref))
            elif item.get("type") == "file" and item.get("name", "").endswith(".json"):
                download_url = item.get("download_url")
                if download_url:
                    try:
                        async with self.session.get(download_url) as fresp:
                            if fresp.status == 200:
                                contents.append(await fresp.text())
                    except Exception as e:
                        print(f"Failed to fetch {download_url}: {e}")
        return contents

    def _parse_manifest(self, obj: Any, depth: int) -> Tuple[List[Dict[str, Any]], List[Tuple[str, int]]]:
        """Normalize a manifest, entry list, single entry, or inline bundle.

        Returns (entries, collection_links). Inline application/ai-catalog+json
        bundles carried in an entry's `data` are expanded; `collections` links are
        returned as crawl edges for the caller to follow.
        """
        entries: List[Dict[str, Any]] = []
        links: List[Tuple[str, int]] = []

        if isinstance(obj, list):
            for item in obj:
                e, l = self._parse_manifest(item, depth)
                entries.extend(e)
                links.extend(l)
            return entries, links
        if not isinstance(obj, dict):
            return entries, links

        if isinstance(obj.get("entries"), list):
            for entry in obj["entries"]:
                entries.extend(self._expand_entry(entry, depth))
            for coll in obj.get("collections", []) or []:
                url = coll.get("url") if isinstance(coll, dict) else None
                if url:
                    links.append((url, depth + 1))
            return entries, links

        # A bare entry object.
        return self._expand_entry(obj, depth), links

    def _expand_entry(self, entry: Dict[str, Any], depth: int) -> List[Dict[str, Any]]:
        if not isinstance(entry, dict):
            return []
        out = [entry]
        data = entry.get("data")
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            for nested in data["entries"]:
                out.extend(self._expand_entry(nested, depth))
        return out

    # ----- indexing + retrieval -----

    def _build_index(self, entries: List[Dict[str, Any]]):
        self.docs = []
        self.texts = []
        seen = set()
        for entry in entries:
            ident = entry.get("identifier") or entry.get("url")
            if not ident or ident in seen:
                continue
            seen.add(ident)
            url = entry.get("url") or entry.get("identifier") or ""
            name = entry.get("displayName") or entry.get("name", "Unknown")
            description = entry.get("description", "")
            self.docs.append({
                "url": url,
                "name": name,
                "json_ld": json.dumps(entry),  # carry the full entry for rich mapping
                "description": description,
            })
            # Embed `search_text` when an enrichment pass provided one (factual
            # blurb + example queries); else mirror the Go reference entryText():
            # displayName + "\n" + description.
            self.texts.append((entry.get("search_text") or f"{name}\n{description}").strip())

    def _embed_cache_path(self) -> Optional[str]:
        """Where to persist embeddings. Defaults into the local catalog dir (as a
        non-.json file so the catalog loader's *.json glob ignores it). When
        loading from GitHub instead, cache under a local .agent_finder_cache/ dir
        keyed by repo/path/ref so a restart re-embeds nothing (otherwise every
        boot re-embeds the whole catalog at API cost)."""
        explicit = os.getenv("AGENT_FINDER_EMBED_CACHE")
        if explicit:
            return explicit
        local = GITHUB_CONFIG.get("local_dir")
        if local:
            return os.path.join(local, ".embeddings.pkl")
        repo = GITHUB_CONFIG.get("repo")
        if repo:
            slug = f"{repo}_{GITHUB_CONFIG.get('path','')}_{GITHUB_CONFIG.get('ref','')}"
            slug = "".join(c if c.isalnum() else "_" for c in slug)
            cache_dir = os.path.join(os.getcwd(), ".agent_finder_cache")
            os.makedirs(cache_dir, exist_ok=True)
            return os.path.join(cache_dir, f"{slug}.embeddings.pkl")
        return None

    async def _embed_index(self):
        """Embed every entry's text and store L2-normalized vectors (§ brute-force scan).

        Embeddings are cached on disk keyed by a hash of the entry text, so a
        restart only embeds entries whose text is new or changed.
        """
        self.vectors = []
        if not self.docs or self.embedder is None:
            if self.docs and self.embedder is None:
                print("Warning: in-memory backend has no embedder; cosine search disabled")
            return

        cache_path = self._embed_cache_path()
        cache: Dict[str, List[float]] = {}
        if cache_path and os.path.exists(cache_path):
            try:
                with open(cache_path, "rb") as f:
                    cache = pickle.load(f)
            except (pickle.UnpicklingError, OSError, EOFError) as e:
                print(f"Embedding cache unreadable ({e}); re-embedding from scratch")

        keys = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in self.texts]
        missing = [i for i, k in enumerate(keys) if k not in cache]
        print(f"Embedding {len(missing)} entries ({len(self.texts) - len(missing)} cached)...")

        if missing:
            # Cap concurrency: firing thousands of embedding calls at once exhausts
            # the HTTP connection pool and every request fails with a connection error.
            sem = asyncio.Semaphore(int(os.getenv("AGENT_FINDER_EMBED_CONCURRENCY", "20")))

            async def embed(text: str):
                async with sem:
                    return await self.embedder.get_embedding(text)

            new_vecs = await asyncio.gather(*(embed(self.texts[i]) for i in missing))
            for i, vec in zip(missing, new_vecs):
                cache[keys[i]] = vec
            if cache_path:
                try:
                    with open(cache_path, "wb") as f:
                        pickle.dump(cache, f)
                except OSError as e:
                    print(f"Could not write embedding cache to {cache_path}: {e}")

        self.vectors = [_l2_normalize(cache[k]) for k in keys]

    async def search(self, query: str, vector: List[float], top_k: int = 30, strategy: str = "agent") -> List[Dict[str, Any]]:
        if not self.docs:
            return []
        # No vectors (no embedder configured): return head of catalog as a degraded fallback.
        if not self.vectors or not vector:
            return self.docs[:top_k]
        q = _l2_normalize(list(vector))
        scored = sorted(
            ((_dot(v, q), i) for i, v in enumerate(self.vectors)),
            key=lambda t: t[0],
            reverse=True,
        )
        return [self.docs[i] for _, i in scored[:top_k]]

    async def list_all(self, top_k: int = 200) -> List[Dict[str, Any]]:
        return self.docs[:top_k]

    async def close(self):
        if self.session:
            await self.session.close()


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
    elif provider in ("github", "memory", "inmemory"):
        return InMemorySearchBackend()
    else:
        raise ValueError(f"Unknown search provider: {SEARCH_CONFIG['provider']}")