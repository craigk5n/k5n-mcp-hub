"""Build the e2e realm through Keycloak's admin API, then export it.

Written as API calls rather than hand-authored JSON: a realm export has a lot of
surface that must be exactly right, and Keycloak is the only authority on it. Run this
once against a throwaway Keycloak; the exported JSON is what docker-compose imports.

Usage: python configure_realm.py http://localhost:8080 [output.json]
"""

from __future__ import annotations

import json
import os
import sys

import httpx

BASE = sys.argv[1].rstrip("/")
OUTPUT = sys.argv[2] if len(sys.argv) > 2 else ""

REALM = "mcp-hub"
HUB_CLIENT = "k5n-mcp-hub"


def required_env(name: str) -> str:
    """Read a credential from the environment, or say plainly what is missing.

    Deliberately no default: a fallback would let the realm be built with values
    nobody chose, and the mismatch would only surface later as an authentication
    failure that looks like a bug in the exchange."""
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(
            f"{name} is not set. Copy e2e/.env.example to e2e/.env and fill it in, "
            f"or export {name} before running."
        )
    return value


HUB_SECRET = required_env("KC_HUB_CLIENT_SECRET")
AGENT_CLIENT = "mcp-client"
TARGET_CLIENT = "mcp-server-files"
USERS = {
    "alice": required_env("KC_ALICE_PASSWORD"),
    "bob": required_env("KC_BOB_PASSWORD"),
}


def admin_headers(client: httpx.Client) -> dict[str, str]:
    response = client.post(
        f"{BASE}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": "admin",
            "password": "admin",
        },
    )
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def audience_mapper(audience: str) -> dict[str, object]:
    """Put `audience` into the issued token's `aud`.

    The hub only accepts tokens minted for it, so the agent's token needs the hub in
    its audience or the very first hop fails."""
    return {
        "name": f"audience-{audience}",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "config": {
            "included.client.audience": audience,
            "id.token.claim": "false",
            "access.token.claim": "true",
        },
    }


def user_definition(username: str, password: str) -> dict[str, object]:
    """A user the direct-grant flow can actually log in as.

    firstName/lastName/email and an empty requiredActions matter: Keycloak's Verify
    Profile action is on by default, and without them every login fails with
    "Account is not fully set up"."""
    return {
        "username": username,
        "enabled": True,
        "emailVerified": True,
        "firstName": username.capitalize(),
        "lastName": "Tester",
        "email": f"{username}@example.com",
        "requiredActions": [],
        "credentials": [{"type": "password", "value": password, "temporary": False}],
    }


def main() -> None:
    with httpx.Client(timeout=30.0) as client:
        headers = admin_headers(client)

        client.delete(f"{BASE}/admin/realms/{REALM}", headers=headers)
        client.post(
            f"{BASE}/admin/realms",
            headers=headers,
            json={"realm": REALM, "enabled": True, "accessTokenLifespan": 900},
        ).raise_for_status()

        clients: list[dict[str, object]] = [
            # The downstream MCP server. Exists so "mcp-server-files" resolves as an
            # audience; nothing ever authenticates as it.
            {
                "clientId": TARGET_CLIENT,
                "enabled": True,
                "publicClient": False,
                "standardFlowEnabled": False,
                "serviceAccountsEnabled": False,
            },
            # The hub: confidential, and the client that performs the exchange.
            {
                "clientId": HUB_CLIENT,
                "enabled": True,
                "publicClient": False,
                "secret": HUB_SECRET,
                "standardFlowEnabled": False,
                "serviceAccountsEnabled": True,
                "directAccessGrantsEnabled": False,
                # Keycloak 26.2+ gates standard (V2) token exchange on this switch,
                # set on the *requesting* client. No fine-grained admin permissions
                # are needed for it.
                "attributes": {"standard.token.exchange.enabled": "true"},
                "protocolMappers": [audience_mapper(TARGET_CLIENT)],
            },
            # The AI agent. Public + PKCE in the real world; direct access grants are
            # enabled here so the e2e script can log in without a browser.
            {
                "clientId": AGENT_CLIENT,
                "enabled": True,
                "publicClient": True,
                "standardFlowEnabled": True,
                "directAccessGrantsEnabled": True,
                "redirectUris": ["http://localhost:*"],
                "attributes": {"pkce.code.challenge.method": "S256"},
                "protocolMappers": [audience_mapper(HUB_CLIENT)],
            },
        ]
        for definition in clients:
            response = client.post(
                f"{BASE}/admin/realms/{REALM}/clients", headers=headers, json=definition
            )
            response.raise_for_status()

        # Two users, so cross-user cache isolation can be exercised for real.
        for username, password in USERS.items():
            response = client.post(
                f"{BASE}/admin/realms/{REALM}/users",
                headers=headers,
                json=user_definition(username, password),
            )
            response.raise_for_status()

        print(f"realm {REALM!r} configured at {BASE}")

        if OUTPUT:
            export = client.post(
                f"{BASE}/admin/realms/{REALM}/partial-export"
                "?exportClients=true&exportGroupsAndRoles=true",
                headers=headers,
            )
            export.raise_for_status()
            document = export.json()
            # partial-export omits secrets and users; put back what the import needs —
            # as ${PLACEHOLDER}s, not values, so the committed file carries no
            # credentials. Keycloak substitutes them at import when started with
            # KC_SPI_IMPORT_SINGLE_FILE_REPLACE_PLACEHOLDERS=true.
            for entry in document.get("clients", []):
                if entry.get("clientId") == HUB_CLIENT:
                    entry["secret"] = "${KC_HUB_CLIENT_SECRET}"
            document["users"] = [
                user_definition(username, "${KC_%s_PASSWORD}" % username.upper())
                for username in USERS
            ]
            with open(OUTPUT, "w") as handle:
                json.dump(document, handle, indent=2)
            print(f"exported realm to {OUTPUT}")


if __name__ == "__main__":
    main()
