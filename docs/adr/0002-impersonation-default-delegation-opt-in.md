# 0002 — Impersonation-shaped exchange by default, delegation opt-in

**Status:** Accepted
**Date:** 2026-09-04
**Context:** OBO token exchange (TODO.md Epic 6)

## Context

RFC 8693 describes two outcomes for a token exchange:

| Shape | Request | Resulting token | Downstream can distinguish? |
|-------|---------|-----------------|------------------------------|
| **Impersonation** | `subject_token` | `sub` = user | Not from `sub` alone — the token looks like the user |
| **Delegation** | `subject_token` + `actor_token` | `sub` = user, `act` = hub | Yes — the `act` claim names the broker |

Delegation is the better security story: the downstream server sees both parties
and can authorize on either. The question is whether it can actually be
implemented and tested.

Keycloak — the reference IdP for this project — answers that unevenly:

- **Standard token exchange (V2) is fully supported and enabled by default.** It
  accepts `grant_type`, `subject_token`, `subject_token_type`, and optionally
  `requested_token_type`, `scope`, and `audience`. It has **no `actor_token`
  parameter at all.**
- **Delegation is experimental**, gated behind
  `--features=token-exchange-delegation,parameterized-scopes`, and is modelled
  differently from RFC 8693's actor token: the subject's token carries a
  `may_act` claim pre-authorizing a named actor, rather than the client
  presenting its own token alongside the subject's.

So delegation is neither universally available nor uniformly shaped across IdPs.

## Decision

**Implement both shapes; default to impersonation; make delegation opt-in per
server.**

The exchange client sends the impersonation-shaped request (`subject_token` +
`audience`/`resource`) unless the server record sets an actor-token source, in
which case it additionally sends `actor_token` / `actor_token_type`.

Delegation is configured per registered server, not globally — one hub may front
both a delegation-capable IdP and a stock Keycloak.

## Rationale

**Supporting both is nearly free.** `actor_token` is one additional optional form
field on a POST the hub already has to build. Hard-wiring impersonation would
save perhaps ten lines and forfeit the better shape wherever it is available.

**Defaulting to delegation would make the hub untestable against a stock
Keycloak.** The default-enabled, fully-supported Keycloak path does not accept an
actor token. A default that only works behind two experimental feature flags is
the wrong default.

**The audit trail survives impersonation anyway.** In Keycloak's V2 exchange the
issued token's `azp` is set to the requesting client — the hub — while `aud`
carries the target audience. A downstream server can therefore still tell that
the hub brokered the call and that the user did not authenticate to it directly.
That is weaker than a standardized `act` claim, but it is not nothing, and it
removes the argument that impersonation leaves no trace. The end-to-end test
asserts this explicitly (Story 7.4) rather than assuming it.

**The hub's own trace is the second audit record.** Every proxied call is already
recorded with server id, method, and timing (`trace/recorder.py`). Adding the
principal's `sub` to the trace entry gives an operator-side answer to "who asked"
that does not depend on the IdP's delegation support at all.

## Consequences

- `mcp/token_exchange.py` takes an optional actor token; server records gain
  `obo_actor_token_source` (`none` | `client_credentials`).
- The default path works against an out-of-the-box Keycloak 26.2+, so the
  docker-compose e2e stack needs no feature flags.
- Downstream servers cannot rely on an `act` claim being present. Any
  authorization example in the docs must key on `sub` + `azp`, not `act`.
- If Keycloak's delegation feature graduates from experimental — or the project
  moves to an IdP with first-class RFC 8693 delegation — flipping the default is
  a config change, not a redesign. That would supersede this ADR.
- `may_act`-style pre-authorization (the subject's token naming a permitted
  actor) is an IdP-side concern and needs no hub support either way.

## Alternatives considered

**Impersonation only.** Rejected: forfeits the stronger shape for a trivial code
saving, and bakes an assumption about the IdP into the hub.

**Delegation only.** Rejected: does not work against a default Keycloak, so the
primary test target could not exercise the primary path.

**Synthesize an actor claim hub-side** (e.g. inject an `X-MCP-Actor` header).
Rejected outright — an unsigned, hub-asserted identity header is exactly the
token-passthrough anti-pattern the MCP authorization spec forbids, and a
downstream server trusting it would be trusting anything that can reach it.
