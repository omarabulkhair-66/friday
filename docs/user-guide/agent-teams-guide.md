# Run a business with agent teams

This guide shows how to set up OpenJarvis as a small "agency": several
**agent teams** (for example content and ads, monitoring, posting), each with
its **own private MCP accounts**, all steerable from Claude Code or any other
MCP client.

It ties together three pages that go into more depth:

- [Agent teams](../agent-teams.md): team storage, limits and the full REST API.
- [External MCP servers](mcp-external-servers.md): global MCP servers and `config.toml`.
- [Agents over MCP](mcp-agents-endpoint.md): the `/mcp` endpoint reference.

An Arabic quick start is at the [end of this page](#arabic-quick-start).

## Concepts

**Team.** A named group of managed agents, usually one per company, client or
department. A team has its own MCP connections, its own memory and knowledge
(`team-data/<team_id>/` beside the agents database) and its own requests. An
agent belongs to at most one team. Deleting a team keeps its agents (they
become unassigned) but deletes its connections and their credentials.

**Orchestrator (lead).** One agent per team, chosen in the team's settings
under **منسّق الفريق**. Requests to the team go to the lead. Only the lead gets
the `team_assign_tasks` tool, which assigns up to four tasks per run to team
members. Each task **runs the receiving agent automatically**, and its reply
comes back to the lead as a report. You never pass results between agents
yourself.

**Request (campaign).** A job handed to a team: a title, a brief, optional
product photos, a logo, approved contact details and a run limit (default 16
agent runs, up to 60). Each request has its own page
(`/agents/teams/<team>/requests/<request>`) and its own folder with
`originals/` (your uploads), `drafts/` (work in progress, including images
returned by MCP tools) and `final/` (deliverables). Every agent in the
request can list and read these files with `campaign_files`. Requests,
handoffs and files survive a restart.

**Cross-team policy.** Agents can message other agents in their own team.
Messaging another team is denied by default; it works only after **both**
teams select each other under **التواصل مع الفرق الأخرى** and save. A
message from another team is delivered as untrusted data. It never gives the
recipient the sender's accounts, tools, memory or history, and
`managed_agent_message` never runs the recipient; it reads the message on its
next turn. Files are never shared between teams.

!!! warning "Not an OS sandbox"
    Team isolation is enforced in tool resolution, the message queue and the
    handoff dispatcher. File, shell, code and HTTP tools keep their normal
    permissions and can reach other teams' files on the same machine. Do not
    give team agents general-purpose tools if you rely on team separation.

## Global vs team MCP

| | Global MCP servers | Team connections |
|---|---|---|
| Where | **MCP** page in the sidebar, or `[tools.mcp]` in `config.toml` | Team settings → **اتصالات الفريق** |
| Used by | Agents that have **never** been in a team, and chat | Members of that team only |
| Credentials | `~/.openjarvis/mcp/secrets.json` | Per-connection files beside the agents database |
| Apply changes | Restart OpenJarvis | From the agent's next turn; no restart |

Isolation guarantees, as implemented:

- **No global fallback.** An agent that is or has ever been in a team never
  receives global MCP tools. If its team connection is missing a credential,
  fails discovery or errors, the agent simply has no tool from it; nothing
  falls back to a global account. Removing an agent from its team does not
  give it global accounts back; assign it to another team instead.
- **Per-agent access.** Each connection is open to all team members or only
  to the members you pick (**من يستخدم هذا الاتصال؟**). The agent's own
  **Enable this team's connections for this agent** switch (agent
  **Overview**) can turn all connections off for that agent.
- **Checked on every call.** Before each tool call the runtime rechecks that
  the agent is still an active member, the connection still belongs to its
  team and is open to that agent. Tool names include the connection ID
  (`team_<connection-id>_<tool>_<hash>`), so two teams' identically named
  tools never resolve to the wrong account.
- **Secrets are write-only.** Tokens, header values, stdio environment values,
  OAuth client secrets and OAuth tokens are never returned by the API or
  shown in the UI. Responses only say what is set (`env_set`,
  `has_client_secret`, `oauth_status`). Error messages from a server are not
  echoed, because they could contain the credential.
- **Local commands get no host secrets.** A team's stdio server starts with
  only basic system variables (`PATH`, `HOME`, `USER`, locale…) plus that
  connection's own environment values.

## Add a connection to a team

On **Agents**, pick the team, open **الإعدادات والاتصالات**, then
**+ إضافة اتصال**. You first see the same MCP library as the global **MCP**
page. Pick an entry to prefill the form, or choose **Custom server**.

Give the connection a name that is unique within the team (for example
`Gmail – Sales`). Use an account that belongs to this company, not your
personal one.

### From the catalog

The library entry fills in the transport, URL or command, auth type, scopes
and required environment variable names. Fill in the credential and save.
For bearer, header and stdio connections, **حفظ واختبار الاتصال** saves and
immediately tests. A successful test lists the server's tools and marks the
connection **متحقق منه** (verified).

### Custom HTTP server

Choose **خادم بعيد (Streamable HTTP)**, enter the MCP URL and an auth method:

- **مفتاح Bearer**: sent as `Authorization: Bearer <token>`.
- **مفتاح في ترويسة مخصصة**: you name the header (for example `X-API-Key`)
  and the token is sent as its value.
- **تسجيل الدخول (OAuth)**: see below.
- **بدون مصادقة**: an explicitly unauthenticated server.

The URL must use HTTPS, or HTTP only for `localhost`, `127.0.0.1` or `::1`.
It must not contain a username, password, query string or fragment; put the
credential in the credential field.

### stdio server

Choose **أمر محلي (stdio)** and enter the command (for example `npx`), its
arguments one per line, and secret environment variables as name/value
pairs. Arguments are stored as plain text, so put keys in environment
variables, never in arguments. The command runs on the OpenJarvis machine
and must be installed there.

### OAuth sign-in

1. Choose OAuth. The form shows the **Redirect URI** to register with the
   provider. It is `<server address>/v1/mcp/oauth/callback`, the same one
   used by global sign-ins, so you register it once. Set
   `[tools.mcp] oauth_redirect_uri` in `config.toml` to override it.
2. Leave **Client ID** empty if the server supports automatic (dynamic)
   client registration. Otherwise create an OAuth client with the provider
   and paste its client ID and secret. Catalog entries that need this show
   the steps under **خطوات إنشاء عميل OAuth**.
3. **حفظ الاتصال**, then **تسجيل الدخول** on the connection card and sign in
   with this company's account in the window that opens. When it reports
   back, the connection is tested automatically.

The tokens are saved to this team connection only, never to the global store
or another team. **تسجيل الخروج** removes them. To change a key, client or
environment value, delete the connection and add it again.

### Google Workspace (Gmail, Calendar, Drive)

Google's Gmail, Calendar and Drive MCP servers
(`https://gmailmcp.googleapis.com/mcp/v1`, `calendarmcp…`, `drivemcp…`) are in
**Developer Preview** and have no automatic client registration. For each
Google Cloud project you use:

1. Join the [Google Workspace Developer Preview Program](https://developers.google.com/workspace/preview)
   with that project.
2. Enable both the product API and its MCP API: `gmail.googleapis.com` and
   `gmailmcp.googleapis.com`; `calendar-json.googleapis.com` and
   `calendarmcp.googleapis.com`; or `drive.googleapis.com` and
   `drivemcp.googleapis.com`.
3. In Google Auth Platform, set up Branding, Audience and Data Access, and add
   the scopes shown in the catalog entry. With an External audience, add the
   signing-in account as a test user.
4. Create a client (**Clients → Create Client**, type **Web application**),
   add the redirect URI from the form exactly, select **Use this client for an
   AI-powered agent**, and copy the client ID and secret into the form.

The catalog requests offline access, so the connection refreshes its token
without signing in again. See Google's
[MCP server guide](https://developers.google.com/workspace/guides/configure-mcp-servers).

## Worked example: an agency with two teams

The names below are examples; use any MCP servers you have accounts for.

### 1. Marketing team

1. **Agents → New team**: `Acme Marketing`.
2. Create three agents in the team:
    - **Lead**: plans and assigns. Leave its MCP connections off (its template
      or **Overview** switch); it does not need them.
    - **Creative**: writes copy and prepares images. The bundled
      `softpack_production` template is an example of such an agent: it uses
      the team's image MCP tools and `marketing_creative` to export a final
      PNG.
    - **Publisher**: the only agent that may post.
3. Team settings → **منسّق الفريق** → select **Lead**.
4. **+ إضافة اتصال** → your image service (catalog entry or custom HTTP) →
   **من يستخدم هذا الاتصال؟** → only **Creative**.
5. **+ إضافة اتصال** → your publishing service's MCP server (for example a
   social scheduler at `https://mcp.example-scheduler.com/mcp` with a bearer
   key) → only **Publisher**. In Publisher's instructions, say that it posts
   only what the request explicitly approves. Nothing is posted unless an
   agent calls that tool.
6. **صفحة الفريق والطلبات → طلب جديد للفريق**: title, brief, product photos
   and contact details. The lead assigns tasks; Creative passes photos to MCP
   tools as `campaign:originals/<file>` (the runtime sends the file bytes, so
   images never go through the model) and results land in `drafts/`; the
   finished image lands in `final/` with a **Download PNG** link on the
   request page.

For a complete five-agent Arabic setup, see
[Soft Pack media team](../soft-pack-marketing.md) and the
`softpack_*.toml` templates.

### 2. Monitoring team

1. New team `Acme Monitoring` with one **Watcher** agent on a cron or
   interval schedule, and set it as the lead.
2. Add the connections it watches: for example Sentry from the catalog, or a
   search server such as Brave Search over stdio with its `BRAVE_API_KEY` as
   a secret environment variable.
3. To let Watcher alert marketing, select `Acme Marketing` under
   **التواصل مع الفرق الأخرى** in **both** teams and save. Watcher can then
   send a message to the marketing lead with `managed_agent_message`; the
   lead reads it on its next turn as untrusted input. It never gets
   monitoring's accounts.

## Steer your teams from Claude Code

`jarvis serve` serves your agents and teams as an MCP server at `/mcp`
(Streamable HTTP). With the default port and no API key:

```bash
claude mcp add --transport http openjarvis http://127.0.0.1:8000/mcp
```

If an API key is set (`OPENJARVIS_API_KEY`, or `jarvis auth generate-key`),
send it as a bearer header:

```bash
claude mcp add --transport http openjarvis http://127.0.0.1:8000/mcp --header "Authorization: Bearer $OPENJARVIS_API_KEY"
```

Add `--scope user` to use it in every project, and change the port if you
serve on another one. Check it with `claude mcp list`, or `/mcp` inside
Claude Code. Then ask in plain language, for example:

> List my OpenJarvis teams. Start a request for Acme Marketing titled
> "Spring promo" with this brief: … Then check its progress and show me the
> final image.

Claude uses `list_agents`, `ask_agent`, `read_agent_messages`,
`start_team_request`, `list_team_requests`, `get_team_request` and
`read_team_request_file`. The endpoint never exposes team connections,
tokens, OAuth state or agent configuration, and cannot create or change
agents, teams or connections: work always goes through an agent, so its team
isolation still applies. `/mcp` accepts only local clients unless you set an
API key and `[server] mcp_allow_remote = true`. Details are in
[Agents over MCP](mcp-agents-endpoint.md).

## Troubleshooting

### Saving a connection

| Message | Fix |
|---|---|
| `Use HTTPS, or HTTP for an explicitly local MCP server` | Use an `https://` URL; plain HTTP works only for localhost. |
| `Use an MCP endpoint without credentials in its URL` | Remove `user:pass@` from the URL. |
| `Put authentication in the credential field, not the URL` | Remove `?key=…` or `#…` from the URL and use the key field. |
| `Connection name already exists` | Names are unique within a team; pick another. |
| `Choose a valid authentication header name` | Enter a header name such as `X-API-Key`. |
| `Enter the command that starts the MCP server` | stdio needs a command. |
| `env_names must be valid environment variable names` | Use names like `API_KEY` (letters, digits, `_`). |
| `Allowed agents must be active members of this team` | Pick only active members of this team. |

### Testing a connection (**اختبار الاتصال**)

| Message | Meaning |
|---|---|
| `رفض خادم MCP المصادقة؛ راجع المفتاح.` | HTTP 401/403: wrong or expired key, or wrong header. |
| `رفض الخادم تسجيل الدخول؛ سجّل الدخول من جديد.` | OAuth token rejected: sign in again. |
| `سجّل الدخول إلى هذا الاتصال أولًا.` | OAuth connection that is not signed in yet. |
| `ردّ خادم MCP بخطأ (HTTP <code>).` | The server answered with another HTTP error. |
| `تعذّر الاتصال بخادم MCP؛ راجع العنوان والمصادقة.` | Unreachable, or not an MCP server. Check the URL (often it ends in `/mcp`). |
| `أدخل قيم متغيرات البيئة المطلوبة لهذا الأمر.` | stdio connection missing a required environment value (status **ينقصه متغيرات بيئة**). |
| `تعذّر تشغيل الأمر؛ تأكد أنه مثبت على هذا الجهاز.` | The command (for example `npx`, `uvx`, `docker`) is not installed or not on `PATH`. |
| `بدأ الأمر لكنه لم يستجب كخادم MCP؛ راجع الأمر ووسائطه.` | The process started but did not speak MCP over stdio; check arguments (some servers need `--transport stdio`). |
| `اتصل الخادم لكنه لم يعرض أي أدوات.` | Connected, but the server lists no tools for this account. |

A connection whose last test failed (**فشل الاختبار**) or that needs a
credential is not offered to agents. Fix it and test again.

### OAuth sign-in

| Message | Fix |
|---|---|
| `This server needs an OAuth client you create yourself. Add the connection again with its client ID and secret.` | No dynamic registration (Google, Slack…): create a client and re-add the connection with it. |
| `This server does not publish OAuth metadata, so it cannot be signed in to here. Use a token instead if the server supports one.` | Use bearer or header auth. |
| `The client ID or secret was rejected.` | Recheck both values; for Google, the client type must be Web application. |
| `This sign-in link expired. Start again.` | Click **تسجيل الدخول** again. |
| `Sign-in was cancelled or refused.` | The provider refused: check the redirect URI matches exactly, the APIs are enabled, the scopes are added and (Google) you are a test user in the Developer Preview project. |
| `The sign-in expired or was revoked. Sign in again.` | Sign in again. Status **انتهى تسجيل الدخول** means the same. |

### While agents run

| Message | Meaning |
|---|---|
| `هذا الاتصال لم يعد متاحًا لك (أُزيل أو قُصر على وكلاء آخرين).` | The connection was deleted, limited to other members, or the agent left the team. |
| `فشل اتصال MCP الخاص بالفريق؛ راجع المصادقة والعنوان من اتصالات الفريق.` | The tool call failed; test the connection. |
| `Global connections are not available to this agent.` | A global tool was used by an agent that just joined a team; add a team connection instead. |
| `ملفات الطلب متاحة فقط أثناء تنفيذ مهمة للفريق.` | `campaign:` file paths work only inside a team request. |
| The agent has no team tools | Check the agent is in the team, its **Enable this team's connections** switch is on, the connection is open to it, and the connection is verified. |

### From Claude Code (`/mcp`)

| Response | Fix |
|---|---|
| `401 Missing Authorization header` / `Invalid API key` | Add or fix `--header "Authorization: Bearer <key>"`. |
| `403 The MCP endpoint only accepts local clients.` | Connect from the same machine, or set an API key and `mcp_allow_remote = true`. |
| `403 Origin not allowed` | Add the browser origin to `[server] cors_origins`. |
| `503 The agent manager is not running.` | Start the server with `jarvis serve`. |
| `Team <name> has no lead; set one in team settings or ask an agent by agent_id.` | Choose **منسّق الفريق** for the team. |
| `Agent <name> is paused.` | Resume the agent first. |

## Arabic quick start

<div dir="rtl" markdown>

### البداية السريعة

1. **أنشئ فريقًا لكل شركة أو قسم**: من **الوكلاء** اختر **New team** وأضف الوكلاء إليه.
2. **اختر المنسّق**: من **الإعدادات والاتصالات** ← **منسّق الفريق**. كل طلب للفريق يذهب للمنسّق، وهو يوزّع المهام بنفسه.
3. **أضف حسابات الفريق الخاصة**: **+ إضافة اتصال** ← اختر خادمًا من مكتبة MCP أو **Custom server**:
    - خادم بعيد (HTTP) بمفتاح Bearer أو ترويسة مخصصة أو تسجيل دخول OAuth.
    - أمر محلي (stdio) مع متغيرات بيئة سرية؛ لا تضع المفاتيح في الوسائط.
    - لـ Gmail وCalendar وDrive: يلزم الانضمام لبرنامج Google Workspace Developer Preview وإنشاء عميل OAuth من نوع Web application بنفس رابط إعادة التوجيه الظاهر في النموذج.
4. **حدّد من يستخدم كل اتصال**: كل وكلاء الفريق أو وكلاء محددون فقط (مثلًا وكيل النشر وحده لحساب النشر).
5. **اختبر الاتصال**: الحالة «متحقق منه» تظهر فقط بعد اختبار ناجح فعلي.
6. **أرسل طلبًا**: **صفحة الفريق والطلبات** ← **طلب جديد للفريق**. النتائج في صفحة الطلب، والصور النهائية في `final/` مع زر التنزيل.
7. **تحكّم من Claude Code**:

</div>

```bash
claude mcp add --transport http openjarvis http://127.0.0.1:8000/mcp --header "Authorization: Bearer $OPENJARVIS_API_KEY"
```

<div dir="rtl" markdown>

احذف `--header` إن لم تضبط مفتاح API.

**ضمانات العزل:** وكلاء الفريق لا يستخدمون أي حساب MCP عام أبدًا، حتى عند فشل اتصال الفريق. المفاتيح وتسجيلات الدخول لا تُعرض مرة أخرى بعد الحفظ. التواصل بين فريقين يحتاج موافقة الفريقين، ولا يعطي أي فريق حسابات الآخر. هذا ليس عزلًا على مستوى نظام التشغيل: لا تعطِ وكلاء الفريق أدوات ملفات أو أوامر عامة.

</div>
