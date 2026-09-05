from mcp_hub.auth.base import Authenticator, auth_required
from mcp_hub.auth.basic import BasicAuthStrategy
from mcp_hub.auth.noauth import NoAuthStrategy
from mcp_hub.auth.principal import ANONYMOUS_SUBJECT, Principal
from mcp_hub.config import AuthConfig


def build_authenticator(settings: AuthConfig) -> Authenticator:
    if settings.type == "none" or settings.type == "noauth":
        return NoAuthStrategy()
    if settings.type == "basic" or settings.type == "":
        # Fail closed rather than silently running with no/empty-password "protection":
        # basic auth is only meaningful with a real password the operator set.
        if not settings.basic_auth.register_pass:
            raise ValueError(
                "auth.type is 'basic' but no password is set. "
                "Set auth.basic_auth.register_pass "
                "(e.g. MCPHUB_AUTH__BASIC_AUTH__REGISTER_PASS=...), "
                "or use auth.type: none for local/trusted use."
            )
        return BasicAuthStrategy(
            settings.basic_auth.register_user, settings.basic_auth.register_pass
        )
    raise ValueError(f"Unknown auth type: {settings.type}")


__all__ = [
    "ANONYMOUS_SUBJECT",
    "Authenticator",
    "BasicAuthStrategy",
    "NoAuthStrategy",
    "Principal",
    "auth_required",
    "build_authenticator",
]
