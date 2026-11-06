"""MCP tools that let outside clients talk to managed agents and teams.

Served over Streamable HTTP at ``/mcp`` (see ``server/mcp_endpoint.py``).
Every tool goes through the agent manager the same way the UI does: a
message is stored for the agent and the agent runs with its own tools, so
team MCP connections, their secrets and team isolation never leave the
server. Nothing here returns agent config, connection settings or
tool-call arguments.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import time
from typing import Any, Callable, Dict, List, Optional

from openjarvis.core.types import ToolResult
from openjarvis.mcp.server import MCPServer
from openjarvis.tools._stubs import BaseTool, ToolSpec

MAX_WAIT_SECONDS = 600
DEFAULT_WAIT_SECONDS = 120
_MAX_TEXT_FILE = 200_000
_MAX_IMAGE_FILE = 5_000_000
_TEXT_SUFFIXES = {".md", ".txt", ".json", ".html", ".csv", ".yaml", ".yml"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False}
# Agents may act on the outside world with their own tools.
_DELEGATES = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}

INSTRUCTIONS = (
    "Talk to the OpenJarvis managed agents and teams on this machine. "
    "Use list_agents first to find agent and team ids. ask_agent sends a "
    "message to one agent (or a team's lead) and waits for its reply; the "
    "agent works with its own tools and permissions. start_team_request hands "
    "a larger job to a team's lead, who splits it across the team; follow it "
    "with get_team_request and read_team_request_file."
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _ok(value: Any, name: str) -> ToolResult:
    return ToolResult(tool_name=name, content=_json(value))


def _fail(message: str, name: str) -> ToolResult:
    return ToolResult(tool_name=name, content=message, success=False)


def _agent_summary(agent: Dict[str, Any], lead_ids: set) -> Dict[str, Any]:
    return {
        "id": agent["id"],
        "name": agent["name"],
        "agent_type": agent["agent_type"],
        "team_id": agent["team_id"],
        "status": agent["status"],
        "current_activity": agent["current_activity"],
        "last_run_at": agent["last_run_at"],
        "is_team_lead": agent["id"] in lead_ids,
    }


def _message_view(message: Dict[str, Any]) -> Dict[str, Any]:
    view = {
        "id": message["id"],
        "direction": message["direction"],
        "content": message["content"],
        "status": message["status"],
        "created_at": message["created_at"],
    }
    # Tool names only: arguments and results can hold connection data.
    if message.get("tool_calls"):
        view["tools_used"] = [
            str(call.get("tool") or call.get("name") or "")
            for call in message["tool_calls"]
            if isinstance(call, dict)
        ]
    return view


class _AgentTool(BaseTool):
    """Base for tools bound to one agent manager and server state."""

    is_local = True
    name = ""
    description = ""
    parameters: Dict[str, Any] = {}
    annotations: Dict[str, Any] = _READ_ONLY
    timeout_seconds = 30.0

    def __init__(self, manager: Any, app_state: Any = None) -> None:
        self._manager = manager
        self._app_state = app_state
        self.tool_id = self.name

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
            category="agents",
            timeout_seconds=self.timeout_seconds,
            metadata={"annotations": self.annotations},
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            return self.run(**params)
        except (KeyError, ValueError, TypeError) as exc:
            return _fail(str(exc).strip("'\"") or type(exc).__name__, self.name)

    def run(self, **params: Any) -> ToolResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def _agent(self, agent_id: Any) -> Dict[str, Any]:
        agent = self._manager.get_agent(str(agent_id or ""))
        if agent is None or agent["status"] == "archived":
            raise KeyError(f"Agent not found: {agent_id}")
        return agent

    def _team(self, team_id: Any) -> Dict[str, Any]:
        team = self._manager.get_team(str(team_id or ""))
        if team is None:
            raise KeyError(f"Team not found: {team_id}")
        return team

    def _campaign(self, request_id: Any) -> Dict[str, Any]:
        campaign = self._manager.campaigns.get_campaign(str(request_id or ""))
        if campaign is None:
            raise KeyError(f"Team request not found: {request_id}")
        return campaign


class ListAgentsTool(_AgentTool):
    name = "list_agents"
    description = (
        "List the OpenJarvis teams and managed agents: ids, names, status, "
        "which agent leads each team, and agents that belong to no team."
    )
    parameters = {
        "type": "object",
        "properties": {
            "team_id": {
                "type": "string",
                "description": "Only this team. Omit for every team.",
            }
        },
    }

    def run(self, team_id: Optional[str] = None) -> ToolResult:
        all_teams = self._manager.list_teams()
        teams = [self._team(team_id)] if team_id else all_teams
        leads = {t["lead_agent_id"] for t in all_teams} - {None}
        agents = self._manager.list_agents()
        result: Dict[str, Any] = {
            "teams": [
                {
                    "id": team["id"],
                    "name": team["name"],
                    "lead_agent_id": team["lead_agent_id"],
                    "agents": [
                        _agent_summary(a, leads)
                        for a in agents
                        if a["team_id"] == team["id"]
                    ],
                }
                for team in teams
            ]
        }
        if not team_id:
            result["agents_without_team"] = [
                _agent_summary(a, leads) for a in agents if not a["team_id"]
            ]
        return _ok(result, self.name)


class AskAgentTool(_AgentTool):
    name = "ask_agent"
    description = (
        "Send a message to one managed agent, or to a team's lead, and wait "
        "for its reply. The agent answers with its own tools and permissions. "
        "If it is still working when the wait ends, the result says so; read "
        "the reply later with read_agent_messages."
    )
    parameters = {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "The agent to ask."},
            "team_id": {
                "type": "string",
                "description": "Ask this team's lead instead of naming an agent.",
            },
            "message": {"type": "string", "description": "What to ask or hand over."},
            "wait_seconds": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_WAIT_SECONDS,
                "default": DEFAULT_WAIT_SECONDS,
                "description": "How long to wait for the reply (0 = don't wait).",
            },
        },
        "required": ["message"],
    }
    annotations = _DELEGATES
    timeout_seconds = MAX_WAIT_SECONDS + 30.0
    poll_seconds = 1.0
    # Re-trigger a tick when the agent is idle but our message is still queued.
    rekick_seconds = 5.0

    def __init__(
        self,
        manager: Any,
        app_state: Any = None,
        *,
        start_tick: Optional[Callable[[Any, Any, str], Any]] = None,
    ) -> None:
        super().__init__(manager, app_state)
        if start_tick is None:
            from openjarvis.server.agent_manager_routes import start_immediate_tick

            start_tick = start_immediate_tick
        self._start_tick = start_tick

    def run(
        self,
        message: str = "",
        agent_id: Optional[str] = None,
        team_id: Optional[str] = None,
        wait_seconds: int = DEFAULT_WAIT_SECONDS,
    ) -> ToolResult:
        message = (message or "").strip()
        if not message:
            return _fail("message is required.", self.name)
        if bool(agent_id) == bool(team_id):
            return _fail("Give exactly one of agent_id or team_id.", self.name)
        if team_id:
            team = self._team(team_id)
            if not team["lead_agent_id"]:
                return _fail(
                    f"Team {team['name']} has no lead; set one in team settings "
                    "or ask an agent by agent_id.",
                    self.name,
                )
            agent_id = team["lead_agent_id"]
        agent = self._agent(agent_id)
        if agent["status"] == "paused":
            return _fail(f"Agent {agent['name']} is paused.", self.name)
        if agent["status"] in ("error", "needs_attention"):
            self._manager.update_agent(agent["id"], status="idle")

        sent = self._manager.send_message(agent["id"], message, mode="immediate")
        self._start_tick(self._manager, self._app_state, agent["id"])
        wait = max(0, min(int(wait_seconds), MAX_WAIT_SECONDS))
        return _ok(self._wait(agent, sent, time.monotonic() + wait), self.name)

    def _wait(
        self, agent: Dict[str, Any], sent: Dict[str, Any], deadline: float
    ) -> Dict[str, Any]:
        base = {"agent_id": agent["id"], "agent_name": agent["name"]}
        base["message_id"] = sent["id"]
        last_kick = time.monotonic()
        while True:
            current = self._manager.get_agent(agent["id"]) or {}
            messages = self._manager.list_messages(agent["id"], limit=200)
            ours = next((m for m in messages if m["id"] == sent["id"]), None)
            running = current.get("status") == "running"
            if ours is not None and ours["status"] != "pending" and not running:
                replies = [
                    m
                    for m in messages
                    if m["direction"] == "agent_to_user"
                    and m["created_at"] >= sent["created_at"]
                ]
                if replies:
                    reply = max(replies, key=lambda m: m["created_at"])
                    return {**base, "status": "replied", "reply": reply["content"]}
                if current.get("status") == "error":
                    error = (current.get("summary_memory") or "")[-500:]
                    return {**base, "status": "error", "error": error}
                return {**base, "status": "no_reply"}
            now = time.monotonic()
            if now >= deadline:
                return {
                    **base,
                    "status": "working" if running else "queued",
                    "note": "Still working. Call read_agent_messages later.",
                }
            if not running and now - last_kick >= self.rekick_seconds:
                self._start_tick(self._manager, self._app_state, agent["id"])
                last_kick = now
            time.sleep(min(self.poll_seconds, max(0.0, deadline - now)))


class ReadAgentMessagesTool(_AgentTool):
    name = "read_agent_messages"
    description = (
        "Read an agent's recent conversation (your messages, its replies and "
        "messages from other agents), oldest first."
    )
    parameters = {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        },
        "required": ["agent_id"],
    }

    def run(self, agent_id: str = "", limit: int = 20) -> ToolResult:
        agent = self._agent(agent_id)
        limit = max(1, min(int(limit), 100))
        messages = self._manager.list_messages(agent["id"], limit=limit)
        return _ok(
            {
                "agent_id": agent["id"],
                "agent_name": agent["name"],
                "status": agent["status"],
                "current_activity": agent["current_activity"],
                "messages": [_message_view(m) for m in reversed(messages)],
            },
            self.name,
        )


def _request_view(manager: Any, campaign: Dict[str, Any], detail: bool) -> Dict:
    from openjarvis.server.team_campaign_routes import campaign_view

    value = campaign_view(manager, campaign, detail=detail)
    keep = (
        "id",
        "team_id",
        "title",
        "brief",
        "status",
        "display_status",
        "status_reason",
        "lead_agent_id",
        "max_runs",
        "runs_used",
        "open_handoffs",
        "attention_count",
        "created_at",
        "updated_at",
        "files",
    )
    view = {key: value[key] for key in keep if key in value}
    if detail:
        view["handoffs"] = [
            {
                key: handoff.get(key)
                for key in (
                    "id",
                    "kind",
                    "status",
                    "status_reason",
                    "title",
                    "source_name",
                    "target_name",
                    "target_agent_id",
                    "files",
                    "result",
                    "updated_at",
                )
            }
            for handoff in value.get("handoffs", [])
        ]
    return view


class StartTeamRequestTool(_AgentTool):
    name = "start_team_request"
    description = (
        "Hand a job to a team. The team's lead receives the brief, splits it "
        "into tasks for team members and collects the results and files. "
        "Returns the request id; check progress with get_team_request."
    )
    parameters = {
        "type": "object",
        "properties": {
            "team_id": {"type": "string"},
            "title": {"type": "string", "maxLength": 120},
            "brief": {"type": "string", "description": "Everything the team needs."},
            "max_runs": {
                "type": "integer",
                "minimum": 1,
                "maximum": 60,
                "default": 16,
                "description": "Most agent runs this request may use.",
            },
        },
        "required": ["team_id", "title", "brief"],
    }
    annotations = _DELEGATES

    def run(
        self, team_id: str = "", title: str = "", brief: str = "", max_runs: int = 16
    ) -> ToolResult:
        from openjarvis.agents.campaigns import HandoffRejected

        team = self._team(team_id)
        store = self._manager.campaigns
        try:
            campaign = store.create_campaign(
                team["id"], title=title, brief=brief, max_runs=int(max_runs)
            )
        except HandoffRejected as exc:
            return _fail(str(exc), self.name)
        try:
            store.add_request(campaign["id"], content=campaign["brief"], files=[])
        except HandoffRejected as exc:
            store.set_campaign_status(campaign["id"], "failed", str(exc))
            return _fail(str(exc), self.name)
        return _ok(
            _request_view(self._manager, store.get_campaign(campaign["id"]), True),
            self.name,
        )


class ListTeamRequestsTool(_AgentTool):
    name = "list_team_requests"
    description = "List a team's requests, newest first, with their status."
    parameters = {
        "type": "object",
        "properties": {"team_id": {"type": "string"}},
        "required": ["team_id"],
    }

    def run(self, team_id: str = "") -> ToolResult:
        team = self._team(team_id)
        campaigns = self._manager.campaigns.list_campaigns(team["id"])
        return _ok(
            {"requests": [_request_view(self._manager, c, False) for c in campaigns]},
            self.name,
        )


class GetTeamRequestTool(_AgentTool):
    name = "get_team_request"
    description = (
        "Read a team request: status, each handoff between agents with its "
        "result, and the files the team produced."
    )
    parameters = {
        "type": "object",
        "properties": {"request_id": {"type": "string"}},
        "required": ["request_id"],
    }

    def run(self, request_id: str = "") -> ToolResult:
        campaign = self._campaign(request_id)
        return _ok(_request_view(self._manager, campaign, True), self.name)


class ReadTeamRequestFileTool(_AgentTool):
    name = "read_team_request_file"
    description = (
        "Read one file from a team request's folder (a path from "
        "get_team_request's files). Text comes back as text, images as images."
    )
    parameters = {
        "type": "object",
        "properties": {
            "request_id": {"type": "string"},
            "path": {"type": "string", "description": "e.g. final/post.md"},
        },
        "required": ["request_id", "path"],
    }

    def run(self, request_id: str = "", path: str = "") -> ToolResult:
        from openjarvis.agents.campaigns import HandoffRejected

        campaign = self._campaign(request_id)
        try:
            file = self._manager.campaigns.resolve_campaign_file(campaign, path)
        except HandoffRejected as exc:
            return _fail(str(exc), self.name)
        suffix = file.suffix.lower()
        size = file.stat().st_size
        if suffix in _TEXT_SUFFIXES:
            if size > _MAX_TEXT_FILE:
                return _fail(
                    f"{path} is larger than {_MAX_TEXT_FILE} bytes.", self.name
                )
            return ToolResult(
                tool_name=self.name,
                content=file.read_text(encoding="utf-8", errors="replace"),
            )
        if suffix in _IMAGE_SUFFIXES:
            if size > _MAX_IMAGE_FILE:
                return _fail(
                    f"{path} is larger than {_MAX_IMAGE_FILE} bytes.", self.name
                )
            mime = mimetypes.guess_type(file.name)[0] or "image/png"
            data = base64.b64encode(file.read_bytes()).decode("ascii")
            return ToolResult(
                tool_name=self.name,
                content=f"[image {path}, {size} bytes]",
                metadata={
                    "mcp_content": [
                        {"type": "text", "text": f"{path} ({mime}, {size} bytes)"},
                        {"type": "image", "data": data, "mimeType": mime},
                    ]
                },
            )
        return _fail(f"{path}: this file type can't be read here.", self.name)


TOOL_CLASSES = (
    ListAgentsTool,
    AskAgentTool,
    ReadAgentMessagesTool,
    StartTeamRequestTool,
    ListTeamRequestsTool,
    GetTeamRequestTool,
    ReadTeamRequestFileTool,
)


def create_agents_mcp_server(
    manager: Any,
    app_state: Any = None,
    *,
    bus: Any = None,
    capability_policy: Any = None,
    rate_limiter: Any = None,
) -> MCPServer:
    """An :class:`MCPServer` exposing only the agent and team tools."""
    tools: List[BaseTool] = [cls(manager, app_state) for cls in TOOL_CLASSES]
    return MCPServer(
        tools,
        bus=bus,
        capability_policy=capability_policy,
        rate_limiter=rate_limiter,
        agent_id="mcp-agents",
        title="OpenJarvis Agents",
        instructions=INSTRUCTIONS,
    )


__all__ = ["TOOL_CLASSES", "create_agents_mcp_server"]
