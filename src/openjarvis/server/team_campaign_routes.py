"""Team requests, the orchestrator and team MCP connection checks.

Registered on the managed-agents router (``/v1/managed-agents``). Operator
facing errors are Arabic. Credentials are accepted only on creation and never
returned.
"""

from __future__ import annotations

import json
import mimetypes
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_PREVIEW_SUFFIXES = _IMAGE_SUFFIXES | {".html", ".json"}
_MAX_UPLOAD = 20_000_000
_MAX_PHOTOS = 10


class LeadRequest(BaseModel):
    agent_id: Optional[str] = None


class FollowUpRequest(BaseModel):
    content: str
    files: List[str] = []


class RunLimitRequest(BaseModel):
    max_runs: int


class AccessRequest(BaseModel):
    agent_ids: List[str] = []


def _http(exc: Exception, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


async def _save_image(upload: UploadFile, folder: Path, stem: str) -> str:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in _IMAGE_SUFFIXES:
        raise HTTPException(400, "ارفع صور PNG أو JPEG أو WebP فقط.")
    data = await upload.read(_MAX_UPLOAD + 1)
    if len(data) > _MAX_UPLOAD:
        raise HTTPException(400, "حجم الصورة أكبر من 20 ميجابايت.")
    if not (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        or data.startswith(b"\xff\xd8\xff")
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    ):
        raise HTTPException(400, "الملف المرفوع ليس صورة صالحة.")
    path = folder / f"{stem}{'.jpg' if suffix == '.jpeg' else suffix}"
    path.write_bytes(data)
    path.chmod(0o600)
    return f"originals/{path.name}"


def campaign_view(
    manager: Any, campaign: Dict[str, Any], *, detail: bool = False
) -> Dict[str, Any]:
    """A request with its display status and, with *detail*, handoffs and files."""
    store = manager.campaigns
    handoffs = store.list_handoffs(campaign["id"])
    open_count = sum(
        h["status"] in {"pending", "running", "waiting_paused"} for h in handoffs
    )
    attention = [h for h in handoffs if h["status"] in {"blocked", "failed"}]
    value = dict(campaign)
    if campaign["status"] == "active" and attention and not open_count:
        value["display_status"] = "needs_attention"
    elif campaign["status"] == "active" and not open_count:
        value["display_status"] = "waiting_for_you"
    else:
        value["display_status"] = campaign["status"]
    value["open_handoffs"] = open_count
    value["attention_count"] = len(attention)
    if detail:
        names = {a["id"]: a["name"] for a in manager.list_agents(include_archived=True)}
        for handoff in handoffs:
            handoff["source_name"] = (
                names.get(handoff["source_agent_id"], "")
                if handoff["source_agent_id"]
                else "أنت"
            )
            handoff["target_name"] = names.get(handoff["target_agent_id"], "")
            handoff["result"] = None
            if handoff["result_message_id"]:
                message = next(
                    (
                        m
                        for m in manager.list_messages(
                            handoff["target_agent_id"], limit=200
                        )
                        if m["id"] == handoff["result_message_id"]
                    ),
                    None,
                )
                handoff["result"] = (message or {}).get("content")
        value["handoffs"] = handoffs
        value["files"] = store.list_files(campaign)
    return value


def register_campaign_routes(router: Any, manager: Any) -> None:
    from openjarvis.agents.campaigns import HandoffRejected

    store = manager.campaigns  # Also creates the lead column.

    def campaign_or_404(campaign_id: str) -> Dict[str, Any]:
        campaign = store.get_campaign(campaign_id)
        if campaign is None:
            raise HTTPException(404, "الطلب غير موجود.")
        return campaign

    def view(campaign: Dict[str, Any], *, detail: bool = False) -> Dict[str, Any]:
        return campaign_view(manager, campaign, detail=detail)

    # ── Orchestrator ─────────────────────────────────────────────

    @router.put("/teams/{team_id}/lead")
    async def set_team_lead(team_id: str, req: LeadRequest):
        try:
            store.set_team_lead(team_id, req.agent_id)
        except KeyError as exc:
            raise _http(exc, 404) from exc
        except HandoffRejected as exc:
            raise _http(exc) from exc
        return manager.get_team(team_id)

    # ── Requests to the team ─────────────────────────────────────

    @router.get("/teams/{team_id}/campaigns")
    async def list_campaigns(team_id: str):
        if manager.get_team(team_id) is None:
            raise HTTPException(404, "الفريق غير موجود.")
        return {"campaigns": [view(c) for c in store.list_campaigns(team_id)]}

    @router.post("/teams/{team_id}/campaigns", status_code=201)
    async def create_campaign(
        team_id: str,
        title: str = Form(...),
        brief: str = Form(...),
        contact: str = Form(""),
        max_runs: int = Form(16),
        photos: List[UploadFile] = File(default=[]),
        logo: Optional[UploadFile] = File(default=None),
    ):
        if manager.get_team(team_id) is None:
            raise HTTPException(404, "الفريق غير موجود.")
        if len(photos) > _MAX_PHOTOS:
            raise HTTPException(400, f"ارفع حتى {_MAX_PHOTOS} صور.")
        contact = contact.strip()
        if len(contact) > 160:
            raise HTTPException(400, "بيانات التواصل حتى 160 حرفًا.")
        try:
            campaign = store.create_campaign(
                team_id,
                title=title,
                brief=brief,
                options={"contact": contact},
                max_runs=max_runs,
            )
        except KeyError as exc:
            raise _http(exc, 404) from exc
        except HandoffRejected as exc:
            raise _http(exc) from exc
        folder = store.campaign_dir(team_id, campaign["id"]) / "originals"
        files = []
        try:
            for index, photo in enumerate(photos, start=1):
                files.append(await _save_image(photo, folder, f"product-{index}"))
            options = dict(campaign["options"])
            if logo is not None and logo.filename:
                options["logo"] = await _save_image(logo, folder, "logo")
                files.append(options["logo"])
            with manager._db_lock:
                manager._conn.execute(
                    "UPDATE agent_campaigns SET options_json = ? WHERE id = ?",
                    (json.dumps(options, ensure_ascii=False), campaign["id"]),
                )
                manager._conn.commit()
            store.add_request(campaign["id"], content=brief, files=files)
        except HTTPException:
            store.set_campaign_status(campaign["id"], "failed", "تعذّر حفظ المرفقات.")
            raise
        except HandoffRejected as exc:
            store.set_campaign_status(campaign["id"], "failed", str(exc))
            raise _http(exc) from exc
        return view(store.get_campaign(campaign["id"]), detail=True)

    @router.get("/campaigns/{campaign_id}")
    async def get_campaign(campaign_id: str):
        return view(campaign_or_404(campaign_id), detail=True)

    @router.post("/campaigns/{campaign_id}/messages", status_code=201)
    async def follow_up(campaign_id: str, req: FollowUpRequest):
        campaign_or_404(campaign_id)
        try:
            store.add_request(
                campaign_id,
                content=req.content,
                files=req.files,
                title="رسالة متابعة منك",
            )
        except HandoffRejected as exc:
            raise _http(exc) from exc
        return view(store.get_campaign(campaign_id), detail=True)

    @router.post("/campaigns/{campaign_id}/cancel")
    async def cancel_campaign(campaign_id: str):
        campaign_or_404(campaign_id)
        store.set_campaign_status(campaign_id, "cancelled", "أوقفت الطلب.")
        return view(store.get_campaign(campaign_id), detail=True)

    @router.put("/campaigns/{campaign_id}/max-runs")
    async def set_max_runs(campaign_id: str, req: RunLimitRequest):
        campaign_or_404(campaign_id)
        try:
            store.set_max_runs(campaign_id, req.max_runs)
        except HandoffRejected as exc:
            raise _http(exc) from exc
        return view(store.get_campaign(campaign_id), detail=True)

    @router.post("/handoffs/{handoff_id}/retry")
    async def retry_handoff(handoff_id: str):
        try:
            handoff = store.retry_handoff(handoff_id)
        except KeyError as exc:
            raise _http(exc, 404) from exc
        except HandoffRejected as exc:
            raise _http(exc) from exc
        return view(store.get_campaign(handoff["campaign_id"]), detail=True)

    @router.get("/campaigns/{campaign_id}/file")
    async def campaign_file(campaign_id: str, path: str):
        """Preview one file of this request; nothing outside its folder."""
        campaign = campaign_or_404(campaign_id)
        try:
            file = store.resolve_campaign_file(campaign, path)
        except HandoffRejected as exc:
            raise _http(exc, 404) from exc
        if file.suffix.lower() not in _PREVIEW_SUFFIXES:
            raise HTTPException(404, "نوع الملف غير متاح للمعاينة.")
        media = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            # Drafts are self-contained; never let them run with this origin.
            "Content-Security-Policy": "sandbox; default-src 'none'; img-src data:;"
            " style-src 'unsafe-inline'",
        }
        return FileResponse(file, media_type=media, headers=headers)

    # ── Team MCP connections ─────────────────────────────────────

    @router.post("/teams/{team_id}/connections/{connection_id}/verify")
    async def verify_connection(team_id: str, connection_id: str):
        connection = manager.get_team_connection(team_id, connection_id)
        if connection is None:
            raise HTTPException(404, "الاتصال غير موجود.")
        try:
            tools = _verify_mcp(manager, connection)
        except _McpVerifyError as exc:
            manager.update_team_connection_settings(
                team_id, connection_id, verified_at=None, last_error=str(exc)
            )
            raise _http(exc, 409) from exc
        manager.update_team_connection_settings(
            team_id,
            connection_id,
            tools=tools,
            verified_at=time.time(),
            last_error=None,
        )
        result = manager.get_team_connection(team_id, connection_id)
        result["tools"] = tools
        return result

    @router.post("/teams/{team_id}/connections/{connection_id}/oauth/start")
    async def connection_oauth_start(
        team_id: str, connection_id: str, request: Request
    ):
        """Begin OAuth sign-in for this team's connection only.

        Completes at the shared ``/v1/mcp/oauth/callback``; the tokens are
        saved to this connection, never to the global MCP store.
        """
        from starlette.concurrency import run_in_threadpool

        connection = manager.get_team_connection(team_id, connection_id)
        if connection is None:
            raise HTTPException(404, "الاتصال غير موجود.")
        if connection["auth_type"] != "oauth":
            raise HTTPException(400, "هذا الاتصال لا يستخدم تسجيل الدخول OAuth.")
        redirect_uri = _team_redirect_uri(request)
        try:
            url = await run_in_threadpool(
                _start_team_sign_in, manager, connection, redirect_uri
            )
        except _McpVerifyError as exc:
            raise _http(exc) from exc
        return {"authorization_url": url, "redirect_uri": redirect_uri}

    @router.post("/teams/{team_id}/connections/{connection_id}/oauth/sign-out")
    async def connection_oauth_sign_out(team_id: str, connection_id: str):
        try:
            manager.set_team_connection_oauth_tokens(team_id, connection_id, None)
        except KeyError as exc:
            raise HTTPException(404, "الاتصال غير موجود.") from exc
        manager.update_team_connection_settings(
            team_id, connection_id, verified_at=None, last_error=None
        )
        return manager.get_team_connection(team_id, connection_id)

    @router.put("/teams/{team_id}/connections/{connection_id}/access")
    async def connection_access(team_id: str, connection_id: str, req: AccessRequest):
        try:
            return manager.set_team_connection_access(
                team_id, connection_id, agent_ids=req.agent_ids
            )
        except KeyError as exc:
            raise HTTPException(404, "اتصال MCP غير موجود.") from exc
        except ValueError as exc:
            raise HTTPException(400, "اختر وكلاء من أعضاء هذا الفريق فقط.") from exc


class _McpVerifyError(RuntimeError):
    pass


def _team_redirect_uri(request: Any) -> str:
    """The same redirect URI as global sign-ins (one to register)."""
    from openjarvis.server.mcp_routes import oauth_redirect_uri_for

    return oauth_redirect_uri_for(request, getattr(request.app.state, "config", None))


def _start_team_sign_in(
    manager: Any, connection: Dict[str, Any], redirect_uri: str
) -> str:
    """Discover, register a client if needed, and return the sign-in URL."""
    from openjarvis.mcp import oauth as mcp_oauth

    team_id, connection_id = connection["team_id"], connection["id"]
    settings = connection.get("settings") or {}
    client_id = settings.get("oauth_client_id")
    client_secret = manager._team_connection_client_secret(connection_id)
    try:
        discovery = mcp_oauth.discover(connection["url"])
        if not client_id:
            if not discovery.registration_endpoint:
                raise mcp_oauth.OAuthError(
                    "This server needs an OAuth client you create yourself. "
                    "Add the connection again with its client ID and secret."
                )
            registered = mcp_oauth.register_client(
                discovery.registration_endpoint, redirect_uri
            )
            client_id = registered["client_id"]
            client_secret = registered.get("client_secret")
    except mcp_oauth.OAuthError as exc:
        raise _McpVerifyError(str(exc)) from None

    def save(tokens: Dict[str, Any]) -> Optional[str]:
        try:
            manager.set_team_connection_oauth_tokens(team_id, connection_id, tokens)
        except KeyError:
            return "The team connection was deleted."
        manager.update_team_connection_settings(
            team_id, connection_id, verified_at=None, last_error=None
        )
        return None

    sign_in = mcp_oauth.PendingSignIn(
        server_id=connection_id,
        verifier="",
        redirect_uri=redirect_uri,
        discovery=discovery,
        client_id=client_id,
        client_secret=client_secret,
        save=save,
    )
    scopes = list(settings.get("oauth_scopes") or []) or discovery.scopes
    return mcp_oauth.start_sign_in(
        sign_in,
        mcp_oauth.shared_pending(),
        scopes,
        settings.get("oauth_auth_params") or {},
    )


def _verify_mcp(manager: Any, connection: Dict[str, Any]) -> List[Dict[str, str]]:
    """Connect with the team's credential and list the server's tools."""
    import httpx

    from openjarvis.agents.team_tools import open_team_mcp_client
    from openjarvis.tools.mcp_adapter import MCPToolProvider

    if connection.get("oauth_status") == "signed_out":
        raise _McpVerifyError("سجّل الدخول إلى هذا الاتصال أولًا.")
    if connection.get("transport") == "stdio" and connection["status"] == (
        "needs_authentication"
    ):
        raise _McpVerifyError("أدخل قيم متغيرات البيئة المطلوبة لهذا الأمر.")
    client = None
    try:
        client = open_team_mcp_client(manager, connection)
        tools = MCPToolProvider(client).discover()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in {401, 403} and connection["auth_type"] == "oauth":
            message = "رفض الخادم تسجيل الدخول؛ سجّل الدخول من جديد."
        elif code in {401, 403}:
            message = "رفض خادم MCP المصادقة؛ راجع المفتاح."
        else:
            message = f"ردّ خادم MCP بخطأ (HTTP {code})."
        raise _McpVerifyError(message) from None
    except FileNotFoundError:
        raise _McpVerifyError(
            "تعذّر تشغيل الأمر؛ تأكد أنه مثبت على هذا الجهاز."
        ) from None
    except Exception:
        # Never echo upstream text: it may contain the credential.
        raise _McpVerifyError(
            "تعذّر الاتصال بخادم MCP؛ راجع العنوان والمصادقة."
            if connection.get("transport") != "stdio"
            else "بدأ الأمر لكنه لم يستجب كخادم MCP؛ راجع الأمر ووسائطه."
        ) from None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    if not tools:
        raise _McpVerifyError("اتصل الخادم لكنه لم يعرض أي أدوات.")
    return [
        {
            "name": tool.spec.name,
            "description": (tool.spec.description or "")[:200],
        }
        for tool in tools[:80]
    ]


__all__ = ["campaign_view", "register_campaign_routes"]
