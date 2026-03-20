"""
Web server with REST and MCP endpoints for WHO handler.
Implements the Who Protocol specification (Version 0.1).

See who_protocol.txt for full specification.
"""
import os
import json
import asyncio
import sys
from pathlib import Path
from typing import Optional
from aiohttp import web

import who_handler

# Server configuration
PORT = int(os.getenv("WHO_SERVER_PORT", "8080"))
HOST = os.getenv("WHO_SERVER_HOST", "0.0.0.0")

# MCP Protocol version
MCP_PROTOCOL_VERSION = "2024-11-05"


# ========== REST ENDPOINTS ==========

async def who_endpoint(request: web.Request) -> web.Response:
    """
    REST endpoint for WHO queries per /who protocol specification (Section 7).
    Supports both GET and POST requests.

    Request format (POST) per Section 3:
    {
        "query": {
            "text": "natural language query",
            "type": "A2AAgent",    // optional - filter by augment type
            "domain": "recipes",   // optional - filter by domain
            "capabilities": []     // optional - required capabilities
        },
        "meta": {
            "version": "0.1",
            "max_results": 10      // optional
        }
    }

    Also supports legacy format:
    {
        "query": "natural language query"
    }

    Response format per Section 6:
    {
        "_meta": {
            "response_type": "answer",
            "version": "0.1",
            "result_count": N
        },
        "results": [...]
    }
    """
    try:
        # Support both GET and POST requests
        if request.method == "GET":
            # Get query from URL parameters (legacy format)
            query_text = request.query.get("query", "").strip()
            augment_type = request.query.get("type")
            domain = request.query.get("domain")
            retrieval_strategy = request.query.get("strategy", "agent")
            max_results = request.query.get("max_results")
            if max_results:
                try:
                    max_results = int(max_results)
                except ValueError:
                    max_results = None
        else:
            # Parse JSON request body for POST
            data = await request.json()

            # Support both new /who protocol format and legacy format
            query_obj = data.get("query", {})
            meta = data.get("meta", {})

            if isinstance(query_obj, str):
                # Legacy format: query is a string
                query_text = query_obj.strip()
                augment_type = None
                domain = None
            else:
                # New /who protocol format: query is an object
                query_text = query_obj.get("text", "").strip()
                augment_type = query_obj.get("type")
                domain = query_obj.get("domain")

            max_results = meta.get("max_results")
            retrieval_strategy = meta.get("strategy", "agent")
            model = meta.get("model")  # Optional model override

        if not query_text:
            # Return /who protocol error response
            return web.json_response({
                "_meta": {
                    "response_type": "failure",
                    "version": "0.1"
                },
                "error": {
                    "code": "INVALID_QUERY",
                    "message": "Query text is required"
                }
            }, status=400)

        print(f"REST request ({request.method}): {query_text[:100]} [strategy={retrieval_strategy}]")

        # Process query with /who protocol parameters
        result = await who_handler.who_query(
            query=query_text,
            augment_type=augment_type,
            domain=domain,
            max_results=max_results,
            retrieval_strategy=retrieval_strategy,
            ranking_model=model
        )

        # Return /who protocol response directly
        return web.json_response(result)

    except json.JSONDecodeError:
        return web.json_response({
            "_meta": {
                "response_type": "failure",
                "version": "0.1"
            },
            "error": {
                "code": "INVALID_QUERY",
                "message": "Invalid JSON in request body"
            }
        }, status=400)
    except Exception as e:
        print(f"Error in WHO endpoint: {e}")
        return web.json_response({
            "_meta": {
                "response_type": "failure",
                "version": "0.1"
            },
            "error": {
                "code": "INTERNAL_ERROR",
                "message": str(e)
            }
        }, status=500)


async def who_stream_endpoint(request: web.Request) -> web.StreamResponse:
    """
    SSE (Server-Sent Events) streaming endpoint for WHO queries.
    Returns results incrementally as they complete ranking.

    POST /who-stream with same request format as /who

    SSE event format:
    event: result
    data: {"augment_name": "...", "score": 0.95, ...}

    event: done
    data: {"total_count": 5}
    """
    try:
        # Parse request
        data = await request.json()
        query_obj = data.get("query", {})
        meta = data.get("meta", {})

        if isinstance(query_obj, str):
            query_text = query_obj.strip()
            augment_type = None
            domain = None
        else:
            query_text = query_obj.get("text", "").strip()
            augment_type = query_obj.get("type")
            domain = query_obj.get("domain")

        if not query_text:
            return web.json_response({
                "_meta": {"response_type": "failure", "version": "0.1"},
                "error": {"code": "INVALID_QUERY", "message": "Query text is required"}
            }, status=400)

        max_results = meta.get("max_results")
        retrieval_strategy = meta.get("strategy", "query")  # Default to query strategy for streaming
        model = meta.get("model")

        print(f"SSE Stream request: {query_text[:100]} [strategy={retrieval_strategy}]")

        # Set up SSE response
        response = web.StreamResponse()
        response.headers['Content-Type'] = 'text/event-stream'
        response.headers['Cache-Control'] = 'no-cache'
        response.headers['Connection'] = 'keep-alive'
        response.headers['X-Accel-Buffering'] = 'no'  # Disable nginx buffering
        await response.prepare(request)

        # Define callback to stream results
        result_count = 0
        async def stream_callback(result: dict):
            nonlocal result_count
            result_count += 1
            # Send result as SSE event
            event_data = json.dumps(result)
            await response.write(f"event: result\ndata: {event_data}\n\n".encode('utf-8'))
            await response.drain()

        # Process query with streaming callback
        await who_handler.who_query_stream(
            query=query_text,
            augment_type=augment_type,
            domain=domain,
            max_results=max_results,
            retrieval_strategy=retrieval_strategy,
            ranking_model=model,
            stream_callback=stream_callback
        )

        # Send done event
        done_data = json.dumps({"total_count": result_count})
        await response.write(f"event: done\ndata: {done_data}\n\n".encode('utf-8'))
        await response.drain()

        return response

    except json.JSONDecodeError:
        return web.json_response({
            "_meta": {"response_type": "failure", "version": "0.1"},
            "error": {"code": "INVALID_QUERY", "message": "Invalid JSON in request body"}
        }, status=400)
    except Exception as e:
        print(f"Error in WHO stream endpoint: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({
            "_meta": {"response_type": "failure", "version": "0.1"},
            "error": {"code": "INTERNAL_ERROR", "message": str(e)}
        }, status=500)


# ========== MCP ENDPOINTS ==========

async def mcp_endpoint(request: web.Request) -> web.Response:
    """MCP protocol endpoint supporting JSON-RPC 2.0"""
    try:
        # Parse JSON-RPC request
        data = await request.json()
        method = data.get("method")
        params = data.get("params", {})
        request_id = data.get("id")
        jsonrpc = data.get("jsonrpc", "2.0")

        print(f"MCP request: method={method}, id={request_id}")

        # Check if this is a notification (no id)
        is_notification = request_id is None

        result = None
        error = None

        # Route MCP methods
        if method == "initialize":
            result = {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": {
                    "name": "who-standalone",
                    "version": "1.0.0"
                },
                "instructions": "WHO Handler - Find the most relevant augments to answer your queries"
            }

        elif method == "initialized" or method == "notifications/initialized":
            # Notification that client is ready
            if not is_notification:
                result = {"status": "ok"}
            else:
                # Notifications don't get responses
                return web.Response(status=204)

        elif method == "tools/list":
            # MCP tool definition per /who protocol specification (Section 8.1)
            result = {
                "tools": [{
                    "name": "who",
                    "description": "Find augments, tools, and services that can help answer a query. Returns ranked augments with invocation details.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "object",
                                "description": "The query specifying what augments are needed",
                                "properties": {
                                    "text": {
                                        "type": "string",
                                        "description": "Natural language description of the need"
                                    },
                                    "type": {
                                        "type": "string",
                                        "description": "Filter by augment type (e.g., A2AAgent, MCPTool, Skill)"
                                    },
                                    "domain": {
                                        "type": "string",
                                        "description": "Filter by domain (e.g., recipes, travel, finance)"
                                    },
                                    "capabilities": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Required capabilities"
                                    }
                                },
                                "required": ["text"]
                            },
                            "meta": {
                                "type": "object",
                                "description": "Request metadata",
                                "properties": {
                                    "version": {"type": "string"},
                                    "max_results": {"type": "integer"},
                                    "strategy": {
                                        "type": "string",
                                        "description": "Retrieval strategy: 'agent' (default) or 'query'",
                                        "enum": ["agent", "query"]
                                    }
                                }
                            }
                        },
                        "required": ["query"]
                    }
                }]
            }

        elif method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})

            if tool_name == "who":
                # Parse /who protocol request format
                query_obj = arguments.get("query", {})
                meta = arguments.get("meta", {})

                # Support both new format (object) and legacy format (string)
                if isinstance(query_obj, str):
                    query_text = query_obj.strip()
                    augment_type = None
                    domain = None
                else:
                    query_text = query_obj.get("text", "").strip()
                    augment_type = query_obj.get("type")
                    domain = query_obj.get("domain")

                max_results = meta.get("max_results")
                retrieval_strategy = meta.get("strategy", "agent")
                model = meta.get("model")  # Optional model override

                if not query_text:
                    error = {
                        "code": -32602,  # Invalid params
                        "message": "Query text is required"
                    }
                else:
                    print(f"MCP tool call: who query='{query_text[:100]}' [strategy={retrieval_strategy}]")

                    # Process the query with /who protocol parameters
                    try:
                        who_result = await who_handler.who_query(
                            query=query_text,
                            augment_type=augment_type,
                            domain=domain,
                            max_results=max_results,
                            retrieval_strategy=retrieval_strategy,
                            ranking_model=model
                        )

                        # Format response for MCP per /who protocol spec
                        # Include the full /who response structure
                        is_error = who_result.get("_meta", {}).get("response_type") == "failure"

                        result = {
                            "content": [{
                                "type": "text",
                                "text": f"Found {who_result.get('_meta', {}).get('result_count', 0)} augments that can help"
                            }],
                            "_meta": who_result.get("_meta"),
                            "results": who_result.get("results", []),
                            "isError": is_error
                        }

                        # Include error info if present
                        if "error" in who_result:
                            result["error"] = who_result["error"]

                    except Exception as e:
                        result = {
                            "content": [{
                                "type": "text",
                                "text": f"Error processing query: {str(e)}"
                            }],
                            "_meta": {
                                "response_type": "failure",
                                "version": "0.1"
                            },
                            "error": {
                                "code": "INTERNAL_ERROR",
                                "message": str(e)
                            },
                            "isError": True
                        }
            else:
                error = {
                    "code": -32601,  # Method not found
                    "message": f"Unknown tool: {tool_name}"
                }

        elif method == "notifications/cancelled":
            # Handle cancellation notification
            request_id_to_cancel = params.get("requestId")
            reason = params.get("reason", "Unknown")
            print(f"Received cancellation for request {request_id_to_cancel}: {reason}")
            # Notifications don't get responses
            return web.Response(status=204)

        else:
            error = {
                "code": -32601,  # Method not found
                "message": f"Method not found: {method}"
            }

        # Build JSON-RPC response
        response = {"jsonrpc": jsonrpc}

        if error:
            response["error"] = error
        else:
            response["result"] = result

        # Include id for non-notifications
        if not is_notification:
            response["id"] = request_id

        return web.json_response(response)

    except json.JSONDecodeError:
        return web.json_response({
            "jsonrpc": "2.0",
            "error": {
                "code": -32700,  # Parse error
                "message": "Parse error: Invalid JSON"
            },
            "id": None
        })
    except Exception as e:
        print(f"Error in MCP endpoint: {e}")
        return web.json_response({
            "jsonrpc": "2.0",
            "error": {
                "code": -32603,  # Internal error
                "message": f"Internal error: {str(e)}"
            },
            "id": data.get("id") if "data" in locals() else None
        })


# ========== STATIC FILE SERVING ==========

async def serve_html_file(request: web.Request, filename: str) -> web.Response:
    """Serve an HTML file from the code directory"""
    try:
        # Get the directory where this script is located
        script_dir = Path(__file__).parent
        file_path = script_dir / filename

        if not file_path.exists():
            return web.Response(
                text=f"{filename} not found",
                status=404
            )

        # Read and serve the HTML file
        with open(file_path, 'r', encoding='utf-8') as f:
            html_content = f.read()

        return web.Response(
            text=html_content,
            content_type='text/html',
            charset='utf-8'
        )
    except Exception as e:
        print(f"Error serving {filename}: {e}")
        return web.Response(
            text=f"Error loading page: {str(e)}",
            status=500
        )


async def index_page(request: web.Request) -> web.Response:
    """Serve the index.html page"""
    return await serve_html_file(request, "index.html")


async def evaluation_report(request: web.Request) -> web.Response:
    """Serve the evaluation_report.html page"""
    return await serve_html_file(request, "evaluation_report.html")


async def docs_page(request: web.Request) -> web.Response:
    """Serve the docs.html page"""
    return await serve_html_file(request, "docs.html")


async def architecture_docs(request: web.Request) -> web.Response:
    """Serve the architecture documentation"""
    return await serve_html_file(request, "architecture.html")


async def retrieval_strategies_docs(request: web.Request) -> web.Response:
    """Serve the retrieval strategies documentation"""
    return await serve_html_file(request, "retrieval_strategies.html")


async def multi_model_evaluation_docs(request: web.Request) -> web.Response:
    """Serve the multi-model evaluation documentation"""
    return await serve_html_file(request, "multi_model_evaluation.html")


# ========== ADMIN ENDPOINTS ==========

async def health_check(request: web.Request) -> web.Response:
    """Health check endpoint"""
    try:
        # Get handler stats
        stats = await who_handler.get_stats()

        return web.json_response({
            "status": "healthy",
            "stats": stats
        })
    except Exception as e:
        return web.json_response({
            "status": "unhealthy",
            "error": str(e)
        }, status=503)


async def stats_endpoint(request: web.Request) -> web.Response:
    """Statistics endpoint"""
    try:
        stats = await who_handler.get_stats()
        return web.json_response(stats)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def clear_cache_endpoint(request: web.Request) -> web.Response:
    """Clear all caches"""
    try:
        await who_handler.clear_caches()
        return web.json_response({"status": "Caches cleared"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ========== MIDDLEWARE ==========

@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Add CORS headers to responses"""
    response = await handler(request)

    # Add CORS headers
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'

    return response


@web.middleware
async def error_middleware(request: web.Request, handler):
    """Global error handler"""
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:
        print(f"Unhandled error: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({
            "error": "Internal server error",
            "detail": str(e)
        }, status=500)


# ========== APP LIFECYCLE ==========

async def startup(app: web.Application):
    """Initialize handler on startup"""
    print(f"Starting WHO server on {HOST}:{PORT}")
    print("Initializing handler...")

    # Initialize the handler (creates connections, etc.)
    await who_handler.get_handler()

    print("Server ready to accept requests")


async def cleanup(app: web.Application):
    """Cleanup on shutdown"""
    print("Shutting down server...")
    await who_handler.cleanup()
    print("Server shutdown complete")


# ========== APPLICATION SETUP ==========

def create_app() -> web.Application:
    """Create the web application"""
    app = web.Application(middlewares=[error_middleware, cors_middleware])

    # Static pages
    app.router.add_get("/", index_page)
    app.router.add_get("/index.html", index_page)
    app.router.add_get("/evaluation_report.html", evaluation_report)
    app.router.add_get("/docs.html", docs_page)
    app.router.add_get("/architecture.html", architecture_docs)
    app.router.add_get("/retrieval_strategies.html", retrieval_strategies_docs)
    app.router.add_get("/multi_model_evaluation.html", multi_model_evaluation_docs)

    # REST endpoints (support both GET and POST)
    app.router.add_post("/who", who_endpoint)
    app.router.add_get("/who", who_endpoint)
    app.router.add_post("/who-stream", who_stream_endpoint)

    # MCP endpoints
    app.router.add_post("/mcp", mcp_endpoint)

    # Admin endpoints
    app.router.add_get("/health", health_check)
    app.router.add_get("/stats", stats_endpoint)
    app.router.add_post("/clear-cache", clear_cache_endpoint)

    # Lifecycle hooks
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)

    return app


# ========== MAIN ==========

if __name__ == "__main__":
    # Print configuration
    print("=" * 60)
    print("WHO Standalone Server")
    print("=" * 60)
    print(f"Web UI: http://{HOST}:{PORT}/")
    print(f"REST endpoint: POST http://{HOST}:{PORT}/who")
    print(f"MCP endpoint: POST http://{HOST}:{PORT}/mcp")
    print(f"Health check: GET http://{HOST}:{PORT}/health")
    print(f"Statistics: GET http://{HOST}:{PORT}/stats")
    print("=" * 60)

    # Create and run application
    app = create_app()

    # Run the server - let aiohttp handle signals properly
    try:
        web.run_app(
            app,
            host=HOST,
            port=PORT,
            access_log=None,  # Disable access logs for performance
            print=None  # Suppress aiohttp startup message (we have our own)
        )
    except KeyboardInterrupt:
        print("\nServer stopped by user")
    except Exception as e:
        print(f"\nServer error: {e}")
        sys.exit(1)