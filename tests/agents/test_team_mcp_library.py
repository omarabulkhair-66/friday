"""Team MCP connections from the library: OAuth and stdio, isolated per team.

Uses the local fake OAuth/MCP server from the global OAuth tests and a tiny
stdio MCP server written by the test; nothing external is contacted.
"""

from __future__ import annotations

import sys
import textwrap
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from openjarvis.agents.manager import AgentManager  # noqa: E402
from openjarvis.agents.team_tools import discover_team_connections  # noqa: E402
from openjarvis.mcp.store import MCPServerStore  # noqa: E402
from openjarvis.server.agent_manager_routes import (  # noqa: E402
    create_agent_manager_router,
)
from openjarvis.server.mcp_routes import create_mcp_router  # noqa: E402
from tests.server.test_mcp_oauth import _serve  # noqa: E402

CLIENT_SECRET = "team-client-secret-551"


@pytest.fixture
def auth_server():
    fake, server = _serve(dcr=True)
    yield fake
    server.shutdown()


@pytest.fixture
def env(tmp_path):
    manager = AgentManager(str(tmp_path / "agents.db"))
    config = SimpleNamespace(
        tools=SimpleNamespace(
            mcp=SimpleNamespace(enabled=True, servers="", oauth_redirect_uri="")
        ),
        _config_dir=tmp_path,
    )
    global_store = MCPServerStore(tmp_path / "mcp")
    app = FastAPI()
    app.state.config = config
    app.include_router(create_mcp_router(config=config, store=global_store))
    for router in create_agent_manager_router(manager):
        app.include_router(router)
    team_a = manager.create_team("Team A")
    team_b = manager.create_team("Team B")
    agent_a = manager.create_agent("Writer A", team_id=team_a["id"])
    other_a = manager.create_agent("Designer A", team_id=team_a["id"])
    agent_b = manager.create_agent("Writer B", team_id=team_b["id"])
    yield SimpleNamespace(
        client=TestClient(app),
        manager=manager,
        global_store=global_store,
        tmp=tmp_path,
        team_a=team_a,
        team_b=team_b,
        agent_a=agent_a,
        other_a=other_a,
        agent_b=agent_b,
    )
    manager.close()


def _base(team):
    return f"/v1/managed-agents/teams/{team['id']}/connections"


def _add_oauth(env, team, fake, **settings):
    res = env.client.post(
        _base(team),
        json={
            "name": "Mail",
            "provider": "Mail",
            "url": f"{fake.base}/mcp",
            "auth_type": "oauth",
            "settings": {"catalog_id": "gmail", **settings},
        },
    )
    assert res.status_code == 201, res.text
    return res.json()


def _sign_in(env, team, connection):
    res = env.client.post(f"{_base(team)}/{connection['id']}/oauth/start")
    assert res.status_code == 200, res.text
    auth_url = res.json()["authorization_url"]
    redirect = httpx.get(auth_url, follow_redirects=False).headers["location"]
    target = urlparse(redirect)
    # One redirect URI for global servers and every team.
    assert target.path == "/v1/mcp/oauth/callback"
    return env.client.get(f"{target.path}?{target.query}")


def _close(clients):
    for client in clients:
        client.close()


def test_team_oauth_tokens_stay_with_that_team(env, auth_server):
    conn_a = _add_oauth(env, env.team_a, auth_server)
    conn_b = _add_oauth(env, env.team_b, auth_server)
    assert conn_a["oauth_status"] == "signed_out"
    assert conn_a["status"] == "needs_authentication"

    page = _sign_in(env, env.team_a, conn_a)
    assert page.status_code == 200 and "Signed in" in page.text
    assert "access-" not in page.text

    listing_a = env.client.get(_base(env.team_a))
    listing_b = env.client.get(_base(env.team_b))
    assert listing_a.json()["connections"][0]["oauth_status"] == "connected"
    assert listing_b.json()["connections"][0]["oauth_status"] == "signed_out"
    for text in (listing_a.text, listing_b.text):
        assert "access-" not in text and "refresh-" not in text

    # Stored for A's connection only: not B's, not the global store.
    tokens = env.manager.team_connection_oauth_tokens(env.team_a["id"], conn_a["id"])
    assert tokens["access_token"].startswith("access-")
    assert (
        env.manager.team_connection_oauth_tokens(env.team_b["id"], conn_b["id"]) is None
    )
    assert (
        env.manager.team_connection_oauth_tokens(env.team_b["id"], conn_a["id"]) is None
    )
    assert not (env.tmp / "mcp" / "secrets.json").exists()
    assert env.global_store.list_servers() == []

    tools_a, clients_a = discover_team_connections(env.manager, env.agent_a["id"])
    tools_b, clients_b = discover_team_connections(env.manager, env.agent_b["id"])
    assert [t._tool.spec.name for t in tools_a] == ["inbox"]
    assert tools_b == []
    _close(clients_a + clients_b)

    verify = env.client.post(f"{_base(env.team_a)}/{conn_a['id']}/verify")
    assert verify.status_code == 200, verify.text
    assert verify.json()["status"] == "verified"
    assert "access-" not in verify.text

    signed_out = env.client.post(f"{_base(env.team_a)}/{conn_a['id']}/oauth/sign-out")
    assert signed_out.json()["oauth_status"] == "signed_out"
    tools_a, clients_a = discover_team_connections(env.manager, env.agent_a["id"])
    assert tools_a == []


def test_a_team_cannot_reach_another_teams_connection(env, auth_server):
    conn_b = _add_oauth(env, env.team_b, auth_server)
    for path in ("oauth/start", "oauth/sign-out", "verify"):
        res = env.client.post(f"{_base(env.team_a)}/{conn_b['id']}/{path}")
        assert res.status_code == 404, path
    res = env.client.delete(f"{_base(env.team_a)}/{conn_b['id']}")
    assert res.status_code == 404
    with pytest.raises(KeyError):
        env.manager.set_team_connection_oauth_tokens(
            env.team_a["id"], conn_b["id"], {"access_token": "x"}
        )
    assert (
        env.manager.team_connection_oauth_tokens(env.team_b["id"], conn_b["id"]) is None
    )


def test_global_sign_in_never_reaches_team_agents(env, auth_server):
    """A signed-in global server's tools and tokens stay out of teams."""
    res = env.client.post(
        "/v1/mcp/servers",
        json={
            "name": "Global mail",
            "transport": "http",
            "url": f"{auth_server.base}/mcp",
            "auth_type": "oauth",
        },
    )
    sid = res.json()["server"]["id"]
    start = env.client.post(f"/v1/mcp/servers/{sid}/oauth/start").json()
    redirect = httpx.get(start["authorization_url"]).headers["location"]
    target = urlparse(redirect)
    assert env.client.get(f"{target.path}?{target.query}").status_code == 200
    assert env.global_store.oauth_tokens(sid)

    # The team has a connection to the same server but has not signed in.
    conn_a = _add_oauth(env, env.team_a, auth_server)
    tools, clients = discover_team_connections(env.manager, env.agent_a["id"])
    assert tools == [] and clients == []
    verify = env.client.post(f"{_base(env.team_a)}/{conn_a['id']}/verify")
    assert verify.status_code == 409
    assert (
        env.manager.team_connection_oauth_tokens(env.team_a["id"], conn_a["id"]) is None
    )

    from openjarvis.agents.tool_resolver import (
        ensure_registries_populated,
        resolve_agent_tools,
    )
    from openjarvis.core.types import ToolResult
    from openjarvis.tools._stubs import BaseTool, ToolSpec

    class GlobalInbox(BaseTool):
        @property
        def spec(self):
            return ToolSpec(name="global_inbox", description="", parameters={})

        def execute(self, **params):
            return ToolResult(tool_name="global_inbox", success=True, content="x")

    ensure_registries_populated()
    agent = env.manager.get_agent(env.agent_a["id"])
    kit = resolve_agent_tools(
        agent, manager=env.manager, engine=None, model="", mcp_tools=[GlobalInbox()]
    )
    assert "global_inbox" not in kit.by_name
    kit.close()

    # Once the team signs in itself, it gets its own tools, still no global.
    _sign_in(env, env.team_a, conn_a)
    kit = resolve_agent_tools(
        agent, manager=env.manager, engine=None, model="", mcp_tools=[GlobalInbox()]
    )
    assert "global_inbox" not in kit.by_name
    assert any(name.startswith("team_") for name in kit.by_name)
    kit.close()


def test_oauth_connection_respects_agent_access(env, auth_server):
    conn = _add_oauth(env, env.team_a, auth_server, agent_ids=[env.other_a["id"]])
    _sign_in(env, env.team_a, conn)
    tools, clients = discover_team_connections(env.manager, env.agent_a["id"])
    assert tools == []
    tools, clients = discover_team_connections(env.manager, env.other_a["id"])
    assert [t._tool.spec.name for t in tools] == ["inbox"]
    _close(clients)


def test_client_secret_is_write_only(env, auth_server):
    res = env.client.post(
        _base(env.team_a),
        json={
            "name": "Mail",
            "provider": "Mail",
            "url": f"{auth_server.base}/mcp",
            "auth_type": "oauth",
            "oauth_client_secret": CLIENT_SECRET,
            "settings": {"oauth_client_id": "my-client", "oauth_scopes": ["read"]},
        },
    )
    assert res.status_code == 201, res.text
    assert CLIENT_SECRET not in res.text
    assert res.json()["has_client_secret"] is True
    listing = env.client.get(_base(env.team_a))
    assert CLIENT_SECRET not in listing.text
    start = env.client.post(f"{_base(env.team_a)}/{res.json()['id']}/oauth/start")
    assert "client_id=my-client" in start.json()["authorization_url"]
    assert CLIENT_SECRET not in start.text
    bad = env.client.post(
        _base(env.team_a),
        json={
            "name": "x",
            "provider": "x",
            "url": f"{auth_server.base}/mcp",
            "auth_type": "oauth",
            "oauth_client_secret": CLIENT_SECRET,
            "settings": {"oauth_auth_params": {"redirect_uri": "https://evil"}},
        },
    )
    assert bad.status_code == 400
    assert CLIENT_SECRET not in bad.text


_STDIO_SERVER = textwrap.dedent(
    """
    import json, os, sys
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        if msg["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "env", "version": "1"}}
        elif msg["method"] == "tools/list":
            names = ["has_" + k.lower() for k in ("TEAM_KEY", "PARENT_SECRET")
                     if os.environ.get(k)]
            names.append("key_" + os.environ.get("TEAM_KEY", "none"))
            result = {"tools": [{"name": n, "description": "", "inputSchema": {}}
                                for n in names]}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                     "result": result}) + "\\n")
        sys.stdout.flush()
    """
)


def test_stdio_connection_gets_only_its_own_env(env, monkeypatch):
    script = env.tmp / "server.py"
    script.write_text(_STDIO_SERVER)
    # A credential in the server's own environment must not leak to teams.
    monkeypatch.setenv("PARENT_SECRET", "from-the-host")

    def add(team, value):
        res = env.client.post(
            _base(team),
            json={
                "name": "Local",
                "provider": "Local",
                "transport": "stdio",
                "settings": {
                    "command": sys.executable,
                    "args": [str(script)],
                    "env_names": ["TEAM_KEY"],
                },
                "env": {"TEAM_KEY": value} if value else {},
            },
        )
        assert res.status_code == 201, res.text
        assert "alpha" not in res.text and "bravo" not in res.text
        return res.json()

    conn_a = add(env.team_a, "alpha")
    assert conn_a["transport"] == "stdio" and conn_a["env_set"] == ["TEAM_KEY"]
    assert conn_a["status"] == "configured"
    add(env.team_b, "bravo")

    tools_a, clients_a = discover_team_connections(env.manager, env.agent_a["id"])
    tools_b, clients_b = discover_team_connections(env.manager, env.agent_b["id"])
    assert sorted(t._tool.spec.name for t in tools_a) == ["has_team_key", "key_alpha"]
    assert sorted(t._tool.spec.name for t in tools_b) == ["has_team_key", "key_bravo"]
    _close(clients_a + clients_b)

    listing = env.client.get(_base(env.team_a))
    assert "alpha" not in listing.text

    verify = env.client.post(f"{_base(env.team_a)}/{conn_a['id']}/verify")
    assert verify.status_code == 200, verify.text
    assert verify.json()["status"] == "verified"


def test_stdio_without_required_env_is_not_started(env):
    res = env.client.post(
        _base(env.team_a),
        json={
            "name": "Local",
            "provider": "Local",
            "transport": "stdio",
            "settings": {"command": "definitely-not-a-command", "env_names": ["KEY"]},
        },
    )
    assert res.status_code == 201, res.text
    assert res.json()["status"] == "needs_authentication"
    tools, clients = discover_team_connections(env.manager, env.agent_a["id"])
    assert tools == [] and clients == []
    bad = env.client.post(
        _base(env.team_a),
        json={
            "name": "Bad",
            "provider": "Bad",
            "transport": "stdio",
            "settings": {"command": "x", "env_names": ["1BAD"]},
        },
    )
    assert bad.status_code == 400


def test_deleting_a_connection_removes_its_tokens_only(env, auth_server):
    conn_a = _add_oauth(env, env.team_a, auth_server)
    conn_b = _add_oauth(env, env.team_b, auth_server)
    _sign_in(env, env.team_a, conn_a)
    _sign_in(env, env.team_b, conn_b)
    secrets_dir = env.tmp / "team-connection-secrets"
    assert (secrets_dir / f"{conn_a['id']}.json").exists()
    env.client.delete(f"{_base(env.team_a)}/{conn_a['id']}")
    assert not (secrets_dir / f"{conn_a['id']}.json").exists()
    assert env.manager.team_connection_oauth_tokens(env.team_b["id"], conn_b["id"])
    mode = (secrets_dir / f"{conn_b['id']}.json").stat().st_mode & 0o777
    assert mode == 0o600
