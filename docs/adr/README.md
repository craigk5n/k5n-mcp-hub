# Architecture Decision Records

Each ADR records one decision: the context that forced it, what was decided, and
what it costs. They are immutable once accepted — if a decision changes, add a
new ADR that supersedes the old one rather than editing history.

Format: `NNNN-short-title.md`, with `Status`, `Context`, `Decision`,
`Consequences`, and `Alternatives considered`.

| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-hub-validates-inbound-tokens.md) | The hub validates inbound tokens itself | Accepted |
| [0002](0002-impersonation-default-delegation-opt-in.md) | Impersonation-shaped exchange by default, delegation opt-in | Accepted |
| [0003](0003-fail-closed-on-token-exchange-failure.md) | Token exchange fails closed | Accepted |
| [0004](0004-background-paths-use-service-identity.md) | Background paths use the service identity | Accepted |
| [0005](0005-hub-is-the-mcp-client-in-ema.md) | The hub plays the MCP Client role in Enterprise-Managed Authorization | Accepted |
| [0006](0006-ema-subject-assertion-source.md) | Where the EMA subject assertion comes from | Accepted |
| [0007](0007-stdio-servers-are-opt-in-and-service-identity-only.md) | stdio servers are opt-in, allowlisted, and service-identity only | Proposed |
| [0008](0008-registry-import-yes-publish-no.md) | The hub imports from the MCP registry but never publishes to it | Proposed |

ADRs 0001–0004 together specify on-behalf-of (OBO) token exchange, tracked as
Epics 5–7 in [`TODO.md`](../../TODO.md). ADRs 0005–0006 cover Enterprise-Managed
Authorization (ID-JAG), tracked as Epic 8. ADR 0007 covers stdio transport,
tracked as Epic 9, and ADR 0008 registry interop, tracked as Epic 10 — still Proposed, so it is a design under review rather than
one the code implements.
