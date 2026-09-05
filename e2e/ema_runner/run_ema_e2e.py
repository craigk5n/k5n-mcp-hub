"""End-to-end assertions for Enterprise-Managed Authorization (ID-JAG).

What this proves that the OBO stack cannot: the downstream MCP server's access token
is issued by *its own* authorization server, which is not the hub's IdP, and the hub
got there by exchanging the caller's identity assertion in two legs.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time

import httpx

IDP = os.environ.get("IDP", "http://ema-idp:9200").rstrip("/")
RESOURCE_AS = os.environ.get("RESOURCE_AS", "http://ema-resource-as:9300").rstrip("/")
HUB = os.environ.get("HUB", "http://hub:8080").rstrip("/")
MCP = os.environ.get("MCP", "http://ema-mcp:9400").rstrip("/")
SERVER_ID = "files"
RESOURCE_ID = f"{MCP}/mcp"

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
    return json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))


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


def login(client: httpx.Client, username: str, password: str) -> tuple[str, str]:
    response = client.post(
        f"{IDP}/token",
        data={"grant_type": "password", "username": username, "password": password},
    )
    response.raise_for_status()
    body = response.json()
    return body["access_token"], body["id_token"]


def call_tool(client: httpx.Client, access: str, id_token: str) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {access}",
        "X-MCP-Target-Server": SERVER_ID,
        "Content-Type": "application/json",
    }
    if id_token:
        headers["X-MCP-Identity-Assertion"] = id_token
    return client.post(
        f"{HUB}/mcp",
        headers=headers,
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
        for url, label in (
            (f"{IDP}/health", "enterprise idp"),
            (f"{RESOURCE_AS}/health", "resource authorization server"),
            (f"{MCP}/health", "mcp server"),
            (f"{HUB}/healthz", "hub"),
        ):
            wait_for(client, url, label)

        alice_access, alice_id = login(client, "alice", "alice-pw")
        bob_access, bob_id = login(client, "bob", "bob-pw")

        print("\nthe resource authorization server advertises the grant profile")
        metadata = client.get(f"{RESOURCE_AS}/.well-known/oauth-authorization-server").json()
        check(
            "id-jag profile is advertised",
            "urn:ietf:params:oauth:grant-profile:id-jag"
            in metadata.get("authorization_grant_profiles_supported", []),
        )

        print("\nregistering the downstream server for enterprise-managed auth")
        registered = client.post(
            f"{HUB}/v1/register",
            headers={"Authorization": f"Bearer {alice_access}"},
            json={
                "id": SERVER_ID,
                "url": RESOURCE_ID,
                "name": "Files",
                "registration_type": "self",
                "auth_type": "ema",
                "oauth_token_url": f"{IDP}/token",
                "oauth_client_id": "k5n-mcp-hub",
                "oauth_client_secret": "hub-secret",
                "ema_resource_as_issuer": RESOURCE_AS,
                "ema_resource_as_token_url": f"{RESOURCE_AS}/token",
                "ema_resource_id": RESOURCE_ID,
            },
        )
        check(
            "registration succeeds",
            registered.status_code in (200, 201),
            f"HTTP {registered.status_code}: {registered.text[:200]}",
        )

        print("\nthe MCP server really does validate its own tokens")
        # The caller's token is minted by the enterprise IdP for the hub. The MCP
        # server trusts only its own authorization server, so this must be refused --
        # otherwise the test proves nothing about the two legs.
        passthrough = client.post(
            f"{MCP}/mcp",
            headers={"Authorization": f"Bearer {alice_access}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}},
            },
        )
        check(
            "an enterprise IdP token is refused downstream",
            passthrough.status_code == 401,
            f"HTTP {passthrough.status_code}",
        )

        print("\ntwo-leg exchange through the proxy")
        for username, access, id_token in (
            ("alice", alice_access, alice_id),
            ("bob", bob_access, bob_id),
        ):
            response = call_tool(client, access, id_token)
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
                f"{username}: the token came from the resource AS, not the hub's IdP",
                identity.get("aud") == RESOURCE_ID,
                str(identity.get("aud")),
            )
            check(
                f"{username}: the hub is named as the broker",
                identity.get("azp") == "k5n-mcp-hub",
                str(identity.get("azp")),
            )

        print("\nfailing closed")
        # ADR 0006: the server is configured for id_token, so a caller who sends none
        # must be refused rather than silently downgraded to their access token.
        without_assertion = call_tool(client, alice_access, "")
        check(
            "a caller with no identity assertion is refused",
            without_assertion.status_code in (401, 502),
            f"HTTP {without_assertion.status_code}",
        )

        no_identity = client.post(
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
            "an unauthenticated call is refused",
            no_identity.status_code == 401,
            f"HTTP {no_identity.status_code}",
        )

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("failed: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
