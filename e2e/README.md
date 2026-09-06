# End-to-end stacks

Two stacks live here:

| Stack | Flow | IdP |
|---|---|---|
| `docker-compose.obo.yml` | On-behalf-of (Epic 6) — one RFC 8693 leg | **Real Keycloak** |
| `docker-compose.ema.yml` | Enterprise-Managed Authorization (Epic 8) — two legs | Stubs, for a tested reason (below) |

## On-behalf-of stack

Everything else in this repo tests the RFC 8693 exchange against stubs and
locally-signed tokens. This stack is the only place a **real identity provider** is
involved, and it exists to demonstrate the things only a real IdP can:

- the hub validates a genuine Keycloak access token,
- it exchanges that token for one whose audience is the downstream MCP server,
- the downstream server **attributes the call to the calling user**, not to the hub,
- and a token minted for the hub is **refused** downstream (no passthrough).

## Running it

```bash
cp e2e/.env.example e2e/.env          # required -- see Credentials below
docker compose -f e2e/docker-compose.obo.yml up --build -d
docker compose -f e2e/docker-compose.obo.yml run --rm --build e2e
docker compose -f e2e/docker-compose.obo.yml down -v
```

The runner prints one line per assertion and exits non-zero if any fail. Keep the
`--build`: without it compose reuses a cached runner image, and a stale copy fails in
ways that look like real regressions.

This is deliberately **not** part of `pytest`: the default suite must keep needing no
Docker and no network. Run it by hand, or wire it into a separate CI job.

Host ports are published only so you can poke at the admin console
(`http://localhost:38085`, admin/admin) and the hub (`http://localhost:38086`). The
runner talks to everything inside the compose network and needs no host ports, so
override them if they clash:

```bash
KEYCLOAK_PORT=39085 HUB_PORT=39086 docker compose -f e2e/docker-compose.obo.yml up -d
```

## Credentials

The committed realm file contains no credentials. The hub's client secret and both
user passwords are `${...}` placeholders that Keycloak substitutes at import time
(`KC_SPI_IMPORT_SINGLE_FILE_REPLACE_PLACEHOLDERS=true`), from environment variables
compose supplies to both Keycloak and the runner — so the two can't drift.

**`e2e/.env` is required.** There are no defaults anywhere:

```bash
cp e2e/.env.example e2e/.env    # then edit if you like; .env is gitignored
```

Compose reads `.env` from the directory holding the compose file, so `e2e/.env` is
picked up even when you run from the repo root. Without it the command stops
immediately with the name of the missing variable, rather than substituting empty
strings and building a realm with blank passwords that fails later and far less
clearly.

These are not production secrets and never should be — the realm they configure
exists only while the stack is up. Keeping them out of `realm-mcp-hub.json` is about
not committing credential-shaped strings, not about protecting these particular
values.

## What's in the stack

| Service | Role |
|---|---|
| `keycloak` | Real IdP, realm imported from `keycloak/realm-mcp-hub.json` |
| `hub` | This project, built from the repo `Dockerfile`, running `auth.type: jwt` |
| `mcp-stub` | Downstream MCP server that **validates JWTs** and echoes the identity it saw |
| `e2e` | The assertions (`profiles: ["test"]`, so it doesn't start with the others) |

The stub is the load-bearing piece. A downstream that accepted anything would let
the test pass whether or not an exchange happened, so it rejects a wrong `aud` with
`401` plus a `WWW-Authenticate` challenge, and reports back `sub`, `azp`, and `act`.

## Authorization

Under `auth.type: jwt` the hub enforces per-server authorization, so the stack has to
model it: the server is registered with `required_scope: files:use`, alice and bob log
in requesting that scope, and registration is done with a separate `mcp:admin` token.

Two checks exist purely to prove enforcement rather than assume it: a login that does
*not* request `files:use` is refused with 403, and registering without `mcp:admin` is
refused too.

Both scopes are declared as **optional** client scopes, which is what lets one login
have them and another not. A real deployment would gate them by role or group instead
of letting the client ask; here the point is to exercise the hub's enforcement, not
the IdP's policy engine.

## The realm

Three clients, two users:

- **`mcp-client`** — the AI agent. Public, PKCE in the real world; direct access
  grants are enabled here so the runner can log in without a browser. An audience
  mapper puts `k5n-mcp-hub` in the token's `aud`, or the very first hop would fail.
- **`k5n-mcp-hub`** — the hub. Confidential, and the client that performs the
  exchange. `standard.token.exchange.enabled=true` is the Keycloak 26.2+ switch that
  permits it, set on the *requesting* client; no fine-grained admin permissions are
  needed.
- **`mcp-server-files`** — the downstream server. Exists purely so that name resolves
  as an audience; nothing ever authenticates as it.
- **`alice` / `bob`** — two users, so cross-user token-cache isolation is exercised
  with real tokens rather than stubs.

`keycloak/realm-mcp-hub.json` was generated by `keycloak/configure_realm.py` against a
throwaway Keycloak rather than hand-authored — a realm export has a lot of surface
that has to be exactly right, and Keycloak is the only authority on it. To regenerate:

```bash
python3 e2e/keycloak/configure_realm.py http://localhost:38085 e2e/keycloak/realm-mcp-hub.json
```

The export writes `${...}` placeholders back in place of the secret and passwords, so
regenerating never reintroduces literals.

## What this run actually proved

Against Keycloak 26.4.7, the exchanged token came back as:

```
aud : mcp-server-files      # audience-bound to the backend, not the hub
azp : k5n-mcp-hub           # the hub is identifiable as the broker
sub : alice                 # the call is attributed to the user
act : None                  # no actor claim
```

That last line is the empirical confirmation of
[ADR 0002](../docs/adr/0002-impersonation-default-delegation-opt-in.md): Keycloak's
*supported* standard token exchange has no `actor_token` parameter and issues no `act`
claim, which is why impersonation is the default shape and delegation is opt-in. The
audit trail survives via `azp`, and the runner asserts that rather than assuming it.


# Enterprise-Managed Authorization stack

```bash
docker compose -f e2e/docker-compose.ema.yml up --build -d
docker compose -f e2e/docker-compose.ema.yml run --rm --build e2e-ema
docker compose -f e2e/docker-compose.ema.yml down -v
```

No `.env` needed: every credential here belongs to a stub, not a realm import.

## Why this one has no Keycloak

Not for convenience — it was tested. Two findings, both in TODO.md Story 8.6:

1. **Keycloak cannot issue ID-JAGs.** Its token exchange refuses an ID Token as
   `subject_token` outright (`"Parameter 'subject_token' supports access tokens
   only"`), and its own docs say issuer-side support is "not yet fully implemented".
2. **Keycloak 26.7 *can* receive them, but not usably here.** With
   `--features=identity-assertion-jwt` it accepts the `jwt-bearer` grant and rejects
   assertions on their content rather than the grant type — so the path exists. But
   the feature is flagged experimental ("do not use in production"), and the
   per-client switch that permits the grant is undocumented; `invalid_grant: JWT
   Authorization Grant is not supported for the requested client` persisted through
   the attribute names that seemed most likely. Worth revisiting when the feature
   stabilises.

So leg 2 runs against a stub authorization server. That still exercises everything the
hub does; what it does not prove is interoperability with someone else's
implementation, which is a real gap and is recorded as one.

## What's in this stack

| Service | Role |
|---|---|
| `ema-idp` | Enterprise IdP: SSO (access + ID tokens), JWKS, and the leg-1 ID-JAG exchange |
| `ema-resource-as` | The backend's **own** authorization server: validates ID-JAGs, issues its own access tokens |
| `ema-mcp` | The MCP server, trusting `ema-resource-as` — *not* the enterprise IdP |
| `hub` | This project, `auth.type: jwt` against `ema-idp` |
| `e2e-ema` | The assertions |

The stub IdP is deliberately credulous: it signs what it is asked for, so the suite
can request a wrong `resource` or an expired assertion and watch the hub refuse them —
edge cases a real IdP would never produce on demand.

## What this run proves

That the MCP server's access token is issued by an authorization server which is
**not** the hub's IdP, and that the hub reached it in two legs. The load-bearing
check is that a token minted by the enterprise IdP is *refused* by the MCP server:
without that, the test would pass whether or not the second leg happened at all.
