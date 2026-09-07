# 0008 — The hub imports from the MCP registry but never publishes to it

**Status:** Proposed
**Date:** 2026-09-06
**Context:** MCP registry interop (TODO.md Epic 10)

## Context

The official registry at `registry.modelcontextprotocol.io` is a public index of MCP
servers. Its API (confirmed live, 2026-09-06) offers both directions:

```
GET   /v0.1/servers?search=&cursor=&limit=&version=latest&updated_since=
GET   /v0.1/servers/{serverName}/versions/{version}
POST  /v0.1/publish                     ← requires a Registry JWT
POST  /v0.1/auth/{dns,github-at,github-oidc,http,oidc}
POST  /v0.1/validate
```

A record carries `name` (reverse-DNS namespaced, e.g. `ai.smithery/foo`),
`description`, `version`, `repository`, and then either `remotes[]` — a URL plus a
transport and header hints — or `packages[]`, which are npm/PyPI programs whose
`transport.type` is `stdio`.

"Interop (import/export)" reads as symmetric. It is not, and the asymmetry is the
whole decision.

### Import is a natural fit

A `remotes[]` entry is exactly what `POST /v1/register` already takes: a URL, a name,
a description. Finding a server and registering it is the flow the admin UI exists
for, and the registry is a much better source than pasting URLs from a README.

### Publishing is a disclosure primitive pointed at the operator

The hub's registry is not a catalogue of things worth sharing. It is an operational
record of what this hub proxies, and on a real deployment it contains:

- internal URLs (`http://octopus/webcalendar/mcp.php`, `http://127.0.1.1/...`)
- bearer tokens, basic-auth passwords, and OAuth client secrets
- `required_scope` values that describe an organisation's authorization model

The public registry is a **public, append-only index**. A button that pushes hub
entries to it converts an admin action into an irreversible publication of internal
topology. The failure is silent and total: nobody notices until someone else's
crawler has the URL.

Two further problems make it not merely risky but mostly impossible:

- **Namespace ownership.** Publishing requires a Registry JWT obtained by proving
  control of the namespace via DNS, GitHub, or OIDC. An operator cannot publish
  `ai.smithery/foo` because they registered it in their hub; they can only publish
  under a namespace they own. So "export what I have" does not translate.
- **Registering is not endorsing.** Adding a server to a hub is operational. Listing
  it publicly is editorial — a claim about what the thing is and that it should be
  used. Conflating them means an operational click makes an editorial claim.

## Decision

**Import from the registry. Never publish to it. Export to a file instead.**

1. **Import is a first-class flow.** Search the registry from the admin UI, pick a
   server, and register it. The imported URL goes through exactly the same
   `is_url_safe_for_discovery` validation as a hand-typed one — a registry record is
   attacker-influenceable input, and a `remotes[]` URL pointing at `169.254.169.254`
   must be refused for the same reasons a typed one is.

2. **No `POST /v0.1/publish`, and no code that could grow into it.** The hub does not
   hold Registry JWTs, does not implement the auth exchanges, and has no publish
   button. This is a deliberate absence, not a missing feature.

3. **Export means writing `server.json`, not pushing it.** An operator who genuinely
   wants to publish gets a file in the registry's own schema, which they review and
   publish themselves with the official CLI. Review is the point: the file is the
   moment a human sees what would become public. Secrets are never written to it —
   the same redaction the API responses use — and the export names any private-range
   or loopback URL it emitted, since those are the entries most likely to be a
   mistake.

4. **Package-based (stdio) records import as a suggestion, not a registration.** A
   `packages[]` record names a program, and ADR 0007 requires programs to come from
   the operator's `stdio.allowed_commands` rather than from a request — a registry
   record is exactly the untrusted request that rule exists to refuse. So the import
   UI shows the command it *would* need and tells the operator to add it to config.
   The allowlist would be worthless if an import could write to it.

## Consequences

- The feature is one-directional, and the docs say so plainly rather than leaving
  someone to discover that "export" does not mean "publish".
- Operators who want their servers in the public registry use the official tooling,
  which is also where namespace proof lives. The hub does not reimplement that.
- The import path needs pagination (`metadata.nextCursor`) and should default to
  `version=latest`, since the list otherwise returns one row per version.
- Registry records carry differing `$schema` values (2025-09-16, 2025-09-29 and
  2025-12-11 all appear in the live index today), so the parser must tolerate schema
  drift and ignore fields it does not know — the same leniency the capability parser
  already applies to non-conformant servers.
