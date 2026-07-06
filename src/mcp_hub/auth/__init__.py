from mcp_hub.auth.base import Authenticator, auth_required
from mcp_hub.auth.basic import BasicAuthStrategy
from mcp_hub.auth.noauth import NoAuthStrategy
from mcp_hub.config import AuthConfig


def build_authenticator(settings: AuthConfig) -> Authenticator:
    if settings.type == "none" or settings.type == "noauth":
        return NoAuthStrategy()
    if settings.type == "basic" or settings.type == "":
        if not settings.basic_auth.register_user and not settings.basic_auth.register_pass:
            return NoAuthStrategy()
        return BasicAuthStrategy(
            settings.basic_auth.register_user, settings.basic_auth.register_pass
        )
    raise ValueError(f"Unknown auth type: {settings.type}")


__all__ = [
    "Authenticator",
    "auth_required",
    "BasicAuthStrategy",
    "NoAuthStrategy",
    "build_authenticator",
]
