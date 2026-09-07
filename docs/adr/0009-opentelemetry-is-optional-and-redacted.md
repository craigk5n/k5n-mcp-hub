# 0009 — OpenTelemetry is optional, off by default, and redacted

**Status:** Proposed
**Date:** 2026-09-07
**Context:** OpenTelemetry traces and metrics (TODO.md Epic 11)

## Context

The hub exposes four unlabelled counters at `/metrics` (`metrics.py`) and records
request/response bodies for the admin UI (`trace/recorder.py`). Neither answers the
question an operator has in production — *which* server is slow, *whose* calls are
failing, where a proxied request spent its time — and neither leaves the process in a
form anything else can consume.

OpenTelemetry does answer those. It also changes the hub's data-handling posture in a
way none of its other features do: **it sends data about every request to a third
party**. That is the decision to get right; the instrumentation itself is ordinary.

Three specific tensions:

### 1. The word "trace" is already taken

`trace/recorder.py` captures request and response *bodies* and renders them in the
admin UI. OTel traces are spans. They are different things with the same name, and
an operator reading "traces" in this codebase's docs must not have to guess which.

### 2. Spans naturally attract exactly what must not leave

The interesting attributes here are the dangerous ones. A proxied call knows the
target server, the caller's `sub`, the OBO exchange audience, and the outbound URL.
Of those:

- The **subject** is a user identity. Exporting it to a collector means every user's
  activity is now in a second system with its own retention and access control, one
  the hub's operator may not even run.
- **Tokens** appear in headers on every path OBO touches. `sanitize_trace_body`
  exists because the hub already learned that credentials turn up inside error text.
- **URLs** can carry credentials in query strings.

An exporter is a much worse place to leak these than the admin UI: the UI shows them
to an authenticated admin looking at one server, while an exporter ships them
continuously to somewhere else.

### 3. A dependency that most installs will not use

`opentelemetry-sdk` plus an OTLP exporter is a substantial dependency tree for a
local-first tool where the common case is one developer with no collector running.
CI's clean-install gate exists to keep declared dependencies honest, and adding a
tree nobody uses to satisfy an optional feature works against it.

Worth being precise, because it changes what "optional" costs: **`opentelemetry-api`
is already installed**, as a transitive dependency of `mcp`. Only the SDK, the
exporter and their proto packages are added by the extra. So the API's types (`Status`,
`StatusCode`) can be relied on unconditionally, and the thing being made optional is
the exporting machinery rather than OpenTelemetry as such. (Discovered by removing the
packages to simulate a default install, which broke `mcp` — a good reminder that a
dependency claim is worth checking rather than assuming.)

## Decision

**Optional dependency, off by default, and nothing sensitive in a span unless the
operator asks for it by name.**

1. **`otel` is an install extra, not a base dependency.** `pip install k5n-mcp-hub`
   is unchanged; `pip install k5n-mcp-hub[otel]` adds the SDK and OTLP exporter. The
   dev extra includes it so the suite exercises the real thing rather than a stub.

2. **`otel.enabled` defaults to false, and enabling it without the extra installed is
   a startup failure**, naming the extra. A telemetry feature that silently does
   nothing is worse than one that is off: the operator believes they have visibility
   they do not have.

3. **The config section is `otel`, and the docs say "OpenTelemetry" wherever they
   might otherwise say "tracing".** `trace.*` keeps meaning request/response capture.
   Renaming the existing one would break a documented public contract for a
   cosmetic gain.

4. **Spans carry the server id, not the caller.** `mcp.server.id`, method, outcome and
   duration are exported by default. The subject is **not**, unless
   `otel.include_subject: true` — an explicit statement that the operator accepts
   user identities leaving for their collector. Even then it is the `sub` claim only,
   never a token, name or email.

5. **No span attribute is ever built from a header, body, or credential field**, and
   outbound URLs are exported with query strings stripped. This is the same rule
   `sanitize_trace_body` enforces for the UI, applied at a boundary that is harder to
   inspect after the fact.

6. **`/metrics` keeps working exactly as it does now.** OTel metrics are additive.
   The existing endpoint is a documented contract, and an operator who scrapes it
   should not have to care that this feature exists.

7. **Explicit spans at meaningful boundaries, not blanket auto-instrumentation.** The
   FastAPI/httpx instrumentation packages are still pre-1.0 (`0.65b0` against a stable
   `1.44.0` SDK), and they capture route and header detail the hub has deliberate
   opinions about. Hand-placed spans on the proxy path, discovery, health checks and
   token exchange are fewer, more meaningful, and cannot surprise us by exporting
   something new after a minor version bump.

## Consequences

- Operators who want observability install an extra and set two settings. That is
  slightly more friction than auto-instrumentation, and it buys a system that exports
  only what was chosen deliberately.
- The correlation story is `X-Request-ID` ↔ span, so an entry in the admin UI's trace
  view can be found in the collector and vice versa. Without that the two systems
  describe the same request with no way to line them up.
- Turning `include_subject` on makes the collector subject to whatever policy covers
  user identities. The setting is named so nobody enables it by accident, and the
  docs say what it implies rather than describing it as an extra attribute.
- If the OTel SDK's API changes, the blast radius is the shim module rather than
  every call site.
