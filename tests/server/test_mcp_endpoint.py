"""Tests for the /mcp Streamable HTTP endpoint and its agent/team tools."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from openjarvis.agents.manager import AgentManager  # noqa: E402
from openjarvis.mcp import agents_server  # noqa: E402
from openjarvis.server import agent_manager_routes  # noqa: E402
from openjarvis.server.mcp_endpoint import create_mcp_endpoint_router  # noqa: E402

SECRET = "sk-team-secret-do-not-leak"
LOCAL = ("127.0.0.1", 50000)


@pytest.fixture
def manager(tmp_path):
    value = AgentManager(str(tmp_path / "agents.db"))
    yield value
    value.close()


@pytest.fixture
def team(manager):
    team = manager.create_team("Test Media")
    lead = manager.create_agent(
        "Test Lead", config={"api_key": SECRET}, team_id=team["id"]
    )
    writer = manager.create_agent("Test Writer", team_id=team["id"])
    manager.campaigns.set_team_lead(team["id"], lead["id"])
    manager.create_team_connection(
        team["id"],
        name="Test Connection",
        provider="Example",
        url="https://mcp.example.test/mcp",
        token=SECRET,
    )
    return SimpleNamespace(id=team["id"], lead=lead, writer=writer)


def _app(manager, *, allow_remote=False, api_key=""):
    app = FastAPI()
    app.state.agent_manager = manager
    app.state.config = SimpleNamespace(
        server=SimpleNamespace(
            mcp_allow_remote=allow_remote,
            cors_origins=["https://ui.example.test"],
        )
    )
    app.include_router(create_mcp_endpoint_router())
    if api_key:
        from openjarvis.server.auth_middleware import AuthMiddleware

        app.add_middleware(AuthMiddleware, api_key=api_key)
    return app


@pytest.fixture
def client(manager):
    return TestClient(_app(manager), client=LOCAL)


@pytest.fixture
def replies(monkeypatch):
    """Stand in for the agent run: answer each pending message."""
    monkeypatch.setattr(agents_server.AskAgentTool, "poll_seconds", 0.01)
    calls = []

    def fake_tick(manager, app_state, agent_id):
        calls.append(agent_id)

        # Like the real tick: the agent is "running" until its reply is stored.
        manager.start_tick(agent_id)

        def run():
            for message in manager.get_pending_messages(agent_id):
                manager.mark_message_delivered(message["id"])
                manager.store_agent_response(
                    agent_id,
                    f"reply to: {message['content']}",
                    tool_calls=[{"tool": "web_search", "arguments": SECRET}],
                )
            manager.end_tick(agent_id)

        thread = threading.Thread(target=run)
        thread.start()
        return thread

    monkeypatch.setattr(agent_manager_routes, "start_immediate_tick", fake_tick)
    return calls


def rpc(client, method, params=None, id=1, **kwargs):
    body = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, **kwargs)


def call(client, name, **arguments):
    resp = rpc(client, "tools/call", {"name": name, "arguments": arguments})
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert SECRET not in json.dumps(result)
    return result


def payload(result):
    assert not result["isError"], result
    return json.loads(result["content"][0]["text"])


class TestProtocol:
    def test_initialize_negotiates_version(self, client):
        resp = rpc(client, "initialize", {"protocolVersion": "2025-06-18"})
        result = resp.json()["result"]
        assert result["protocolVersion"] == "2025-06-18"
        assert result["serverInfo"]["title"] == "OpenJarvis Agents"
        assert "ask_agent" in result["instructions"]

    def test_unknown_version_gets_latest(self, client):
        result = rpc(client, "initialize", {"protocolVersion": "1999-01-01"}).json()
        assert result["result"]["protocolVersion"] == "2025-11-25"

    def test_notification_is_accepted_without_body(self, client):
        resp = client.post(
            "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        assert resp.status_code == 202
        assert resp.content == b""

    def test_batch_and_ping(self, client):
        resp = client.post(
            "/mcp",
            json=[
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ],
        )
        body = resp.json()
        assert [m["id"] for m in body] == [1, 2]
        assert body[0]["result"] == {}

    def test_parse_error(self, client):
        resp = client.post(
            "/mcp", content=b"{not json", headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32700

    def test_get_and_delete_are_not_streams(self, client):
        assert client.get("/mcp").status_code == 405
        assert client.delete("/mcp").status_code == 405

    def test_only_agent_tools_are_listed(self, client):
        tools = rpc(client, "tools/list").json()["result"]["tools"]
        assert {t["name"] for t in tools} == {
            "list_agents",
            "ask_agent",
            "read_agent_messages",
            "start_team_request",
            "list_team_requests",
            "get_team_request",
            "read_team_request_file",
        }
        by_name = {t["name"]: t for t in tools}
        assert by_name["list_agents"]["annotations"]["readOnlyHint"] is True
        assert by_name["ask_agent"]["annotations"]["readOnlyHint"] is False

    def test_no_manager_is_503(self, manager):
        app = _app(manager)
        app.state.agent_manager = None
        assert rpc(TestClient(app, client=LOCAL), "ping").status_code == 503


class TestAccess:
    def test_remote_clients_refused_by_default(self, manager):
        remote = TestClient(_app(manager), client=("203.0.113.5", 4000))
        assert rpc(remote, "ping").status_code == 403

    def test_remote_clients_allowed_when_configured(self, manager):
        app = _app(manager, allow_remote=True)
        remote = TestClient(app, client=("203.0.113.5", 4000))
        assert rpc(remote, "ping").status_code == 200

    @pytest.mark.parametrize(
        ("origin", "status"),
        [
            ("https://evil.example.com", 403),
            ("http://localhost:5173", 200),
            ("http://127.0.0.1:18765", 200),
            ("https://ui.example.test", 200),
        ],
    )
    def test_origin_check(self, client, origin, status):
        assert rpc(client, "ping", headers={"Origin": origin}).status_code == status

    def test_api_key_required(self, manager):
        client = TestClient(_app(manager, api_key="oj_sk_test"), client=LOCAL)
        assert rpc(client, "ping").status_code == 401
        resp = rpc(client, "ping", headers={"Authorization": "Bearer oj_sk_test"})
        assert resp.status_code == 200

    def test_middleware_guards_mcp_path(self):
        from openjarvis.server.auth_middleware import AuthMiddleware

        assert AuthMiddleware._requires_auth("/mcp")
        assert AuthMiddleware._requires_auth("/mcp/")
        assert not AuthMiddleware._requires_auth("/mcpx")


class TestAgentTools:
    def test_list_agents_hides_config_and_connections(self, client, manager, team):
        loner = manager.create_agent("Test Loner")
        data = payload(call(client, "list_agents"))
        (listed,) = data["teams"]
        assert listed["lead_agent_id"] == team.lead["id"]
        agents = {a["name"]: a for a in listed["agents"]}
        assert agents["Test Lead"]["is_team_lead"] is True
        assert agents["Test Writer"]["is_team_lead"] is False
        assert "config" not in agents["Test Lead"]
        assert "connections" not in listed
        assert [a["id"] for a in data["agents_without_team"]] == [loner["id"]]

    def test_list_one_team(self, client, team):
        data = payload(call(client, "list_agents", team_id=team.id))
        assert len(data["teams"]) == 1
        assert "agents_without_team" not in data
        assert call(client, "list_agents", team_id="nope")["isError"]

    def test_ask_agent_waits_for_reply(self, client, manager, team, replies):
        data = payload(
            call(client, "ask_agent", agent_id=team.writer["id"], message="hi")
        )
        assert data["status"] == "replied"
        assert data["reply"] == "reply to: hi"
        assert replies == [team.writer["id"]]

    def test_ask_team_goes_to_lead(self, client, team, replies):
        data = payload(call(client, "ask_agent", team_id=team.id, message="plan?"))
        assert data["agent_id"] == team.lead["id"]
        assert data["reply"] == "reply to: plan?"

    def test_ask_without_waiting(self, client, manager, team, monkeypatch):
        monkeypatch.setattr(
            agent_manager_routes, "start_immediate_tick", lambda *a: None
        )
        data = payload(
            call(
                client,
                "ask_agent",
                agent_id=team.writer["id"],
                message="later",
                wait_seconds=0,
            )
        )
        assert data["status"] == "queued"
        pending = manager.get_pending_messages(team.writer["id"])
        assert [m["content"] for m in pending] == ["later"]

    def test_ask_reports_error(self, client, manager, team, monkeypatch):
        monkeypatch.setattr(agents_server.AskAgentTool, "poll_seconds", 0.01)

        def failing_tick(mgr, state, agent_id):
            for message in mgr.get_pending_messages(agent_id):
                mgr.mark_message_delivered(message["id"])
            mgr.update_agent(agent_id, status="error")
            mgr.update_summary_memory(agent_id, "ERROR: engine down")

        monkeypatch.setattr(agent_manager_routes, "start_immediate_tick", failing_tick)
        data = payload(
            call(client, "ask_agent", agent_id=team.writer["id"], message="x")
        )
        assert data["status"] == "error"
        assert "engine down" in data["error"]

    @pytest.mark.parametrize(
        "arguments",
        [
            {"message": "hi"},
            {"message": "hi", "agent_id": "a", "team_id": "b"},
            {"message": "", "agent_id": "a"},
            {"message": "hi", "agent_id": "missing"},
        ],
    )
    def test_ask_rejects_bad_arguments(self, client, team, replies, arguments):
        assert call(client, "ask_agent", **arguments)["isError"]
        assert replies == []

    def test_ask_paused_agent(self, client, manager, team, replies):
        manager.pause_agent(team.writer["id"])
        result = call(client, "ask_agent", agent_id=team.writer["id"], message="hi")
        assert result["isError"]

    def test_ask_team_without_lead(self, client, manager, replies):
        lonely = manager.create_team("Test Leaderless")
        assert call(client, "ask_agent", team_id=lonely["id"], message="hi")["isError"]

    def test_read_messages_oldest_first_without_tool_arguments(
        self, client, team, replies
    ):
        call(client, "ask_agent", agent_id=team.writer["id"], message="one")
        data = payload(call(client, "read_agent_messages", agent_id=team.writer["id"]))
        messages = data["messages"]
        assert [m["content"] for m in messages] == ["one", "reply to: one"]
        assert messages[1]["tools_used"] == ["web_search"]
        assert "tool_calls" not in messages[1]


class TestTeamRequests:
    def test_start_and_read_request(self, client, manager, team):
        data = payload(
            call(
                client,
                "start_team_request",
                team_id=team.id,
                title="Test post",
                brief="Write a test post",
                max_runs=4,
            )
        )
        assert data["status"] == "active"
        assert data["max_runs"] == 4
        (handoff,) = data["handoffs"]
        assert handoff["target_agent_id"] == team.lead["id"]
        assert handoff["status"] == "pending"

        listed = payload(call(client, "list_team_requests", team_id=team.id))
        assert [r["id"] for r in listed["requests"]] == [data["id"]]

        folder = manager.campaigns.campaign_dir(team.id, data["id"])
        (folder / "final" / "post.md").write_text("# Done", encoding="utf-8")
        (folder / "final" / "cover.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")

        detail = payload(call(client, "get_team_request", request_id=data["id"]))
        paths = {f["path"] for f in detail["files"]}
        assert {"final/post.md", "final/cover.png"} <= paths

        text = call(
            client,
            "read_team_request_file",
            request_id=data["id"],
            path="final/post.md",
        )
        assert text["content"][0]["text"] == "# Done"
        image = call(
            client,
            "read_team_request_file",
            request_id=data["id"],
            path="final/cover.png",
        )
        assert image["content"][1]["type"] == "image"
        assert image["content"][1]["mimeType"] == "image/png"

    def test_file_outside_request_is_refused(self, client, manager, team, tmp_path):
        data = payload(
            call(client, "start_team_request", team_id=team.id, title="t", brief="b")
        )
        outside = tmp_path / "outside.md"
        outside.write_text("outside-marker-42", encoding="utf-8")
        for path in ("../../../outside.md", str(outside)):
            result = call(
                client, "read_team_request_file", request_id=data["id"], path=path
            )
            assert result["isError"]
            assert "outside-marker-42" not in json.dumps(result)

    def test_request_needs_a_lead(self, client, manager):
        lonely = manager.create_team("Test Leaderless")
        result = call(
            client, "start_team_request", team_id=lonely["id"], title="t", brief="b"
        )
        assert result["isError"]
        assert manager.campaigns.list_campaigns(lonely["id"]) == []

    def test_unknown_request(self, client):
        assert call(client, "get_team_request", request_id="nope")["isError"]


class TestCreateApp:
    def _config(self):
        from openjarvis.core.config import JarvisConfig

        cfg = JarvisConfig()
        cfg.analytics.enabled = False
        cfg.traces.enabled = False
        cfg.security.enabled = False
        return cfg

    def test_mounted_with_agent_manager_behind_auth(self, manager, team):
        from unittest.mock import MagicMock

        from openjarvis.server.app import create_app

        app = create_app(
            MagicMock(),
            "test-model",
            config=self._config(),
            agent_manager=manager,
            api_key="oj_sk_test",
        )
        client = TestClient(app, client=LOCAL)
        headers = {"Authorization": "Bearer oj_sk_test"}
        assert rpc(client, "tools/list").status_code == 401
        assert client.get("/mcp", headers=headers).status_code == 405
        tools = rpc(client, "tools/list", headers=headers).json()["result"]["tools"]
        assert "ask_agent" in {t["name"] for t in tools}
        data = payload(
            rpc(
                client,
                "tools/call",
                {"name": "list_agents", "arguments": {}},
                headers=headers,
            ).json()["result"]
        )
        assert data["teams"][0]["id"] == team.id

    def test_not_mounted_without_agent_manager(self):
        from unittest.mock import MagicMock

        from openjarvis.server.app import create_app

        app = create_app(MagicMock(), "test-model", config=self._config())
        assert "/mcp" not in {getattr(r, "path", "") for r in app.routes}
