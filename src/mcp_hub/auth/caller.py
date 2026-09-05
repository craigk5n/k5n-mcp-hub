from __future__ import annotations

import logging
from typing import Union

from fastapi import Request

from mcp_hub.auth.principal import Principal

logger = logging.getLogger(__name__)


class ServiceIdentity:
    """Marker for an outbound call made with no user in scope.

    Health checks and periodic discovery run on a timer, so there is no principal to
    act for (ADR 0004). Naming that case explicitly — rather than letting a missing
    argument mean it — is what stops a plumbing bug from silently downgrading a user's
    request to the hub's own, broader, service identity.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "SERVICE_IDENTITY"


SERVICE_IDENTITY = ServiceIdentity()

# Every outbound auth decision takes one of these, and the parameter is required so
# the choice is made deliberately at each call site.
CallerIdentity = Union[Principal, ServiceIdentity]


def caller_from_request(request: Request) -> CallerIdentity:
    """The identity to act as for an outbound call serving ``request``."""
    principal = getattr(request.state, "principal", None)
    if isinstance(principal, Principal):
        return principal

    # Every route reaching an outbound call is auth-guarded, so arriving here means a
    # route was added without the dependency. Fall back to the anonymous principal and
    # never to SERVICE_IDENTITY: anonymous fails closed against an OBO server, whereas
    # the service identity would quietly succeed with broader rights.
    logger.warning(
        "no principal on request to %s; treating the caller as anonymous", request.url.path
    )
    return Principal.anonymous()
