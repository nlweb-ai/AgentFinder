"""
Core WHO handler logic - orchestration, caching, and ranking.
Backend-agnostic implementation following the /who protocol specification.

Protocol version: 0.1
See: who_protocol.md for full specification
"""
import os
import asyncio
import hashlib
import time
import json
from typing import List, Dict, Any, Optional, Tuple
from collections import OrderedDict

from search_backend import get_search_backend
from llm_backend import get_llm_backend

# Protocol version
WHO_PROTOCOL_VERSION = "0.1"

# Debug logging control
DEBUG_ENABLED = os.getenv("WHO_DEBUG", "false").lower() in ["true", "1", "yes"]

def debug_log(message: str, **kwargs):
    """Print debug message if DEBUG_ENABLED is True"""
    if DEBUG_ENABLED:
        prefix = "[WHO_DEBUG]"
        if kwargs:
            print(f"{prefix} {message} | {json.dumps(kwargs, indent=2)}")
        else:
            print(f"{prefix} {message}")

# Settings from environment variables
SETTINGS = {
    "score_threshold": int(os.getenv("WHO_SCORE_THRESHOLD", "64")),
    "early_threshold": int(os.getenv("WHO_EARLY_THRESHOLD", "85")),  # Return early if enough high scores
    "max_results": int(os.getenv("WHO_MAX_RESULTS", "10")),
    "search_top_k": int(os.getenv("WHO_SEARCH_TOP_K", "30")),
    "cache_ttl": int(os.getenv("WHO_CACHE_TTL", "3600")),
    "max_cache_entries": int(os.getenv("WHO_MAX_CACHE_ENTRIES", "10000")),
    "ranking_cache_entries": int(os.getenv("WHO_RANKING_CACHE_ENTRIES", "100000")),
}

# Error codes per /who protocol
ERROR_CODES = {
    "INVALID_QUERY": "Malformed or missing query",
    "NO_RESULTS": "No matching augments found",
    "RATE_LIMITED": "Too many requests",
    "INTERNAL_ERROR": "Service error",
}

class TTLCache:
    """Simple TTL cache with LRU eviction"""

    def __init__(self, max_size: int = 10000, ttl: int = 3600):
        self.cache = OrderedDict()
        self.max_size = max_size
        self.ttl = ttl

    def get(self, key: Any) -> Optional[Any]:
        """Get value from cache if not expired"""
        if key in self.cache:
            value, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl:
                # Move to end for LRU
                self.cache.move_to_end(key)
                return value
            else:
                # Expired - remove it
                del self.cache[key]
        return None

    def set(self, key: Any, value: Any):
        """Set value in cache with current timestamp"""
        # Evict oldest if at capacity
        if len(self.cache) >= self.max_size:
            self.cache.popitem(last=False)

        self.cache[key] = (value, time.time())

    def clear(self):
        """Clear all cache entries"""
        self.cache.clear()

    def size(self) -> int:
        """Get current cache size"""
        return len(self.cache)

class WHOHandler:
    """Main WHO query handler with caching and parallel processing"""

    def __init__(self):
        self.search_backend = None
        self.llm_backend = None

        # Caches with different TTLs and sizes
        self.embedding_cache = {}  # Never expires - embeddings are stable
        self.search_cache = TTLCache(
            max_size=SETTINGS["max_cache_entries"],
            ttl=SETTINGS["cache_ttl"]
        )
        self.ranking_cache = TTLCache(
            max_size=SETTINGS["ranking_cache_entries"],
            ttl=SETTINGS["cache_ttl"] * 2  # Rankings cached longer
        )

        # Statistics
        self.stats = {
            "queries_processed": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "total_sites_ranked": 0,
        }

    async def initialize(self):
        """Initialize backends"""
        # Get and initialize backends
        self.search_backend = get_search_backend()
        self.llm_backend = get_llm_backend()

        await asyncio.gather(
            self.search_backend.initialize(),
            self.llm_backend.initialize()
        )

    async def process_query(
        self,
        query: str,
        augment_type: Optional[str] = None,
        domain: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
        max_results: Optional[int] = None,
        retrieval_strategy: str = "agent"
    ) -> Dict[str, Any]:
        """
        Process a WHO query and return ranked results per /who protocol (Section 3).

        Args:
            query: Natural language description of the need
            augment_type: Filter by augment type (e.g., "A2AAgent", "MCPTool", "Skill")
            domain: Filter by domain (e.g., "recipes", "travel", "finance")
            capabilities: Array of required capabilities
            max_results: Maximum number of results to return
            retrieval_strategy: "agent" (Strategy 1) or "query" (Strategy 2)

        Returns:
            Dict with _meta and results per /who protocol specification (Section 6)
        """
        debug_log("=== PROCESS_QUERY START ===", query=query, strategy=retrieval_strategy,
                  augment_type=augment_type, max_results=max_results)

        self.stats["queries_processed"] += 1
        start_time = time.time()

        # Use provided max_results or default from settings
        result_limit = max_results if max_results is not None else SETTINGS["max_results"]
        debug_log("Result limit set", result_limit=result_limit)

        # Route to appropriate strategy
        if retrieval_strategy == "query":
            debug_log("Routing to QUERY strategy")
            result = await self._process_query_strategy(query, augment_type, domain, capabilities, result_limit)
        else:
            debug_log("Routing to AUGMENT strategy")
            result = await self._process_augment_strategy(query, augment_type, domain, capabilities, result_limit)

        elapsed = time.time() - start_time
        result_count = len(result.get("results", [])) if isinstance(result, dict) else 0
        debug_log("=== PROCESS_QUERY END ===", elapsed_seconds=elapsed, result_count=result_count)

        return result

    async def process_query_stream(
        self,
        query: str,
        augment_type: Optional[str] = None,
        domain: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
        max_results: Optional[int] = None,
        retrieval_strategy: str = "query",
        stream_callback: Optional[callable] = None
    ):
        """
        Streaming version of process_query. Calls stream_callback for each result as it completes.

        Only supports query strategy (Strategy 2) for streaming.
        """
        self.stats["queries_processed"] += 1
        result_limit = max_results if max_results is not None else SETTINGS["max_results"]

        # Only query strategy supports streaming
        if retrieval_strategy == "query":
            # Call _process_query_strategy with stream_callback
            await self._process_query_strategy(query, augment_type, domain, capabilities, result_limit, stream_callback)
        else:
            # Fall back to non-streaming for augment strategy
            result = await self._process_augment_strategy(query, augment_type, domain, capabilities, result_limit)
            # Stream the final results
            if stream_callback and "results" in result:
                for r in result["results"]:
                    await stream_callback(r)

    async def _process_augment_strategy(
        self,
        query: str,
        augment_type: Optional[str],
        domain: Optional[str],
        capabilities: Optional[List[str]],
        result_limit: int,
        stream_callback: Optional[callable] = None
    ) -> Dict[str, Any]:
        """
        Strategy 1: Augment-Level Retrieval
        Retrieve augment documents directly.
        """
        try:
            debug_log(">>> AUGMENT STRATEGY: Step 1 - Get embedding")
            # 1. Get embedding (with cache)
            if query not in self.embedding_cache:
                debug_log("Embedding cache MISS - generating new embedding")
                self.embedding_cache[query] = await self.llm_backend.get_embedding(query)
                debug_log("Embedding generated", vector_length=len(self.embedding_cache[query]))
            else:
                debug_log("Embedding cache HIT")

            vector = self.embedding_cache[query]

            # 2. Search for relevant augment documents
            debug_log(">>> AUGMENT STRATEGY: Step 2 - Search for augments", search_top_k=SETTINGS["search_top_k"])
            cache_key = hashlib.md5(query.encode()).hexdigest()
            search_results = self.search_cache.get(cache_key)

            if search_results is None:
                debug_log("Search cache MISS - calling search backend")
                self.stats["cache_misses"] += 1
                raw_augments = await self.search_backend.search(
                    query, vector, SETTINGS["search_top_k"], strategy="augment"
                )
                debug_log("Search backend returned results", count=len(raw_augments) if raw_augments else 0)
                if raw_augments:
                    debug_log("Sample raw augment", sample=raw_augments[0] if raw_augments else None)

                # Normalize augment documents to standard format
                search_results = self._normalize_augment_documents(raw_augments)
                debug_log("Normalized augments", count=len(search_results))
                if search_results:
                    debug_log("Sample normalized augment", sample=search_results[0])

                self.search_cache.set(cache_key, search_results)
            else:
                debug_log("Search cache HIT", count=len(search_results))
                self.stats["cache_hits"] += 1

            if not search_results:
                debug_log("!!! NO SEARCH RESULTS - returning empty response")
                return self._build_response([])

            # 3. Rank, filter, and return results (unified logic)
            debug_log(">>> AUGMENT STRATEGY: Step 3 - Rank and build results", document_count=len(search_results))
            return await self._rank_and_build_results(
                query=query,
                documents=search_results,
                cache_key=cache_key,
                augment_type=augment_type,
                result_limit=result_limit,
                stream_callback=stream_callback
            )

        except Exception as e:
            debug_log("!!! AUGMENT STRATEGY ERROR", error=str(e), error_type=type(e).__name__)
            import traceback
            debug_log("Traceback", trace=traceback.format_exc())
            return self._build_error_response("INTERNAL_ERROR", str(e))

    async def _process_query_strategy(
        self,
        query: str,
        augment_type: Optional[str],
        domain: Optional[str],
        capabilities: Optional[List[str]],
        result_limit: int,
        stream_callback: Optional[callable] = None
    ) -> Dict[str, Any]:
        """
        Strategy 2: Query-Level Retrieval with Aggregation
        Retrieve sample query documents and aggregate by agent.
        """
        try:
            debug_log(">>> QUERY STRATEGY: Step 1 - Get embedding")
            # 1. Get embedding (with cache)
            if query not in self.embedding_cache:
                debug_log("Embedding cache MISS - generating new embedding")
                self.embedding_cache[query] = await self.llm_backend.get_embedding(query)
                debug_log("Embedding generated", vector_length=len(self.embedding_cache[query]))
            else:
                debug_log("Embedding cache HIT")

            vector = self.embedding_cache[query]

            # 2. Search for relevant query documents and aggregate by agent
            debug_log(">>> QUERY STRATEGY: Step 2 - Search for query docs", search_top_k=SETTINGS["search_top_k"] * 2)
            cache_key = hashlib.md5((query + "_query_strategy").encode()).hexdigest()
            search_results = self.search_cache.get(cache_key)

            if search_results is None:
                debug_log("Search cache MISS - calling search backend")
                self.stats["cache_misses"] += 1
                # Retrieve more documents since we need to aggregate
                raw_query_docs = await self.search_backend.search(
                    query, vector, SETTINGS["search_top_k"] * 2, strategy="query"
                )
                debug_log("Search backend returned query docs", count=len(raw_query_docs) if raw_query_docs else 0)
                if raw_query_docs:
                    debug_log("Sample raw query doc", sample=raw_query_docs[0])

                # Aggregate query documents by agent - produces same format as normalized augments
                debug_log(">>> QUERY STRATEGY: Step 2b - Aggregate by augment")
                search_results = self._aggregate_by_augment(raw_query_docs)
                debug_log("Aggregated augments", count=len(search_results))
                if search_results:
                    debug_log("Sample aggregated augment", sample=search_results[0])

                self.search_cache.set(cache_key, search_results)
            else:
                debug_log("Search cache HIT", count=len(search_results))
                self.stats["cache_hits"] += 1

            if not search_results:
                debug_log("!!! NO SEARCH RESULTS - returning empty response")
                return self._build_response([])

            # 3. Rank, filter, and return results (unified logic - same as augment strategy)
            debug_log(">>> QUERY STRATEGY: Step 3 - Rank and build results", document_count=len(search_results))
            return await self._rank_and_build_results(
                query=query,
                documents=search_results,
                cache_key=cache_key,
                augment_type=augment_type,
                result_limit=result_limit,
                stream_callback=stream_callback
            )

        except Exception as e:
            debug_log("!!! QUERY STRATEGY ERROR", error=str(e), error_type=type(e).__name__)
            import traceback
            debug_log("Traceback", trace=traceback.format_exc())
            return self._build_error_response("INTERNAL_ERROR", str(e))

    def _aggregate_by_augment(self, query_docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Aggregate query documents by augment_id.

        Args:
            query_docs: List of query documents from search

        Returns:
            List of aggregated agent objects
        """
        augments = {}

        for doc in query_docs:
            # Extract augment_id from metadata
            # Assume query docs have augment_id, augment_name, etc in metadata
            augment_id = doc.get("augment_id") or doc.get("url")
            if not augment_id:
                continue

            if augment_id not in augments:
                augments[augment_id] = {
                    "augment_id": augment_id,
                    "augment_name": doc.get("augment_name", doc.get("name", "Unknown")),
                    "augment_url": doc.get("augment_url", doc.get("url", augment_id)),
                    "augment_json_ld": doc.get("augment_json_ld", doc.get("json_ld", "{}")),
                    "augment_description": doc.get("augment_description", doc.get("description", "")),
                    "matched_queries": [],
                    "max_score": 0
                }

            # Add matched query
            query_text = doc.get("query", doc.get("name", ""))
            query_detail = doc.get("query_detail", doc.get("description", ""))
            query_score = doc.get("@search.score", 0)

            augments[augment_id]["matched_queries"].append({
                "query": query_text,
                "detail": query_detail,
                "score": query_score
            })

            # Track max score
            augments[augment_id]["max_score"] = max(
                augments[augment_id]["max_score"],
                query_score
            )

        # Sort augments by max score
        sorted_agents = sorted(
            augments.values(),
            key=lambda a: a["max_score"],
            reverse=True
        )

        # Sort matched_queries within each agent by score
        for agent in sorted_agents:
            agent["matched_queries"].sort(
                key=lambda q: q.get("score", 0),
                reverse=True
            )

        return sorted_agents

    def _normalize_augment_documents(self, augment_docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Normalize augment documents to standard format.
        Converts raw augment search results to the same format as aggregated augments.

        Args:
            augment_docs: List of augment documents from search

        Returns:
            List of normalized agent objects with standard fields
        """
        normalized = []

        for doc in augment_docs:
            normalized.append({
                "augment_id": doc.get("url"),
                "augment_name": doc.get("name", "Unknown"),
                "augment_url": doc.get("url"),
                "augment_json_ld": doc.get("json_ld", "{}"),
                "augment_description": doc.get("description", ""),
                "matched_queries": []  # Empty for augment strategy
            })

        return normalized

    async def _rank_and_build_results(
        self,
        query: str,
        documents: List[Dict[str, Any]],
        cache_key: str,
        augment_type: Optional[str],
        result_limit: int,
        stream_callback: Optional[callable] = None
    ) -> Dict[str, Any]:
        """
        Unified ranking, filtering, and result building logic.
        Used by both agent and query strategies.

        Args:
            query: The user's query
            documents: List of normalized documents (augments)
            cache_key: Cache key prefix
            augment_type: Optional type filter
            result_limit: Maximum number of results to return
            stream_callback: Optional callback for streaming results

        Returns:
            Response dictionary with results
        """
        debug_log(">>> RANK_AND_BUILD: Step 1 - Create ranking tasks",
                  total_documents=len(documents),
                  score_threshold=SETTINGS["score_threshold"])

        # 1. Rank documents in parallel
        ranking_tasks = []
        ranking_cache_hits = 0

        for doc in documents:
            rank_cache_key = (cache_key, doc["augment_id"])

            # Check if already ranked
            cached_ranking = self.ranking_cache.get(rank_cache_key)
            if cached_ranking is not None:
                ranking_cache_hits += 1
                debug_log("Ranking cache HIT", augment_id=doc["augment_id"], cached_score=cached_ranking.get("score"))
                continue

            # Create ranking task
            debug_log("Creating ranking task", augment_id=doc["augment_id"], augment_name=doc.get("augment_name"))
            ranking_tasks.append(self._rank_document(query, doc, rank_cache_key))

        debug_log("Ranking tasks created", new_tasks=len(ranking_tasks), cached=ranking_cache_hits)

        # Execute ranking tasks with streaming support
        if stream_callback and ranking_tasks:
            # STREAMING MODE: Stream results as they complete
            debug_log("Executing ranking tasks in STREAMING mode...")

            # Create a document lookup by augment_id for quick access
            doc_lookup = {doc["augment_id"]: doc for doc in documents}

            for completed_task in asyncio.as_completed(ranking_tasks):
                try:
                    await completed_task

                    # Process ALL documents to find newly completed ones
                    for doc in documents:
                        rank_cache_key = (cache_key, doc["augment_id"])
                        ranking = self.ranking_cache.get(rank_cache_key)

                        # Skip if not ranked yet or already streamed
                        if not ranking or doc.get("_streamed"):
                            continue

                        # Mark as streamed
                        doc["_streamed"] = True

                        # Filter by score
                        if ranking["score"] <= SETTINGS["score_threshold"]:
                            debug_log("Filtered by SCORE (streaming)", augment_id=doc["augment_id"],
                                    score=ranking["score"], threshold=SETTINGS["score_threshold"])
                            continue

                        # Build result
                        augment = {
                            "url": doc.get("augment_url", doc["augment_id"]),
                            "name": doc["augment_name"],
                            "json_ld": doc.get("augment_json_ld", "{}"),
                            "description": doc.get("augment_description", "")
                        }

                        schema_type = self._extract_schema_type(augment)

                        # Apply type filter if specified
                        if augment_type and not self._matches_type(schema_type, augment_type):
                            debug_log("Filtered by TYPE (streaming)", augment_id=doc["augment_id"],
                                    schema_type=schema_type, required_type=augment_type)
                            continue

                        result = self._build_result_object(augment, ranking, schema_type)

                        # Add matched queries if present
                        if doc.get("matched_queries"):
                            result["matched_queries"] = [
                                {"query": mq["query"], "score": mq.get("score", 0)}
                                for mq in doc["matched_queries"][:3]
                            ]

                        # Stream immediately
                        debug_log("STREAMING result", augment_id=doc["augment_id"], score=ranking["score"])
                        await stream_callback(result)

                except Exception as e:
                    debug_log("Ranking task FAILED", error=str(e))

            # Return empty response - results already streamed
            return self._build_response([])

        else:
            # NON-STREAMING MODE: Collect all results first
            if ranking_tasks:
                debug_log("Executing ranking tasks in NON-STREAMING mode...")
                for completed_task in asyncio.as_completed(ranking_tasks):
                    try:
                        await completed_task
                    except Exception as e:
                        debug_log("Ranking task FAILED", error=str(e))

            # 2. Collect and filter results
            debug_log(">>> RANK_AND_BUILD: Step 2 - Collect and filter results")
            final_results = []
            filtered_by_score = 0
            filtered_by_type = 0

            for doc in documents:
                rank_cache_key = (cache_key, doc["augment_id"])
                ranking = self.ranking_cache.get(rank_cache_key)

                debug_log("Processing document",
                         augment_id=doc["augment_id"],
                         augment_name=doc.get("augment_name"),
                         has_ranking=ranking is not None,
                         score=ranking.get("score") if ranking else None)

                if ranking and ranking["score"] > SETTINGS["score_threshold"]:
                    # Build augment object from document data
                    augment = {
                        "url": doc.get("augment_url", doc["augment_id"]),
                        "name": doc["augment_name"],
                        "json_ld": doc.get("augment_json_ld", "{}"),
                        "description": doc.get("augment_description", "")
                    }

                    schema_type = self._extract_schema_type(augment)

                    # Apply type filter if specified
                    if augment_type and not self._matches_type(schema_type, augment_type):
                        debug_log("Filtered by TYPE", augment_id=doc["augment_id"], schema_type=schema_type, required_type=augment_type)
                        filtered_by_type += 1
                        continue

                    result = self._build_result_object(augment, ranking, schema_type)

                    # Add matched queries for explainability (if present)
                    if doc.get("matched_queries"):
                        result["matched_queries"] = [
                            {
                                "query": mq["query"],
                                "score": mq.get("score", 0)
                            }
                            for mq in doc["matched_queries"][:3]
                        ]

                    debug_log("Added to final results", augment_id=doc["augment_id"], score=ranking["score"])
                    final_results.append(result)
                elif ranking:
                    debug_log("Filtered by SCORE", augment_id=doc["augment_id"], score=ranking["score"], threshold=SETTINGS["score_threshold"])
                    filtered_by_score += 1
                else:
                    debug_log("No ranking found", augment_id=doc["augment_id"])

            debug_log("Filtering complete",
                      final_results_count=len(final_results),
                      filtered_by_score=filtered_by_score,
                      filtered_by_type=filtered_by_type)

            # 3. Sort by score and return top results
            debug_log(">>> RANK_AND_BUILD: Step 3 - Sort and limit results")
            final_results.sort(key=lambda x: x["score"], reverse=True)
            top_results = final_results[:result_limit]

            debug_log("Top results selected",
                      top_count=len(top_results),
                      result_limit=result_limit,
                      top_scores=[r["score"] for r in top_results[:5]] if top_results else [])

        # Update statistics
        self.stats["total_sites_ranked"] += len(ranking_tasks)

        return self._build_response(top_results)

    async def _rank_document(self, query: str, doc: Dict[str, Any], cache_key: Tuple) -> bool:
        """
        Unified ranking function for both strategies.
        Ranks a document (agent or aggregated agent) and caches the result.

        Args:
            query: The user's query
            doc: Document to rank (normalized format)
            cache_key: Cache key for storing the ranking

        Returns:
            True if ranking was successful
        """
        try:
            debug_log("  >> Ranking document", augment_id=doc.get("augment_id"), augment_name=doc.get("augment_name"))

            # Build ranking context based on available information
            if doc.get("matched_queries"):
                # Query strategy: use matched queries as context
                debug_log("  >> Using QUERY STRATEGY ranking (matched queries)",
                         matched_count=len(doc["matched_queries"]))
                context = {
                    "name": doc["augment_name"],
                    "matched_capabilities": [
                        {
                            "capability": mq["query"],
                            "description": mq.get("detail", "")
                        }
                        for mq in doc["matched_queries"][:5]
                    ]
                }
                ranking_input = json.dumps(context, indent=2)
                debug_log("  >> Ranking input (first 200 chars)", input=ranking_input[:200])
            else:
                # Augment strategy: use description as context
                debug_log("  >> Using AUGMENT STRATEGY ranking (description)")
                ranking_input = doc.get("augment_description", "") or doc.get("augment_json_ld", "{}")
                debug_log("  >> Ranking input (first 200 chars)", input=ranking_input[:200] if ranking_input else "EMPTY")

            # Get ranking from LLM
            debug_log("  >> Calling LLM backend for ranking...")
            ranking = await self.llm_backend.rank_augment(query, ranking_input)
            debug_log("  >> LLM ranking received", score=ranking.get("score"), description=ranking.get("description")[:100] if ranking.get("description") else None)

            # Cache the result
            self.ranking_cache.set(cache_key, ranking)

            return True
        except Exception as e:
            debug_log("  !! Ranking FAILED", augment_id=doc.get("augment_id"), error=str(e), error_type=type(e).__name__)
            return False

    async def _rank_aggregated_augment(self, query: str, agent: Dict[str, Any], cache_key: Tuple) -> bool:
        """
        Rank an aggregated agent using its matched queries as context.

        Args:
            query: The user's query
            agent: Aggregated agent object
            cache_key: Cache key for storing the ranking

        Returns:
            True if ranking was successful
        """
        try:
            # Build ranking context from matched queries (top 5)
            context = {
                "name": agent["augment_name"],
                "matched_capabilities": [
                    {
                        "capability": mq["query"],
                        "description": mq.get("detail", "")
                    }
                    for mq in agent["matched_queries"][:5]
                ]
            }

            # Get ranking from LLM
            ranking = await self.llm_backend.rank_augment(query, json.dumps(context, indent=2))

            # Cache the result
            self.ranking_cache.set(cache_key, ranking)

            return True

        except Exception as e:
            import traceback
            traceback.print_exc()

            # Cache error result to avoid retrying
            self.ranking_cache.set(cache_key, {
                "score": 0,
                "description": f"Ranking failed: {str(e)[:50]}"
            })

            return False

    def _extract_schema_type(self, augment: Dict[str, Any]) -> str:
        """Extract @type from json_ld if available"""
        schema_type = "Site"  # Default type
        try:
            json_ld_data = json.loads(augment.get("json_ld", "{}"))
            if isinstance(json_ld_data, dict):
                schema_type = json_ld_data.get("@type", "Site")
            elif isinstance(json_ld_data, list) and json_ld_data:
                schema_type = json_ld_data[0].get("@type", "Site")
        except:
            pass  # Use default if parsing fails
        return schema_type

    def _matches_type(self, schema_type: str, augment_type: str) -> bool:
        """Check if schema type matches the requested augment type filter"""
        # Map /who protocol types to schema types (per specification Section 3.1)
        type_mappings = {
            "A2AAgent": ["A2AAgent", "Agent"],
            "MCPTool": ["MCPTool", "Tool"],
            "MCPServer": ["MCPServer", "Server"],
            "Skill": ["Skill", "AgentSkill"],
            "OpenAPIService": ["OpenAPIService", "API"],
        }
        allowed_types = type_mappings.get(augment_type, [augment_type])
        return schema_type in allowed_types

    def _build_result_object(self, augment: Dict[str, Any], ranking: Dict[str, Any], schema_type: str) -> Dict[str, Any]:
        """
        Build a result object per /who protocol specification (Section 5).

        Returns:
            Dict with protocol, endpoint, score, and definition fields
        """
        # Parse json_ld to get protocol-specific information
        try:
            json_ld_data = json.loads(augment.get("json_ld", "{}"))
        except:
            json_ld_data = {}

        # Determine protocol based on schema type (Section 5.1)
        protocol = "http"  # Default fallback
        if schema_type in ["MCPTool", "MCPServer"]:
            protocol = "mcp"
        elif schema_type in ["A2AAgent"]:
            protocol = "a2a"
        elif schema_type in ["Skill", "AgentSkill"]:
            protocol = "skill"
        elif schema_type in ["OpenAPIService", "API"]:
            protocol = "openapi"

        # Build endpoint URL
        endpoint = augment["url"]

        # Build definition object based on protocol (Section 5.2)
        definition = self._build_definition(protocol, augment, ranking, json_ld_data)

        result = {
            "protocol": protocol,
            "endpoint": endpoint,
            "score": ranking["score"],
            "definition": definition
        }

        # Add source field if available (Section 11.3)
        if augment.get("source"):
            result["source"] = augment["source"]

        return result

    def _build_definition(self, protocol: str, augment: Dict[str, Any], ranking: Dict[str, Any], json_ld_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build protocol-specific definition object (Section 5.2).

        The definition field contains the native metadata format for the protocol:
        - mcp: MCP server info + tools array (per tools/list)
        - a2a: Native A2A Agent Card
        - openapi: OpenAPI spec reference
        - skill: Agent Skill frontmatter (per agentskills.io spec)
        - http: Custom HTTP invocation details
        """
        if protocol == "a2a":
            # A2A Agent Card format (Section 5.2)
            definition = {
                "name": augment["name"],
                "description": ranking.get("description", augment.get("description", "")),
                "url": augment["url"],
                "version": json_ld_data.get("version", "1.0.0"),
            }

            # Add capabilities if present
            if "capabilities" in json_ld_data:
                definition["capabilities"] = json_ld_data["capabilities"]

            # Add skills array if present
            if "skills" in json_ld_data:
                definition["skills"] = json_ld_data["skills"]
            elif "potentialAction" in json_ld_data:
                # Convert potentialAction to skills format
                actions = json_ld_data["potentialAction"]
                if isinstance(actions, list):
                    definition["skills"] = [
                        {
                            "id": a.get("@type", "").lower(),
                            "name": a.get("name", ""),
                            "description": a.get("description", ""),
                            "examples": a.get("examples", [])
                        }
                        for a in actions if isinstance(a, dict)
                    ]

            return definition

        elif protocol == "mcp":
            # MCP server format with tools array (Section 5.2)
            definition = {
                "name": augment["name"],
                "description": ranking.get("description", augment.get("description", "")),
                "tools": []
            }

            # Add tools if present in json_ld
            if "tools" in json_ld_data:
                definition["tools"] = json_ld_data["tools"]

            # Add version if present
            if "version" in json_ld_data:
                definition["version"] = json_ld_data["version"]

            return definition

        elif protocol == "skill":
            # Agent Skill frontmatter format (Section 5.2)
            definition = {
                "name": json_ld_data.get("name", augment["name"]),
                "description": ranking.get("description", augment.get("description", "")),
            }

            # Add optional skill fields
            for field in ["license", "compatibility", "allowed-tools", "metadata"]:
                if field in json_ld_data:
                    definition[field] = json_ld_data[field]

            return definition

        elif protocol == "openapi":
            # OpenAPI spec reference (Section 5.2)
            definition = {
                "name": augment["name"],
                "description": ranking.get("description", augment.get("description", "")),
                "specUrl": json_ld_data.get("specUrl", augment["url"] + "/openapi.json")
            }

            return definition

        else:
            # Custom HTTP endpoint (Section 5.2)
            definition = {
                "name": augment["name"],
                "description": ranking.get("description", augment.get("description", "")),
                "method": json_ld_data.get("method", "POST"),
                "contentType": json_ld_data.get("contentType", "application/json"),
            }

            # Add inputSchema if present
            if "inputSchema" in json_ld_data:
                definition["inputSchema"] = json_ld_data["inputSchema"]

            # Add authentication if present
            if "authentication" in json_ld_data:
                definition["authentication"] = json_ld_data["authentication"]

            return definition

    def _build_response(self, results: List[Dict[str, Any]], referrals: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """
        Build a successful /who protocol response (Section 6.1).

        Args:
            results: List of ranked augment results
            referrals: Optional list of referrals to other Who servers (Section 11.4)

        Returns:
            Dict with _meta and results per specification
        """
        response = {
            "_meta": {
                "response_type": "answer",
                "version": WHO_PROTOCOL_VERSION,
                "result_count": len(results)
            },
            "results": results
        }

        # Add referrals if provided (Section 11.4)
        if referrals:
            response["referrals"] = referrals

        return response

    def _build_error_response(self, error_code: str, message: Optional[str] = None) -> Dict[str, Any]:
        """
        Build an error /who protocol response.

        Args:
            error_code: One of INVALID_QUERY, NO_RESULTS, RATE_LIMITED, INTERNAL_ERROR
            message: Optional detailed error message

        Returns:
            Dict with _meta and error per specification
        """
        return {
            "_meta": {
                "response_type": "failure",
                "version": WHO_PROTOCOL_VERSION
            },
            "error": {
                "code": error_code,
                "message": message or ERROR_CODES.get(error_code, "Unknown error")
            }
        }

    async def _rank_site(self, query: str, augment: Dict[str, Any], cache_key: Tuple) -> bool:
        """
        Rank a single augment and cache the result.

        Args:
            query: The user's query
            augment: Site information dictionary
            cache_key: Cache key for storing the ranking

        Returns:
            True if ranking was successful
        """
        try:
            # Get ranking from LLM using description field
            augment_description = augment.get("description", "") or augment.get("json_ld", "{}")
            ranking = await self.llm_backend.rank_augment(query, augment_description)

            # Cache the result
            self.ranking_cache.set(cache_key, ranking)

            return True

        except Exception as e:
            import traceback
            traceback.print_exc()

            # Cache error result to avoid retrying
            self.ranking_cache.set(cache_key, {
                "score": 0,
                "description": f"Ranking failed: {str(e)[:50]}"
            })

            return False

    async def get_stats(self) -> Dict[str, Any]:
        """Get handler statistics"""
        return {
            **self.stats,
            "embedding_cache_size": len(self.embedding_cache),
            "search_cache_size": self.search_cache.size(),
            "ranking_cache_size": self.ranking_cache.size(),
        }

    async def clear_caches(self):
        """Clear all caches"""
        self.embedding_cache.clear()
        self.search_cache.clear()
        self.ranking_cache.clear()

    async def cleanup(self):
        """Cleanup resources"""

        # Cleanup backends
        cleanup_tasks = []
        if self.search_backend:
            cleanup_tasks.append(self.search_backend.close())
        if self.llm_backend:
            cleanup_tasks.append(self.llm_backend.close())

        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

# Global handler instance
_handler: Optional[WHOHandler] = None

async def get_handler() -> WHOHandler:
    """Get or create the global handler instance"""
    global _handler
    if _handler is None:
        _handler = WHOHandler()
        await _handler.initialize()
    return _handler

async def who_query(
    query: str,
    augment_type: Optional[str] = None,
    domain: Optional[str] = None,
    capabilities: Optional[List[str]] = None,
    max_results: Optional[int] = None,
    retrieval_strategy: str = "agent",
    ranking_model: Optional[str] = None
) -> Dict[str, Any]:
    """
    Main entry point for WHO queries per /who protocol (Section 3).

    Args:
        query: Natural language description of the need
        augment_type: Filter by augment type (e.g., "A2AAgent", "MCPTool", "Skill")
        domain: Filter by domain (e.g., "recipes", "travel", "finance")
        capabilities: Array of required capabilities
        max_results: Maximum number of results to return
        retrieval_strategy: "agent" (Strategy 1) or "query" (Strategy 2)
        ranking_model: LLM model to use for ranking (e.g., "gpt-4.1", "gpt-4.1-mini")

    Returns:
        Dict with _meta and results per /who protocol specification (Section 6)
    """
    if not query or not query.strip():
        # Return error response for empty query
        return {
            "_meta": {
                "response_type": "failure",
                "version": WHO_PROTOCOL_VERSION
            },
            "error": {
                "code": "INVALID_QUERY",
                "message": "Query text is required"
            }
        }

    handler = await get_handler()
    return await handler.process_query(
        query=query.strip(),
        augment_type=augment_type,
        domain=domain,
        capabilities=capabilities,
        max_results=max_results,
        retrieval_strategy=retrieval_strategy
    )

async def who_query_stream(
    query: str,
    augment_type: Optional[str] = None,
    domain: Optional[str] = None,
    capabilities: Optional[List[str]] = None,
    max_results: Optional[int] = None,
    retrieval_strategy: str = "query",
    ranking_model: Optional[str] = None,
    stream_callback: Optional[callable] = None
):
    """
    Streaming version of who_query that calls a callback for each result as it completes.

    Args:
        query: Natural language description of the need
        augment_type: Filter by augment type
        domain: Filter by domain
        capabilities: Array of required capabilities
        max_results: Maximum number of results to return
        retrieval_strategy: "agent" or "query" (query is better for streaming)
        ranking_model: LLM model to use for ranking
        stream_callback: Async function to call with each result as it completes
    """
    if not query or not query.strip():
        return

    handler = await get_handler()
    await handler.process_query_stream(
        query=query.strip(),
        augment_type=augment_type,
        domain=domain,
        capabilities=capabilities,
        max_results=max_results,
        retrieval_strategy=retrieval_strategy,
        stream_callback=stream_callback
    )

async def get_stats() -> Dict[str, Any]:
    """Get handler statistics"""
    handler = await get_handler()
    return await handler.get_stats()

async def clear_caches():
    """Clear all caches"""
    handler = await get_handler()
    await handler.clear_caches()

async def cleanup():
    """Cleanup resources on shutdown"""
    global _handler
    if _handler:
        await _handler.cleanup()
        _handler = None