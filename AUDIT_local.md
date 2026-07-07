# k5n-mcp-hub — Project Audit

**Date:** 2026-07-07
**Scope:** Engineering best practices, security posture, usefulness & competitive positioning
**Method:** Static + dynamic review of `src/mcp_hub/` and `tests/` by two specialist review agents (Python/FastAPI best-practices, security) plus a market survey of the 2026 MCP-hub landscape. Read-only — no code was modified. Verified tooling state: `ruff check`, `ruff format --check`, and `mypy` all pass; full suite **850 passed / 14 failed, 82% line coverage**.

---

## Executive Summary

k5n-mcp-hub is a **local-first, single-process Python/FastAPI control plane for MCP servers** that bundles a registry, a reverse proxy/gateway, request tracing, fault injection, health monitoring, capability discovery, an admin UI, and A2A agent cards into one small app. That all-in-one combination — especially **fault injection / chaos testing for MCP**, which almost no other tool offers — is its genuine differentiator.

- **Code quality: B** (would be B+/A- once two CRITICAL security-default issues and stale tests are fixed). Well-organized, idiomatic FastAPI with a genuine composition root, cohesive packages, a clean baseline on classic anti-patterns (no bare excepts / eval / SQL-or-shell injection surface / mutable defaults), 82% coverage, and an unusually rigorous clean-venv CI gate. Let down by duplication (trace-entry construction ×3, MCP handshake reimplemented in `ui_invoke.py`) and two dead-code pockets.
- **As a personal / dev-team tool on a trusted network:** useful and reasonably well-built. Good test breadth, clean package structure, sound path-traversal and constant-time-auth handling, and an SSRF-pinned transport that (where it's actually wired in) closes the DNS-rebinding window correctly.
- **Correction to an earlier claim:** the 14 test failures are all pre-existing (present on `main`), but they are *not* all "environment-dependent." The accurate breakdown: **2** truly environmental (`mcp` SDK absent on a Python 3.10 box that doesn't meet the declared `>=3.11`), **7** stale/dead assertions from a UI refactor (old landing page, CDN `<script>` tags now vendored locally, a renamed download route) that will never pass again without maintenance, **1** racy test, and **3** (`test_oauth.py::TestResolvePinnedIp`) a *real* symptom of a design defect — a process-global SSRF flag leaking across app instances (see §1 CRITICAL).
- **As an exposed or multi-tenant gateway:** **not safe as-is.** The security review found genuine defects (not local-first tradeoffs): several of the most powerful UI routes have **no auth even under `auth.type: basic`**, and SSRF pinning is **not applied uniformly** — the reverse proxy, health checker, initialize route, and the entire OAuth token-endpoint flow all bypass it.
- **Competitively:** it sits in an already-crowded, consolidating space (official registry + Smithery + MCPJungle + IBM ContextForge + Docker MCP Gateway + MCP Inspector). Each individual capability is matched or exceeded by a specialized tool, and the ecosystem is standardizing on the **official registry API** and **OpenTelemetry** — neither of which k5n speaks. Its defensible niche is "**MCP dev/chaos testbench**," not "another enterprise gateway."

---

## 1. Code Quality & Best Practices

**Grade: B.** 62 source files / 6,447 lines; 59 test files / 12,979 lines. Ruff + mypy clean; 850 passed / 14 failed; 82% coverage.

### CRITICAL

1. **✅ FIXED (2026-07-07).** **Hardcoded default admin credentials activate outside the repo checkout.** `config.py:60-68` defaults `BasicAuthConfig` to `admin` / `admin123`, and `AuthConfig.type` defaults to `"basic"` (not `"none"`). The safe `type: none` lives only in the checked-in `config.yaml`, which `_load_yaml_config()` reads solely from `cwd()/config.yaml` (or `--config`), returning `{}` if absent — no packaged fallback. So `pip install k5n-mcp-hub && k5n-mcp-hub` from any directory without this repo's `config.yaml` (the normal installed-CLI case) boots with Basic auth using credentials that are public in the source tree. **Fix:** make `type: "none"` the Pydantic-level default, and refuse to start with the built-in default password. _(Also a security defect — cross-references §2.)_

2. **✅ FIXED (2026-07-07).** **Global mutable SSRF-guard flag leaks across app instances / pollutes tests.** `utils.py:32-45` `_ALLOW_PRIVATE_NETWORKS` is a module global flipped as a side effect of every `create_app()` (`app.py:190-194`). Reproduced: after `create_app()` (shipped `allow_private_networks: true`), `resolve_pinned_ip("127.0.0.1")` returns `"127.0.0.1"` instead of `None`. This is the real cause of the 3 `test_oauth.py::TestResolvePinnedIp` failures (pass isolated, fail in-suite) — not DNS flakiness. Any app built with `allow_private_networks=True` permanently weakens SSRF protection for the whole process. **Fix:** store the flag on `app.state` / thread it through `SafePinnedTransport`, not a module global.

### HIGH

3. **Verbose trace mode silently drops entries for SSE responses.** `proxy/handler.py:174-230`: when `verbose=True` and the response is SSE with `capture_sse=False`, neither the `verbose` nor the `not verbose` branch records an entry — unlike the non-verbose path, which always logs a minimal one. No test covers verbose + SSE + `capture_sse=False`. **Fix:** always record a minimal status/duration entry.
4. **230-line route handler reimplements the MCP handshake instead of reusing `MCPClient`.** `routes/ui_invoke.py:157-387` inlines a hand-rolled `initialize → notifications/initialized → tools/call` over raw httpx (~4.5× the 50-line guideline), duplicating `mcp/sdk_client.py` and building its own pinned client rather than reusing `utils.safe_http_client_factory` — two SSRF-client paths that can drift. **Fix:** extract helpers / extend `MCPClient`.
5. **Trace-entry construction duplicated (and inconsistently sanitized) across 3 files** — `proxy/handler.py`, `routes/ui_invoke.py`, `health/checker.py` each re-implement the verbose/non-verbose header-redaction + body-truncation branching. Drift risk on a security-sensitive concern. **Fix:** one shared `build_trace_entry(...)` in `trace/recorder.py`.
6. **Overly broad exception handling swallows unexpected errors.** `health/checker.py:57-58,103-104` catch bare `except Exception` with no `logger` call, misreporting programming bugs (e.g. `AttributeError`) as ordinary health-check failures. **Fix:** narrow types + `logger.exception(...)`.
7. **~7 of the 14 test failures are stale assertions from a UI refactor, not flakiness** — `test_index_html.py` (×4), `test_servers_template.py` (×2), `test_agent_templates.py`, `test_ui_capabilities.py` assert against content that no longer exists (hero text "MCP Development Hub", a `<main>` container, `unpkg.com` CDN tags now vendored locally, an "Inspect" button, an old download URL). Dead tests, not flaky. **Fix:** update or delete them so CI can be truly green.

### MEDIUM

8. **Dead byte-for-byte duplicate module** `src/mcp_hub/trace.py` (0% coverage) shadowed by the `trace/` package (97%) — leftover from a flat→package refactor. **Delete it.**
9. **Dead placeholder classes** `Placeholder{Storage,Registry,Authenticator,TraceRecorder}` in `app.py:40-57`, never referenced. **Remove.**
10. **No-op validator with misleading name** — `config.py:53-57` `validate_redis_not_implemented` just `pass`es; real enforcement is in `app.py:152-153`. **Fix:** move the check in or delete.
11. **Racy shutdown test** `test_main.py:150-179` — background task sometimes cancelled before it starts; production `_cancel_and_await_tasks` is correct. **Fix:** add a yield after task creation.
12. **Test tooling shipped as a production dependency; the documented `[dev]` extra doesn't exist.** `pyproject.toml:9-19` lists `pytest`/`pytest-asyncio` under `[project].dependencies`, while `CLAUDE.md`'s `pip install -e .[dev]` references an undefined extras group. **Fix:** move test deps to `[project.optional-dependencies].dev`.
13. **Redundant isinstance branch** with identical bodies — `proxy/handler.py:59-66`. **Collapse.**

### LOW

- `routes/ui_invoke.py:171-174` — convoluted `getattr(getattr(...))` where `app.state.settings` is always set; simplify.
- Two different classes both named `AgentRegistry` (`registry/service.py:70`, `agents/card.py:117`, aliased `CardAgentRegistry` only at import) — confusing when grepping.
- Coverage cold spots: `ui_invoke.py` 39%, `ui_initialize.py` 21%, `storage/fixture.py` 28%, `ui_servers.py` 69%.
- `pip install -e .[dev]` failed on the local box (pip 22 / setuptools 59 lack PEP 660) — stale toolchain, not a project defect, but the documented setup isn't turnkey there.

### Strengths (verified)

- **Real composition root** — every subsystem wired onto `app.state` in `app.py:167-197`; routers read from state, not globals, exactly as `CLAUDE.md` claims. Cohesive, small packages (median file ~90 lines; largest source file 387).
- **Idiomatic & clean baseline** — `from __future__ import annotations`, modern `X | None`, well-used `Protocol`s (`StorageStrategy`, `Authenticator`, `TraceRecorder`), dataclass DTOs, solid Pydantic config. No bare excepts, `print()`, mutable defaults, `eval`/`exec`, unsafe `yaml.load`, or string-built SQL/shell anywhere in `src/`.
- **Above-average CI** — lints/types, then re-installs into a **fresh venv with only declared deps** before pytest (catches undeclared imports), then `pip-audit`. Sensible ruff/mypy config for the `src/` layout.
- **Caveat on CI green-ness:** since 7 failures are permanent-until-fixed, either `main`'s CI is red now or these are being tolerated — which undercuts the value of the otherwise-excellent gate.

---

## 2. Security Posture

The security review read the SSRF transport layer, every outbound-HTTP call site, each route file's auth wiring, the fixture/download path-traversal guards, JSON-RPC/registration validation, and CI/dependency setup — and ran Python repros against `ipaddress`/`socket.getaddrinfo` to verify specific bypass techniques rather than eyeballing.

### CRITICAL

1. **The reverse proxy — the one path SSRF-pinning was built for — doesn't use it.** `proxy/handler.py:164-167` creates a bare `httpx.AsyncClient` and streams arbitrary method/path/body to `srv.url`, re-resolving DNS at request time with no pin. `is_url_safe_for_discovery()` runs only once at registration (`routes/v1.py:106-113`). Classic TOCTOU / DNS-rebinding: register a name that resolves publicly, rebind it afterward to `127.0.0.1` / `169.254.169.254` / any LAN host, and every `/mcp` call connects there with the full response streamed back — **even when the operator sets `allow_private_networks: false` to harden the deployment.**

2. **`GET /ui/server/{id}/initialize` has no SSRF pin and no auth.** `ui_initialize.py:124` uses a plain `httpx.AsyncClient()`, and the router has no `Depends(auth_dependency)`. Any caller — unauthenticated even under basic auth — makes the hub POST to `srv.url` with zero SSRF protection, and the response is echoed into the rendered HTML.

3. **SSRF via unvalidated OAuth `token_endpoint` / `oauth_token_url`.** `routes/v1.py` validates `url` and `oauth_discovery_url` but never validates `oauth_token_url`, nor the `token_endpoint` extracted from a fetched OAuth metadata document (`routes/v1.py:203-204`) — attacker-controlled content. That URL is POSTed to (with `client_id`/`client_secret`/`scope`) via another bare `httpx.AsyncClient()` in `mcp/auth.py:55` on every proxied/invoke/playground/initialize call. Net: registering one server yields a repeatable, unauthenticated (default `auth.type: none`) blind-SSRF **write** primitive against any internal URL — independent of `allow_private_networks` and independent of DNS rebinding (it's simply never checked).

### HIGH

4. **Auth is inconsistently applied — flipping to `auth.type: basic` does not protect the most powerful endpoints.** Routes with **no** `Depends(auth_dependency)` regardless of `auth.type`:
   - `ui_playground.py` — sends arbitrary raw JSON-RPC to any backend with the hub's stored credentials auto-attached (line 95).
   - `ui_initialize.py` — see #2.
   - `ui_faults.py` (16, 64) — enable/disable fault injection (timeouts, malformed JSON, SSE interrupt): an unauthenticated DoS/tamper primitive.
   - `ui_capabilities.py` (27, 112, 129) — unauthenticated outbound discovery + schema disclosure.
   - `ui_trace.py` (15, 38, 72) — unauthenticated read of captured traces, verbose toggle, and trace-clear (audit-trail wipe).
   - `ui_agents.py` (51, 58, 72, 81) — fixture CRUD; only `/v1/agents/register` is guarded.

   `tests/test_integration_auth.py` only exercises `/v1/register*`, `/mcp`, and the intentionally-open `/v1/servers` — nothing covers the routes above, indicating unintended gaps. `config.yaml:40-42` tells operators to switch to basic auth "before any non-trusted deployment," which is a false promise while this gap exists.

5. **Trace capture leaks secrets/PII and is unauthenticated (compounds #4).** `trace/recorder.py::sanitize_trace_headers` (19-26) redacts only `Authorization`; `Cookie`, `Set-Cookie`, `Mcp-Session-Id`, and custom API-key headers pass through, and **bodies are only length-truncated, never redacted**. With trace view/verbose/clear all unauthenticated, an attacker can enable verbose tracing, trigger traffic, then read out session IDs or body secrets — a real exfiltration path in a "hardened" basic-auth deployment.

### MEDIUM

6. **No per-server ownership/RBAC on registration** (`routes/v1.py::_register_server_impl`, 162-178). Anyone able to call `/v1/register` (single shared admin credential) can overwrite any server's `id`, redirecting its `url`/auth. Fine for single-admin/local use; a problem if ever shared among mutually-untrusted operators.

7. **`/v1/servers`, `/v1/servers/{id}`, `/api/servers` are unauthenticated by design** even under basic auth. `sanitize_for_api()` (`models/server.py:107-114`) correctly strips `bearer_token`/`oauth_client_secret`, so this is topology/metadata disclosure (URLs, tags, OAuth issuer, tool schemas, health), not credential leakage — still reconnaissance value.

8. **`mcp/oauth.py::_is_url_safe` (24-60) is a second, weaker SSRF filter** using a hardcoded CIDR tuple; `::ffff:127.0.0.1` (IPv4-mapped) slips through (`ip in ip_network('127.0.0.0/8')` → `False`, verified). **Not currently exploitable** because the real fetch also goes through `SafePinnedTransport`, which re-validates correctly — but a latent defense-in-depth gap. Recommend deleting the duplicate and reusing `utils.py`.

9. **`health/checker.py:137`** uses an unpinned `httpx.AsyncClient()` for periodic health GETs — lower severity (body not surfaced directly) but still an automatic internal probe if `srv.url` is rebindable, with status/timing visible in `/ui/servers`.

### LOW

10. **`mcp/sdk_client.py` SSRF pinning is best-effort** (111-123) — only attaches `SafePinnedTransport` if the installed `mcp` SDK's `streamablehttp_client` accepts an `httpx_client_factory` kwarg (runtime `inspect.signature` check). Works with pinned `mcp==1.28.1`, but no test asserts it and no fail-closed if a future SDK drops the param.

11. **Unpinned dependencies** (`pyproject.toml`: `mcp>=1.0`, `httpx>=0.27.0`, …) — CI `pip-audit` audits whatever resolves as latest, not what a deployment actually runs. Hygiene gap.

### Verified sound / intentional (not vulnerabilities)

- `auth/basic.py:33-34` — `secrets.compare_digest` for user + pass (constant-time, no timing oracle).
- `agents/fixtures.py` + `routes/ui_downloads.py` — sound traversal guards: allow-list filename sanitization + `Path.resolve().is_relative_to(base)`, atomic `O_CREAT|O_EXCL` writes with `0o600`/`0o700`.
- `utils.py::resolve_pinned_ip`/`SafePinnedTransport` (used by `ui_invoke`, `ui_playground`, `sdk_client`) — pins to the validated IP (closes rebinding TOCTOU), rejects decimal/octal/hex loopback literals, flags IPv4-mapped IPv6 — all verified experimentally.
- `sanitize_for_api()` redacts `bearer_token`/`oauth_client_secret` before any list/get.
- `templates/tool_script.sh.j2` single-quote-escapes every interpolated value (client-side download script; not server-executed).
- No hardcoded secrets in `src/`; `Dockerfile` runs as non-root `USER 1000`; `allow_private_networks: true` default is documented, not hidden.

### Security Verdict

- **(a) Local/personal, trusted LAN, shipped defaults** (`auth.type: none`, `allow_private_networks: true`): **acceptable** — matches the stated local-first threat model; the "anyone on this segment has full control" posture is explicit and documented.
- **(b) Exposed / multi-tenant:** **not safe as-is**, even after switching to basic auth. The auth-bypass gaps (playground, faults, capabilities, trace, initialize) leave the most powerful and most data-exposing capabilities reachable with no credential, and SSRF pinning is bypassed by the proxy, health checker, initialize route, and OAuth token flow — turning the hub into an internal-network SSRF pivot via rebinding (proxy) or a malicious OAuth token endpoint (no rebinding needed). Genuine defects to fix before any internet-facing use.

**Follow-up files:** `proxy/handler.py`, `routes/ui_initialize.py`, `routes/ui_playground.py`, `routes/ui_faults.py`, `routes/ui_capabilities.py`, `routes/ui_trace.py`, `routes/ui_agents.py`, `mcp/auth.py`, `routes/v1.py`, `mcp/oauth.py`, `trace/recorder.py`, `health/checker.py`, `tests/test_integration_auth.py`.

---

## 3. Usefulness & Competitive Positioning

### What it is

A single-process, local-first hub that combines, in one FastAPI app:
- **Registry** of MCP servers (register/list/get, tool/prompt/resource discovery, merge-on-reregister)
- **Reverse proxy / gateway** to registered backends (streaming + SSE, per-server auth application)
- **Observability / dev tooling** — request tracing, health monitoring, capability inspection, an initialize-handshake inspector, a JSON-RPC playground, downloadable client tool-scripts
- **Fault injection** — latency, malformed JSON, SSE interruption (chaos testing)
- **A2A agent cards** + fixture store
- **Admin web UI** (Jinja2 + htmx/hyperscript)

### The 2026 landscape it competes in

| Category | Representative tools | Overlap with k5n |
|---|---|---|
| **Official metaregistry** | [Official MCP Registry](https://registry.modelcontextprotocol.io/) (Anthropic/GitHub/Microsoft/PulseMCP) — metadata only, self-hosting unsupported | Registry/discovery |
| **Public registry / hosting** | [Smithery](https://smithery.ai) ("Docker Hub for MCP", 2,500+ servers) | Registry/discovery |
| **Self-hosted gateway + registry** | [MCPJungle](https://github.com/mcpjungle/MCPJungle) (Go, single binary, OTel/Prometheus, RBAC), [IBM ContextForge](https://github.com/IBM/mcp-context-forge) (Python, federation, Redis, admin UI, guardrails), [Docker MCP Gateway], Envoy MCPRoute, Kong | Registry + proxy + health + admin UI |
| **Debug / test tools** | [Official MCP Inspector](https://github.com/modelcontextprotocol/inspector) ("Postman for MCP", React+Node), [MCPJam inspector](https://github.com/MCPJam/inspector), mcpsnoop (transparent tracing proxy) | Tracing, capability inspection, playground |

### Where k5n-mcp-hub fits

- **Unique angle:** it straddles two categories that are usually separate products — the **gateway/registry** side (MCPJungle, ContextForge) and the **inspector/debug** side (MCP Inspector, MCPJam). No mainstream tool bundles registry + proxy + trace + **fault injection** + health + A2A in one small footprint. **Fault/chaos injection for MCP is genuinely rare** and is the clearest differentiator — useful for testing how MCP *clients* handle slow/broken/misbehaving servers.
- **Where it's outclassed:** each individual feature has a stronger specialist. Discovery/hosting → Smithery/official registry (thousands of servers, ecosystem gravity). Debug UX → Inspector/MCPJam (official, richer, chat/eval). Enterprise gateway → ContextForge/MCPJungle (OTel, RBAC, federation, guardrails).
- **Ecosystem-fit gaps:** the space is consolidating on (1) the **official registry API schema** and (2) **OpenTelemetry**. k5n exposes its *own* registry API and a custom Prometheus-ish `/metrics`, so it doesn't interoperate with the official registry or standard OTel backends out of the box — a real adoption headwind if the goal is broad uptake rather than personal use.

### Usefulness Verdict

**Genuinely useful in a narrow, real niche:** a personal / small-team **MCP control plane + resilience testbench** you can run in one process with no Postgres/Redis/K8s. The fault-injection + tracing + proxy combination is a legitimately handy local dev harness. It is **least compelling as "another gateway/registry"** — that market is crowded, better-funded, and standardizing on APIs k5n doesn't yet speak. Strongest go-to-market framing: lean into "**MCP chaos/dev testbench**," not enterprise governance.

**Highest-leverage improvements for usefulness (independent of the security fixes above):**
1. Speak the **official MCP registry API** (import/export or proxy) so it plugs into the ecosystem instead of being an island.
2. Emit **OpenTelemetry** traces/metrics alongside the current `/metrics`.
3. Support **stdio** MCP servers (the landscape is multi-transport: stdio + streamable-HTTP + SSE); a hub that only reaches HTTP backends misses most local servers.
4. Lead with fault injection as the headline feature — it's the thing nothing else does well.

**Sources:** [TrueFoundry — Best MCP Registries 2026](https://www.truefoundry.com/blog/best-mcp-registries) · [Composio — Best MCP Gateways 2026](https://composio.dev/content/best-mcp-gateway-for-developers) · [Lunar.dev — Best Open-Source MCP Gateways 2026](https://www.lunar.dev/post/the-best-open-source-mcp-gateways-in-2026) · [Official MCP Registry](https://registry.modelcontextprotocol.io/) · [modelcontextprotocol/registry](https://github.com/modelcontextprotocol/registry) · [MCPJungle](https://github.com/mcpjungle/MCPJungle) · [IBM ContextForge](https://github.com/IBM/mcp-context-forge) · [MCP Inspector](https://github.com/modelcontextprotocol/inspector) · [MCPJam inspector](https://github.com/MCPJam/inspector) · [Kong — What is an MCP Gateway](https://konghq.com/blog/learning-center/what-is-a-mcp-gateway)
