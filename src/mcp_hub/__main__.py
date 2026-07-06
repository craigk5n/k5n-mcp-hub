from __future__ import annotations

import argparse
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
    return parser.parse_args(args)


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

    uvicorn.run(
        "mcp_hub.app:create_app",
        host=settings.server.http_host,
        port=settings.server.http_port,
        factory=True,
        lifespan="on",
    )


if __name__ == "__main__":
    main(sys.argv[1:])
