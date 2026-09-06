from __future__ import annotations

from fastapi import HTTPException, Request

from mcp_hub.auth.principal import Principal
from mcp_hub.config import AuthConfig
from mcp_hub.models.server import RegisteredServer

# Callers holding this scope may administer the hub: register and delete servers,
# edit credentials, toggle fault injection, and read every trace. Overridable with
# `auth.jwt.admin_scope`.
DEFAULT_ADMIN_SCOPE = "mcp:admin"


def authorization_enforced(auth: AuthConfig) -> bool:
    """Whether per-caller authorization applies at all.

    Only under `auth.type: jwt`. `none` and `basic` are single-user modes — there is
    one caller, so there is no tenancy to enforce and nothing to migrate.
    """
    return auth.type == "jwt"


def admin_scope(auth: AuthConfig) -> str:
    return (auth.jwt.admin_scope or "").strip() or DEFAULT_ADMIN_SCOPE


def is_admin(principal: Principal, auth: AuthConfig) -> bool:
    """Whether this caller may perform administrative operations."""
    if not authorization_enforced(auth):
        return True
    if principal.is_anonymous:
        return False
    return principal.has_scope(admin_scope(auth))


def may_call_server(principal: Principal, server: RegisteredServer, auth: AuthConfig) -> bool:
    """Whether this caller may reach this backend.

    A server that declares no ``required_scope`` is reachable by nobody once
    enforcement is on. That is the deliberate default: an unlabelled server is
    ambiguous, and resolving ambiguity toward "everyone" is how the hole this closes
    came about in the first place.

    Admins may reach anything. That is honest rather than notional — an admin can
    edit the server's ``required_scope`` or read its stored credential, so pretending
    otherwise would buy nothing and complicate the rules.
    """
    if not authorization_enforced(auth):
        return True
    if is_admin(principal, auth):
        return True
    if principal.is_anonymous:
        return False

    required = (server.required_scope or "").strip()
    if not required:
        return False
    return principal.has_scope(required)


def _auth_config(request: Request) -> AuthConfig:
    settings = getattr(request.app.state, "settings", None)
    return settings.auth if settings is not None else AuthConfig()


def _principal(request: Request) -> Principal:
    from mcp_hub.auth.caller import caller_from_request

    caller = caller_from_request(request)
    return caller if isinstance(caller, Principal) else Principal.anonymous()


def request_is_admin(request: Request) -> bool:
    return is_admin(_principal(request), _auth_config(request))


def require_admin(request: Request) -> None:
    """403 unless the caller holds the admin scope.

    Separate from "may call this server": registering and deleting servers are not
    per-server operations, and fault injection is a denial-of-service primitive
    against every other caller of that server."""
    auth = _auth_config(request)
    if is_admin(_principal(request), auth):
        return
    raise HTTPException(
        status_code=403,
        detail=f"requires the {admin_scope(auth)!r} scope",
    )


def require_server_access(request: Request, server: RegisteredServer) -> None:
    """Refuse unless the caller may reach this server.

    401 for an anonymous caller and 403 for an authenticated one, which is the
    distinction that matters to a client: 401 with the RFC 9728 challenge says
    "authenticate, here is where", while 403 says "you did authenticate, and this
    still isn't yours" — retrying the login would not help.
    """
    auth = _auth_config(request)
    principal = _principal(request)
    if may_call_server(principal, server, auth):
        return

    if principal.is_anonymous:
        from mcp_hub.auth.metadata import bearer_challenge

        headers = (
            {"WWW-Authenticate": bearer_challenge(request, auth)} if auth.type == "jwt" else {}
        )
        raise HTTPException(status_code=401, detail="Unauthorized", headers=headers)

    required = (server.required_scope or "").strip()
    detail = (
        f"requires the {required!r} scope"
        if required
        else "this server declares no required_scope, so only an admin may reach it"
    )
    raise HTTPException(status_code=403, detail=detail)
