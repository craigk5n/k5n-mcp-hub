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
        result = server_url.rstrip("/")
    else:
        if not server_url.endswith("/") and not rel.startswith("/"):
            result = server_url + "/" + rel
        else:
            result = server_url + rel

    parsed = urlparse(result)
    if parsed.path.endswith("/mcp") and rel == "":
        result = result + "/"

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
