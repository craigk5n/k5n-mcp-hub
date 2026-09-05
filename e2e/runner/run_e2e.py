"""End-to-end assertions for on-behalf-of token exchange.

Everything else in this repo tests the exchange against stubs and locally-signed
tokens. This is the only place a real IdP is involved, so it asserts the properties
that only a real IdP can demonstrate -- above all that the downstream server sees the
*calling user*, and that a token minted for the hub is refused there.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time

import httpx

KEYCLOAK = os.environ.get("KEYCLOAK", "http://keycloak:8080").rstrip("/")
HUB = os.environ.get("HUB", "http://hub:8080").rstrip("/")
STUB = os.environ.get("STUB", "http://mcp-stub:9100").rstrip("/")

REALM = "mcp-hub"
TOKEN_URL = f"{KEYCLOAK}/realms/{REALM}/protocol/openid-connect/token"
SERVER_ID = "files"

failures: list[str] = []
checks = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{' -- ' + detail if detail else ''}")
        failures.append(label)


def claims(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=="))


def login(client: httpx.Client, username: str, password: str) -> str:
    response = client.post(
        TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": "mcp-client",
            "username": username,
            "password": password,
        },
    )
    response.raise_for_status()
    return response.json()["access_token"]


def wait_for(client: httpx.Client, url: str, label: str, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if client.get(url).status_code < 500:
                print(f"  ready: {label}")
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise SystemExit(f"timed out waiting for {label} at {url}")


def call_tool(client: httpx.Client, token: str) -> httpx.Response:
    return client.post(
        f"{HUB}/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "X-MCP-Target-Server": SERVER_ID,
            "Content-Type": "application/json",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "whoami", "arguments": {}},
        },
    )


def main() -> int:
    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        print("waiting for services")
        wait_for(client, f"{KEYCLOAK}/realms/{REALM}", "keycloak")
        wait_for(client, f"{STUB}/health", "mcp-stub")
        wait_for(client, f"{HUB}/healthz", "hub")

        alice = login(client, "alice", "alice-password")
        bob = login(client, "bob", "bob-password")

        print("\nthe hub as an OAuth resource server")
        metadata = client.get(f"{HUB}/.well-known/oauth-protected-resource")
        check("protected-resource metadata is served", metadata.status_code == 200)
        if metadata.status_code == 200:
            check(
                "metadata names the authorization server",
                metadata.json().get("authorization_servers") == [f"{KEYCLOAK}/realms/{REALM}"],
                str(metadata.json()),
            )

        anonymous = client.post(
            f"{HUB}/v1/register", json={"id": "x", "url": "http://example.invalid"}
        )
        check("an unauthenticated call is refused", anonymous.status_code == 401)
        check(
            "the 401 points at the metadata document",
            "resource_metadata=" in anonymous.headers.get("WWW-Authenticate", ""),
            anonymous.headers.get("WWW-Authenticate", "<none>"),
        )

        print("\nregistering the downstream server for on-behalf-of")
        registered = client.post(
            f"{HUB}/v1/register",
            headers={"Authorization": f"Bearer {alice}"},
            json={
                "id": SERVER_ID,
                "url": f"{STUB}/mcp",
                "name": "Files",
                "registration_type": "self",
                "auth_type": "obo",
                "oauth_token_url": TOKEN_URL,
                "oauth_client_id": "k5n-mcp-hub",
                "oauth_client_secret": "hub-client-secret",
                "obo_audience": "mcp-server-files",
            },
        )
        check(
            "registration succeeds with a valid token",
            registered.status_code in (200, 201),
            f"HTTP {registered.status_code}: {registered.text[:200]}",
        )

        print("\nthe downstream server really does validate its tokens")
        # Without this the whole exercise proves nothing: a stub that accepts anything
        # would pass whether or not an exchange happened.
        passthrough = client.post(
            f"{STUB}/mcp",
            headers={"Authorization": f"Bearer {alice}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}},
            },
        )
        check(
            "a token minted for the hub is refused downstream",
            passthrough.status_code == 401,
            f"HTTP {passthrough.status_code}",
        )

        print("\non-behalf-of through the proxy")
        for username, token in (("alice", alice), ("bob", bob)):
            response = call_tool(client, token)
            ok = response.status_code == 200
            check(
                f"{username}: the proxied call succeeds",
                ok,
                f"HTTP {response.status_code}: {response.text[:300]}",
            )
            if not ok:
                continue
            identity = response.json().get("result", {}).get("structuredContent", {})
            check(
                f"{username}: downstream attributes the call to {username}",
                identity.get("preferred_username") == username,
                str(identity),
            )
            check(
                f"{username}: downstream sees the hub as the broker (azp)",
                identity.get("azp") == "k5n-mcp-hub",
                str(identity),
            )
            aud = identity.get("aud")
            check(
                f"{username}: the token is audience-bound to the backend",
                aud == "mcp-server-files" or (isinstance(aud, list) and "mcp-server-files" in aud),
                str(aud),
            )

        print("\nADR 0002: Keycloak's supported exchange yields no actor claim")
        response = call_tool(client, alice)
        if response.status_code == 200:
            identity = response.json().get("result", {}).get("structuredContent", {})
            check(
                "no `act` claim (impersonation shape, as documented)",
                identity.get("act") is None,
                str(identity.get("act")),
            )

        print("\nfailing closed")
        no_token = client.post(
            f"{HUB}/mcp",
            headers={"X-MCP-Target-Server": SERVER_ID, "Content-Type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}},
            },
        )
        check(
            "a call with no identity is refused",
            no_token.status_code == 401,
            f"HTTP {no_token.status_code}",
        )

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("failed: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
