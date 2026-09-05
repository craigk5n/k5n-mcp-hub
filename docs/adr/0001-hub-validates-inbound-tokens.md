# 0001 — The hub validates inbound tokens itself

**Status:** Accepted
**Date:** 2026-09-04
**Context:** OBO token exchange (TODO.md Epic 5)

## Context

Adding on-behalf-of support requires the hub to know *who is calling it*. Today
it does not: `Authenticator.authenticate()` returns `bool`
(`src/mcp_hub/auth/base.py:9`), and `auth_required` only checks
`result is not True`. Neither `NoAuthStrategy` nor `BasicAuthStrategy` produces a
principal, and no request-scoped identity is plumbed anywhere.

Two ways to obtain that identity:

1. The hub validates a JWT access token itself (JWKS, `iss`, `aud`, `exp`).
2. The hub trusts a fronting proxy (oauth2-proxy, an API gateway, an Istio
   sidecar) that already authenticated the caller and asserts identity in a
   header such as `X-Forwarded-User`.

Option 2 is meaningfully less work — Epic 5 nearly disappears.

## Decision

**The hub validates inbound access tokens itself.** A new `auth.type: "jwt"`
strategy verifies the bearer token against the IdP's JWKS and produces a
`Principal` carrying at minimum the raw token, `sub`, `iss`, and granted scopes.

Header-asserted identity from a fronting proxy is **not** supported as an
identity source.

Additionally, a server registered with `auth_type: "obo"` is **inoperable unless
inbound authentication actually establishes identity**. That constraint is
enforced where it is actionable, and never by refusing to start:

- **Registration time** — registering an OBO server while `auth.type` is `none`
  or `basic` is rejected with a message naming the conflict.
- **Startup** — an OBO server already present in persisted storage does *not*
  block boot. It is marked unusable, logged once with the reason, and shown in
  the UI as needing `auth.type: jwt`. Every other server keeps working.
- **Request time** — a call to such a server returns `401`, per ADR 0003.

This deliberately differs from `auth/__init__.py:build_authenticator`, which
*does* refuse to start when `auth.type: basic` has no password. That check guards
a global setting where the only alternative is serving every route unprotected.
An OBO server is one row in a registry: failing the whole process over it would
let a single stored registration brick a local hub, which is the opposite of
fail-safe.

## Rationale

**RFC 8693 needs a token, not a name.** The exchange request carries
`subject_token` — the caller's actual access token. A trusted header gives the
hub a username and nothing to exchange. Option 2 does not merely cost less
security; for this feature it does not function.

**The hub is a credential broker, and the blast radius is total.** It already
holds per-server bearer tokens, basic credentials, and OAuth client secrets
(`models/server.py:64-72`); OBO adds a cache of live user-scoped tokens. If
identity arrives in a spoofable header, anyone who can open a TCP connection to
the process can name themselves any user and pull that user's downstream token
out of the cache. The trust boundary would sit entirely outside a codebase whose
own defaults (`auth.type: none`, `security.allow_private_networks: true`) assume
a friendly local network.

**The deployment shape that makes option 2 safe is not this project's shape.**
Header-asserted identity is sound when the proxy is the only possible route to
the process. This hub ships as a `pip install` CLI binding `127.0.0.1:8080` and
is documented for `docker run`; there is no mechanism to guarantee it is
unreachable except through a proxy, and no way to detect that it is not.

**It is required anyway.** Under the MCP authorization spec the hub *is* an
OAuth 2.1 resource server. Resource servers validate their own tokens.

## Consequences

- Epic 5 (JWKS validation, `Principal` plumbing, protected-resource metadata) is
  a genuine prerequisite and roughly 60% of the OBO effort.
- A new crypto dependency (`joserfc` or `pyjwt[crypto]`) must be declared in
  `pyproject.toml` `dependencies`, not just installed locally — CI's clean-venv
  gate fails otherwise (see CLAUDE.md).
- `Authenticator` changes shape. `auth_required`'s `result is not True` check and
  `tests/test_auth.py` / `tests/test_integration_auth.py` change with it. The
  protocol is internal (not part of the public HTTP contract), so this is safe.
- **The hub still runs with no identity provider anywhere.** `auth.type` stays
  `none` by default; a hub with no OBO servers registered never enters this code
  path, makes no IdP network call, and needs no Keycloak. See "Running without an
  identity provider" below.
- Running behind an authenticating proxy remains fine — the proxy simply must
  forward the original `Authorization` header rather than replace it with an
  assertion.

## Running without an identity provider

This is a hard invariant, not a best-effort goal. The hub is a local-first tool
that must stay `pip install` → `k5n-mcp-hub` → working, with no IdP in the
picture:

- Default `auth.type: none` is unchanged, and nothing about OBO alters the
  behavior of a hub that has no OBO servers registered.
- The JWT/JWKS machinery is constructed only when `auth.type: jwt`. The crypto
  library is a declared dependency (so CI's clean-venv gate is satisfied), but
  importing it must not require, contact, or assume an IdP.
- JWKS is fetched **lazily on first token validation**, never eagerly at startup —
  otherwise a correctly configured hub would fail to boot whenever the IdP is
  merely down.
- A persisted OBO registration degrades that one server, never the process
  (above).
- The Keycloak docker-compose stack is opt-in and runs outside `pytest`; the
  default suite continues to need no Docker and no network.

## Alternatives considered

**Trust a fronting proxy.** Rejected: no `subject_token` to exchange, and a
spoofable header would expose every cached user token in a process whose default
posture assumes a trusted network.

**Introspection (RFC 7662) instead of local JWKS validation.** Rejected as the
default — a network round-trip per request on the proxy hot path. Reasonable as a
later addition for opaque tokens; the config shape should not preclude it.
