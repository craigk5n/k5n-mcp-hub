"""A REST API the hub serves as an OpenAPI backend, which validates its tokens.

The OpenAPI counterpart of `mcp_stub`: a plain REST service with no MCP in it. Its
OpenAPI document is the one FastAPI generates, served unauthenticated at
`/openapi.json` for the hub to import -- a spec the hub did not write. Every API route
rejects a token whose audience is not `pets-api`, and `whoami` echoes the identity it
saw, so the e2e runner can assert the call was made on behalf of the calling user.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import jwt
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

ISSUER = os.environ["OIDC_ISSUER"]
AUDIENCE = os.environ.get("OIDC_AUDIENCE", "pets-api")
JWKS_URI = os.environ.get("OIDC_JWKS_URI") or f"{ISSUER}/protocol/openid-connect/certs"

app = FastAPI(title="Pets API", version="1.0.0")
_keys: dict[str, Any] = {}


class Identity(BaseModel):
    sub: str | None = None
    preferred_username: str | None = None
    azp: str | None = None
    aud: str | list[str] | None = None


class NewPet(BaseModel):
    name: str


class Pet(BaseModel):
    id: int
    name: str
    owner: str | None = None


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


async def caller(request: Request) -> dict[str, Any]:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "no bearer token")
    token = header[7:].strip()
    try:
        kid = jwt.get_unverified_header(token).get("kid", "")
    except jwt.PyJWTError as exc:
        raise HTTPException(401, f"unreadable token: {exc}") from exc
    if kid not in _keys:
        await _load_keys()
    if kid not in _keys:
        raise HTTPException(401, f"unknown kid {kid!r}")
    try:
        return jwt.decode(
            token,
            _keys[kid],
            algorithms=["RS256", "RS384", "RS512", "ES256", "PS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
        )
    except jwt.InvalidAudienceError as exc:
        # What makes the test meaningful: a token minted for the hub is refused here.
        raise HTTPException(401, f"audience is not {AUDIENCE}") from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(401, str(exc)) from exc


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/whoami", operation_id="whoami", summary="Report the identity this API saw")
async def whoami(claims: dict[str, Any] = Depends(caller)) -> Identity:
    return Identity(**{key: claims.get(key) for key in Identity.model_fields})


@app.get("/pets", operation_id="listPets", summary="List pets")
async def list_pets(limit: int = 10, claims: dict[str, Any] = Depends(caller)) -> list[Pet]:
    return [Pet(id=1, name="rex", owner=claims.get("preferred_username"))][:limit]


@app.post("/pets", operation_id="createPet", summary="Create a pet")
async def create_pet(pet: NewPet, claims: dict[str, Any] = Depends(caller)) -> Pet:
    return Pet(id=2, name=pet.name, owner=claims.get("preferred_username"))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9200)
