# Talking to your agents over MCP

`jarvis serve` exposes your managed agents and teams as an
[MCP](https://modelcontextprotocol.io/) server at `/mcp` (Streamable HTTP).
Outside MCP clients such as Claude Code can then ask an agent a question,
hand work to a team and read the results, without using the REST API.

The endpoint is available whenever the server runs the agent manager.

## Add it to Claude Code

With the server on the default port and no API key:

```bash
claude mcp add --transport http openjarvis http://127.0.0.1:8000/mcp
```

If you set `OPENJARVIS_API_KEY` (or `jarvis auth generate-key`), pass it as a
header:

```bash
claude mcp add --transport http openjarvis http://127.0.0.1:8000/mcp --header "Authorization: Bearer $OPENJARVIS_API_KEY"
```

Claude Code saves the header value in its own settings. Add `--scope user` to
make the server available in every project, and change the URL if you serve on
another host or port (`jarvis serve --port 18765` → `http://127.0.0.1:18765/mcp`).
Run `claude mcp list` to check the connection; inside Claude Code, `/mcp`
shows the server and its tools.

Other clients use the same URL and header. Clients that only speak stdio can
go through a bridge such as `npx mcp-remote http://127.0.0.1:8000/mcp`.

## Tools

| Tool | What it does |
|------|--------------|
| `list_agents` | Teams with their agents and lead, plus agents without a team. Optional `team_id`. |
| `ask_agent` | Sends `message` to `agent_id`, or to the lead of `team_id`, runs the agent and waits up to `wait_seconds` (default 120, max 600) for its reply. Returns `status` `replied`, `working`/`queued` (still busy: read the reply later), `no_reply` or `error`. |
| `read_agent_messages` | An agent's recent conversation, oldest first (`limit` up to 100). |
| `start_team_request` | Hands a job (`title`, `brief`, optional `max_runs`) to a team. The team's lead receives it and splits it into tasks, exactly like **طلب جديد للفريق** in the UI. The team needs a lead. |
| `list_team_requests` | A team's requests with their status. |
| `get_team_request` | A request's status, every handoff between agents with its result, and the files it produced. |
| `read_team_request_file` | One file from a request's folder (`originals/`, `drafts/`, `final/`): text as text, images as images. |

`ask_agent` uses the same path as sending an immediate message on the agent's
page: the message is stored for the agent and the agent runs one turn with
the server's engine and model.

## What it does not expose

Requests always go through an agent, so the agent's own permissions and team
isolation still apply:

- No team MCP connections, connection settings, tokens or OAuth state, and no
  tool from a team connection. Only the agent can call those, and only the
  connections its team gives it.
- No agent configuration. `read_agent_messages` lists the names of the tools
  an agent used, not their arguments or results.
- Request files are read only inside that request's folder.
- It cannot create, change or delete agents, teams or connections.

## Access

- When an API key is set, `/mcp` requires `Authorization: Bearer <key>`, like
  `/v1/*`.
- It is local-only by default: requests from non-loopback addresses get
  `403`, even with a valid key. A reverse proxy on the same machine (for example
  a local `.test` domain) connects from loopback and works. To allow other
  machines, set an API key and add this to `config.toml`:

  ```toml
  [server]
  mcp_allow_remote = true
  ```

- Browser requests are accepted only from loopback origins or origins in
  `[server] cors_origins`, which blocks DNS-rebinding attacks.

The endpoint is stateless and answers every `POST` with JSON; `GET` and
`DELETE` return `405` because it never opens a server-to-client stream.
Protocol versions `2025-11-25`, `2025-06-18` and `2025-03-26` are accepted.
