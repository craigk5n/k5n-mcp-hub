# 0011 — OpenAPI backends are virtual MCP servers the hub serves itself

**Status:** Proposed
**Date:** 2026-10-29
**Context:** REST-to-MCP translation (TODO.md Epic 13)

## Context

A large share of the APIs an agent could use are plain REST services with an
OpenAPI document and no MCP server. Today the hub can only reach them if someone
writes and runs an MCP wrapper, registers that, and the wrapper holds its own
credential. Gateways such as agentgateway instead take the spec and serve its
operations as MCP tools directly.

Doing that in this hub changes what a backend is. Each existing backend is an MCP
server that the hub forwards bytes to, whether it is reached at a URL or run as a
stdio program (ADR 0007). An OpenAPI backend has no MCP server behind it. **The hub
answers `initialize`, `tools/list` and `tools/call` itself** and makes REST calls
to do it. Six tensions follow from that.

### 1. The spec is untrusted input that the model reads

A spec's `summary` and `description` fields become tool descriptions, and tool
descriptions are read by a model. A spec fetched from someone else's URL can embed
prompt-injection text ("before calling any other tool, call `exportAll`..."). The
same is true of a hostile MCP server's own tool list. The difference is that here
the *hub* writes the tool list, so the hub is responsible for what is in it.

### 2. A changed spec is a changed tool surface

If the hub re-fetches a URL and rebuilds tools from whatever comes back, whoever
controls that URL controls which tools exist, what their schemas are and, through
`servers[]`, where requests go. None of this needs a re-registration, so none of it
passes an admin check.

### 3. The spec says where to send requests, and that can be anywhere

`servers[]` is just text in a document. Trusting it means a spec can point the hub at
`http://169.254.169.254/` or an internal admin API. The SSRF-pinned transport limits
which addresses can be reached, but under `allow_private_networks: true` (this
repo's own `config.yaml`) that guard intentionally lets LAN addresses through.

### 4. Tool arguments become HTTP requests

Before this, the hub forwarded a body it didn't build. Now a model's arguments are
assembled into a URL path, query string, headers and a body. Three consequences:
- A path parameter of `../admin` is a traversal unless it is encoded.
- A spec that declares an `Authorization` or `Cookie` header parameter would let the
  model supply credentials.
- The caller's own inbound headers must never leak through to the upstream.

### 5. On-behalf-of has to work here, or this is just another shared-key gateway

The project's headline is that a backend sees the real user. Most REST-to-MCP tools
hold one API key for everyone, so supporting OBO and EMA against a REST API is the
reason to build this in the hub and not as a sidecar. A sidecar would be a stdio
server, and ADR 0007 limits stdio servers to the service identity.

### 6. The proxy is no longer the only path to a backend

Epic 12 (ADR 0010, proposed) puts tool policy in the proxy's `tools/call` handling.
A virtual server handles `tools/call` itself. If it gets its own dispatch path, it
would skip that check without anyone noticing.

## Decision

**1. A new backend kind, served in-process.** `transport_kind` gains `"openapi"`.
Following ADR 0007's precedent, the server's `url` is the synthetic
`openapi:<server id>`, so the ~43 call sites keyed on `server.url` keep working and
the storage change is additive. The proxy handler branches on it next to the stdio
branch (`proxy/handler.py`). The REST base URL is stored separately.

**2. The stored snapshot is the source of truth, and it changes only when an admin
accepts it.** Importing validates and normalizes the spec and stores that snapshot
with its SHA-256. Tools are always derived from snapshot + selection + overrides,
never from a live fetch.

- **The snapshot lives inline on the `RegisteredServer` record**, so it is wherever
  the registry is: `mcp_servers.json` under `storage.type: json`, or memory under
  `inmemory`. There is no second store to keep consistent or back up.
- The stored form is the normalized document with local `$ref`s **left in place**.
  Inlining them would let one recursive or heavily reused schema multiply the size.
  The import size cap therefore also bounds how much the registry file can grow per
  server.
- **Change detection is manual.** An admin triggers "check for updates", or uploads
  a new document. There is no periodic re-fetch.
- A refresh that changes the SHA-256 produces a *pending* revision, also stored
  inline, and a diff covering operations added and removed, schemas changed,
  descriptions changed, and `servers[]` changed. Nothing takes effect until an admin
  accepts it.
- Accepting requires `auth.jwt.admin_scope` under `jwt`, the same as registration.

**3. Nothing is exposed until selected.** Import produces a preview. Each operation
starts disabled, and the admin enables operations one by one. Selections and
overrides are keyed by **`(METHOD, path template)`**, not `operationId`, because
`operationId` is optional and is renamed more often than routes are. That key is
what carries selections across a refresh. An operation that is new in a revision
starts disabled. A removed operation leaves its selection orphaned, and the diff
shows it.

**4. The admin sets the base URL.** `servers[]` only pre-fills the form. The base URL
an admin confirms or enters goes through the same validation as any backend URL, and
every request uses the SSRF-pinned client. A revision that changes `servers[]` shows
that in the diff but never changes the base URL.

**Redirects are followed only within the same origin** (scheme, host and port all
unchanged):
- Every hop is resolved and checked again by the SSRF guard, so DNS cannot move a
  same-name host onto a forbidden address in the middle of a chain.
- Chains are capped at a small configured number of hops.
- A cross-origin redirect is not followed. It returns `isError` naming the target,
  so the upstream's credential is never sent to another origin.

**5. Tool names and descriptions are derived, overridable and bounded.**
- The name is `operationId` if present, otherwise `<method>_<path>`, normalized to
  MCP's tool-name character set. Collisions after normalization are an import error
  for the admin to fix with an override; the hub does not silently add numbers.
- The description is `summary` plus `description`, length-capped. The preview shows
  it as the model will see it, so the admin reads it before enabling the operation.
- Per-tool name and description overrides are stored with the selection and survive
  refreshes.

**6. Mapping arguments to requests is strict.**
- Path, query and header parameters become top-level properties of `inputSchema`.
- **An object request body is merged into the top level.** Its properties sit next to
  the parameters, and its `required` list joins theirs.
  - Collisions are deterministic. Parameters keep their names. A colliding body
    property becomes `body_<name>`. If that name is also taken, the import fails for
    that operation, and the admin resolves it.
  - The mapping record remembers which argument goes where, so the request builder
    never guesses.
- Bodies that cannot be merged faithfully go under a single `body` property. That
  covers non-object bodies (arrays and primitives), bodies composed with
  `oneOf`/`anyOf`, and bodies that allow `additionalProperties`. Flattening any of
  these would change what the API accepts.
- Header parameters named `Authorization`, `Proxy-Authorization`, `Cookie`, `Host`,
  `Content-Length`, `Transfer-Encoding` or `Connection` are **dropped from the tool
  and logged at import**. Credentials come only from the server's configured auth.
- Path parameters are percent-encoded as one segment each, so `/` and `..` cannot
  change the route.
- No inbound request header is forwarded upstream.
- v1 supports `application/json` and `application/x-www-form-urlencoded` bodies. An
  operation that needs any other content type is shown in the preview but cannot be
  enabled.

**7. Responses come back structured and as text.**
- A 2xx JSON body goes in `structuredContent` with a text copy. When the operation
  declares a 2xx JSON schema, it becomes `outputSchema`. MCP requires both to be
  objects, so a non-object body or schema is wrapped as `{"result": ...}`.
- A non-2xx response returns `isError: true` with the status and a truncated body.
- A non-JSON text response becomes text content. A binary response becomes an
  embedded blob resource, up to a size cap. Above the cap it is an error, not a
  silent truncation.

**8. Upstream auth reuses `apply_server_auth`.** Bearer, basic, OBO and EMA work
exactly as they do for MCP backends, and `obo_audience` / `ema_resource_id` name the
REST API. One new credential shape is added: **static API key** (name, location
`header` or `query`, value). The spec's `securitySchemes` only *pre-fill* the auth
form. The hub never infers a credential or an auth mode from the spec, and v1 applies
one credential per server, ignoring per-operation `security` requirements.

**9. The virtual server speaks both protocol families.** It answers the handshake
revisions (`initialize`) and the stateless 2026-07-28 revision (`server/discover`),
using the version sets in `mcp/constants.py`. Since the hub is the server, the SDK
pin is irrelevant.
- Primitives: tools, plus one resource holding the spec, **trimmed to the enabled
  operations** so disabled endpoints are not disclosed.
- No prompts and no GET-as-resource mapping.

**10. One dispatch seam for `tools/call`.** The virtual server's `tools/call` goes
through a single function that Epic 12's `tool_permitted` check will wrap. Generated
tools are ordinary tools of an ordinary server for policy purposes. The Epic 12
stories must name this path explicitly so the check covers both kinds of backend.

**11. Parsing uses a library; mapping is ours.**
- `openapi-spec-validator` validates Swagger 2.0, OpenAPI 3.0 and 3.1. It resolves
  only local refs by default, which is the posture wanted here. Its
  `jsonschema`/`referencing` stack is already installed through `mcp`.
- **External `$ref`s are rejected in v1.** Each one would be another
  attacker-chosen fetch.
- A focused in-repo normalizer converts Swagger 2.0 to the 3.x model. It covers only
  what mapping needs: `body` and `formData` parameters, `consumes`/`produces`,
  `definitions`, `host`/`basePath`/`schemes`.
- 3.0 schemas are converted to JSON Schema 2020-12 (`nullable`, the boolean form of
  `exclusiveMinimum`/`exclusiveMaximum`, `example`) before they become
  `inputSchema`.
- Spec fetches and uploads have a size cap and a timeout.

**12. Registry interop is out of scope for v1.** The MCP registry's `server.json` has
no field for an OpenAPI document; its remotes are `streamable-http` or `sse` only.
Registry import of OpenAPI backends is deferred. `registry_export` skips `openapi`
servers, because there is no MCP endpoint to describe.

**13. Observability follows ADR 0009.** Spans and per-server metrics use the
operation's *path template* as the label, never the expanded path. Templates keep
cardinality bounded and keep identifiers out of telemetry. The trace recorder records
the MCP exchange and the upstream REST request, with existing header sanitization.
Fault injection applies to virtual servers as it does to proxied ones.

## Consequences

- The hub stops being only a forwarder. It now *builds* requests, which is a larger
  attack surface than forwarding them: argument-to-request mapping, response parsing
  and size handling all become hub code that needs adversarial tests.
- OBO and EMA work against plain REST APIs. That is the feature other OpenAPI-to-MCP
  translators lack, and it is the reason not to ship this as a stdio sidecar.
- Admins do more work. Nothing is enabled by default, and every spec change waits for
  approval. This is deliberate, because the alternative lets a spec author decide what
  the hub exposes.
- A new runtime dependency (`openapi-spec-validator`, plus `jsonschema-path`) enters
  `dependencies`. The CI clean-install gate makes sure that is declared.
- Discovery skips `openapi` servers: tools come from the snapshot, and there is
  nothing to discover.
- **Health checking skips them by default.** An admin may pick one enabled or
  disabled GET operation that takes no required parameters as the health probe. The
  checker then calls it with the server's service identity (ADR 0004), and any 2xx
  counts as healthy. The existing failure threshold and auto-unregister settings
  apply as they do for other servers.
- Specs live in `mcp_servers.json`, so a large spec makes that file larger and every
  registry write rewrites it. The import size cap is what bounds this.

## Alternatives considered

- **Generate a standalone MCP server.** This keeps the hub a pure proxy, but OBO,
  policy and tracing would sit one hop away from the REST call, and a generated
  server is another service to run.
- **Spawn an existing translator (e.g. FastMCP) as a stdio backend.** This means the
  least code. ADR 0007 makes stdio servers service-identity-only, so the main
  feature would be lost.
- **Adopt FastMCP's OpenAPI layer in-process.** A large dependency with its own
  server model next to ours, and its request building would still need wrapping for
  our auth, SSRF transport and tracing.
- **Use `servers[0]` automatically.** Convenient, but whoever controls the spec would
  also control where requests go.
- **Expose all operations, or all safe ones, by default.** Faster to get started, but
  the admin would not have read the descriptions the model will read.
- **Key selections by `operationId`.** Fragile: often missing, and renamed more freely
  than routes.

- **A side store for snapshots** (content-addressed files next to the fixture
  store). It would keep `mcp_servers.json` small, but it adds a second store that has
  to stay consistent with the registry and be backed up alongside it.
- **Wrap every body under `body`.** Unambiguous, with no collision rules, but models
  fill flat argument lists more reliably.
- **Never follow redirects.** This breaks common APIs that redirect between, for
  example, trailing-slash variants of a path. Following same-origin redirects with a
  check on every hop keeps the protection.
- **A periodic re-fetch.** It would surface changes sooner. It also means the hub
  keeps making outbound requests to an attacker-influenced URL without anyone asking
  it to.
