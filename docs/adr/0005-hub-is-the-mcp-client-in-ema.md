# 0005 — The hub plays the MCP Client role in Enterprise-Managed Authorization

**Status:** Accepted
**Date:** 2026-09-05
**Context:** Enterprise-Managed Authorization (TODO.md Epic 8)

## Context

In June 2026 MCP adopted the Identity Assertion JWT Authorization Grant (ID-JAG,
from Cross-App Access) as its **Enterprise-Managed Authorization** extension,
`io.modelcontextprotocol/enterprise-managed-authorization`. The flow has four
parties and two token legs:

1. **MCP Client** authenticates the user against the **enterprise IdP** by SSO and
   keeps the resulting identity assertion (an OIDC ID Token or SAML assertion).
2. MCP Client → enterprise IdP: RFC 8693 exchange, `requested_token_type` =
   `urn:ietf:params:oauth:token-type:id-jag`, `audience` = the issuer identifier of
   the **Resource Authorization Server**. The IdP evaluates org policy here.
3. MCP Client → **Resource Authorization Server**: RFC 7523,
   `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`, `assertion` = the
   ID-JAG. It returns an access token audience-restricted to the MCP server named in
   the ID-JAG's `resource` claim.
4. MCP Client → **MCP Resource Server**: ordinary calls with that access token.

The hub could plausibly occupy either of two roles:

- **Client role (outbound)** — the hub is the thing that actually calls downstream
  MCP servers, so it performs legs 2 and 3 on the user's behalf.
- **Authorization-server role (inbound)** — the hub accepts ID-JAGs from *its* own
  callers and issues them access tokens, i.e. it becomes the "MCP Authorization
  Server" box in the diagram.

## Decision

**Epic 8 implements the Client role only.** The hub obtains an ID-JAG for a
downstream server and redeems it there. Becoming an Authorization Server that mints
its own access tokens is explicitly out of scope.

The hub keeps its existing inbound posture from ADR 0001: an OAuth *resource
server* that validates access tokens against an IdP's JWKS. That is unchanged.

## Rationale

**The Client role is the continuation of what Epic 6 already built.** Our
on-behalf-of exchange *is* leg 2 in a single-IdP form: RFC 8693, at the IdP, for a
token bound to the downstream audience. Epic 8 adds leg 3 and swaps the requested
token type. The `Principal`, the per-subject cache, the fail-closed error path, the
SSRF-pinned transport, and the admin UI all carry over.

**It closes the gap our current design cannot reach.** Plain RFC 8693 assumes one
IdP that can mint a token for the backend. EMA exists precisely for the case where
the downstream MCP server has its *own* authorization server, in another tenant or
another vendor. That is the case an enterprise deployment actually hits, and today
we simply cannot serve it.

**Becoming an Authorization Server is a different product.** Issuing access tokens
means key management and rotation, token lifetime and revocation policy, consent
records, and being the thing an auditor examines when a token is misused. None of
that exists here, none of it is a small addition, and an organisation deploying EMA
already has an IdP that does it properly. A half-built AS in a gateway is worse than
no AS at all.

**Nothing forecloses the other role.** If the hub ever needs to accept ID-JAGs from
its own callers, the validation half is largely the JWKS machinery from Story 5.2,
and this ADR would be superseded rather than contradicted.

## Consequences

- The hub needs the caller's *identity assertion*, not just their access token. That
  is a genuine new requirement and gets its own decision: see
  [0006](0006-ema-subject-assertion-source.md).
- The hub must discover whether a downstream's authorization server supports the
  profile, via `authorization_grant_profiles_supported` containing
  `urn:ietf:params:oauth:grant-profile:id-jag`. The AS-metadata discovery in
  `mcp/oauth.py` already fetches that document.
- The hub must declare the extension in its outbound `_meta`
  `io.modelcontextprotocol/clientCapabilities.extensions`. The stateless client
  already sends that structure (Epic 2), so this is an addition, not new plumbing.
- Two exchange shapes now coexist: `auth_type: "obo"` (one leg, one IdP) and
  `auth_type: "ema"` (two legs, separate resource AS). They are different enough
  that collapsing them into one code path would obscure both.
- **ID-JAG is an IETF draft, not a finished RFC.** The wire format may still move.
  This is the same posture ADR 0002 took toward delegation: implement it, keep it
  opt-in per server, and pin the URNs as named constants so a draft revision is a
  one-file change.

## Alternatives considered

**Both roles at once.** Rejected for this epic: the AS role is a separate product
with its own operational burden, and shipping it alongside would delay the half that
closes a real gap.

**Authorization-server role only.** Rejected: it would let others' clients reach the
hub under enterprise policy but would not help the hub reach downstream servers,
which is the direction our users actually proxy.

**Wait for the RFC to finalise.** Rejected: MCP has adopted the extension and
gateways in this space already ship it. The draft risk is real but is managed by
opt-in configuration and named constants, not by abstention.
