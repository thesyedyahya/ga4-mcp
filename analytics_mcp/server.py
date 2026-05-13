#!/usr/bin/env python

# Copyright 2025 Google LLC All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Entry point for the Google Analytics MCP server.

Supports three transports selected via the MCP_TRANSPORT env var:

  * stdio (default) — for local Claude Desktop / Code via uvx/pipx
  * sse              — Server-Sent Events at /sse + /messages/ (legacy clients)
  * streamable-http  — single bidirectional endpoint at /mcp (recommended for
                       remote/web clients; matches the Claude.ai connector spec)

When transport is sse or streamable-http, the server adds:
  * CORS — wildcard allow_origins by default (set MCP_ALLOWED_ORIGINS to restrict)
  * Token auth — required if MCP_AUTH_TOKEN is set. Accepted via any of:
        ?token=<value>
        Authorization: Bearer <value>
        X-Api-Key: <value>
        Mcp-Auth-Token: <value>
  * No-compress override on /mcp, /sse, /messages — prevents reverse proxies
    (Traefik, nginx) from compressing SSE-formatted responses, which would
    buffer chunks and visibly slow browser-based clients (Claude.ai shows
    "A bit longer...").
  * /healthz unauthenticated GET — for Dokploy/k8s liveness probes.

This applies the same hardening pattern used in companion MCP servers so all
deploy and authenticate the same way.
"""

import asyncio
import contextlib
import logging
import os
import traceback

# Load .env from CWD or the script directory before any os.environ lookups so
# Dokploy / local docker compose pick up the same file. Silent if python-dotenv
# isn't installed (uvx installs without optional extras sometimes).
try:
    from dotenv import load_dotenv
    _SCRIPT_DIR_FOR_ENV = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(os.getcwd(), ".env"))
    load_dotenv(os.path.join(_SCRIPT_DIR_FOR_ENV, ".env"))
    load_dotenv(os.path.join(os.path.dirname(_SCRIPT_DIR_FOR_ENV), ".env"))
except ImportError:
    pass

import analytics_mcp.coordinator as coordinator
from mcp.server.lowlevel import NotificationOptions
from mcp.server.models import InitializationOptions
import mcp.server.stdio
import mcp.server


def _init_options():
    return InitializationOptions(
        server_name=coordinator.app.name,
        server_version="1.0.0",
        capabilities=coordinator.app.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        ),
    )


async def run_stdio_async():
    """Runs the MCP server over standard I/O."""
    print("Starting MCP Stdio Server:", coordinator.app.name)
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await coordinator.app.run(read_stream, write_stream, _init_options())


# =============================================================================
# Shared HTTP scaffolding (token auth, CORS, no-compress, healthz)
# =============================================================================

def _make_token_auth_middleware():
    """Returns a Starlette BaseHTTPMiddleware that enforces MCP_AUTH_TOKEN.

    OPTIONS preflight always passes through so CORS can answer browser clients.
    The token can be supplied four ways (URL ?token=, Authorization: Bearer,
    X-Api-Key, Mcp-Auth-Token). /healthz is always open."""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    expected_token = os.environ.get("MCP_AUTH_TOKEN", "").strip()

    class TokenAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.method == "OPTIONS":
                return await call_next(request)
            path = request.url.path
            if not expected_token or path == "/healthz":
                return await call_next(request)
            supplied = (
                request.query_params.get("token")
                or (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
                or (request.headers.get("x-api-key") or "").strip()
                or (request.headers.get("mcp-auth-token") or "").strip()
            )
            if supplied != expected_token:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    return TokenAuthMiddleware


def _make_no_compress_middleware():
    """Returns a middleware that sets Content-Encoding: identity on all /mcp,
    /sse, and /messages responses. Browser MCP clients (Claude.ai's connector)
    parse text/event-stream strictly and fail when the body is compressed.
    Mirroring this behaviour with the working Odoo MCP is what makes the
    connector handshake actually succeed."""
    from starlette.middleware.base import BaseHTTPMiddleware

    class NoCompressMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            resp = await call_next(request)
            p = request.url.path
            if p.startswith("/mcp") or p.startswith("/sse") or p.startswith("/messages"):
                resp.headers["Content-Encoding"] = "identity"
                resp.headers["X-Content-Encoding-Override"] = "no-compress"
            return resp

    return NoCompressMiddleware


def _cors_origins():
    """Return the allow_origins list for CORSMiddleware.

    Defaults to wildcard '*' because the public deployment is already gated by
    the auth token. Override via MCP_ALLOWED_ORIGINS (comma-separated) to
    restrict."""
    raw = os.environ.get("MCP_ALLOWED_ORIGINS", "*").strip()
    if not raw or raw == "*":
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


def _healthz_route():
    from starlette.routing import Route
    from starlette.responses import JSONResponse

    async def healthz(_request):
        return JSONResponse({"status": "ok", "service": "ga4-mcp"})

    return Route("/healthz", healthz, methods=["GET"])


# =============================================================================
# SSE transport (legacy)
# =============================================================================

def run_sse():
    """Runs the MCP server as a Starlette ASGI app over SSE."""
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.middleware.cors import CORSMiddleware
    from starlette.routing import Mount, Route
    from starlette.responses import Response, JSONResponse
    import uvicorn

    sse = SseServerTransport("/messages/")
    expected_token = os.environ.get("MCP_AUTH_TOKEN", "").strip()

    def _authorized(request) -> bool:
        if not expected_token:
            return True
        supplied = (
            request.query_params.get("token")
            or (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
            or (request.headers.get("x-api-key") or "").strip()
            or (request.headers.get("mcp-auth-token") or "").strip()
        )
        return supplied == expected_token

    async def handle_sse(request):
        if request.method == "OPTIONS":
            return Response(status_code=200)
        if not _authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as (read_stream, write_stream):
            await coordinator.app.run(read_stream, write_stream, _init_options())
        return Response()

    async def handle_messages(scope, receive, send):
        # SseServerTransport's POST handler doesn't know about our token
        # wrapper, so we check here before delegating.
        from starlette.requests import Request as _Req
        req = _Req(scope, receive=receive)
        if req.method == "OPTIONS":
            await Response(status_code=200)(scope, receive, send)
            return
        if not _authorized(req):
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await sse.handle_post_message(scope, receive, send)

    app = Starlette(
        debug=False,
        routes=[
            _healthz_route(),
            Route("/sse", endpoint=handle_sse, methods=["GET", "OPTIONS"]),
            Mount("/messages/", app=handle_messages),
        ],
    )

    NoCompress = _make_no_compress_middleware()
    app.add_middleware(NoCompress)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id", "Content-Type"],
        max_age=3600,
    )

    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8080"))
    print(f"Starting MCP SSE Server on http://{host}:{port}/sse")
    uvicorn.run(app, host=host, port=port, log_level="info")


# =============================================================================
# Streamable-HTTP transport (recommended for remote/web clients)
# =============================================================================

class _PathRewriteASGI:
    """Pure-ASGI middleware that rewrites bare /mcp → /mcp/ BEFORE Starlette
    routes the request.

    Without this, Starlette's Mount("/mcp", X) regex requires the trailing
    slash and falls through to a 307 redirect to /mcp/. Worse, when Traefik
    is in front it terminates HTTPS but doesn't always rewrite the Location
    header back to https, so Claude.ai's connector receives a redirect to
    http:// and rejects it with "Couldn't reach the MCP server".

    Rewriting in pure ASGI (rather than BaseHTTPMiddleware) is necessary
    because the rewrite must happen before the Starlette router sees the
    request, and BaseHTTPMiddleware processes responses, not scopes."""
    def __init__(self, app, path_to_rewrite="/mcp"):
        self.app = app
        self.target = path_to_rewrite
        self.target_slash = path_to_rewrite + "/"

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == self.target:
            scope = dict(scope)
            scope["path"] = self.target_slash
            scope["raw_path"] = self.target_slash.encode()
        await self.app(scope, receive, send)


def run_streamable_http():
    """Runs the MCP server using the streamable-http transport at /mcp.

    Architecture:
      uvicorn (proxy_headers=True so X-Forwarded-Proto is respected)
        → _PathRewriteASGI (bare /mcp → /mcp/)
        → CORS → TokenAuth → NoCompress middleware
        → Starlette router
          → Route /healthz (unauthenticated)
          → Mount /mcp (delegates to StreamableHTTPSessionManager)"""
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.middleware.cors import CORSMiddleware
    from starlette.routing import Mount
    import uvicorn

    session_manager = StreamableHTTPSessionManager(
        app=coordinator.app,
        event_store=None,
        json_response=False,
        stateless=False,
    )

    async def handle_streamable_http(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with session_manager.run():
            logging.info("[startup] streamable-http session manager running")
            yield

    app = Starlette(
        debug=False,
        routes=[
            _healthz_route(),
            Mount("/mcp", app=handle_streamable_http),
        ],
        lifespan=lifespan,
    )

    # Middleware order: CORS outermost (handles preflight before anything else),
    # then TokenAuth, then NoCompress innermost.
    NoCompress = _make_no_compress_middleware()
    TokenAuth = _make_token_auth_middleware()
    app.add_middleware(NoCompress)
    app.add_middleware(TokenAuth)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id", "Content-Type"],
        max_age=3600,
    )

    # Wrap with pure-ASGI path rewriter — this MUST be the outermost layer so
    # the rewrite happens before Starlette's router sees the scope.
    wrapped = _PathRewriteASGI(app, path_to_rewrite="/mcp")

    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8080"))
    print(f"Starting MCP Streamable-HTTP Server on http://{host}:{port}/mcp")
    # proxy_headers=True makes uvicorn trust X-Forwarded-Proto from Traefik so
    # any redirect Location headers come back as https://, not http://.
    uvicorn.run(
        wrapped,
        host=host,
        port=port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


# =============================================================================
# Entry-point dispatch
# =============================================================================

def run_server():
    """Entry point — dispatches to stdio, SSE, or streamable-http based on
    MCP_TRANSPORT."""
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    if transport == "stdio":
        asyncio.run(run_stdio_async())
    elif transport == "sse":
        run_sse()
    elif transport in {"streamable-http", "http", "streamable_http"}:
        run_streamable_http()
    else:
        raise SystemExit(
            f"Unknown MCP_TRANSPORT '{transport}'. "
            "Use 'stdio' (default), 'sse', or 'streamable-http'."
        )


if __name__ == "__main__":
    try:
        run_server()
    except KeyboardInterrupt:
        print("\nMCP Server stopped by user.")
    except Exception:
        print("MCP Server encountered an error:")
        traceback.print_exc()
    finally:
        print("MCP Server process exiting.")
