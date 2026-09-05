# 0003 — Token exchange fails closed

**Status:** Accepted
**Date:** 2026-09-04
**Context:** OBO token exchange (TODO.md Epic 6)

## Context

A token exchange can fail for mundane reasons: the subject token expired, the
IdP is down, the hub's client lost its exchange permission, the requested
audience is not configured. The hub must decide what a proxied call does next.

The tempting answer is to fall back to whatever static credential the server
record already holds — `bearer_token`, basic credentials, or the existing
`client_credentials` OAuth path (`mcp/auth.py:98-160`). The call then succeeds
and the user sees no error.

There is precedent in the codebase for the *opposite* instinct:
`build_authenticator` refuses to start rather than run `auth.type: basic` with an
empty password (`auth/__init__.py:9-24`), and `apply_server_auth` deliberately
leaves `Authorization` unset when an OAuth token fetch fails rather than sending
something weaker.

## Decision

**Fail closed. A failed exchange never falls back to a service credential.**

For a server with `auth_type: "obo"`:

1. Exchange failure → the proxied call is not made. Return `502` with the IdP's
   error surfaced via the existing `format_auth_challenge`
   (`mcp/oauth.py:152-176`).
2. Backend returns `401` on a token that previously worked → invalidate that
   cache entry, re-exchange **exactly once**, retry. A second `401` is returned
   to the caller with the backend's `WWW-Authenticate` challenge intact.
3. No inbound principal on a request to an OBO server → `401` with
   `WWW-Authenticate: Bearer resource_metadata="<protected-resource-metadata-url>"`
   (RFC 9728's challenge parameter), so a spec-compliant MCP client knows where to
   authenticate.
4. Outcomes are recorded on `obo_status` / `obo_error`, mirroring the existing
   `oauth_token_status` / `oauth_token_error` fields and their sanitization
   rules (`models/server.py:91-92`, `sanitize_for_api`).

## Rationale

**Silent fallback is privilege escalation.** The hub's `client_credentials`
identity is a service account — in practice broader than any individual user,
because it must cover every user's needs. Falling back means a request the user
was not entitled to make succeeds under an identity that was entitled to make it,
and the response looks completely normal. That is the single worst failure mode
this feature could have.

**It also destroys the property that motivated OBO.** The reason to build this is
that the backend can authorize and audit per user. A fallback that quietly
reverts to "the hub did it" makes those guarantees hold only when nothing goes
wrong — which is precisely when guarantees matter least.

**Silent fallback is undebuggable.** A misconfigured audience would manifest as
"authorization mysteriously does nothing", months later, with no error anywhere.
Failing loudly at the moment of misconfiguration is cheaper by any measure.

**One retry, not zero and not many.** Zero retries makes ordinary token expiry
during a long-lived stream user-visible. Unbounded retries turn a
down-or-misconfigured IdP into a request amplifier against it. The 401-driven
single re-exchange is the standard shape and bounds the amplification at 2×.

## Consequences

- Users see real errors when the IdP is misconfigured. This is intended and
  should be treated as the feature working, not a regression.
- An IdP outage takes OBO-configured servers down. Servers using static
  credentials are unaffected — they never enter this path.
- The UI needs a distinguishable OBO error state; reuse the existing
  `oauth_token_status` badge pattern rather than inventing a second vocabulary.
- Error text from the IdP reaches the UI, so it must be sanitized like every
  other credential-adjacent field. Token material appearing in an IdP error body
  is a real risk — see Story 7.2 on trace-body redaction.
- Falling back is still available deliberately: an operator who wants
  service-identity behavior registers the server with `auth_type: "oauth"`
  instead of `"obo"`. The choice stays explicit and visible in the registration.

## Alternatives considered

**Fall back to the service credential.** Rejected — privilege escalation,
invisible in the response, and it negates the feature's purpose.

**Configurable per server (`obo_fallback: true|false`).** Rejected. The safe
value is the only defensible default, and the unsafe value is indistinguishable
from `auth_type: "oauth"`, which already exists. A flag would add a second way to
express something the model can already say, with the added hazard that it can be
switched on without re-examining the registration.
