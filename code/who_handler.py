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

# Settings from environment variables
SETTINGS = {
    "score_threshold": int(os.getenv("WHO_SCORE_THRESHOLD", "70")),
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
        print("Initializing WHO Handler...")

        # Get and initialize backends
        self.search_backend = get_search_backend()
        self.llm_backend = get_llm_backend()

        await asyncio.gather(
            self.search_backend.initialize(),
            self.llm_backend.initialize()
        )

        print("WHO Handler initialized successfully")

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
        self.stats["queries_processed"] += 1
        start_time = time.time()

        # Use provided max_results or default from settings
        result_limit = max_results if max_results is not None else SETTINGS["max_results"]

        # Route to appropriate strategy
        if retrieval_strategy == "query":
            return await self._process_query_strategy(query, augment_type, domain, capabilities, result_limit)
        else:
            return await self._process_agent_strategy(query, augment_type, domain, capabilities, result_limit)

    async def _process_agent_strategy(
        self,
        query: str,
        augment_type: Optional[str],
        domain: Optional[str],
        capabilities: Optional[List[str]],
        result_limit: int
    ) -> Dict[str, Any]:
        """
        Strategy 1: Agent-Level Retrieval
        Retrieve agent documents directly and rank them.
        """
        start_time = time.time()

        try:
            # 1. Get embedding (with cache)
            embedding_start = time.time()
            if query not in self.embedding_cache:
                self.embedding_cache[query] = await self.llm_backend.get_embedding(query)
                print(f"Generated embedding for query in {time.time() - embedding_start:.2f}s")
            else:
                print(f"Using cached embedding")

            vector = self.embedding_cache[query]

            # 2. Search for relevant sites (with cache)
            search_start = time.time()
            cache_key = hashlib.md5(query.encode()).hexdigest()
            search_results = self.search_cache.get(cache_key)

            if search_results is None:
                self.stats["cache_misses"] += 1
                search_results = await self.search_backend.search(
                    query, vector, SETTINGS["search_top_k"]
                )
                self.search_cache.set(cache_key, search_results)
                print(f"Retrieved {len(search_results)} sites from search in {time.time() - search_start:.2f}s")
            else:
                self.stats["cache_hits"] += 1
                print(f"Using cached search results ({len(search_results)} sites)")

            if not search_results:
                print("No search results found")
                return self._build_response([])

            # 3. Rank sites in parallel
            ranking_start = time.time()
            ranking_tasks = []

            for site in search_results:
                rank_cache_key = (cache_key, site["url"])

                # Check if already ranked
                cached_ranking = self.ranking_cache.get(rank_cache_key)
                if cached_ranking is not None:
                    continue

                # Create ranking task
                ranking_tasks.append(self._rank_site(query, site, rank_cache_key))

            # Execute ranking tasks and check for early return
            high_score_results = []  # Track high-scoring results for early return
            completed_count = 0
            error_count = 0

            if ranking_tasks:
                print(f"Ranking {len(ranking_tasks)} new sites...")

                # Process results as they complete
                for completed_task in asyncio.as_completed(ranking_tasks):
                    try:
                        result = await completed_task
                        completed_count += 1

                        # Check cached result for high score
                        # The task has already cached the result, so check all cached results
                        for site in search_results:
                            rank_cache_key = (cache_key, site["url"])
                            ranking = self.ranking_cache.get(rank_cache_key)

                            if ranking and ranking["score"] >= SETTINGS["early_threshold"]:
                                # Check if we already have this high-scoring site
                                if not any(r["endpoint"] == site["url"] or r["endpoint"].startswith(site["url"]) for r in high_score_results):
                                    # Extract @type from json_ld
                                    schema_type = self._extract_schema_type(site)

                                    high_score_results.append(
                                        self._build_result_object(site, ranking, schema_type)
                                    )

                                    # Check if we have enough high-scoring results for early return
                                    if len(high_score_results) >= result_limit:
                                        print(f"Early return: Found {len(high_score_results)} high-scoring results (>= {SETTINGS['early_threshold']})")
                                        # Sort and return early
                                        high_score_results.sort(key=lambda x: x["score"], reverse=True)
                                        return self._build_response(high_score_results[:result_limit])

                    except Exception as e:
                        error_count += 1
                        # Continue processing other tasks

                print(f"Ranked {completed_count} sites, {error_count} errors in {time.time() - ranking_start:.2f}s")
            else:
                print("All sites already ranked (from cache)")

            # 4. Collect and filter results
            final_results = []

            for site in search_results:
                rank_cache_key = (cache_key, site["url"])
                ranking = self.ranking_cache.get(rank_cache_key)

                if ranking and ranking["score"] > SETTINGS["score_threshold"]:
                    # Extract @type from json_ld if available
                    schema_type = self._extract_schema_type(site)

                    # Apply type filter if specified
                    if augment_type and not self._matches_type(schema_type, augment_type):
                        continue

                    final_results.append(
                        self._build_result_object(site, ranking, schema_type)
                    )

            # 5. Sort by score and return top results
            final_results.sort(key=lambda x: x["score"], reverse=True)
            top_results = final_results[:result_limit]

            # Log performance metrics
            total_time = time.time() - start_time
            print(f"Query processed in {total_time:.2f}s - Returned {len(top_results)} results")

            # Update statistics
            self.stats["total_sites_ranked"] += len(ranking_tasks)

            return self._build_response(top_results)

        except Exception as e:
            print(f"Error processing query: {e}")
            import traceback
            traceback.print_exc()
            return self._build_error_response("INTERNAL_ERROR", str(e))

    async def _process_query_strategy(
        self,
        query: str,
        augment_type: Optional[str],
        domain: Optional[str],
        capabilities: Optional[List[str]],
        result_limit: int
    ) -> Dict[str, Any]:
        """
        Strategy 2: Query-Level Retrieval with Aggregation
        Retrieve sample query documents, aggregate by agent, then rank.
        """
        start_time = time.time()

        try:
            # 1. Get embedding (with cache)
            embedding_start = time.time()
            if query not in self.embedding_cache:
                self.embedding_cache[query] = await self.llm_backend.get_embedding(query)
                print(f"Generated embedding for query in {time.time() - embedding_start:.2f}s")
            else:
                print(f"Using cached embedding")

            vector = self.embedding_cache[query]

            # 2. Search for relevant QUERY documents (more results needed)
            search_start = time.time()
            cache_key = hashlib.md5((query + "_query_strategy").encode()).hexdigest()
            search_results = self.search_cache.get(cache_key)

            if search_results is None:
                self.stats["cache_misses"] += 1
                # Retrieve more documents since we need to aggregate
                search_results = await self.search_backend.search(
                    query, vector, SETTINGS["search_top_k"] * 2
                )
                self.search_cache.set(cache_key, search_results)
                print(f"Retrieved {len(search_results)} query documents from search in {time.time() - search_start:.2f}s")
            else:
                self.stats["cache_hits"] += 1
                print(f"Using cached search results ({len(search_results)} query documents)")

            if not search_results:
                print("No search results found")
                return self._build_response([])

            # 3. Aggregate query documents by agent
            aggregation_start = time.time()
            aggregated_agents = self._aggregate_by_agent(search_results)
            print(f"Aggregated {len(search_results)} query docs into {len(aggregated_agents)} agents in {time.time() - aggregation_start:.2f}s")

            if not aggregated_agents:
                print("No agents after aggregation")
                return self._build_response([])

            # 4. Rank aggregated agents in parallel
            ranking_start = time.time()
            ranking_tasks = []

            for agent in aggregated_agents:
                # Use agent_id for cache key
                rank_cache_key = (cache_key, agent["agent_id"])

                # Check if already ranked
                cached_ranking = self.ranking_cache.get(rank_cache_key)
                if cached_ranking is not None:
                    continue

                # Create ranking task with aggregated context
                ranking_tasks.append(self._rank_aggregated_agent(query, agent, rank_cache_key))

            # Execute ranking tasks
            completed_count = 0
            error_count = 0

            if ranking_tasks:
                print(f"Ranking {len(ranking_tasks)} aggregated agents...")

                for completed_task in asyncio.as_completed(ranking_tasks):
                    try:
                        await completed_task
                        completed_count += 1
                    except Exception as e:
                        error_count += 1

                print(f"Ranked {completed_count} agents, {error_count} errors in {time.time() - ranking_start:.2f}s")
            else:
                print("All agents already ranked (from cache)")

            # 5. Collect and filter results
            final_results = []

            for agent in aggregated_agents:
                rank_cache_key = (cache_key, agent["agent_id"])
                ranking = self.ranking_cache.get(rank_cache_key)

                if ranking and ranking["score"] > SETTINGS["score_threshold"]:
                    # Get the original site document for building result
                    # Use the first matched query's metadata
                    site = {
                        "url": agent.get("agent_url", agent["agent_id"]),
                        "name": agent["agent_name"],
                        "json_ld": agent.get("agent_json_ld", "{}"),
                        "description": agent.get("agent_description", "")
                    }

                    schema_type = self._extract_schema_type(site)

                    # Apply type filter if specified
                    if augment_type and not self._matches_type(schema_type, augment_type):
                        continue

                    # Enhance ranking with matched queries info
                    enhanced_ranking = {
                        **ranking,
                        "matched_queries": agent.get("matched_queries", [])[:3]  # Include top 3
                    }

                    result = self._build_result_object(site, ranking, schema_type)

                    # Add matched queries for explainability
                    if agent.get("matched_queries"):
                        result["matched_queries"] = [
                            {
                                "query": mq["query"],
                                "score": mq.get("score", 0)
                            }
                            for mq in agent["matched_queries"][:3]
                        ]

                    final_results.append(result)

            # 6. Sort by score and return top results
            final_results.sort(key=lambda x: x["score"], reverse=True)
            top_results = final_results[:result_limit]

            # Log performance metrics
            total_time = time.time() - start_time
            print(f"Query-level strategy processed in {total_time:.2f}s - Returned {len(top_results)} results")

            # Update statistics
            self.stats["total_sites_ranked"] += len(ranking_tasks)

            return self._build_response(top_results)

        except Exception as e:
            print(f"Error processing query (query strategy): {e}")
            import traceback
            traceback.print_exc()
            return self._build_error_response("INTERNAL_ERROR", str(e))

    def _aggregate_by_agent(self, query_docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Aggregate query documents by agent_id.

        Args:
            query_docs: List of query documents from search

        Returns:
            List of aggregated agent objects
        """
        agents = {}

        for doc in query_docs:
            # Extract agent_id from metadata
            # Assume query docs have agent_id, agent_name, etc in metadata
            agent_id = doc.get("agent_id") or doc.get("url")
            if not agent_id:
                continue

            if agent_id not in agents:
                agents[agent_id] = {
                    "agent_id": agent_id,
                    "agent_name": doc.get("agent_name", doc.get("name", "Unknown")),
                    "agent_url": doc.get("agent_url", doc.get("url", agent_id)),
                    "agent_json_ld": doc.get("agent_json_ld", doc.get("json_ld", "{}")),
                    "agent_description": doc.get("agent_description", doc.get("description", "")),
                    "matched_queries": [],
                    "max_score": 0
                }

            # Add matched query
            query_text = doc.get("query", doc.get("name", ""))
            query_detail = doc.get("query_detail", doc.get("description", ""))
            query_score = doc.get("@search.score", 0)

            agents[agent_id]["matched_queries"].append({
                "query": query_text,
                "detail": query_detail,
                "score": query_score
            })

            # Track max score
            agents[agent_id]["max_score"] = max(
                agents[agent_id]["max_score"],
                query_score
            )

        # Sort agents by max score
        sorted_agents = sorted(
            agents.values(),
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

    async def _rank_aggregated_agent(self, query: str, agent: Dict[str, Any], cache_key: Tuple) -> bool:
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
                "name": agent["agent_name"],
                "matched_capabilities": [
                    {
                        "capability": mq["query"],
                        "description": mq.get("detail", "")
                    }
                    for mq in agent["matched_queries"][:5]
                ]
            }

            # Get ranking from LLM
            ranking = await self.llm_backend.rank_site(query, json.dumps(context, indent=2))

            # Cache the result
            self.ranking_cache.set(cache_key, ranking)

            return True

        except Exception as e:
            print(f"Error ranking aggregated agent {agent.get('agent_name', 'Unknown')}: {e}")

            # Cache error result to avoid retrying
            self.ranking_cache.set(cache_key, {
                "score": 0,
                "description": f"Ranking failed: {str(e)[:50]}"
            })

            return False

    def _extract_schema_type(self, site: Dict[str, Any]) -> str:
        """Extract @type from json_ld if available"""
        schema_type = "Site"  # Default type
        try:
            json_ld_data = json.loads(site.get("json_ld", "{}"))
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

    def _build_result_object(self, site: Dict[str, Any], ranking: Dict[str, Any], schema_type: str) -> Dict[str, Any]:
        """
        Build a result object per /who protocol specification (Section 5).

        Returns:
            Dict with protocol, endpoint, score, and definition fields
        """
        # Parse json_ld to get protocol-specific information
        try:
            json_ld_data = json.loads(site.get("json_ld", "{}"))
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
        endpoint = site["url"]

        # Build definition object based on protocol (Section 5.2)
        definition = self._build_definition(protocol, site, ranking, json_ld_data)

        result = {
            "protocol": protocol,
            "endpoint": endpoint,
            "score": ranking["score"],
            "definition": definition
        }

        # Add source field if available (Section 11.3)
        if site.get("source"):
            result["source"] = site["source"]

        return result

    def _build_definition(self, protocol: str, site: Dict[str, Any], ranking: Dict[str, Any], json_ld_data: Dict[str, Any]) -> Dict[str, Any]:
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
                "name": site["name"],
                "description": ranking.get("description", site.get("description", "")),
                "url": site["url"],
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
                "name": site["name"],
                "description": ranking.get("description", site.get("description", "")),
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
                "name": json_ld_data.get("name", site["name"]),
                "description": ranking.get("description", site.get("description", "")),
            }

            # Add optional skill fields
            for field in ["license", "compatibility", "allowed-tools", "metadata"]:
                if field in json_ld_data:
                    definition[field] = json_ld_data[field]

            return definition

        elif protocol == "openapi":
            # OpenAPI spec reference (Section 5.2)
            definition = {
                "name": site["name"],
                "description": ranking.get("description", site.get("description", "")),
                "specUrl": json_ld_data.get("specUrl", site["url"] + "/openapi.json")
            }

            return definition

        else:
            # Custom HTTP endpoint (Section 5.2)
            definition = {
                "name": site["name"],
                "description": ranking.get("description", site.get("description", "")),
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

    async def _rank_site(self, query: str, site: Dict[str, Any], cache_key: Tuple) -> bool:
        """
        Rank a single site and cache the result.

        Args:
            query: The user's query
            site: Site information dictionary
            cache_key: Cache key for storing the ranking

        Returns:
            True if ranking was successful
        """
        try:
            # Get ranking from LLM
            ranking = await self.llm_backend.rank_site(query, site.get("json_ld", "{}"))

            # Cache the result
            self.ranking_cache.set(cache_key, ranking)

            return True

        except Exception as e:
            print(f"Error ranking site {site.get('name', 'Unknown')}: {e}")

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
        print("All caches cleared")

    async def cleanup(self):
        """Cleanup resources"""
        print("Cleaning up WHO Handler...")

        # Cleanup backends
        cleanup_tasks = []
        if self.search_backend:
            cleanup_tasks.append(self.search_backend.close())
        if self.llm_backend:
            cleanup_tasks.append(self.llm_backend.close())

        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

        print("WHO Handler cleanup complete")


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
    retrieval_strategy: str = "agent"
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