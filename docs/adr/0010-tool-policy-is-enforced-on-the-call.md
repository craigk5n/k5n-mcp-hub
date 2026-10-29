# 0010 — Tool policy is enforced on the call, and hiding is only a courtesy

**Status:** Proposed
**Date:** 2026-10-28
**Context:** Tool governance (TODO.md Epic 12)

## Context

Today the unit of authorization is the server. `require_server_access` decides whether
a caller may reach a backend at all (`required_scope`, admin bypass), and once past
that check the proxy forwards the body without reading it. A caller who may reach a
GitHub MCP server may call every tool it has, `delete_repository` included.

That is coarser than the identity the hub already holds. Under `auth.type: jwt` the
proxy knows who is calling and which scopes they carry. It just does nothing with that
below the server level. Per-tool policy uses identity information the hub already
validates. Most gateways can't offer it, because they forward one shared credential
and have no per-user identity to act on.

The hard part is not the policy model. It is that **the proxy has to start
understanding MCP messages**, which it has so far avoided on purpose. Five specific
tensions:

### 1. Hiding a tool and refusing it are different guarantees

Filtering a tool out of `tools/list` stops an honest client from offering it to a
model. It stops nothing else: a client can send `tools/call` for a name it was never
shown, and tool names are stable and guessable. If enforcement lives in the list
filter, the policy is advisory.

### 2. The response side is a stream

Responses arrive as `application/json` or as SSE on the POST, and the proxy tees
them to the client without buffering, because a stream can stay open indefinitely
(the 2026-07-28 subscriptions). Rewriting `tools/list` means parsing SSE events in
flight. A stream resumed with `Last-Event-ID` can also replay a `tools/list`
response on a GET, where the proxy never saw the request it answers.

### 3. A parser differential is a bypass

`_method_for_span` is best-effort by design: telemetry must never be what rejects
a request. Enforcement can't follow that rule. If the hub reads
`{"name": "read_file", "name": "delete_file"}` as `delete_file` (Python's `json`
keeps the last duplicate) and the backend reads `read_file`, or the other way
round, the policy checked one call and the backend ran another. The same goes for a
body the hub fails to parse but the backend accepts.

### 4. Tool names drift

Backends rename and add tools. An allow-list fails closed when that happens: the new
tool is unavailable until someone adds it. A deny-list fails open: `delete_repo`
renamed to `remove_repo` is now callable. Neither is wrong, but an operator needs to
know which one they chose.

### 5. Re-registration replaces fields

`Registry.register` merges discovered tools/prompts/resources but replaces every other
field. If tool policy behaved like `required_scope`, a re-registration that omits it
(a script, a registry import, an older client) would silently erase a deny-list.

## Decision

**The `tools/call` check is the security boundary. List filtering is UX on top of
it. Any body the hub cannot parse unambiguously is refused.**

1. **Policy is per server, stored on the server.** A new `tool_policy` field on
   `RegisteredServer`:
   - `allow`: tool-name patterns. Empty means every tool.
   - `deny`: tool-name patterns. Deny overrides allow.
   - `scopes`: a map of tool-name pattern to the scope a caller must hold.

   Patterns use `fnmatch.fnmatchcase`: case-sensitive, `*` and `?` only. Globs earn
   their place for deny-lists (`delete_*`). Character classes do not.

2. **Enforcement happens on the request, before fault injection**, in the same place
   and for the same reason as `require_server_access`: a refused caller must not be
   able to trigger a fault. The request body is already fully buffered there, for
   HTTP and stdio alike, so this costs a parse and no change to streaming.

3. **For a server with a non-empty policy, the body must parse strictly or the
   request is refused.** Strictly means valid JSON, duplicate keys rejected
   (`object_pairs_hook`), and for `tools/call` a string `params.name`. A JSON-RPC
   batch is evaluated element by element, and the whole batch is refused if any
   element is denied. A batch in which some calls run and others are rejected is a
   partial outcome no client handles well. Servers with no policy keep today's
   behavior: the body passes through unparsed.

4. **A denied call looks like an unknown tool.** The proxy answers with a JSON-RPC
   error (`-32602`, `Unknown tool: <name>`) and never contacts the backend. It does
   not reply 403. A tool the caller was not shown should not confirm it exists. The
   trace entry and the OTel span record the real outcome, `tool_denied`, with the
   tool name and subject. The operator needs to see the attempt, and the caller
   does not need to know why it failed.

5. **`tools/list` responses are filtered on a best-effort basis.** On a POST the
   filter matches the response by the request's JSON-RPC id, for both JSON and SSE
   bodies, rewrites only that event, and passes every other event through
   untouched. On a GET or resumed stream it filters any result that is
   unambiguously a tool list (`result.tools` is a list of objects with string
   `name`s). A miss there leaks a tool name, never an invocation, because decision 4
   holds regardless.

6. **Server-level `allow`/`deny` apply to everyone, admins included.** They describe
   what the hub exposes from this backend, not who the caller is. An admin who
   disagrees edits the policy, and that edit is recorded. Per-tool `scopes` follow
   `may_call_server`: an admin satisfies them, for the reason already given there.

7. **Per-tool scopes fail closed when they cannot be checked.** Under any `auth.type`
   other than `jwt` the caller carries no verified scopes. A tool with a scope
   requirement is then unavailable, and the admin UI says why. This is stricter than
   `required_scope`, which is not enforced outside `jwt`, and that is deliberate. An
   unlabelled server is ambiguous, but a per-tool scope is always a restriction an
   operator added on purpose. Ignoring it would make the hub silently weaker than its
   configuration says.

8. **Omitting `tool_policy` on re-registration keeps the existing policy.** Only an
   explicit value replaces it, and clearing it takes an explicit empty policy. This
   is the merge rule tools already follow, applied for the opposite reason: the
   discovered tools are preserved for convenience, the policy for safety. Changing a
   policy requires `auth.jwt.admin_scope`, like other config-changing operations.

9. **Policy is local and never leaves the hub.** `registry_export` omits it, as it
   omits `required_scope`. Registry import never sets it. Tool download scripts are
   not offered for tools the requesting caller cannot call.

10. **The hub reads no other claims.** Per-user rules are expressed as scopes, which
    `Principal` already carries and the IdP already issues. A rule language over
    arbitrary claims (groups, departments, ABAC) is out of scope. If the IdP can map
    an attribute to a scope, the hub needs no new trust surface. If it can't, that is
    a future ADR, not something to add here.

## Consequences

- The proxy now parses request bodies for servers that have a policy. That is a small,
  bounded cost, and only those servers pay it. Servers without one are byte-for-byte
  unchanged, which keeps the existing contract and the existing tests honest.
- Strict parsing will refuse some requests a lenient backend would have accepted.
  That is the intended trade: a request the hub cannot read is one it cannot vouch
  for. The refusal is traced with the parse error so it can be diagnosed.
- SSE response rewriting is the most delicate code this feature adds, and it is
  confined to policy-bearing servers. Because call enforcement does not depend on
  it, a bug there leaks names rather than access. The tests are weighted toward
  enforcement accordingly.
- Choosing allow-list or deny-list is now an operator decision with a known failure
  direction. The admin UI flags drift either way: a deny pattern that matches no
  discovered tool (likely stale), and a discovered tool that no allow pattern
  matches (hidden, possibly unintentionally).
- Prompts and resources can be as sensitive as tools. They are out of scope here, but
  the parsing, filtering and fail-closed rules are written to extend to
  `prompts/get` and `resources/read` without a new ADR.

## Alternatives considered

- **Filter `tools/list` only.** Cheapest, and it is what "hide tools from the
  client" usually means. Rejected: it is advisory against any client that sends a
  name it was not shown (tension 1).
- **Reply 403 to a denied call.** More honest at the HTTP layer, and simpler. It
  breaks the list filter's least-information property by confirming the tool exists,
  and some clients surface a 403 as an authentication problem and send the user back
  to log in.
- **Enforce in the backend, and leave the hub as a pure pass-through.** With OBO the
  backend does see the real user and *can* do this. Many MCP servers can't, though,
  and the operator who deploys the hub often does not control the backend.
  Per-tool policy at the hub is a second layer, not a replacement. The README should
  say so.
- **A general claims-based policy language (CEL, OPA, Rego).** Rejected for now (see
  decision 10). It adds a trust surface and a dependency for flexibility no user has
  asked for, and scopes already cover the common case.
- **Lenient parsing, consistent with `_method_for_span`.** Rejected because of
  tension 3: a lenient enforcer opens a parser-differential bypass.
