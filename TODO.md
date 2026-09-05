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
- [ ] (Optional) Automate build+push in CI (`.github/workflows/`) on tagged releases, using
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

### Epic 4 — SDK upgrade & advanced features *(UNBLOCKED: `mcp` 2.0.0 is out)*

> **2026-07-31:** `mcp` 2.0.0 shipped on PyPI with the 2026-07-28 APIs — and
> renamed the client APIs this codebase uses (`streamablehttp_client` →
> `streamable_http_client`, `InitializeResult.serverInfo` → `server_info`,
> `ClientNotification` no longer callable), which broke CI's clean-install
> mypy gate. `pyproject.toml` now pins `mcp>=1.0,<2`; Story 4.1 lifts the pin
> and adopts the 2.0 client.

**Story 4.1 — Adopt the official SDK's stateless client**

- Acceptance criteria:
  - [ ] `pyproject.toml` bumps `mcp` to the first stable 2026-07-28 release
        (with an upper bound this time); the dual-import fallbacks in
        `_get_streamable_http_client()` (`sdk_client.py:17-34`) are removed if no
        longer needed.
  - [ ] `sdk_client.py` uses the SDK's stateless transport for 2026-07-28
        servers; the hardcoded `self._transport_type = "sse"` (`:286`) and the
        `InitializedNotification` import inside `handshake()` (`:296-299`) fixed.

> Line numbers above were re-checked 2026-09-05; `:271` and `:283-285` had drifted
> by ~15 lines after Story 5.5 added the `caller` parameter to this file. Prefer the
> named symbols over the line numbers — the symbols are what actually pin the work.
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

## OAuth on-behalf-of (OBO) token exchange

Goal: let the hub call a downstream MCP server **as the user who called the hub**
rather than as its own service identity. Today `mcp/auth.py` speaks
`grant_type=client_credentials` only, so every proxied call arrives at the backend
as "the hub" — a backend can neither authorize per user nor audit who asked. The
target is RFC 8693 token exchange: the hub validates the caller's access token,
swaps it at the IdP for one whose `aud` is the downstream server, and forwards
that.

This also closes an MCP authorization-spec gap. The spec forbids token passthrough
(a server "MUST NOT accept any tokens that were not explicitly issued for the MCP
server") and wants audience-bound tokens via resource indicators (RFC 8707).
`proxy/handler.py:80-82` already strips inbound `Authorization` correctly; OBO is
what lets it put something legitimate back.

Design decisions are recorded in [`docs/adr/`](docs/adr/README.md) — read these
before starting, they answer the questions each epic assumes settled:

| ADR | Decision |
|-----|----------|
| [0001](docs/adr/0001-hub-validates-inbound-tokens.md) | The hub validates inbound JWTs itself; header-asserted identity from a fronting proxy is not an identity source |
| [0002](docs/adr/0002-impersonation-default-delegation-opt-in.md) | Impersonation-shaped exchange by default; `actor_token` delegation opt-in per server |
| [0003](docs/adr/0003-fail-closed-on-token-exchange-failure.md) | Exchange failure fails closed — never fall back to the service credential |
| [0004](docs/adr/0004-background-paths-use-service-identity.md) | Health/discovery keep the service identity and never borrow a user's token |

All stories follow TDD (RED → GREEN → IMPROVE): write/extend the named tests
first, watch them fail, then implement. The work is strictly additive — a server
with no OBO configuration must behave byte-identically to today, and the default
`auth.type: none` local-first path must not change at all (public contract is
stable per CLAUDE.md).

> **Invariant — the hub runs with no identity provider.** `pip install` →
> `k5n-mcp-hub` → working, with no Keycloak anywhere, is not negotiable. Concretely:
> `auth.type` stays `none` by default; the JWT/JWKS machinery is constructed only
> when `auth.type: jwt`; JWKS is fetched lazily on first validation, never at
> startup, so a down IdP can't stop the hub booting; a persisted OBO registration
> degrades that one server rather than failing the process; and the Keycloak
> compose stack stays out of `pytest`, so the default suite needs no Docker and no
> network. Story 5.3 and Story 6.3 each carry a regression test for this.

### Epic 5 — Hub as an OAuth resource server *(prerequisite, ~60% of the effort)*

The hub cannot exchange a token it never received. Nothing in Epic 6 is reachable
until a request carries a verified identity.

**Story 5.1 — Principal-carrying authenticator**

As a developer, I want the authenticator to yield *who* authenticated, not just
whether they did, so downstream code can act on identity.

- TDD: extend `tests/test_auth.py` and `tests/test_integration_auth.py` first —
  both assert today's boolean contract, so they go RED by construction.
- Acceptance criteria:
  - [x] A `Principal` type carries at minimum `subject`, `issuer`, `scopes`, and
        the raw token (needed as `subject_token` in Epic 6).
  - [x] `Authenticator.authenticate()` returns `Principal | None`;
        `auth_required` (`auth/base.py:17-30`) no longer tests `result is not True`.
  - [x] `NoAuthStrategy` returns an anonymous `Principal`; `BasicAuthStrategy`
        returns one identifying the configured user. Neither changes its
        allow/deny behavior — existing 401 responses and `WWW-Authenticate`
        headers are byte-identical.
  - [x] The authenticated principal is attached to `request.state`.

**Story 5.2 — JWT bearer authenticator**

As a hub operator, I want the hub to validate OAuth access tokens against my IdP
so callers have a real, verified identity.

- TDD: new `tests/test_jwt_auth.py` first, signing tokens locally with a test key
  and serving JWKS through an httpx `MockTransport` — no live IdP in unit tests.
- Acceptance criteria:
  - [x] `auth/jwt_bearer.py` validates signature, `iss`, `aud`, `exp`, `nbf`, and
        required scopes; rejects `alg: none` and algorithm confusion.
  - [x] JWKS is fetched over the SSRF-pinned transport (`utils.SafePinnedTransport`,
        `follow_redirects=False`), cached, and refreshed on unknown `kid` with a
        rate limit so an unknown-kid flood can't hammer the IdP.
  - [x] Rejections produce `401` with a spec-shaped `WWW-Authenticate: Bearer`
        challenge including `error` and `resource`.
  - [x] The crypto dependency (`joserfc` or `pyjwt[crypto]`) is declared in
        `pyproject.toml` `dependencies` — CI's clean-venv gate fails otherwise.

**Story 5.3 — `auth.type: "jwt"` configuration**

- TDD: extend `tests/test_config.py` first.
- Acceptance criteria:
  - [x] `AuthConfig.validate_type` (`config.py:93-101`) accepts `jwt`; a new
        `JWTAuthConfig` carries issuer, JWKS URL, audience, algorithms, and
        required scopes.
  - [x] Env overrides work through both documented patterns
        (`MCPHUB_AUTH__JWT__ISSUER=...`).
  - [x] Fails closed like `basic` does: `auth.type: jwt` without an issuer or
        audience raises at `build_authenticator`, with a message naming the
        missing setting.
  - [x] `config.yaml` gains the commented-out block; defaults are unchanged
        (`auth.type` stays `none`).
  - [x] Regression test: with default config and no network, the app starts, serves
        `/healthz`, and registers/proxies to a non-OBO server exactly as today —
        no JWKS fetch attempted, no IdP contacted.
  - [x] With `auth.type: jwt` and an unreachable IdP, the app still **starts**
        (JWKS is lazy); only token validation fails, and it fails with a `401`
        carrying a diagnostic, not a crash.

**Story 5.4 — Protected-resource metadata endpoint (RFC 9728)**

As an MCP client, I want to discover which authorization server guards this hub
so I can obtain a token without out-of-band configuration.

- TDD: new `tests/test_protected_resource_metadata.py` first.
- Acceptance criteria:
  - [x] `GET /.well-known/oauth-protected-resource` returns `resource`,
        `authorization_servers`, `scopes_supported`, and
        `bearer_methods_supported`.
  - [x] Served unauthenticated (it is discovery metadata) and omitted entirely
        when `auth.type` is not `jwt`.
  - [x] `401`s from Story 5.2 point at this URL in their `resource_metadata`
        parameter (RFC 9728's challenge parameter — `resource` is RFC 8707's
        *request* parameter, a different thing). Note `mcp/oauth.py:64-110`
        parses only `resource`, so the hub's own client-side challenge parsing
        does not yet read this; that is a backend-facing path and out of scope
        here.

**Story 5.5 — Thread the principal to the outbound call sites**

- Acceptance criteria:
  - [x] `apply_server_auth` takes an explicit caller-identity argument that is
        **required, not defaulted** — a plumbing bug must be a type error, never a
        silent downgrade to the service identity (ADR 0004).
  - [x] All eight call sites pass it: `proxy/handler.py:92`, `health/checker.py:56`,
        `sdk_client.py:195`, `stateless.py:92`, `ui_invoke.py:203`,
        `ui_initialize.py:173`, `ui_playground.py:111`, and discovery.
  - [x] Existing behavior for non-OBO servers is unchanged; the full suite passes
        with no test modified except for the new required argument.

### Epic 6 — RFC 8693 token exchange

**Story 6.1 — Token exchange client**

- TDD: new `tests/test_token_exchange.py` first.
- Acceptance criteria:
  - [x] `mcp/token_exchange.py` POSTs
        `grant_type=urn:ietf:params:oauth:grant-type:token-exchange` with
        `subject_token`, `subject_token_type`, and `audience` / `resource`
        (RFC 8707), plus optional `scope` and `requested_token_type`.
  - [x] `actor_token` / `actor_token_type` are sent only when the server opts into
        delegation (ADR 0002); the default request shape works against a stock
        Keycloak 26.2+ with no feature flags.
  - [x] Reuses the credential-safe transport pattern already in
        `TokenCache.token` — `SafePinnedTransport` plus `follow_redirects=False`,
        since a 3xx would leak the subject token.
  - [x] RFC 6749 error responses (`invalid_grant`, `invalid_target`, …) are parsed
        and preserved, not flattened into a generic failure.

**Story 6.2 — Per-subject token cache** *(security-critical)*

As a user, I want my exchanged token used only for my requests.

- TDD: extend `tests/test_token_cache.py` first with the cross-user test — two
  principals, one server; RED against today's per-server key
  (`mcp/auth.py:36`), which hands the first caller's token to the second.
- Acceptance criteria:
  - [x] Cache key is `(subject, issuer, server.id, audience, scope)`. A test
        proves Alice's token is never returned for Bob.
  - [x] The cache is bounded — LRU or TTL with a configurable maximum. Today's
        `dict` is unbounded, which under OBO grows with every user seen.
  - [x] Entries never outlive the subject token: cached expiry is
        `min(exchanged_token_exp, subject_token_exp)`.
  - [x] Existing `client_credentials` caching keeps its current per-server
        behavior and its current tests.
  - [x] Cache contents never reach logs, traces, or `/metrics` — subject values
        are hashed if they appear in metric labels at all.

**Story 6.3 — `auth_type: "obo"` server registration**

- TDD: extend `tests/test_register.py`, `tests/test_models.py`, and
  `tests/test_apply_server_auth.py` first.
- Acceptance criteria:
  - [x] `RegisteredServer` and `RegisterRequest` accept `auth_type: "obo"` plus
        `obo_audience`, `obo_resource`, `obo_scope`, `obo_actor_token_source`,
        `obo_status`, `obo_error`.
  - [x] A new rule in `apply_server_auth` fires only for `auth_type == "obo"`,
        ahead of the `client_credentials` branch; every other registration takes
        exactly today's path.
  - [x] `sanitize_for_api` scrubs `obo_error`; `sanitize_for_persistence` clears
        `obo_status` / `obo_error`, matching the `oauth_token_*` precedent.
  - [x] No user token is ever written to storage — asserted by a test that
        registers an OBO server, proxies a call, and greps the persisted JSON.
  - [x] An OBO server without inbound identity is refused where it's actionable
        and never by refusing to boot (ADR 0001): **registration** is rejected
        while `auth.type` is `none` or `basic`; a **persisted** OBO server is
        marked unusable, logged once, and surfaced in the UI, leaving every other
        server working; a **request** to it returns `401`.
  - [x] Regression test: a stored `mcp_servers.json` containing an OBO server does
        not prevent startup under the default `auth.type: none`.

**Story 6.4 — Fail-closed error handling**

- TDD: extend `tests/test_proxy_handler.py` first.
- Acceptance criteria:
  - [x] Exchange failure → `502`, call never made, IdP error surfaced through
        `format_auth_challenge` (`mcp/oauth.py:152-176`). No fallback to any
        static credential (ADR 0003).
  - [x] Backend `401` on a previously-good token → invalidate that cache entry,
        re-exchange **once**, retry; a second `401` propagates with the backend's
        `WWW-Authenticate` intact.
  - [x] A request to an OBO server with no principal → `401` with
        `resource` pointing at the Story 5.4 metadata URL.
  - [x] Outcomes recorded on `obo_status` / `obo_error`; the server list badge
        reuses the existing `oauth_token_status` vocabulary.

**Story 6.5 — Background paths keep the service identity**

- TDD: extend `tests/test_health_checker.py` and `tests/test_discovery.py` first.
- Acceptance criteria:
  - [x] Health checks and discovery never attempt an exchange and never read the
        per-subject cache (ADR 0004).
  - [x] An OBO server with a service credential is health-checked and discovered
        exactly as today.
  - [x] An OBO server *without* one degrades rather than fails: transport-level
        reachability only (the MCP probe is skipped rather than drawing a 401 and
        marking a reachable server unhealthy), discovery skipped.
  - [x] ...and both are stated in the UI (done in Story 7.1).
  - [x] Capabilities discovered under the service identity are flagged as such, so
        a backend with a per-user tool list can't show one identity's list to
        everyone (done in Story 7.1).

### Epic 7 — Surface, safety, and end-to-end verification

**Story 7.1 — Admin UI**

- TDD: extend `tests/test_servers_template.py` and `tests/test_ui_capabilities.py`.
- Acceptance criteria:
  - [x] `servers.html` gains an OBO section, shown only when `auth_type` is `obo`,
        following the existing OAuth field group.
  - [x] The server detail view shows exchange status, the audience in use, and
        whether delegation is active.
  - [x] Capabilities page carries the service-identity label from Story 6.5.

**Story 7.2 — Redact token material in trace bodies**

Promotes the existing open item under "Security follow-ups"; OBO makes it
mandatory, since IdP error bodies routinely echo token material.

- TDD: extend `tests/test_trace_redaction.py` first.
- Acceptance criteria:
  - [x] Trace bodies redact `access_token`, `refresh_token`, `subject_token`,
        `actor_token`, `id_token`, `client_secret`, and `assertion` in both JSON
        and form-encoded payloads.
  - [x] Header redaction (`trace/recorder.py:12-27`) is unchanged.
  - [x] A test proves a failed exchange's response body reaches the trace redacted.

**Story 7.3 — Generated client scripts**

- TDD: extend `tests/test_tool_script_template.py` and `tests/test_ui_downloads.py`.
- Acceptance criteria:
  - [x] `tool_script.py.j2` / `tool_script.sh.j2` cannot embed an OBO credential —
        the token is the live caller's. Scripts for OBO servers emit a comment
        explaining that the caller must supply their own bearer token, and read it
        from an env var rather than baking one in.
  - [x] Non-OBO scripts are byte-identical to today.

**Story 7.4 — Keycloak-backed end-to-end stack**

As a developer, I want to prove the exchange really happens against a real IdP.

- Acceptance criteria:
  - [x] `docker-compose.obo.yml` brings up: Keycloak (realm imported from JSON so
        it is reproducible), the hub with `auth.type: jwt`, and a downstream MCP
        stub that **validates JWTs** — rejecting a wrong `aud` with `401` +
        `WWW-Authenticate: Bearer resource="..."` and echoing `sub` / `azp` / `act`.
        Without a validating downstream, nothing proves the exchange did anything;
        the fake server in `tests/conftest.py:128` does no auth at all.
  - [x] Realm defines: `mcp-client` (public, PKCE — the agent), `k5n-mcp-hub`
        (confidential, **Standard token exchange** switch enabled — this is the
        requesting client, no fine-grained admin permissions needed), and
        `mcp-server-files` as the target audience. Two users, to exercise cache
        isolation for real.
  - [x] An e2e script logs in as each user, calls a tool through the proxy, and
        asserts the downstream saw `sub` = that user and `azp` = the hub
        (verifying ADR 0002's audit-trail claim rather than assuming it).
  - [x] Runs as a separate CI job or manually — **not** in `pytest`, so the
        default suite keeps needing no Docker.
  - [x] `security.allow_private_networks: true` is already the local default, so
        reaching Keycloak on localhost needs no extra configuration.

> **Sizing (estimate made before the work; Epics 5-7 are now done).** Guessed
> 25-35 files and 8-10 new test modules on top of the 65 that existed then. Actual:
> the tree now has 79 test modules and 1277 tests. Kept as a record of the estimate,
> not as a live figure.

## Enterprise-Managed Authorization (ID-JAG)

Goal: reach downstream MCP servers whose **authorization server is not the hub's
IdP** — the case Epic 6's single-leg exchange structurally cannot serve.

In June 2026 MCP adopted the Identity Assertion JWT Authorization Grant (ID-JAG,
from Cross-App Access) as its Enterprise-Managed Authorization extension,
`io.modelcontextprotocol/enterprise-managed-authorization`. The enterprise IdP
becomes the policy decision point: it decides which client may reach which MCP
server for which user, and revocation happens in one place.
Spec: <https://github.com/modelcontextprotocol/ext-auth/blob/main/specification/stable/enterprise-managed-authorization.mdx>

Two legs instead of one:

| | `auth_type: "obo"` (Epic 6) | `auth_type: "ema"` (this epic) |
|---|---|---|
| Leg 1 | RFC 8693 at the IdP → access token for the backend | RFC 8693 at the IdP → **ID-JAG** (`requested_token_type: urn:ietf:params:oauth:token-type:id-jag`, `audience` = the Resource AS's *issuer identifier*) |
| Leg 2 | — | RFC 7523 at the **backend's own AS** (`grant_type: urn:ietf:params:oauth:grant-type:jwt-bearer`, `assertion` = the ID-JAG) → access token |
| Assumes | One IdP that can mint a token for the backend | Backend has its own AS, in another tenant or vendor |
| Policy | Per-server config on the hub | The enterprise IdP decides |

Design decisions are in [`docs/adr/`](docs/adr/README.md):

| ADR | Decision |
|-----|----------|
| [0005](docs/adr/0005-hub-is-the-mcp-client-in-ema.md) | The hub plays the **MCP Client** role; becoming an Authorization Server is out of scope |
| [0006](docs/adr/0006-ema-subject-assertion-source.md) | The subject assertion is configured per server, defaults to the spec's ID Token, and fails closed when absent |

Same TDD discipline and the same additive rule: `auth_type: "ema"` is opt-in per
server, and every other registration keeps today's behavior byte-for-byte.

> **Draft risk, deliberately taken.** ID-JAG is an active IETF Web Authorization
> Protocol draft, not a finished RFC, so the wire format may still move. Same posture
> as ADR 0002 took toward delegation: implement it, keep it opt-in per server, and
> pin every URN as a named constant in one module so a draft revision is a one-file
> change rather than a hunt.

### Epic 8 — Enterprise-Managed Authorization

**Story 8.1 — Recognize the id-jag grant profile during discovery**

As a hub operator, I want to know whether a backend's authorization server can
accept an ID-JAG, so misconfiguration surfaces at registration rather than at the
first proxied call.

- TDD: extend `tests/test_oauth.py` first.
- Acceptance criteria:
  - [x] `mcp/oauth.py` parses `authorization_grant_profiles_supported` from
        authorization-server metadata (the discovery it already performs).
  - [x] `urn:ietf:params:oauth:grant-profile:id-jag` and the four token/grant URNs
        live in one constants module, so a draft revision is one edit.
  - [x] `RegisteredServer` records whether the backend's AS advertises the profile
        (`ema_supports_id_jag_profile`, landed with Story 8.4's model work);
        the value is advisory, never a gate — an AS may support it without
        advertising, and the hub must not refuse to try.
  - [x] Servers with no AS metadata are unaffected.

**Story 8.2 — The two-leg exchange**

- TDD: new `tests/test_id_jag.py` first, both legs against `MockTransport`.
- Acceptance criteria:
  - [x] `mcp/id_jag.py` performs leg 1 (`grant_type=...:token-exchange`,
        `requested_token_type=...:token-type:id-jag`, `audience` = the Resource AS
        **issuer identifier**, optional `resource` = the MCP server's resource
        identifier) and leg 2 (`grant_type=...:jwt-bearer`, `assertion` = the ID-JAG).
  - [x] The returned `issued_token_type` is checked to be the id-jag type; a
        different type is an error rather than something to forward blindly.
  - [x] The ID-JAG's `resource` claim is verified to name the server being called
        before leg 2 — the spec makes it MUST-contain, and forwarding one minted for
        a different resource is exactly the confused-deputy case to prevent.
  - [x] Both legs use the SSRF-pinned transport with redirects disabled: the subject
        assertion travels on leg 1 and the ID-JAG on leg 2, and a 3xx would leak
        either.
  - [x] RFC 6749 errors from *either* leg are preserved and say which leg failed —
        "the IdP refused" and "the backend's AS refused" have completely different
        fixes.

**Story 8.3 — Subject assertion plumbing (ADR 0006)**

- TDD: extend `tests/test_principal.py` and `tests/test_jwt_auth.py` first.
- Acceptance criteria:
  - [x] `Principal` gains an optional `id_token`, redacted in `__repr__` and never
        persisted, on the same terms as `token`.
  - [x] A documented inbound header carries it; the header is added to
        `SENSITIVE_HEADERS` in `trace/recorder.py`, with a redaction test.
  - [x] `ema_subject_token_type` (`id_token` default, `access_token` alternative)
        selects what leg 1 sends.
  - [x] Missing the configured assertion fails closed with a 401 carrying the
        RFC 9728 challenge — never a silent fallback to the other token type, which
        would make the effective identity depend on which attempt succeeded.
  - [x] The docs state plainly that the header is hub-specific, not something MCP
        clients send by convention.

**Story 8.4 — `auth_type: "ema"` registration and auth rule**

- TDD: extend `tests/test_register.py`, `tests/test_models.py`,
  `tests/test_apply_server_auth.py` first.
- Acceptance criteria:
  - [x] New fields: `ema_resource_as_issuer`, `ema_resource_id`, `ema_token_url`,
        `ema_subject_token_type`, `ema_status`, `ema_error`, sanitized like the
        `obo_*` pair.
  - [x] A rule in `apply_server_auth` fires only on an exact `auth_type == "ema"`
        match, placed with the OBO rule ahead of the static credentials, for the same
        reason: a stale `bearer_token` must not silently disable per-user auth.
  - [x] `SERVICE_IDENTITY` skips it and falls through to the static rules, and
        `needs_user_identity` treats `ema` like `obo` — so Story 6.5's health and
        discovery degradation applies unchanged (ADR 0004).
  - [x] The per-subject cache is reused with the Resource AS issuer in the key: two
        backends behind different authorization servers must never share an entry.
  - [x] Fail-closed and single-re-exchange-on-401 behave exactly as Story 6.4, with
        both legs re-run.

**Story 8.5 — Declare the extension in outbound capabilities**

- TDD: extend `tests/test_stateless_client.py` first.
- Acceptance criteria:
  - [x] Requests to an `ema` server carry
        `io.modelcontextprotocol/enterprise-managed-authorization` under
        `_meta.io.modelcontextprotocol/clientCapabilities.extensions` — the `_meta`
        structure Epic 2 already sends.
  - [x] Non-EMA servers see no new `_meta` keys.

**Story 8.6 — Admin UI and end-to-end proof**

- Acceptance criteria:
  - [ ] `servers.html` gains an EMA field group beside the OBO one; the card shows
        exchange status, the resource AS in use, and which leg failed.
  - [ ] The e2e stack demonstrates the full two-leg flow.
  - [x] **Settled (2026-09-05): Keycloak cannot issue ID-JAGs, so leg 1 needs a
        different issuer.** Tested against the Epic 7 stack's Keycloak 26.4, which
        refuses the flow at the first parameter:
        `{"error":"invalid_request","error_description":"Parameter 'subject_token'
        supports access tokens only"}` — it will not accept an ID Token as a subject
        token at all, let alone mint an ID-JAG. Keycloak's own docs (nightly 26.7.3)
        confirm it: receiver side only, behind `--features=identity-assertion-jwt`,
        marked experimental and "do not use in production"; issuer side "not yet
        fully implemented" (keycloak/keycloak#43971).
  - [x] Leg 1 runs against a stub enterprise IdP (`e2e/ema_idp/`) that also provides
        SSO, so the hub has a real token to validate. Deliberately credulous: it
        signs what it is asked for, which lets the suite request a wrong `resource`
        or an expired assertion and watch the hub refuse them.
  - [x] **Leg 2 uses a stub too, after testing Keycloak 26.7 and finding it unusable
        here.** With `--features=identity-assertion-jwt` it does accept the
        `jwt-bearer` grant and reject assertions on their content rather than the
        grant type, so the receiver path exists. But the feature is flagged
        experimental ("do not use in production") and the per-client switch that
        permits the grant is undocumented — `invalid_grant: JWT Authorization Grant
        is not supported for the requested client` persisted through the attribute
        names that seemed most likely (`jwt.authorization.grant.enabled`,
        `identity.assertion.jwt.enabled`).
  - [ ] **Known gap:** leg 2 is therefore never tested against an implementation we
        did not write. Revisit when Keycloak's receiver support stabilises, or if a
        free Okta/Auth0 tier can play the resource authorization server.
  - [ ] Revisit if Okta or Auth0 offer a free tier that issues ID-JAGs; a real
        issuer for leg 1 would be strictly better than the stub, and both document
        Cross-App Access.

> **Sizing:** smaller than Epic 6. `Principal`, the per-subject cache, the
> fail-closed path, `caller` plumbing, the SSRF transport, the UI patterns, and the
> e2e harness all carry over; the new work is one extra HTTP leg, one claim check,
> and the assertion plumbing from ADR 0006. Story 8.6's IdP question is the only
> genuine unknown, and it is a research task, not an implementation one.

### Upstream: raise the proxying-gateway gap with MCP *(not yet filed)*

The EMA flow has no seat for a gateway that proxies MCP on behalf of an
already-authenticated user: its "MCP Client" holds the identity assertion first-hand
from its own SSO, and a middlebox does not. Our answer is a hub-specific request
header, so the conformant path is the one that does not interoperate — see
[ADR 0006](docs/adr/0006-ema-subject-assertion-source.md).

A drafted issue for
[`modelcontextprotocol/ext-auth`](https://github.com/modelcontextprotocol/ext-auth)
is at [`docs/ema-gateway-gap.md`](docs/ema-gateway-gap.md). **Not filed — more
research needed first.** Before posting, verify:

- [ ] That the gap is real and not already answered somewhere I did not look — the
      spec's non-normative pages, SEP-990, or the ext-auth discussions. My reading is
      from the stable spec and the extension page only.
- [ ] That no existing issue covers it. I checked the open list and found only #32
      (attenuated authority, a different concern), but did not read every closed one.
- [ ] Whether other MCP gateways already solve this, and how. If there is an emerging
      convention for conveying an assertion to an intermediary, the right move is to
      adopt it rather than propose one.
- [ ] Whether the subject-binding note belongs in a public issue at all, or is better
      raised via `SECURITY.md`. It is a spec omission rather than an exploitable flaw
      in shipped software, and it is already described in this repo's history — but
      that judgement is worth making deliberately rather than by default.

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

- [x] Redact sensitive fields inside trace *bodies* (only headers were redacted).
      Done as Story 7.2: `TraceRecorder.add` redacts OAuth-shaped credential fields in
      JSON and form bodies, with a textual fallback for truncated ones.

## Product / usefulness follow-ups (from AUDIT_local.md §3)

- [ ] Interop with the official MCP registry API (import/export).
- [ ] Emit OpenTelemetry traces/metrics alongside `/metrics`.
- [ ] Support stdio MCP servers (currently HTTP-only).
- [x] Positioning decided (2026-09-05): **on-behalf-of is the headline
      differentiator**, and the README leads with it. Fault injection stays a
      secondary one — it is the strongest *testing* feature, but per-user identity is
      what distinguishes the hub in production, where most MCP proxies forward a
      single shared credential.
