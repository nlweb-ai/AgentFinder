"""
Core Agent Finder handler - orchestration, caching, ranking, and federation.
Backend-agnostic implementation following the Agent Finder specification v0.5.

Implements the catalog-entry data model (§4) and the search/explore/list APIs (§7),
plus registry-to-registry federation (§8).
"""
import os
import asyncio
import base64
import hashlib
import time
import json
import re
from typing import List, Dict, Any, Optional, Tuple
from collections import OrderedDict
from urllib.parse import urlparse

from search_backend import get_search_backend
from llm_backend import get_llm_backend

# Specification version
SPEC_VERSION = "0.5"

# Debug logging control
DEBUG_ENABLED = os.getenv("WHO_DEBUG", "false").lower() in ["true", "1", "yes"]


def debug_log(message: str, **kwargs):
    """Print debug message if DEBUG_ENABLED is True"""
    if DEBUG_ENABLED:
        prefix = "[AF_DEBUG]"
        if kwargs:
            print(f"{prefix} {message} | {json.dumps(kwargs, default=str)}")
        else:
            print(f"{prefix} {message}")


def _load_upstreams() -> List[Dict[str, str]]:
    """Parse upstream registry config from AGENT_FINDER_REGISTRIES (JSON array).

    Each element: {"identifier", "displayName", "type", "url"} where url is the
    upstream POST /search endpoint.
    """
    raw = os.getenv("AGENT_FINDER_REGISTRIES", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict) and r.get("url")]
    except json.JSONDecodeError:
        print("Warning: AGENT_FINDER_REGISTRIES is not valid JSON; federation disabled")
    return []


# Settings from environment variables
SETTINGS = {
    "score_threshold": int(os.getenv("WHO_SCORE_THRESHOLD", "64")),
    "max_results": int(os.getenv("WHO_MAX_RESULTS", "10")),
    "search_top_k": int(os.getenv("WHO_SEARCH_TOP_K", "30")),
    "list_top_k": int(os.getenv("AGENT_FINDER_LIST_TOP_K", "200")),
    "cache_ttl": int(os.getenv("WHO_CACHE_TTL", "3600")),
    "max_cache_entries": int(os.getenv("WHO_MAX_CACHE_ENTRIES", "10000")),
    "ranking_cache_entries": int(os.getenv("WHO_RANKING_CACHE_ENTRIES", "100000")),
    "default_strategy": os.getenv("AGENT_FINDER_STRATEGY", "agent"),
    "default_media_type": os.getenv("AGENT_FINDER_DEFAULT_TYPE", "application/a2a-agent-card+json"),
    "source": os.getenv("AGENT_FINDER_SOURCE", ""),
    "federation_timeout": float(os.getenv("AGENT_FINDER_FEDERATION_TIMEOUT", "8")),
}

UPSTREAMS = _load_upstreams()

# Standard error codes (Appendix B)
ERROR_CODES = {
    "INVALID_ARGUMENT": (400, "Malformed query or invalid filter syntax"),
    "UNAUTHENTICATED": (401, "Invalid or missing credentials"),
    "NOT_FOUND": (404, "Non-existent agent or registry"),
    "RATE_LIMIT_EXCEEDED": (429, "Too many requests"),
    "INTERNAL_ERROR": (500, "Internal server failure"),
    "NOT_IMPLEMENTED": (501, "Endpoint not implemented by this registry"),
}

# Schema @type -> IANA-style media type (§3.3)
MEDIA_TYPE_MAP = {
    "A2AAgent": "application/a2a-agent-card+json",
    "Agent": "application/a2a-agent-card+json",
    "MCPServer": "application/mcp-server+json",
    "MCPTool": "application/mcp-server+json",
    "Server": "application/mcp-server+json",
    "Tool": "application/mcp-server+json",
    "Skill": "application/ai-skill",
    "AgentSkill": "application/ai-skill",
    "OpenAPIService": "application/openapi+json",
    "API": "application/openapi+json",
}


class AgentFinderError(Exception):
    """Raised to produce a standard error response with a known code."""

    def __init__(self, code: str, message: Optional[str] = None):
        self.code = code
        self.http_status, default_msg = ERROR_CODES.get(code, (500, "Unknown error"))
        self.message = message or default_msg
        super().__init__(self.message)


class TTLCache:
    """Simple TTL cache with LRU eviction"""

    def __init__(self, max_size: int = 10000, ttl: int = 3600):
        self.cache = OrderedDict()
        self.max_size = max_size
        self.ttl = ttl

    def get(self, key: Any) -> Optional[Any]:
        if key in self.cache:
            value, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl:
                self.cache.move_to_end(key)
                return value
            del self.cache[key]
        return None

    def set(self, key: Any, value: Any):
        if len(self.cache) >= self.max_size:
            self.cache.popitem(last=False)
        self.cache[key] = (value, time.time())

    def clear(self):
        self.cache.clear()

    def size(self) -> int:
        return len(self.cache)


# ========== Catalog entry helpers (§4) ==========

def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "agent"


def _publisher_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host or "unknown"
    except Exception:
        return "unknown"


def publisher_from_identifier(identifier: str) -> Optional[str]:
    """Extract the <publisher> segment from a urn:ai:<publisher>:... identifier (§4.2.1)."""
    if not identifier:
        return None
    parts = identifier.split(":")
    if len(parts) >= 3 and parts[0] == "urn" and parts[1] == "ai":
        return parts[2]
    return None


def _media_type_for(schema_type: str) -> str:
    return MEDIA_TYPE_MAP.get(schema_type, SETTINGS["default_media_type"])


def _build_identifier(doc: Dict[str, Any], json_ld: Dict[str, Any], schema_type: str) -> str:
    """Use an existing urn:ai identifier if present, else synthesize one (§4.2.1)."""
    existing = json_ld.get("identifier") or json_ld.get("@id") or doc.get("identifier")
    if isinstance(existing, str) and existing.startswith("urn:ai:"):
        return existing

    url = doc.get("augment_url") or doc.get("url") or ""
    publisher = _publisher_from_url(url)
    namespace = {
        "application/mcp-server+json": "server",
        "application/ai-skill": "skill",
    }.get(_media_type_for(schema_type), "agent")
    slug = _slugify(doc.get("augment_name") or doc.get("name") or "")
    return f"urn:ai:{publisher}:{namespace}:{slug}"


def _extract_schema_type(json_ld: Dict[str, Any]) -> str:
    if isinstance(json_ld, dict):
        return json_ld.get("@type", "Agent")
    if isinstance(json_ld, list) and json_ld and isinstance(json_ld[0], dict):
        return json_ld[0].get("@type", "Agent")
    return "Agent"


def _to_catalog_entry(doc: Dict[str, Any], ranking: Optional[Dict[str, Any]], source: str) -> Dict[str, Any]:
    """Convert an internal ranked document into a spec catalog entry (§4.2)."""
    raw_json_ld = doc.get("augment_json_ld") or doc.get("json_ld") or "{}"
    try:
        json_ld = json.loads(raw_json_ld) if isinstance(raw_json_ld, str) else raw_json_ld
    except (json.JSONDecodeError, TypeError):
        json_ld = {}
    if not isinstance(json_ld, dict):
        json_ld = {}

    schema_type = _extract_schema_type(json_ld)
    url = doc.get("augment_url") or doc.get("url") or ""
    display_name = doc.get("augment_name") or doc.get("name") or "Unknown"
    description = (ranking or {}).get("description") or doc.get("augment_description") or doc.get("description") or ""

    # Manifest entries already carry an IANA media type in `type`; prefer it.
    explicit_type = json_ld.get("type")
    media_type = explicit_type if isinstance(explicit_type, str) and "/" in explicit_type else _media_type_for(schema_type)

    entry: Dict[str, Any] = {
        "identifier": _build_identifier(doc, json_ld, schema_type),
        "displayName": display_name,
        "type": media_type,
    }
    if url:
        entry["url"] = url
    if description:
        entry["description"] = description

    # Optional discovery metadata, carried through when present.
    if json_ld.get("tags"):
        entry["tags"] = json_ld["tags"]
    if json_ld.get("capabilities"):
        entry["capabilities"] = json_ld["capabilities"]

    rep_queries = [mq.get("query") for mq in doc.get("matched_queries", []) if mq.get("query")]
    if rep_queries:
        entry["representativeQueries"] = rep_queries[:5]
    elif json_ld.get("representativeQueries"):
        entry["representativeQueries"] = json_ld["representativeQueries"]

    for fld in ("version", "updatedAt", "metadata", "trustManifest"):
        if json_ld.get(fld):
            entry[fld] = json_ld[fld]

    if ranking is not None:
        entry["score"] = ranking.get("score", 0)
    if source:
        entry["source"] = source
    return entry


# ========== Filter matching (§7.1) ==========

def _values_at_path(entry: Dict[str, Any], path: str) -> List[Any]:
    """Resolve a dot-separated path into a flat list of scalar values.

    'publisher' is special-cased: derived from the entry's URN identifier (§7.1).
    """
    if path == "publisher":
        pub = publisher_from_identifier(entry.get("identifier", ""))
        return [pub] if pub else []

    current: List[Any] = [entry]
    for segment in path.split("."):
        nxt: List[Any] = []
        for node in current:
            if isinstance(node, dict) and segment in node:
                nxt.append(node[segment])
            elif isinstance(node, list):
                for item in node:
                    if isinstance(item, dict) and segment in item:
                        nxt.append(item[segment])
        current = nxt

    leaves: List[Any] = []
    for node in current:
        if isinstance(node, list):
            leaves.extend(node)
        else:
            leaves.append(node)
    return leaves


def _matches_filter(entry: Dict[str, Any], filt: Optional[Dict[str, Any]]) -> bool:
    """An entry matches if every key's constraint is satisfied (AND across keys,
    OR within a key)."""
    if not filt:
        return True
    for key, wanted in filt.items():
        if not isinstance(wanted, list):
            wanted = [wanted]
        wanted_set = {str(w) for w in wanted}
        have = {str(v) for v in _values_at_path(entry, key)}
        if not (have & wanted_set):
            return False
    return True


def _encode_page_token(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"offset": offset}).encode()).decode()


def _decode_page_token(token: Optional[str]) -> int:
    if not token:
        return 0
    try:
        return int(json.loads(base64.urlsafe_b64decode(token.encode()).decode()).get("offset", 0))
    except Exception:
        raise AgentFinderError("INVALID_ARGUMENT", "Malformed pageToken")


class AgentFinderHandler:
    """Main handler: search, explore, list, and federation."""

    def __init__(self):
        self.search_backend = None
        self.llm_backend = None
        self.http_session = None

        self.embedding_cache = {}
        self.search_cache = TTLCache(SETTINGS["max_cache_entries"], SETTINGS["cache_ttl"])
        self.ranking_cache = TTLCache(SETTINGS["ranking_cache_entries"], SETTINGS["cache_ttl"] * 2)

        self.stats = {
            "queries_processed": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "total_ranked": 0,
        }

    async def initialize(self):
        self.search_backend = get_search_backend()
        self.llm_backend = get_llm_backend()
        # The LLM backend doubles as the embedder; init it first so the in-memory
        # search backend can build its vectors at load time.
        await self.llm_backend.initialize()
        await self.search_backend.initialize(embedder=self.llm_backend)

    # ----- retrieval + ranking -----

    async def _embed(self, query: str) -> List[float]:
        if query not in self.embedding_cache:
            self.embedding_cache[query] = await self.llm_backend.get_embedding(query)
        return self.embedding_cache[query]

    async def _retrieve(self, query: str, strategy: str, top_k: int) -> Tuple[List[Dict[str, Any]], str]:
        """Retrieve and normalize candidate documents. Returns (docs, cache_key)."""
        vector = await self._embed(query)
        cache_key = hashlib.md5(f"{query}|{strategy}".encode()).hexdigest()

        docs = self.search_cache.get(cache_key)
        if docs is not None:
            self.stats["cache_hits"] += 1
            return docs, cache_key

        self.stats["cache_misses"] += 1
        if strategy == "query":
            raw = await self.search_backend.search(query, vector, top_k * 2, strategy="query")
            docs = self._aggregate_by_augment(raw)
        else:
            raw = await self.search_backend.search(query, vector, top_k, strategy="augment")
            docs = self._normalize_augments(raw)
        self.search_cache.set(cache_key, docs)
        return docs, cache_key

    def _normalize_augments(self, augment_docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for doc in augment_docs:
            out.append({
                "augment_id": doc.get("url"),
                "augment_name": doc.get("name", "Unknown"),
                "augment_url": doc.get("url"),
                "augment_json_ld": doc.get("json_ld", "{}"),
                "augment_description": doc.get("description", ""),
                "matched_queries": [],
            })
        return out

    def _aggregate_by_augment(self, query_docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        augments: Dict[str, Dict[str, Any]] = {}
        for doc in query_docs:
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
                    "max_score": 0,
                }
            score = doc.get("@search.score", 0)
            augments[augment_id]["matched_queries"].append({
                "query": doc.get("query", doc.get("name", "")),
                "detail": doc.get("query_detail", doc.get("description", "")),
                "score": score,
            })
            augments[augment_id]["max_score"] = max(augments[augment_id]["max_score"], score)

        ordered = sorted(augments.values(), key=lambda a: a["max_score"], reverse=True)
        for a in ordered:
            a["matched_queries"].sort(key=lambda q: q.get("score", 0), reverse=True)
        return ordered

    async def _rank_doc(self, query: str, doc: Dict[str, Any], cache_key: Tuple) -> None:
        try:
            if doc.get("matched_queries"):
                context = {
                    "name": doc["augment_name"],
                    "matched_capabilities": [
                        {"capability": mq["query"], "description": mq.get("detail", "")}
                        for mq in doc["matched_queries"][:5]
                    ],
                }
                ranking_input = json.dumps(context)
            else:
                ranking_input = doc.get("augment_description", "") or doc.get("augment_json_ld", "{}")
            ranking = await self.llm_backend.rank_augment(query, ranking_input)
            self.ranking_cache.set(cache_key, ranking)
        except Exception as e:
            debug_log("Ranking failed", augment_id=doc.get("augment_id"), error=str(e))
            self.ranking_cache.set(cache_key, {"score": 0, "description": f"Ranking failed: {str(e)[:50]}"})

    async def _retrieve_and_rank(self, query: str, strategy: str) -> List[Dict[str, Any]]:
        """Retrieve candidates and attach an LLM relevance ranking to each.

        Returns docs with a `_ranking` dict attached, sorted by score descending.
        """
        docs, cache_key = await self._retrieve(query, strategy, SETTINGS["search_top_k"])
        if not docs:
            return []

        tasks = []
        for doc in docs:
            rk = (cache_key, doc["augment_id"])
            if self.ranking_cache.get(rk) is None:
                tasks.append(self._rank_doc(query, doc, rk))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            self.stats["total_ranked"] += len(tasks)

        for doc in docs:
            doc["_ranking"] = self.ranking_cache.get((cache_key, doc["augment_id"])) or {"score": 0}
        docs.sort(key=lambda d: d["_ranking"].get("score", 0), reverse=True)
        return docs

    # ----- public operations -----

    async def search(
        self,
        text: str,
        filt: Optional[Dict[str, Any]] = None,
        federation: str = "auto",
        page_size: Optional[int] = None,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /search (§7.2). `text` is required."""
        self.stats["queries_processed"] += 1
        if not text or not text.strip():
            raise AgentFinderError("INVALID_ARGUMENT", "query.text is required for search")
        if federation not in ("auto", "referrals", "none"):
            raise AgentFinderError("INVALID_ARGUMENT", f"Unknown federation mode: {federation}")

        limit = page_size if page_size is not None else SETTINGS["max_results"]
        offset = _decode_page_token(page_token)
        source = SETTINGS["source"]

        docs = await self._retrieve_and_rank(text.strip(), SETTINGS["default_strategy"])
        entries = []
        for doc in docs:
            ranking = doc["_ranking"]
            if ranking.get("score", 0) <= SETTINGS["score_threshold"]:
                continue
            entry = _to_catalog_entry(doc, ranking, source)
            if _matches_filter(entry, filt):
                entries.append(entry)

        # Federation: merge upstream results when in auto mode (§8).
        if federation == "auto" and UPSTREAMS:
            upstream = await self._federate(text, filt, page_size)
            entries.extend(upstream)
            entries.sort(key=lambda e: e.get("score", 0), reverse=True)

        page = entries[offset:offset + limit]
        response: Dict[str, Any] = {"results": page}

        if federation == "referrals" and UPSTREAMS:
            response["referrals"] = [
                {k: r[k] for k in ("identifier", "displayName", "type", "url") if k in r}
                for r in UPSTREAMS
            ]

        if offset + limit < len(entries):
            response["pageToken"] = _encode_page_token(offset + limit)
        return response

    async def explore(
        self,
        text: Optional[str],
        filt: Optional[Dict[str, Any]],
        facets: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """POST /explore (§7.3). Returns facet aggregations over the matched set."""
        self.stats["queries_processed"] += 1
        if not facets:
            raise AgentFinderError("INVALID_ARGUMENT", "resultType.facets is required for explore")

        source = SETTINGS["source"]
        if text and text.strip():
            docs = await self._retrieve_and_rank(text.strip(), SETTINGS["default_strategy"])
            entries = []
            for doc in docs:
                if doc["_ranking"].get("score", 0) <= SETTINGS["score_threshold"]:
                    continue
                entry = _to_catalog_entry(doc, doc["_ranking"], source)
                if _matches_filter(entry, filt):
                    entries.append(entry)
        else:
            # No text: aggregate over the whole registry, narrowed by filter only.
            raw = await self.search_backend.list_all(SETTINGS["list_top_k"])
            entries = [
                e for e in (_to_catalog_entry(d, None, source) for d in self._normalize_augments(raw))
                if _matches_filter(e, filt)
            ]

        result_facets: Dict[str, Any] = {}
        for spec in facets:
            field = spec.get("field")
            if not field:
                continue
            limit = spec.get("limit", 20)
            min_count = spec.get("minCount", 0)
            counts: Dict[str, int] = {}
            for entry in entries:
                for val in set(str(v) for v in _values_at_path(entry, field)):
                    counts[val] = counts.get(val, 0) + 1
            ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
            kept = [(v, c) for v, c in ordered if c >= min_count]
            buckets = [{"value": v, "count": c} for v, c in kept[:limit]]
            other = sum(c for _, c in kept[limit:])
            facet_out: Dict[str, Any] = {"buckets": buckets}
            if other:
                facet_out["otherCount"] = other
            result_facets[field] = facet_out

        return {"resultType": "facets", "facets": result_facets}

    async def list_agents(
        self,
        filters: Dict[str, str],
        order_by: Optional[str] = None,
        page_size: int = 20,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /agents (§7.4). Deterministic browsing; no relevance sorting."""
        page_size = max(1, min(page_size, 100))
        offset = _decode_page_token(page_token)
        source = SETTINGS["source"]

        raw = await self.search_backend.list_all(SETTINGS["list_top_k"])
        entries = [_to_catalog_entry(d, None, source) for d in self._normalize_augments(raw)]

        # Appendix A scalar filters
        entries = [e for e in entries if self._matches_list_filters(e, filters)]

        if order_by:
            entries = self._apply_order_by(entries, order_by)
        else:
            entries.sort(key=lambda e: e.get("displayName", "").lower())

        page = entries[offset:offset + page_size]
        response: Dict[str, Any] = {"results": page}
        if offset + page_size < len(entries):
            response["pageToken"] = _encode_page_token(offset + page_size)
        return response

    def _matches_list_filters(self, entry: Dict[str, Any], filters: Dict[str, str]) -> bool:
        name = filters.get("displayName")
        if name and name.lower() not in entry.get("displayName", "").lower():
            return False
        types = filters.get("type")
        if types and entry.get("type") not in [t.strip() for t in types.split(",")]:
            return False
        pubs = filters.get("publisherId")
        if pubs:
            pub = publisher_from_identifier(entry.get("identifier", ""))
            if pub not in [p.strip() for p in pubs.split(",")]:
                return False
        created_after = filters.get("createdAfter")
        if created_after and entry.get("updatedAt", "") < created_after:
            return False
        updated_after = filters.get("updatedAfter")
        if updated_after and entry.get("updatedAt", "") < updated_after:
            return False
        return True

    def _apply_order_by(self, entries: List[Dict[str, Any]], order_by: str) -> List[Dict[str, Any]]:
        field = order_by.strip()
        reverse = False
        if " " in field:
            field, direction = field.split(None, 1)
            reverse = direction.strip().upper() == "DESC"
        field_map = {"name": "displayName", "created_at": "updatedAt"}
        key = field_map.get(field, field)
        return sorted(entries, key=lambda e: str(e.get(key, "")).lower(), reverse=reverse)

    # ----- federation (§8) -----

    async def _federate(self, text: str, filt: Optional[Dict[str, Any]], page_size: Optional[int]) -> List[Dict[str, Any]]:
        """Query upstream registries (auto mode) and return their merged results."""
        if self.http_session is None:
            import aiohttp
            self.http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=SETTINGS["federation_timeout"])
            )

        query_obj: Dict[str, Any] = {"text": text}
        if filt:
            query_obj["filter"] = filt
        body = {"query": query_obj, "federation": "none"}
        if page_size is not None:
            body["pageSize"] = page_size

        async def call(reg: Dict[str, str]) -> List[Dict[str, Any]]:
            try:
                async with self.http_session.post(reg["url"], json=body) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()
                    results = data.get("results", []) if isinstance(data, dict) else []
                    for r in results:
                        r.setdefault("source", reg["url"])
                    return results
            except Exception as e:
                debug_log("Federation call failed", url=reg.get("url"), error=str(e))
                return []

        merged: List[Dict[str, Any]] = []
        for batch in await asyncio.gather(*[call(r) for r in UPSTREAMS], return_exceptions=True):
            if isinstance(batch, list):
                merged.extend(batch)
        return merged

    # ----- admin -----

    async def get_stats(self) -> Dict[str, Any]:
        return {
            **self.stats,
            "spec_version": SPEC_VERSION,
            "upstream_registries": len(UPSTREAMS),
            "embedding_cache_size": len(self.embedding_cache),
            "search_cache_size": self.search_cache.size(),
            "ranking_cache_size": self.ranking_cache.size(),
        }

    async def clear_caches(self):
        self.embedding_cache.clear()
        self.search_cache.clear()
        self.ranking_cache.clear()

    async def cleanup(self):
        tasks = []
        if self.search_backend:
            tasks.append(self.search_backend.close())
        if self.llm_backend:
            tasks.append(self.llm_backend.close())
        if self.http_session:
            tasks.append(self.http_session.close())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


# ========== Module-level singleton + entry points ==========

_handler: Optional[AgentFinderHandler] = None


async def get_handler() -> AgentFinderHandler:
    global _handler
    if _handler is None:
        _handler = AgentFinderHandler()
        await _handler.initialize()
    return _handler


async def search(text: str, filt: Optional[Dict[str, Any]] = None, federation: str = "auto",
                 page_size: Optional[int] = None, page_token: Optional[str] = None) -> Dict[str, Any]:
    handler = await get_handler()
    return await handler.search(text, filt, federation, page_size, page_token)


async def explore(text: Optional[str], filt: Optional[Dict[str, Any]], facets: List[Dict[str, Any]]) -> Dict[str, Any]:
    handler = await get_handler()
    return await handler.explore(text, filt, facets)


async def list_agents(filters: Dict[str, str], order_by: Optional[str] = None,
                      page_size: int = 20, page_token: Optional[str] = None) -> Dict[str, Any]:
    handler = await get_handler()
    return await handler.list_agents(filters, order_by, page_size, page_token)


async def get_stats() -> Dict[str, Any]:
    handler = await get_handler()
    return await handler.get_stats()


async def clear_caches():
    handler = await get_handler()
    await handler.clear_caches()


async def cleanup():
    global _handler
    if _handler:
        await _handler.cleanup()
        _handler = None
