from urllib.parse import urlparse, urlunparse


def compose_backend_url(
    server_url: str,
    incoming_path: str,
    incoming_query: str | None = None,
) -> str:
    mcp_prefix = "/mcp"

    if incoming_path.startswith(mcp_prefix):
        rel = incoming_path[len(mcp_prefix) :]
    else:
        rel = incoming_path

    if rel == "":
        # The incoming request targeted the base `/mcp` route, so the registered server
        # URL IS the target. Send it verbatim — including any trailing slash — so the
        # operator controls whether the backend receives `/mcp` or `/mcp/`. Hosted
        # servers (e.g. api.x.com) want the bare `/mcp`; SDK/Starlette-mounted servers
        # 307-redirect `/mcp`→`/mcp/` and we don't follow redirects, so they must be
        # registered as `/mcp/`. Forcing either here would break the other.
        result = server_url
    else:
        result = server_url.rstrip("/") + "/" + rel.lstrip("/")

    if incoming_query:
        parsed = urlparse(result)
        result = urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                parsed.query,
                parsed.fragment,
            )
        )
        if "?" in incoming_query:
            query_part = incoming_query
        else:
            query_part = "?" + incoming_query
        result = result + query_part

    return result
