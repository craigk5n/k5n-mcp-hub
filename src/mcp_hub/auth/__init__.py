from mcp_hub.auth.base import Authenticator, auth_required
from mcp_hub.auth.basic import BasicAuthStrategy
from mcp_hub.auth.jwt_bearer import DEFAULT_ALGORITHMS, JWTBearerStrategy
from mcp_hub.auth.noauth import NoAuthStrategy
from mcp_hub.auth.principal import ANONYMOUS_SUBJECT, Principal
from mcp_hub.config import AuthConfig

REQUIRED_JWT_FIELDS = ("issuer", "audience", "jwks_uri")


def build_authenticator(
    settings: AuthConfig, *, allow_private_networks: bool = False
) -> Authenticator:
    if settings.type == "none" or settings.type == "noauth":
        return NoAuthStrategy()
    if settings.type == "jwt":
        # Fail closed, exactly as basic-without-a-password does: a resource server
        # that can't identify its issuer or audience cannot validate anything, and
        # silently accepting would be worse than not starting.
        missing = [
            f"auth.jwt.{name}"
            for name in REQUIRED_JWT_FIELDS
            if not getattr(settings.jwt, name).strip()
        ]
        if missing:
            raise ValueError(
                f"auth.type is 'jwt' but {', '.join(missing)} is not set. "
                "Set it (e.g. MCPHUB_AUTH__JWT__ISSUER=...), or use auth.type: none "
                "for local/trusted use."
            )
        return JWTBearerStrategy(
            issuer=settings.jwt.issuer.strip(),
            audience=settings.jwt.audience.strip(),
            jwks_uri=settings.jwt.jwks_uri.strip(),
            algorithms=tuple(settings.jwt.algorithms) or DEFAULT_ALGORITHMS,
            required_scopes=frozenset(settings.jwt.required_scopes),
            leeway_seconds=settings.jwt.leeway_seconds,
            allow_private_networks=allow_private_networks,
        )
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
    "JWTBearerStrategy",
    "NoAuthStrategy",
    "Principal",
    "auth_required",
    "build_authenticator",
]
