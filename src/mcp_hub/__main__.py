from __future__ import annotations

import argparse
import os
import sys
from typing import Sequence

import uvicorn

from mcp_hub.config import Settings, load_settings


def parse_args(args: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="k5n-mcp-hub")
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to configuration file",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Override HTTP host",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override HTTP port",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help=(
            "Local development mode: allow reaching MCP servers on localhost/LAN "
            "(relaxes the SSRF guard). Does not change authentication. Not for "
            "any deployment reachable by untrusted callers."
        ),
    )
    return parser.parse_args(args)


DEV_MODE_ENV = "MCPHUB_SECURITY__ALLOW_PRIVATE_NETWORKS"


def apply_dev_mode(settings: Settings) -> None:
    """Relax the SSRF guard so a fresh install can reach local MCP servers.

    Without this, `pip install k5n-mcp-hub` followed by registering
    http://127.0.0.1:.../mcp fails with "URL validation failed": the guard blocks
    private ranges by default and only the repo's own config.yaml turns it off. That
    is the single papercut this flag exists to remove.

    It travels via the documented env override because uvicorn loads create_app as a
    factory, which re-reads settings itself — mutating the Settings object here alone
    would only affect the host/port handed to uvicorn.

    Deliberately narrow: it does NOT touch auth.type. Silently disabling
    authentication that an operator configured would be a far worse thing to do than
    the problem being solved.
    """
    os.environ[DEV_MODE_ENV] = "true"
    settings.security.allow_private_networks = True
    print(
        "k5n-mcp-hub: dev mode — the SSRF guard is relaxed, so localhost/LAN MCP "
        "servers can be registered and proxied. Do not use this where untrusted "
        "callers can reach the hub.",
        file=sys.stderr,
    )


def resolve_settings(
    config_path: str | None,
    host_override: str | None,
    port_override: int | None,
) -> Settings:
    settings = load_settings(config_path)
    if host_override is not None:
        settings.server.http_host = host_override
    if port_override is not None:
        settings.server.http_port = port_override
    return settings


def main(args: Sequence[str] | None = None) -> None:
    parsed = parse_args(args)
    settings = resolve_settings(parsed.config, parsed.host, parsed.port)
    if parsed.dev:
        apply_dev_mode(settings)

    uvicorn.run(
        "mcp_hub.app:create_app",
        host=settings.server.http_host,
        port=settings.server.http_port,
        factory=True,
        lifespan="on",
    )


if __name__ == "__main__":
    main(sys.argv[1:])
