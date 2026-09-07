# 0007 — stdio servers are opt-in, allowlisted, and service-identity only

**Status:** Proposed
**Date:** 2026-09-06
**Context:** stdio MCP server support (TODO.md Epic 9)

## Context

The hub is HTTP-only. A large share of published MCP servers ship as stdio
programs (`npx some-server`, `uvx another`), so today they simply cannot be
registered. Supporting them changes two things that no other backend type does.

### 1. Registering a server becomes running a program

Every existing backend is a URL. The hub connects out to it, and the blast radius
of a hostile registration is bounded by the SSRF-pinned transport
(`utils.set_allow_private_networks`) — an attacker can make the hub *fetch*
things, which is why that guard exists.

A stdio server is a **command line**. Registering one is asking the hub to fork
and exec it. The blast radius is arbitrary code execution as the hub's user.

That collides with how registration is currently authorized:

```python
def is_admin(principal: Principal, auth: AuthConfig) -> bool:
    if not authorization_enforced(auth):   # auth.type != "jwt"
        return True                        # everyone is an admin
```

`authorization_enforced()` is true only under `auth.type: jwt`. The shipped
default is `auth.type: none`, so `require_admin` on `POST /v1/register` is a
no-op in the default configuration. That is a defensible choice today — `none` is
a single-user local mode, and the worst a stray registration achieves is a fetch.
It stops being defensible the moment registration can exec.

This is not hypothetical. The hub warns at startup when it is bound non-loopback
with no authentication, and that warning fires on a realistic local setup — a hub
on `0.0.0.0:3000` with `auth.type: none`, reachable from every host on the LAN.
Adding stdio to that configuration turns a local convenience into remote code
execution for anyone on the network.

### 2. There is no per-request identity on a subprocess

On-behalf-of works because each proxied HTTP request carries the caller's token,
exchanged per caller (`mcp/auth.py` rule 0). A stdio server has no request
headers. Its credentials come from `env` at spawn time and are fixed for the life
of the process, so **one process cannot serve two callers as themselves**.

Three ways out, none free:

1. **One process per caller.** Real isolation, but the hub now runs an unbounded
   number of subprocesses keyed by user, each holding a warm session. Memory and
   process-table cost scale with users, and eviction becomes a correctness
   problem rather than a tuning one.
2. **Pass the caller's token in `env` per process.** Same process explosion, plus
   the token is then readable through `/proc/<pid>/environ` by any process of the
   same user, and inherited by anything the server itself spawns.
3. **Declare stdio service-identity only.** One process per server, shared by all
   callers, running under whatever credential the operator configured.

## Decision

**stdio support ships disabled, allowlisted when enabled, and never speaks
on-behalf-of.**

1. **Off by default.** A new `stdio.enabled` config flag, defaulting to `false`.
   With it false, `POST /v1/register` refuses a stdio registration outright, so
   an existing deployment cannot acquire an exec primitive by upgrading.

2. **Refuse to enable it while untrusted callers could reach registration.** The
   hub **fails to start** with `stdio.enabled` unless at least one of these holds:

   | Condition | Why it closes the hole |
   |---|---|
   | `auth.type: jwt` | registration genuinely requires the admin scope |
   | a loopback bind (`127.0.0.1`, `::1`, `localhost`) | nothing off-box can reach the endpoint at all |
   | `stdio.trusted_network: true` | the operator states explicitly that untrusted callers cannot reach this hub |

   Starting with a warning instead would put an RCE endpoint on the network of
   anyone who skims a release note. `--dev` does **not** relax this.

   The third condition exists because the hub cannot observe its own reachability.
   In a container it binds `0.0.0.0` internally while the port may be published as
   `-p 127.0.0.1:3001:8080`, which is loopback-only in practice and indistinguishable
   from full exposure from inside. Rather than guess — container detection is both
   unreliable and the wrong question — the operator makes the claim, under a name
   that states what is being claimed.

   *(Amended 2026-09-06. The original decision was `auth.type: jwt` only. That made
   the feature untestable without an IdP, which surfaced immediately: the first
   attempt to run a stdio instance locally was blocked by it. Requiring an IdP to
   try a feature is not a security control, it is a reason to skip the feature.)*

3. **Commands come from an operator allowlist, not from the request.** The
   registration body names an allowlist *entry*, not a command line.
   `stdio.allowed_commands` maps a name to an executable and its fixed arguments.
   A caller may supply neither the binary nor its arguments, which keeps argument
   injection (`--config=/etc/shadow`, `-c 'import os'`) out of reach by
   construction rather than by validation. Environment variables are likewise
   operator-supplied.

4. **stdio servers are service-identity only.** `auth_type: obo` and
   `auth_type: ema` are rejected at registration for a stdio server, with a
   message saying why. One shared process cannot act as two different users, and
   the failure mode of pretending otherwise is that every caller silently gets
   the credentials of whoever the process was started with — precisely the
   privilege escalation on-behalf-of exists to prevent (see
   [ADR 0003](0003-fail-closed-on-token-exchange-failure.md)).

   Multi-tenant `required_scope` still applies: it governs who may *reach* the
   server, which is orthogonal to what identity the server sees.

5. **One long-lived process per registered server**, lazily started and restarted
   on exit, not one per call. Option 1 above is the only design that could offer
   per-user identity, and it is rejected for now on cost; if per-user stdio is
   ever wanted, it should arrive as an explicit `stdio.isolation: per-caller`
   mode with a documented process ceiling, not as a silent default.

## Consequences

- A stdio server's tools are discovered and proxied like any other, and appear in
  the admin UI with the same badges. The server card must show *service identity*
  plainly, so an operator reading a tool list never assumes per-user enforcement
  they are not getting.
- Operators who want stdio must either run real authentication or keep the hub
  off the network. That is a real adoption cost, and it is the point.
- **A container is the recommended way to run stdio**, and is stronger than any of
  the conditions above. Those control *who can trigger* an exec; a container bounds
  *what the exec can reach* — the host filesystem, the operator's credentials, the
  other services on the machine. It also keeps the toolchains stdio servers need
  (node, uv) off the host. `docker-compose.stdio.yml` and
  `config.stdio.example.yaml` are that setup. This does not make the allowlist
  redundant: the container limits blast radius, the allowlist limits what runs.
- The allowlist means "register any npm MCP server from the UI" is not a
  supported flow. Adding a server is a config change plus a restart. This is
  worse for demos and better for every other case; revisit only with a sandbox
  (container or seccomp) actually in place.
- `RegisteredServer.url` stops being universally meaningful. It gains a synthetic
  `stdio:<name>` form so existing storage, trace and UI code paths that key off
  `url` keep working without a schema migration.
