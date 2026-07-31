# TODO

## Docker image & Docker Hub publishing

Goal: make `k5n-mcp-hub` trivially runnable via Docker, then publish the image to Docker Hub
under the **k5n** account (use the k5n Docker Hub credentials) so users can `docker run` it
without a local Python setup.

- [ ] Build and smoke-test the image locally (the `Dockerfile` already exists):
      `docker build -t k5n-mcp-hub:dev . && docker run --rm -p 8080:8080 k5n-mcp-hub:dev`
- [ ] Decide the published image name/namespace (likely `k5n/k5n-mcp-hub`; confirm the exact
      Docker Hub org/user for the k5n credentials).
- [ ] Tag strategy: push both `:latest` and a version tag (`:0.1.0`), tied to the app version.
- [ ] Publish to Docker Hub with the k5n credentials
      (`docker login` as k5n → `docker push k5n/k5n-mcp-hub:<tag>`).
- [ ] (Optional) Automate build+push in CI (`.forgejo/workflows/`) on tagged releases, using
      the k5n credentials stored as CI secrets — do NOT hardcode them.
- [ ] (Optional) Multi-arch build (`docker buildx` for `linux/amd64,linux/arm64`).

### README: add a "Run with Docker" section with a variety of commands

Once the image is published, document several ways to run it, e.g.:

- Default (local-first, no auth):
  `docker run --rm -p 8080:8080 k5n/k5n-mcp-hub`
- Custom port:
  `docker run --rm -p 9000:9000 -e SERVER_HTTP_PORT=9000 k5n/k5n-mcp-hub`
- Reach MCP servers on the host (localhost/LAN):
  `docker run --rm -p 8080:8080 --network host k5n/k5n-mcp-hub`  (Linux; local-first mode)
- Mount a custom config:
  `docker run --rm -p 8080:8080 -v "$PWD/config.yaml:/app/config.yaml" k5n/k5n-mcp-hub`
- Enable basic auth for a shared deployment (password via env, never baked in):
  `docker run --rm -p 8080:8080 -e MCPHUB_AUTH__TYPE=basic -e MCPHUB_AUTH__BASIC_AUTH__REGISTER_PASS=... k5n/k5n-mcp-hub`
- JSON file storage persisted to a volume:
  `docker run --rm -p 8080:8080 -e MCPHUB_STORAGE__TYPE=json -e MCPHUB_STORAGE__JSON__PATH=/data/servers.json -v k5n_mcp_hub_data:/data k5n/k5n-mcp-hub`

> Note: for an internet-exposed deployment, also set `security.allow_private_networks: false`
> and review the outstanding security items below.

## MCP spec 2026-07-28 support

Goal: support servers and clients speaking the new **stateless** MCP revision
(2026-07-28) while keeping full compatibility with the currently supported
`2025-11-25` / `2025-06-18` paths. The revision removes the
`initialize`/`notifications/initialized` handshake and `Mcp-Session-Id`, adds a
`server/discover` RPC, replaces the GET/SSE endpoint with `subscriptions/listen`,
removes `ping` and `logging/setLevel`, requires `Mcp-Method`/`Mcp-Name` headers on
Streamable HTTP POSTs, and requires `resultType` on results.
Changelog: <https://modelcontextprotocol.io/specification/2026-07-28/changelog>

All stories follow TDD (RED → GREEN → IMPROVE): write/extend the named tests
first, watch them fail, then implement. Version support is additive — never
break the existing negotiated paths (public contract is stable per CLAUDE.md).

### Epic 1 — Version recognition & validation groundwork (no behavior change)

**Story 1.1 — Recognize 2026-07-28 as a supported protocol version**

As a hub operator, I want servers negotiating 2026-07-28 to show as supported
so the UI doesn't display a misleading amber "unsupported" badge.

- TDD: extend `tests/test_mcp_constants.py` and `tests/test_health_badge.py`
  first (new version in the supported set, badge renders "supported").
- Acceptance criteria:
  - [x] `is_supported_protocol_version("2026-07-28")` is true; `2025-11-25`
        and `2025-06-18` remain supported (`src/mcp_hub/mcp/constants.py:1-3`).
  - [x] `mcp_version_status()` no longer lumps *newer-than-supported* versions
        into "unsupported" — a distinct `newer` (or similar) status, with a
        sensible badge in `templates/_health_badge.html`.
  - [x] Hardcoded version strings in `tests/test_jsonrpc.py`,
        `tests/test_sdk_client.py`, `tests/test_discovery.py`,
        `tests/test_proxy_handler.py`, `tests/test_ui_capabilities.py` updated
        to reference the constants, not literals, where feasible.
  - [x] `ruff` / `mypy` / full `pytest` green.

**Story 1.2 — Version-aware MCP method registry**

As a developer using the request validator, I want method validity to depend on
the negotiated protocol version so new methods don't warn and removed methods do.

- TDD: extend `tests/test_mcp_constants.py` (13-method assertion at `:81-100`
  becomes per-version) and `tests/test_jsonrpc.py` first.
- Acceptance criteria:
  - [x] `server/discover` and `subscriptions/listen` are valid methods for
        2026-07-28; `resources/subscribe`/`unsubscribe`, `elicitation/create`,
        `roots/list` added for the versions where they exist.
  - [x] `ping`, `logging/setLevel`, `initialize`, `notifications/initialized`
        produce a validation warning when used against a 2026-07-28 server.
  - [x] Validator API stays backward compatible for callers that pass no
        version (defaults to current union behavior — no new warnings for
        existing valid traffic).

**Story 1.3 — `resultType` and error-code validation updates**

As a user of the playground/validator, I want responses checked against the new
result and error rules.

- TDD: extend `tests/test_jsonrpc.py` first.
- Acceptance criteria:
  - [x] Results missing `resultType` are treated as `"complete"` (spec rule for
        earlier-protocol servers); `"input_required"` recognized and not
        flagged as an error shape.
  - [x] Error-code constants updated: resource-not-found is `-32602`; new
        MCP-reserved range `-32020..-32099` known to the validator
        (`HeaderMismatch` `-32020`, `MissingRequiredClientCapability` `-32021`,
        `UnsupportedProtocolVersion` `-32022`).
  - [x] `fault_injection.py:81` uses the named constant instead of a literal.

**Story 1.4 — Modernize test fixtures**

As a contributor, I want the fake MCP server in tests to speak current
revisions so tests exercise realistic negotiation.

- Acceptance criteria:
  - [x] `tests/conftest.py:128` fake server no longer answers `2024-11-05`;
        parameterizable per test (legacy, 2025-11-25, and a stateless
        2026-07-28 mode that rejects `initialize` and implements
        `server/discover`).
  - [x] Existing tests pass unchanged against the default fixture mode.

### Epic 2 — Stateless client path (discovery, health, UI, scripts)

**Story 2.1 — `server/discover` probe with `initialize` fallback in discovery**

As a hub operator, I want discovery to detect and record 2026-07-28 servers.

- TDD: extend `tests/test_discovery.py` first using the Story 1.4 stateless
  fixture (RED: discovery against a stateless server currently fails/records
  nothing).
- Acceptance criteria:
  - [x] Discovery tries `server/discover` first; on method-not-found falls back
        to the existing `initialize` handshake (`mcp/discovery.py`,
        `mcp/sdk_client.py`).
  - [x] Negotiated/advertised version stored in
        `RegisteredServer.mcp_protocol_version` from either path; the four
        writer code paths (discovery, ui_servers, ui_initialize, ui_invoke)
        share one helper so they can't drift.
  - [x] `tools/prompts/resources` lists fetched statelessly (single POST with
        `_meta` version/capabilities) for 2026-07-28 servers.
  - [x] Register-merge behavior preserved (empty lists don't clobber
        previously discovered ones).

**Story 2.2 — Health checks without `ping`**

As a hub operator, I want health checks to work for stateless servers, where
`ping` no longer exists.

- TDD: extend `tests/` health-checker tests first.
- Acceptance criteria:
  - [x] For 2026-07-28 servers the fallback probe is `server/discover`
        (replacing the current full-`initialize` probe in
        `sdk_client.py:346-367` / `health/checker.py:190-213`).
  - [x] Legacy servers keep the existing probe; 429 → "degraded" behavior
        unchanged.

**Story 2.3 — Stateless request mode in the UI (invoke, playground, initialize panel)**

As a UI user, I want to exercise 2026-07-28 servers without a handshake.

- TDD: extend `tests/test_ui_playground.py` / ui_invoke tests first.
- Acceptance criteria:
  - [x] `ui_invoke.py` skips `initialize`/`initialized` and sends
        `io.modelcontextprotocol/protocolVersion` (+ client info/capabilities)
        in `_meta` when the server's stored version is 2026-07-28.
  - [x] Playground exposes the `_meta` version fields; session-id field hidden
        or marked legacy-only for stateless servers.
  - [x] The "Initialize" inspection panel gains a "Discover" mode that issues
        `server/discover` and persists the advertised version (reusing the
        Story 2.1 helper).
  - [x] Legacy servers keep today's exact three-step flow (regression tests).

**Story 2.4 — Generated client scripts support stateless mode**

- TDD: extend the downloads/tool-script tests first.
- Acceptance criteria:
  - [x] `tool_script.py.j2` / `tool_script.sh.j2` emit the stateless single-POST
        flow when the server's recorded version is 2026-07-28, and the legacy
        3-step flow otherwise.
  - [x] `tests/test_readme.py` and documented commands stay accurate.

**Story 2.5 — Honor `ttlMs` cache hints in discovery pacing** *(nice-to-have)*

- Acceptance criteria:
  - [x] When list results carry `ttlMs`, discovery does not re-poll that
        server before expiry (bounded below by the configured interval).
  - [x] Missing `ttlMs` → today's fixed 30s interval, unchanged.

### Epic 3 — Proxy & trace hardening

**Story 3.1 — Inject required `Mcp-Method` / `Mcp-Name` headers**

As a proxy user, I want the hub to satisfy the new required-header rule for
clients that don't set them.

- TDD: extend `tests/test_proxy_handler.py` first.
- Acceptance criteria:
  - [x] For POSTs to 2026-07-28 backends, the proxy injects `Mcp-Method` (and
        `Mcp-Name` where derivable from the JSON-RPC body) only when absent —
        mirroring the existing `MCP-Protocol-Version` injection at
        `proxy/handler.py:79-80`.
  - [x] Client-supplied headers are never overwritten; legacy backends see no
        new headers.

**Story 3.2 — Long-lived stream safety for `subscriptions/listen`**

As a trace user, I want verbose tracing to not buffer unbounded streams.

- TDD: add a proxy test with a never-ending SSE body first (RED: current code
  at `proxy/handler.py:187-226` drains the whole stream before responding).
- Acceptance criteria:
  - [x] With `trace_verbose` + `capture_sse` on, streamed responses are teed
        with a capture cap (reuse `trace.body_limit`) instead of drained;
        first bytes reach the client without waiting for stream close.
  - [x] Captured trace marks truncation explicitly.

**Story 3.3 — Treat `Mcp-Session-Id` as sensitive in traces** *(legacy path)*

- Acceptance criteria:
  - [x] `mcp-session-id` added to `SENSITIVE_HEADERS` in `trace/recorder.py`;
        redacted in trace UI and API output; test added.

### Epic 4 — SDK upgrade & advanced features *(blocked: stable `mcp` SDK release with 2026-07-28 support)*

**Story 4.1 — Adopt the official SDK's stateless client**

- Acceptance criteria:
  - [ ] `pyproject.toml` bumps `mcp` to the first stable 2026-07-28 release
        (with an upper bound this time); the dual-import fallbacks in
        `sdk_client.py:16-34` are removed if no longer needed.
  - [ ] `sdk_client.py` uses the SDK's stateless transport for 2026-07-28
        servers; hardcoded `transport_type = "sse"` (`:271`) and the duplicate
        `InitializedNotification` (`:283-285`) fixed.
  - [ ] CI's clean-venv gate passes (no undeclared imports).

**Story 4.2 — Pagination for list endpoints** *(pre-existing gap, more visible now)*

- Acceptance criteria:
  - [ ] Discovery follows `nextCursor` across all pages for tools/prompts/
        resources; test with a paginating fake server.

**Story 4.3 — Surface MRTR (`input_required`) in the playground** *(later)*

- Acceptance criteria:
  - [ ] A `resultType: "input_required"` response renders its `inputRequests`
        and lets the user supply `inputResponses` on a retry of the original
        request.

## Security follow-ups (from AUDIT_local.md)

Two CRITICALs are fixed (safe auth defaults + no hardcoded password; SSRF flag no longer a
process-global). The three audit follow-ups below are now done — see `AUDIT_local.md` §2:

- [x] Apply the SSRF-pinned transport to the reverse proxy, health checker, and the OAuth
      token-endpoint flow (previously bare httpx clients). Also pinned the agent-card fetch
      (`agents/card.py`) and the `ui_initialize` probe, which were the same SSRF class.
      `allow_private_networks` is threaded explicitly to every outbound path (default False =
      fail safe); `follow_redirects=False` everywhere so a 3xx can't bypass the pin.
- [x] Add auth (`Depends(auth_dependency)`) to the powerful UI routes (playground, faults,
      capabilities, trace, initialize) so `auth.type: basic` actually protects them. No-op
      under the default `auth.type: none`, so local use is unchanged.
- [x] Redact sensitive headers (not just `Authorization`) in trace capture — now also
      `X-MCP-Token` (which this hub forwards tokens in), `Cookie`/`Set-Cookie`, `X-Api-Key`,
      `Api-Key`, `X-Auth-Token`, `X-Access-Token`, `X-Amz-Security-Token`, `Proxy-Authorization`.

Still open for an exposed/multi-tenant deployment:

- [ ] Redact sensitive fields inside trace *bodies* (only headers are redacted today).

## Product / usefulness follow-ups (from AUDIT_local.md §3)

- [ ] Interop with the official MCP registry API (import/export).
- [ ] Emit OpenTelemetry traces/metrics alongside `/metrics`.
- [ ] Support stdio MCP servers (currently HTTP-only).
- [ ] Lead with fault injection as the headline differentiator.
