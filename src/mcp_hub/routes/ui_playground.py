import httpx
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from mcp_hub.mcp.auth import apply_server_auth
from mcp_hub.mcp.oauth import format_auth_challenge, parse_www_authenticate
from mcp_hub.mcp.sse import extract_sse_data
from mcp_hub.registry.service import Registry
from mcp_hub.utils import SafePinnedTransport, pretty_json

router = APIRouter(prefix="/ui", tags=["ui"])


async def auth_dependency(request: Request) -> None:
    auth_required_dep = request.app.state.auth_required_dependency
    await auth_required_dep(request)


@router.get("/server/{server_id}/playground", response_class=HTMLResponse)
async def get_playground(
    request: Request, server_id: str, _: None = Depends(auth_dependency)
) -> HTMLResponse:
    registry: Registry = request.app.state.registry
    templates = request.app.state.templates

    server = await registry.get(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")

    template = templates.get_template("playground.html")
    html = await template.render_async(
        server_id=server.id,
        url=server.url,
        request_body="",
        session_id="",
        protocol_version="",
        accept_sse=False,
        error="",
        parse_error="",
    )
    return HTMLResponse(content=html)


@router.post("/server/{server_id}/playground", response_class=HTMLResponse)
async def post_playground(
    request: Request, server_id: str, _: None = Depends(auth_dependency)
) -> HTMLResponse:
    registry: Registry = request.app.state.registry
    templates = request.app.state.templates

    server = await registry.get(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")

    form_data = await request.form()
    request_body = str(form_data.get("request_body", ""))
    session_id = str(form_data.get("session_id", ""))
    protocol_version = str(form_data.get("protocol_version", ""))
    # Checkbox is absent when unchecked; only string values are sent by browsers.
    accept_sse = form_data.get("accept_sse") in ("on", "true", "1")

    request_headers = ""
    response_status = ""
    response_headers = ""
    response_body = ""
    parsed_body = ""
    parse_error = ""
    auth_hint = ""
    error = ""

    if not request_body:
        template = templates.get_template("playground.html")
        html = await template.render_async(
            server_id=server.id,
            url=server.url,
            request_body=request_body,
            session_id=session_id,
            protocol_version=protocol_version,
            accept_sse=accept_sse,
            error="request body is required",
            parse_error="",
        )
        return HTMLResponse(content=html)

    headers: dict[str, str] = {
        "Content-Type": "application/json",
    }
    if accept_sse:
        headers["Accept"] = "text/event-stream, application/json"
    else:
        headers["Accept"] = "application/json, text/event-stream"
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    if protocol_version:
        headers["MCP-Protocol-Version"] = protocol_version

    # Capture headers for display BEFORE auth credentials are injected.
    request_headers = "\n".join(f"{k}: {v}" for k, v in headers.items())

    allow_private = bool(request.app.state.settings.security.allow_private_networks)

    # Track oauth_token_status before auth so we only persist when a new token is acquired.
    prev_oauth_token_status = server.oauth_token_status
    await apply_server_auth(headers, server, allow_private_networks=allow_private)
    # Only persist the server if a new OAuth token was successfully acquired.
    if server.oauth_token_status == "ok" and prev_oauth_token_status != "ok":
        await registry.register(server)

    try:
        async with httpx.AsyncClient(
            transport=SafePinnedTransport(allow_private_networks=allow_private), timeout=30.0
        ) as client:
            resp = await client.post(
                server.url,
                content=request_body.encode(),
                headers=headers,
            )

            response_status = str(resp.status_code)
            response_headers = "\n".join(f"{k}: {v}" for k, v in resp.headers.items())
            response_body = resp.text

            if resp.status_code in (401, 403):
                www_auth = resp.headers.get("WWW-Authenticate", "")
                challenge = parse_www_authenticate(www_auth)
                auth_hint = format_auth_challenge(challenge)

            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                sse_data = extract_sse_data(response_body)
                if sse_data:
                    try:
                        parsed_body = pretty_json(sse_data.decode("utf-8", errors="replace"))
                    except ValueError as e:
                        parse_error = f"Failed to parse SSE data: {str(e)}"
                        logging.warning(parse_error)
            else:
                try:
                    parsed_body = pretty_json(response_body)
                except ValueError as e:
                    parse_error = f"Failed to parse response body: {str(e)}"
                    logging.warning(parse_error)

    except httpx.ConnectError:
        error = f"Could not connect to server at {server.url}"
    except httpx.TimeoutException:
        error = "Request timed out"
    except Exception as e:
        error = f"Request failed: {str(e)}"

    template = templates.get_template("playground.html")
    html = await template.render_async(
        server_id=server.id,
        url=server.url,
        request_body=request_body,
        session_id=session_id,
        protocol_version=protocol_version,
        accept_sse=accept_sse,
        error=error,
        parse_error=parse_error,
        request_headers=request_headers,
        response_status=response_status,
        response_headers=response_headers,
        response_body=response_body,
        parsed_body=parsed_body,
        auth_hint=auth_hint,
    )
    return HTMLResponse(content=html)
