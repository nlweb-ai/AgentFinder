"""
Web server exposing the Agent Finder REST API (specification v0.5).

Endpoints:
  POST /search           Ranked discovery over catalog entries (§7.2)
  POST /explore          Facet aggregation over the matched set (§7.3)
  GET  /agents           Deterministic browsing (§7.4)
  POST /mcp              MCP protocol wrapper for search (§7.5)
  POST /a2a              A2A skill wrapper for search (§7.5, provisional)
  GET  /.well-known/ai-catalog.json   Self-describing registry manifest (§6.1)
  GET  /.well-known/agent-card.json   A2A agent card for the search skill

Admin: GET /health, GET /stats, POST /clear-cache.
"""
import os
import json
import logging
import sys
from pathlib import Path
from aiohttp import web

import who_handler
from who_handler import AgentFinderError, ERROR_CODES, SPEC_VERSION

logger = logging.getLogger(__name__)

# Server configuration
PORT = int(os.getenv("WHO_SERVER_PORT", "8080"))
HOST = os.getenv("WHO_SERVER_HOST", "0.0.0.0")
MCP_ENABLED = os.getenv("MCP_ENABLED", "true").lower() == "true"
A2A_ENABLED = os.getenv("A2A_ENABLED", "true").lower() == "true"

MCP_PROTOCOL_VERSION = "2024-11-05"


# ========== Helpers ==========

def _error_response(code: str, message: str = None) -> web.Response:
    """Build a standard error response (Appendix B)."""
    http_status, default_msg = ERROR_CODES.get(code, (500, "Unknown error"))
    return web.json_response(
        {"error": {"code": code, "message": message or default_msg}},
        status=http_status,
    )


def _normalize_filter(filt):
    """Coerce bare scalars to single-element arrays per §7.1."""
    if not isinstance(filt, dict):
        return None
    return {k: (v if isinstance(v, list) else [v]) for k, v in filt.items()}


# ========== REST: /search (§7.2) ==========

async def search_endpoint(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return _error_response("INVALID_ARGUMENT", "Invalid JSON in request body")

    query = data.get("query", {})
    if not isinstance(query, dict):
        return _error_response("INVALID_ARGUMENT", "query must be an object with a 'text' field")

    text = (query.get("text") or "").strip()
    filt = _normalize_filter(query.get("filter"))
    federation = data.get("federation", "auto")
    page_size = data.get("pageSize")
    page_token = data.get("pageToken")

    print(f"/search: text={text[:80]!r} federation={federation} filter={bool(filt)}")
    try:
        result = await who_handler.search(text, filt, federation, page_size, page_token)
        return web.json_response(result)
    except AgentFinderError as e:
        return _error_response(e.code, e.message)
    except Exception:
        # Log server-side; don't leak exception text to the client.
        logger.exception("Error in /search")
        return _error_response("INTERNAL_ERROR")


# ========== REST: /explore (§7.3) ==========

async def explore_endpoint(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return _error_response("INVALID_ARGUMENT", "Invalid JSON in request body")

    query = data.get("query", {}) or {}
    text = (query.get("text") or "").strip() if isinstance(query, dict) else ""
    filt = _normalize_filter(query.get("filter")) if isinstance(query, dict) else None
    result_type = data.get("resultType", {})
    facets = result_type.get("facets") if isinstance(result_type, dict) else None

    if not facets:
        return _error_response("INVALID_ARGUMENT", "resultType.facets is required")

    print(f"/explore: text={text[:80]!r} facets={[f.get('field') for f in facets]}")
    try:
        result = await who_handler.explore(text or None, filt, facets)
        return web.json_response(result)
    except AgentFinderError as e:
        return _error_response(e.code, e.message)
    except Exception:
        logger.exception("Error in /explore")
        return _error_response("INTERNAL_ERROR")


# ========== REST: GET /agents (§7.4) ==========

async def agents_endpoint(request: web.Request) -> web.Response:
    q = request.query
    filters = {
        k: q[k] for k in ("displayName", "type", "publisherId", "createdAfter", "updatedAfter")
        if k in q
    }
    order_by = q.get("orderBy")
    page_token = q.get("pageToken")
    try:
        page_size = int(q.get("pageSize", "20"))
    except ValueError:
        return _error_response("INVALID_ARGUMENT", "pageSize must be an integer")

    try:
        result = await who_handler.list_agents(filters, order_by, page_size, page_token)
        return web.json_response(result)
    except AgentFinderError as e:
        return _error_response(e.code, e.message)
    except Exception:
        logger.exception("Error in /agents")
        return _error_response("INTERNAL_ERROR")


# ========== MCP wrapper (§7.5) ==========

SEARCH_TOOL_SCHEMA = {
    "name": "search",
    "description": (
        "Discover agents, MCP servers, skills, and other AI capabilities relevant to a "
        "natural-language need. Returns ranked catalog entries."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Natural-language description of the need"},
                    "filter": {"type": "object", "description": "Structured constraints (field path -> values)"},
                },
                "required": ["text"],
            },
            "federation": {"type": "string", "enum": ["auto", "referrals", "none"]},
            "pageSize": {"type": "integer"},
        },
        "required": ["query"],
    },
}


async def mcp_endpoint(request: web.Request) -> web.Response:
    """MCP JSON-RPC 2.0 wrapper exposing the search tool."""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({
            "jsonrpc": "2.0",
            "error": {"code": -32700, "message": "Parse error: Invalid JSON"},
            "id": None,
        })

    method = data.get("method")
    params = data.get("params", {})
    request_id = data.get("id")
    is_notification = request_id is None
    result = None
    error = None

    try:
        if method == "initialize":
            result = {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agent-finder", "version": SPEC_VERSION},
                "instructions": "Agent Finder - discover AI capabilities relevant to a query",
            }
        elif method in ("initialized", "notifications/initialized"):
            if is_notification:
                return web.Response(status=204)
            result = {"status": "ok"}
        elif method == "notifications/cancelled":
            return web.Response(status=204)
        elif method == "tools/list":
            result = {"tools": [SEARCH_TOOL_SCHEMA]}
        elif method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})
            if tool_name != "search":
                error = {"code": -32601, "message": f"Unknown tool: {tool_name}"}
            else:
                query = arguments.get("query", {})
                text = (query.get("text") or "").strip() if isinstance(query, dict) else ""
                filt = _normalize_filter(query.get("filter")) if isinstance(query, dict) else None
                federation = arguments.get("federation", "none")
                page_size = arguments.get("pageSize")
                if not text:
                    error = {"code": -32602, "message": "query.text is required"}
                else:
                    search_result = await who_handler.search(text, filt, federation, page_size)
                    results = search_result.get("results", [])
                    result = {
                        "content": [{"type": "text", "text": f"Found {len(results)} matching capabilities"}],
                        "results": results,
                        "isError": False,
                    }
                    for k in ("referrals", "pageToken"):
                        if k in search_result:
                            result[k] = search_result[k]
        else:
            error = {"code": -32601, "message": f"Method not found: {method}"}
    except AgentFinderError as e:
        error = {"code": -32602, "message": e.message}
    except Exception:
        # Log server-side; don't leak exception text to the client.
        logger.exception("Error in /mcp")
        error = {"code": -32603, "message": "Internal error"}

    response = {"jsonrpc": "2.0"}
    if error:
        response["error"] = error
    else:
        response["result"] = result
    if not is_notification:
        response["id"] = request_id
    return web.json_response(response)


# ========== A2A wrapper (§7.5, provisional request format) ==========

async def a2a_endpoint(request: web.Request) -> web.Response:
    """A2A skill wrapper for search. Returns spec catalog entries.

    Request shape is provisional (§7.5): accepts {text, filter, federation, pageSize}
    or a nested {query: {text, filter}, ...}.
    """
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return _error_response("INVALID_ARGUMENT", "Invalid JSON in request body")

    query = data.get("query") if isinstance(data.get("query"), dict) else data
    text = (query.get("text") or "").strip()
    filt = _normalize_filter(query.get("filter"))
    federation = data.get("federation", "none")
    page_size = data.get("pageSize")

    try:
        result = await who_handler.search(text, filt, federation, page_size)
        return web.json_response(result)
    except AgentFinderError as e:
        return _error_response(e.code, e.message)
    except Exception:
        logger.exception("Error in /a2a")
        return _error_response("INTERNAL_ERROR")


# ========== Well-known discovery documents (§6.1) ==========

def _base_url(request: web.Request) -> str:
    configured = os.getenv("AGENT_FINDER_SOURCE")
    if configured:
        return configured.rstrip("/")
    return f"{request.scheme}://{request.host}"


async def ai_catalog_manifest(request: web.Request) -> web.Response:
    """Self-describing manifest advertising this registry's search interface."""
    base = _base_url(request)
    manifest = {
        "specVersion": "1.0",
        "host": {"displayName": os.getenv("AGENT_FINDER_HOST_NAME", "Agent Finder")},
        "entries": [
            {
                "identifier": os.getenv("AGENT_FINDER_IDENTIFIER", "urn:ai:agentfinder.local:registry:default"),
                "displayName": os.getenv("AGENT_FINDER_HOST_NAME", "Agent Finder"),
                "type": "application/ai-registry+json",
                "url": f"{base}/search",
                "description": "REST search interface for discovering AI capabilities.",
                "tags": ["registry", "search", "dynamic"],
            }
        ],
    }
    return web.json_response(manifest)


async def agent_card(request: web.Request) -> web.Response:
    """A2A agent card describing the search skill."""
    base = _base_url(request)
    card = {
        "name": os.getenv("AGENT_FINDER_HOST_NAME", "Agent Finder"),
        "description": "Discover AI capabilities (agents, MCP servers, skills) relevant to a query.",
        "url": f"{base}/a2a",
        "version": SPEC_VERSION,
        "skills": [
            {
                "id": "search",
                "name": "Capability Search",
                "description": "Return catalog entries ranked by relevance to a natural-language need.",
            }
        ],
    }
    return web.json_response(card)


# ========== Static file serving ==========

async def serve_html_file(request: web.Request, filename: str) -> web.Response:
    try:
        file_path = Path(__file__).parent / filename
        if not file_path.exists():
            return web.Response(text=f"{filename} not found", status=404)
        with open(file_path, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html", charset="utf-8")
    except Exception:
        # Log server-side; don't leak exception text (e.g. filesystem paths) to
        # the client over this browser-reachable route. (CodeQL py/stack-trace-exposure)
        logger.exception("Error serving %s", filename)
        return web.Response(text="Internal server error", status=500)


async def index_page(request: web.Request) -> web.Response:
    return await serve_html_file(request, "index.html")


async def docs_page(request: web.Request) -> web.Response:
    return await serve_html_file(request, "docs.html")


async def architecture_docs(request: web.Request) -> web.Response:
    return await serve_html_file(request, "architecture.html")


async def retrieval_strategies_docs(request: web.Request) -> web.Response:
    return await serve_html_file(request, "retrieval_strategies.html")


# ========== Admin ==========

async def health_check(request: web.Request) -> web.Response:
    try:
        stats = await who_handler.get_stats()
        return web.json_response({"status": "healthy", "stats": stats})
    except Exception:
        logger.exception("Health check failed")
        return web.json_response({"status": "unhealthy", "error": "Health check failed"}, status=503)


async def stats_endpoint(request: web.Request) -> web.Response:
    try:
        return web.json_response(await who_handler.get_stats())
    except Exception:
        logger.exception("Error in stats endpoint")
        return web.json_response({"error": "Internal server error"}, status=500)


async def clear_cache_endpoint(request: web.Request) -> web.Response:
    try:
        await who_handler.clear_caches()
        return web.json_response({"status": "Caches cleared"})
    except Exception:
        logger.exception("Error clearing caches")
        return web.json_response({"error": "Internal server error"}, status=500)


# ========== Middleware ==========

@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:
        logger.exception("Unhandled error")
        return _error_response("INTERNAL_ERROR")


# ========== Lifecycle ==========

async def startup(app: web.Application):
    print(f"Starting Agent Finder on {HOST}:{PORT}")
    await who_handler.get_handler()
    print("Server ready")


async def cleanup(app: web.Application):
    print("Shutting down...")
    await who_handler.cleanup()


def create_app() -> web.Application:
    app = web.Application(middlewares=[error_middleware, cors_middleware])

    # Static pages
    app.router.add_get("/", index_page)
    app.router.add_get("/index.html", index_page)
    app.router.add_get("/docs.html", docs_page)
    app.router.add_get("/architecture.html", architecture_docs)
    app.router.add_get("/retrieval_strategies.html", retrieval_strategies_docs)

    # Agent Finder API
    app.router.add_post("/search", search_endpoint)
    app.router.add_post("/explore", explore_endpoint)
    app.router.add_get("/agents", agents_endpoint)

    # Well-known discovery
    app.router.add_get("/.well-known/ai-catalog.json", ai_catalog_manifest)
    app.router.add_get("/.well-known/agent-card.json", agent_card)

    # Protocol wrappers
    if MCP_ENABLED:
        app.router.add_post("/mcp", mcp_endpoint)
    if A2A_ENABLED:
        app.router.add_post("/a2a", a2a_endpoint)

    # Admin
    app.router.add_get("/health", health_check)
    app.router.add_get("/stats", stats_endpoint)
    app.router.add_post("/clear-cache", clear_cache_endpoint)

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


if __name__ == "__main__":
    print("=" * 60)
    print(f"Agent Finder Server (spec v{SPEC_VERSION})")
    print("=" * 60)
    print(f"Web UI:        http://{HOST}:{PORT}/")
    print(f"Search:        POST http://{HOST}:{PORT}/search")
    print(f"Explore:       POST http://{HOST}:{PORT}/explore")
    print(f"List:          GET  http://{HOST}:{PORT}/agents")
    print(f"MCP wrapper:   {'POST http://%s:%s/mcp' % (HOST, PORT) if MCP_ENABLED else 'disabled'}")
    print(f"A2A wrapper:   {'POST http://%s:%s/a2a' % (HOST, PORT) if A2A_ENABLED else 'disabled'}")
    print(f"Health:        GET  http://{HOST}:{PORT}/health")
    print("=" * 60)

    app = create_app()
    try:
        web.run_app(app, host=HOST, port=PORT, access_log=None, print=None)
    except KeyboardInterrupt:
        print("\nServer stopped by user")
    except Exception:
        logger.exception("Server error")
        sys.exit(1)
