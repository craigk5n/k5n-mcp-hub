# 0006 — Where the EMA subject assertion comes from

**Status:** Accepted
**Date:** 2026-09-05
**Context:** Enterprise-Managed Authorization (TODO.md Epic 8)

## Context

Leg 2 of Enterprise-Managed Authorization is an RFC 8693 exchange whose
`subject_token_type` the extension gives as
`urn:ietf:params:oauth:token-type:id_token` — an OIDC **ID Token**, the identity
assertion the MCP Client kept from its own SSO login.

The hub does not have one. Per ADR 0001 it is an OAuth *resource server*: it
validates the caller's **access token**, and `Principal.token` is that access token.
An access token is not an ID token, and no part of the current flow ever produces
one.

Underneath this is a real mismatch worth naming rather than papering over. **The EMA
flow does not model a proxying gateway.** Its "MCP Client" is the application the
user logged into, which holds the identity assertion first-hand. The hub is a
middlebox: something else performed the SSO, and the hub sees only what that party
chose to send it. We occupy the Client role for downstream purposes (ADR 0005)
without holding what the Client role assumes it holds.

Four ways out:

1. **Accept an ID Token from the caller** alongside the access token.
2. **Send the access token as the subject token** (`subject_token_type` =
   `...:token-type:access_token`). Many IdPs accept this for exchange; it is not what
   the extension specifies.
3. **Have the hub perform its own OIDC login** for the user — impossible on a
   proxied call, which has no user agent to redirect.
4. **Refuse EMA entirely** for proxied traffic.

## Decision

**The subject assertion is configured per server, defaults to the spec's ID Token,
and the hub fails closed when it does not have one.**

- `ema_subject_token_type` on the server record: `id_token` (default) or
  `access_token`.
- With `id_token`, the hub uses an ID Token the caller supplied on the request. It
  is read from a documented header, kept on the `Principal`, and treated as secret:
  never logged, traced, or persisted, exactly like `Principal.token`.
- With `access_token`, the hub sends the access token it already validated. This is
  a deliberate, per-server departure from the extension for issuers that support it,
  and the field name says so.
- If the configured assertion is absent, the call fails closed with a 401 — the same
  shape as an OBO call with no user identity (ADR 0003). It never silently falls back
  to the other token type, and never to a service credential.

Option 3 is rejected outright, and option 4 is what the default effectively gives
until an operator configures a source.

## Rationale

**Defaulting to the spec keeps us conformant where we can be.** An operator whose
MCP client can forward an ID Token gets exactly the flow the extension describes.

**Allowing the access token is honest about the deployments that exist.** Plenty of
MCP clients will never forward an ID Token, and several IdPs happily exchange an
access token. Making that an explicit, named, per-server setting is better than
either pretending it isn't a departure or refusing to serve those deployments.

**Silent fallback between token types would be the worst option.** The two produce
different `sub` semantics and different IdP policy evaluation. A hub that quietly
tried the other one on failure would make the effective identity depend on which
attempt happened to succeed — unauditable, and precisely the class of surprise ADR
0003 exists to prevent.

**Failing closed is consistent with everything else here.** An EMA server that
cannot obtain an assertion is in the same position as an OBO server with no user
identity, and gets the same answer.

## Consequences

- `Principal` gains an optional `id_token`, secret on the same terms as `token`:
  redacted in `__repr__`, never persisted, never traced.
- A documented request header carries the ID Token inbound. It must be added to
  `SENSITIVE_HEADERS` in `trace/recorder.py`, and the token body redaction from
  Story 7.2 already covers `id_token` as a body field.
- The header is non-standard. It is hub-specific configuration, not something we can
  claim MCP clients will send, and the documentation must say so plainly rather than
  implying interoperability we do not have.
- The mismatch above is worth reporting upstream: a gateway that proxies MCP on
  behalf of an already-authenticated user is a real deployment shape, and the
  extension currently has no seat for it. Doing so is better than quietly inventing
  a local convention and calling it support.

## Alternatives considered

**Access token only, always.** Simpler, and it needs no new header — but it makes
the hub non-conformant by construction with no path to conformance, for a saving of
one optional field.

**ID Token only, no alternative.** The purest option, and it would make EMA unusable
for every client that does not forward one, which today is most of them.

**Derive an ID Token from the access token** by calling the IdP's userinfo endpoint
and minting one. Rejected: the hub would be asserting an identity it is not
authorised to assert, which is exactly the unsigned-actor-header anti-pattern ADR
0002 refused.
