"""A stub enterprise IdP that issues ID-JAGs.

Keycloak cannot do this: its ID-JAG support is receiver-side only, and its token
exchange refuses an ID Token as `subject_token` outright (see TODO.md Story 8.6).
No free IdP we could script was available, so leg 1 is stubbed — which also lets the
tests drive claim edge cases a real IdP would never produce on demand.

It is deliberately not a general-purpose IdP: it signs what it is asked for, so the
e2e suite can ask for a *wrong* `resource` or an expired assertion and watch the hub
refuse them.
"""

from __future__ import annotations

import json
import os
import time
import uuid

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form
from fastapi.responses import JSONResponse

ISSUER = os.environ.get("IDP_ISSUER", "http://ema-idp:9200")
KEY_ID = "stub-idp-key-1"
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ID_JAG_TYPE = "urn:ietf:params:oauth:token-type:id-jag"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
app = FastAPI()


@app.get("/.well-known/openid-configuration")
async def discovery() -> dict:
    return {
        "issuer": ISSUER,
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
        # Story 8.1: the hub reads this to see whether the grant profile is offered.
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


USERS = {"alice": "alice-pw", "bob": "bob-pw"}
HUB_AUDIENCE = os.environ.get("HUB_AUDIENCE", "k5n-mcp-hub")


def _sign(claims: dict) -> str:
    now = int(time.time())
    return jwt.encode(
        {"iss": ISSUER, "iat": now, "exp": now + 900, **claims},
        _KEY,
        algorithm="RS256",
        headers={"kid": KEY_ID},
    )


@app.post("/token")
async def token(
    grant_type: str = Form(...),
    subject_token: str = Form(""),
    subject_token_type: str = Form(""),
    requested_token_type: str = Form(""),
    audience: str = Form(""),
    resource: str = Form(""),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    scope: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
) -> JSONResponse:
    if grant_type == "password":
        # Enterprise SSO reduced to what a script can drive. Issues an access token
        # audienced at the hub (so the hub can validate it) and an ID Token, which is
        # what EMA leg 1 actually wants (ADR 0006).
        if USERS.get(username) != password:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        identity = {
            "sub": username,
            "preferred_username": username,
            "email": f"{username}@example.com",
        }
        return JSONResponse(
            {
                # Whatever the caller asked for. A real IdP would grant only what the
                # user is entitled to; this stub lets the suite drive both allow and
                # deny cases without needing per-user policy.
                "access_token": _sign(
                    {**identity, "aud": HUB_AUDIENCE, "scope": scope or "mcp:invoke"}
                ),
                "id_token": _sign({**identity, "aud": "mcp-client"}),
                "token_type": "Bearer",
                "expires_in": 900,
            }
        )

    if grant_type != TOKEN_EXCHANGE:
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    if requested_token_type != ID_JAG_TYPE:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "only id-jag is issued here"},
            status_code=400,
        )
    if not subject_token:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "no subject_token"}, status_code=400
        )
    if not audience:
        return JSONResponse(
            {"error": "invalid_target", "error_description": "audience is required"},
            status_code=400,
        )

    # Whatever the caller authenticated as downstream is what we assert. A real IdP
    # would validate the subject token and evaluate policy here.
    try:
        subject_claims = jwt.decode(subject_token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "subject_token is not a JWT"},
            status_code=400,
        )

    now = int(time.time())
    assertion = jwt.encode(
        {
            "jti": str(uuid.uuid4()),
            "iss": ISSUER,
            "sub": subject_claims.get("sub", "unknown"),
            "email": subject_claims.get("email", ""),
            "aud": audience,
            "resource": resource,
            "client_id": client_id,
            "scope": scope,
            "iat": now,
            "exp": now + 300,
        },
        _KEY,
        algorithm="RS256",
        headers={"kid": KEY_ID},
    )
    return JSONResponse(
        {
            "access_token": assertion,
            "issued_token_type": ID_JAG_TYPE,
            "token_type": "N_A",
            "expires_in": 300,
        }
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9200)
