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

ADRs 0001–0004 together specify on-behalf-of (OBO) token exchange, tracked as
Epics 5–7 in [`TODO.md`](../../TODO.md).
