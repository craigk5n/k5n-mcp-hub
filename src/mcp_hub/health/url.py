from __future__ import annotations


def build_health_url(mcp_url: str) -> str:
    result = mcp_url.rstrip("/")
    if result.endswith("/mcp"):
        result = result[:-4]
    elif result.endswith("mcp"):
        result = result[:-3]
    result = result.rstrip("/")
    if not result.endswith("/"):
        result = result + "/health"
    else:
        result = result + "health"
    return result
