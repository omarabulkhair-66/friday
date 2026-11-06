"""Streamable HTTP MCP endpoint at ``/mcp`` for the managed agents.

Outside MCP clients (Claude Code, other agents) use it to list teams and
agents, ask them questions and hand teams work. The tools themselves live in
:mod:`openjarvis.mcp.agents_server`.

The endpoint is stateless and answers every POST with ``application/json``;
it never opens a server-to-client stream. ``AuthMiddleware`` requires the API
key when one is configured. On top of that it is local-only by default:
requests from non-loopback clients, and browser requests from other origins,
are refused unless ``[server] mcp_allow_remote = true``.
"""

from __future__ import annotations

import ipaddress
import json
import logging
from typing import Any, List
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from openjarvis.mcp.protocol import (
    INVALID_REQUEST,
    PARSE_ERROR,
    MCPRequest,
    MCPResponse,
)

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"


def _is_loopback(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _allow_remote(request: Request) -> bool:
    config = getattr(request.app.state, "config", None)
    server = getattr(config, "server", None)
    return bool(getattr(server, "mcp_allow_remote", False))


def _origin_allowed(request: Request) -> bool:
    """Refuse cross-site browser requests (DNS rebinding, CSRF).

    Non-browser clients send no ``Origin``. Browsers may only call from a
    loopback origin or one listed in ``[server] cors_origins``.
    """
    origin = request.headers.get("origin")
    if not origin:
        return True
    if _is_loopback(urlsplit(origin).hostname or ""):
        return True
    config = getattr(request.app.state, "config", None)
    allowed = getattr(getattr(config, "server", None), "cors_origins", None) or []
    return origin in allowed


def _rpc_error(code: int, message: str, status: int) -> JSONResponse:
    body = {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}
    return JSONResponse(body, status_code=status)


def create_mcp_endpoint_router() -> APIRouter:
    router = APIRouter(tags=["mcp"])

    @router.post(MCP_PATH)
    async def mcp_post(request: Request):
        client_host = request.client.host if request.client else ""
        if not _allow_remote(request) and not _is_loopback(client_host):
            return JSONResponse(
                {"detail": "The MCP endpoint only accepts local clients."},
                status_code=403,
            )
        if not _origin_allowed(request):
            return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
        manager = getattr(request.app.state, "agent_manager", None)
        if manager is None:
            return JSONResponse(
                {"detail": "The agent manager is not running."}, status_code=503
            )

        try:
            payload = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            return _rpc_error(PARSE_ERROR, "Parse error", 400)
        batch = isinstance(payload, list)
        messages = payload if batch else [payload]
        if not messages or not all(isinstance(m, dict) for m in messages):
            return _rpc_error(INVALID_REQUEST, "Invalid request", 400)

        from openjarvis.mcp.agents_server import create_agents_mcp_server

        state = request.app.state
        # A fresh server per request: the endpoint keeps no session state.
        server = create_agents_mcp_server(
            manager,
            state,
            bus=getattr(state, "bus", None),
            capability_policy=getattr(state, "capability_policy", None),
            rate_limiter=getattr(state, "rate_limiter", None),
        )
        responses: List[Any] = []
        for message in messages:
            method = message.get("method")
            if not isinstance(method, str) or "id" not in message:
                # Notifications and client responses get no reply.
                continue
            params = message.get("params") or {}
            if not isinstance(params, dict):
                params = {}
            rpc = MCPRequest(method=method, params=params, id=message["id"])
            try:
                result = await run_in_threadpool(server.handle, rpc)
            except Exception:
                logger.exception("MCP request %s failed", method)
                result = MCPResponse.error_response(
                    message["id"], -32603, "Internal error"
                )
            responses.append(json.loads(result.to_json()))

        if not responses:
            return Response(status_code=202)
        return JSONResponse(responses if batch else responses[0])

    @router.get(MCP_PATH)
    @router.delete(MCP_PATH)
    async def mcp_no_stream():
        # No server-initiated stream and no sessions to end.
        return Response(status_code=405, headers={"Allow": "POST"})

    return router


__all__ = ["MCP_PATH", "create_mcp_endpoint_router"]
