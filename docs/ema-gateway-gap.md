# Draft: EMA has no defined path for a proxying gateway

> **Status: unsent draft.** Written for
> [`modelcontextprotocol/ext-auth`](https://github.com/modelcontextprotocol/ext-auth)
> but not filed. Kept here so the reasoning isn't lost, and so it can be posted later
> without rewriting it.
>
> The gap it describes is already handled in this codebase — see
> [ADR 0006](adr/0006-ema-subject-assertion-source.md) for the decision and
> `tests/test_jwt_auth.py` for the impersonation case it pins. Nothing here is
> outstanding work; it is a question for the extension's maintainers.

## Summary

The Enterprise-Managed Authorization flow assumes the MCP Client holds the identity
assertion first-hand, from its own SSO login. A gateway that proxies MCP on behalf of
an already-authenticated user does not, and the spec currently has no seat for it.

I hit this implementing EMA in an open-source MCP gateway
([k5n-mcp-hub](https://github.com/craigk5n/k5n-mcp-hub)) and would value maintainer
guidance on whether this shape is in scope.

## The mismatch

The flow's "MCP Client" is the application the user logged into. It keeps the ID
Token from that login and later exchanges it (leg 1,
`subject_token_type: urn:ietf:params:oauth:token-type:id_token`).

A proxying gateway is a middlebox. Something else performed the SSO; the gateway sees
only what that party chose to send it. Concretely, a gateway acting as an OAuth 2.1
**resource server** — which the core authorization spec asks it to be — validates the
caller's **access token**. It never receives an ID Token, and there is no defined way
for a client to give it one.

So the gateway occupies the Client role for downstream purposes while not holding
what the Client role assumes it holds.

## What we did, and why it is unsatisfying

Per-server configuration, defaulting to the spec's ID Token, with the caller supplying
it in a hub-specific request header; `access_token` is available as an explicitly
configured alternative, and the call fails closed when the configured assertion is
absent.

The header is the unsatisfying part. It is configuration between one operator and
their own client, not something any MCP client sends by convention, so the
"conformant" path is the one that does not interoperate.

Worth noting: Keycloak's token exchange rejects an ID Token as `subject_token`
outright —

```
{"error":"invalid_request",
 "error_description":"Parameter 'subject_token' supports access tokens only"}
```

— so `access_token` is not merely a shortcut; against at least one major IdP it is the
only thing that works. (Keycloak also cannot issue ID-JAGs at all today; its
`identity-assertion-jwt` support is receiver-side and experimental.)

## A security note, independent of the outcome

Because the caller supplies the assertion, a gateway **must** bind the assertion's
`sub` to the identity it authenticated. Otherwise a caller can present their own
access token alongside someone else's ID Token: leg 1 mints an ID-JAG for that other
subject, and the resource server attributes the call to them. The IdP cannot detect
this — it only ever sees a validly-signed token for the other subject.

We verify the assertion's signature, issuer and expiry against the same JWKS and
require `sub` to match the authenticated caller. `aud` is deliberately not checked,
since an ID Token's audience is the client that requested it, never the gateway.

This may be obvious to implementers who have thought about it, but the extension does
not currently say it, and the failure is silent.

## Questions

1. Is a proxying gateway in scope for Enterprise-Managed Authorization, or
   intentionally out of scope? Either answer is useful; the current text simply does
   not address it.
2. If in scope, should there be a defined way to convey an identity assertion to an
   intermediary — or should the gateway be expected to perform its own SSO and be the
   Client proper?
3. Is `subject_token_type: access_token` an acceptable variation for issuers that
   refuse an ID Token, or does that put an implementation outside the extension?
4. Would the subject-binding requirement above be worth stating normatively for any
   implementation that accepts an assertion it did not obtain itself?

Happy to contribute text or a PR if any of this is worth specifying.
