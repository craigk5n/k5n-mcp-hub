"""A downstream MCP server that actually validates its tokens.

Without this the end-to-end test proves nothing: a stub that accepts anything would
pass whether or not the hub exchanged the caller's token. This one rejects a token
whose audience is not itself, and echoes back the identity it saw so the test can
assert who the call was attributed to.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import jwt
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ISSUER = os.environ["OIDC_ISSUER"]
AUDIENCE = os.environ.get("OIDC_AUDIENCE", "mcp-server-files")
JWKS_URI = os.environ.get("OIDC_JWKS_URI") or f"{ISSUER}/protocol/openid-connect/certs"
RESOURCE = os.environ.get("RESOURCE_ID", "http://mcp-stub:9100")

app = FastAPI()
_keys: dict[str, Any] = {}


async def _load_keys() -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        document = (await client.get(JWKS_URI)).json()
    for entry in document.get("keys", []):
        candidate = dict(entry)
        candidate.setdefault("alg", "RS256")
        try:
            _keys[candidate.get("kid", "")] = jwt.PyJWK.from_dict(candidate).key
        except Exception:  # noqa: BLE001
            continue


def _challenge(error: str, description: str) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=401,
        headers={
            "WWW-Authenticate": (
                f'Bearer realm="{AUDIENCE}", error="{error}", '
                f'error_description="{description}", '
                f'resource_metadata="{RESOURCE}/.well-known/oauth-protected-resource"'
            )
        },
    )


async def _claims_or_challenge(
    request: Request,
) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None, _challenge("invalid_request", "no bearer token")

    token = header[7:].strip()
    if not _keys:
        await _load_keys()

    try:
        kid = jwt.get_unverified_header(token).get("kid", "")
    except jwt.PyJWTError as exc:
        return None, _challenge("invalid_token", f"unreadable header: {exc}")

    key = _keys.get(kid)
    if key is None:
        await _load_keys()
        key = _keys.get(kid)
    if key is None:
        return None, _challenge("invalid_token", f"unknown kid {kid!r}")

    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256", "RS384", "RS512", "ES256", "PS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
        )
    except jwt.InvalidAudienceError:
        # The rule that makes this test meaningful: a token minted for the hub, or
        # for any other service, is refused here.
        return None, _challenge("invalid_token", f"audience is not {AUDIENCE}")
    except jwt.PyJWTError as exc:
        return None, _challenge("invalid_token", str(exc))

    return claims, None


@app.get("/.well-known/oauth-protected-resource")
async def metadata() -> dict[str, Any]:
    return {
        "resource": RESOURCE,
        "authorization_servers": [ISSUER],
        "bearer_methods_supported": ["header"],
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/mcp")
async def mcp(request: Request) -> Any:
    claims, challenge = await _claims_or_challenge(request)
    if challenge is not None:
        return challenge
    assert claims is not None

    body = await request.json()
    method = body.get("method", "")
    request_id = body.get("id")

    if method == "initialize":
        result: dict[str, Any] = {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mcp-stub-files", "version": "0.1.0"},
        }
    elif method == "notifications/initialized":
        return JSONResponse({}, status_code=202)
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "whoami",
                    "description": "Report the identity this server saw.",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        }
    elif method == "tools/call":
        # The whole point of the exercise: report which identity the backend
        # attributed the call to, and who brokered it.
        identity = {
            "sub": claims.get("sub"),
            "preferred_username": claims.get("preferred_username"),
            "azp": claims.get("azp"),
            "aud": claims.get("aud"),
            "act": claims.get("act"),
        }
        result = {
            "content": [{"type": "text", "text": str(identity)}],
            "structuredContent": identity,
            "isError": False,
        }
    else:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": method}}
        )

    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9100)
