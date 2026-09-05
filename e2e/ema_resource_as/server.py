"""The downstream server's own authorization server — EMA's leg 2.

This party is what makes Enterprise-Managed Authorization different from plain
on-behalf-of: it trusts the enterprise IdP's assertion but issues its *own* access
tokens, and it is not the hub's IdP.

Stubbed rather than run on Keycloak. Keycloak 26.7 does have the receiver path — it
accepts the jwt-bearer grant and validates the assertion — but the feature is
experimental ("do not use in production") and the per-client switch that enables it
is undocumented; see TODO.md Story 8.6 for exactly what was tried.
"""

from __future__ import annotations

import json
import os
import time
import uuid

import httpx
import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form
from fastapi.responses import JSONResponse

ISSUER = os.environ.get("RESOURCE_AS_ISSUER", "http://ema-resource-as:9300")
IDP_ISSUER = os.environ.get("IDP_ISSUER", "http://ema-idp:9200")
IDP_JWKS = os.environ.get("IDP_JWKS_URI", f"{IDP_ISSUER}/jwks")
RESOURCE_ID = os.environ.get("RESOURCE_ID", "http://ema-mcp:9400/mcp")
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"

KEY_ID = "resource-as-key-1"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
app = FastAPI()
_idp_keys: dict[str, object] = {}


async def _load_idp_keys() -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        document = (await client.get(IDP_JWKS)).json()
    for entry in document.get("keys", []):
        candidate = dict(entry)
        candidate.setdefault("alg", "RS256")
        try:
            _idp_keys[candidate.get("kid", "")] = jwt.PyJWK.from_dict(candidate).key
        except Exception:  # noqa: BLE001
            continue


@app.get("/.well-known/oauth-authorization-server")
async def metadata() -> dict:
    return {
        "issuer": ISSUER,
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
        "authorization_grant_profiles_supported": ["urn:ietf:params:oauth:grant-profile:id-jag"],
    }


@app.get("/jwks")
async def jwks() -> dict:
    entry = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key()))
    entry.update({"kid": KEY_ID, "use": "sig", "alg": "RS256"})
    return {"keys": [entry]}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/token")
async def token(
    grant_type: str = Form(...),
    assertion: str = Form(""),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    scope: str = Form(""),
) -> JSONResponse:
    if grant_type != JWT_BEARER:
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    if not assertion:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    if not _idp_keys:
        await _load_idp_keys()
    try:
        kid = jwt.get_unverified_header(assertion).get("kid", "")
    except jwt.PyJWTError:
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "unreadable assertion"},
            status_code=400,
        )

    key = _idp_keys.get(kid)
    if key is None:
        await _load_idp_keys()
        key = _idp_keys.get(kid)
    if key is None:
        return JSONResponse(
            {"error": "invalid_grant", "error_description": f"unknown kid {kid!r}"},
            status_code=400,
        )

    try:
        # `aud` must be this authorization server's own issuer identifier.
        claims = jwt.decode(
            assertion, key, algorithms=["RS256"], audience=ISSUER, issuer=IDP_ISSUER
        )
    except jwt.PyJWTError as exc:
        return JSONResponse(
            {"error": "invalid_grant", "error_description": str(exc)}, status_code=400
        )

    # The ID-JAG's `resource` claim names the MCP server it is good for. Honouring it
    # here is what stops one assertion being redeemed for a different server.
    resource = str(claims.get("resource") or "")
    if resource and resource != RESOURCE_ID:
        return JSONResponse(
            {
                "error": "invalid_target",
                "error_description": f"assertion is for {resource}, not {RESOURCE_ID}",
            },
            status_code=400,
        )

    now = int(time.time())
    access_token = jwt.encode(
        {
            "jti": str(uuid.uuid4()),
            "iss": ISSUER,
            "sub": claims.get("sub"),
            "preferred_username": claims.get("sub"),
            "email": claims.get("email", ""),
            # Audience-restricted to the MCP server named in the assertion.
            "aud": RESOURCE_ID,
            "azp": client_id,
            "act": {"sub": client_id},
            "scope": scope or claims.get("scope", ""),
            "iat": now,
            "exp": now + 300,
        },
        _KEY,
        algorithm="RS256",
        headers={"kid": KEY_ID},
    )
    return JSONResponse({"access_token": access_token, "token_type": "Bearer", "expires_in": 300})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9300)
