# 0004 — Background paths use the service identity

**Status:** Accepted
**Date:** 2026-09-04
**Context:** OBO token exchange (TODO.md Epic 6)

## Context

Not every outbound call originates from a user request. Of the eight call sites
that reach `apply_server_auth`, two run on timers with no request in scope:

- `health/checker.py:56` — the background health checker.
- `mcp/discovery.py` via `sdk_client.py:195` and `stateless.py:92` — periodic
  tool/prompt/resource discovery.

The remainder (`proxy/handler.py:92`, `ui_invoke.py:203`,
`ui_initialize.py:173`, `ui_playground.py:111`) all have a live request and
therefore a principal.

The background paths need *some* credential for an OBO-configured server. Three
options: use the server's static/service credential, skip those servers, or reuse
a cached user token from whoever logged in most recently.

## Decision

**Background paths keep the service identity and never borrow a user's token.**

1. The health checker and discovery use the server's configured static or
   `client_credentials` credential — the existing rules 1–3 of
   `apply_server_auth`, unchanged.
2. A server configured with OBO **and no service credential** degrades rather
   than fails: health falls back to transport-level reachability (is the HTTP
   endpoint answering) without an MCP-level probe, and discovery is skipped. The
   UI states plainly that capabilities require a user session.
3. Capabilities discovered under the service identity are recorded as such. The
   capabilities UI labels them, because they are not necessarily what any given
   user would see.

## Rationale

**Borrowing a user's token is disqualified on three independent grounds.** It
attributes automated background polling to a person who did not initiate it,
corrupting the audit trail the feature exists to produce. It makes hub
availability depend on one arbitrary user's session — health checks start failing
when they log out, for reasons no operator would guess. And it widens the blast
radius of the token cache from "used for this user's requests" to "used for
anything".

**Skipping OBO servers entirely is too blunt.** Most such servers will also have
a service credential for exactly this purpose. Reachability monitoring does not
require user identity and should not be sacrificed.

**The cross-user capability leak is the subtle one.** `tools`, `prompts`, and
`resources` are cached on the shared `RegisteredServer` record
(`models/server.py:94-97`). A backend that varies its tool list per user — a
plausible thing for an OBO-aware server to do — would have one identity's list
cached and shown to everyone. Pinning discovery to the service identity does not
make that list *correct* for every user, but it makes it consistently the service
account's view, which is explainable and leaks nothing user-specific. Labelling it
in the UI closes the gap between what is shown and what is true.

## Consequences

- `apply_server_auth` needs an explicit "no principal in scope" mode rather than
  inferring it from a missing argument, so a plumbing bug cannot silently
  downgrade a user request to the service identity. This should be a required
  parameter, not an optional one that defaults.
- Health semantics for credential-less OBO servers are weaker than for other
  servers. Both the badge and its tooltip must say so; `supports_health_endpoint`
  already has precedent for per-server probe differences.
- Per-user capability discovery — populating tools from the requesting user's
  own token when they open the capabilities page — is a plausible later feature.
  This ADR does not preclude it; it decides only what the *background* poller
  does.
- `TokenCache` entries are only ever created on a request path, which keeps the
  per-subject cache bounded by active users rather than by registered servers ×
  users.

## Alternatives considered

**Skip OBO servers in background paths entirely.** Rejected: discards
reachability monitoring that works fine without user identity.

**Reuse the most recent user's token.** Rejected: falsifies the audit trail,
couples hub health to one user's session lifetime, and expands the token cache's
purpose beyond the request that created it.

**Give the hub a dedicated "monitoring" identity distinct from the per-server
service credential.** Deferred, not rejected. It is cleaner in a multi-tenant
deployment, but it is a new global credential to configure and rotate, and the
per-server credential already exists and already works.
