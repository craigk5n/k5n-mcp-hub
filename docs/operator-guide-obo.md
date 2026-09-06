# Turning on per-user identity (on-behalf-of)

By default the hub calls every backend as itself. This guide switches it to calling
them **as the user who called the hub**, so a backend can authorize per user and log
who actually asked.

Two steps, in order — the second does not work without the first:

1. **Make the hub a resource server** (`auth.type: jwt`) so it knows who is calling.
2. **Register a server with `auth_type: obo`** so calls to it are exchanged.

You need an OAuth 2.1 identity provider that supports RFC 8693 token exchange.
Keycloak 26.2+ does, out of the box, and is what the worked example uses. Okta,
Auth0 and Entra ID all support the grant in some form.

> Everything here is verified by [`e2e/`](../e2e/README.md), which stands the whole
> thing up against a real Keycloak and asserts the downstream server sees the calling
> user. If a step here looks ambiguous, that stack is the executable version of it.

---

## Step 1 — The hub as a resource server

### 1.1 Configure your IdP

The hub needs to be a client your IdP knows about, and the token your MCP client
sends must be **audienced at the hub** — otherwise the very first hop fails.

For Keycloak, [`e2e/keycloak/configure_realm.py`](../e2e/keycloak/configure_realm.py)
does this against a live server and is a working reference. In summary:

| Client | Type | Why |
|---|---|---|
| your MCP client | public, PKCE | what the user logs into; needs an **audience mapper adding the hub** so its access tokens name the hub in `aud` |
| `k5n-mcp-hub` | confidential | the hub itself; enable **Standard token exchange** on this client (Keycloak 26.2+ — no fine-grained admin permissions needed) |
| each backend | — | exists so its name resolves as an `audience` for the exchange |

### 1.2 Configure the hub

Four settings, all required. The hub **refuses to start** without the first three,
with a message naming the one you missed:

```yaml
auth:
  type: jwt
  jwt:
    issuer: "https://idp.example.com/realms/mcp-hub"
    audience: "k5n-mcp-hub"
    jwks_uri: "https://idp.example.com/realms/mcp-hub/protocol/openid-connect/certs"
```

Or by environment, which is what a container wants:

```bash
MCPHUB_AUTH__TYPE=jwt
MCPHUB_AUTH__JWT__ISSUER=https://idp.example.com/realms/mcp-hub
MCPHUB_AUTH__JWT__AUDIENCE=k5n-mcp-hub
MCPHUB_AUTH__JWT__JWKS_URI=https://idp.example.com/realms/mcp-hub/protocol/openid-connect/certs
```

`jwks_uri` is required rather than derived from the issuer, because the path differs
per IdP — Keycloak puts it under `protocol/openid-connect/certs`, others elsewhere.
Guessing would fail at the first token rather than at startup.

Optional:

| Setting | Default | Use it when |
|---|---|---|
| `auth.jwt.algorithms` | asymmetric defaults (RS/PS/ES) | your IdP signs with something outside that set. Symmetric algorithms are **refused** — a JWKS publishes public keys |
| `auth.jwt.required_scopes` | none | you want the hub to reject tokens lacking a scope, before any backend sees them |
| `auth.jwt.leeway_seconds` | `0` | clock skew between the hub and your IdP causes spurious `exp`/`nbf` rejections |
| `auth.jwt.resource` | derived from the request | the hub sits behind a reverse proxy, so the request URL isn't its public identity |

### 1.3 Check it

```bash
curl -s http://localhost:8080/.well-known/oauth-protected-resource | jq
```

You should get your issuer back under `authorization_servers`. If you get a **404**,
`auth.type` isn't `jwt` — the hub only advertises this when it is actually enforcing.

An unauthenticated write should now be refused with a challenge pointing back at that
document:

```bash
curl -si -X POST http://localhost:8080/v1/register -d '{}' | grep -i www-authenticate
# WWW-Authenticate: Bearer realm="k5n-mcp-hub", resource_metadata="http://localhost:8080/.well-known/oauth-protected-resource"
```

> **The hub still starts if your IdP is down.** JWKS is fetched lazily on first token
> validation, never at boot, so an unreachable IdP degrades requests rather than
> preventing startup. That is deliberate — see
> [ADR 0001](adr/0001-hub-validates-inbound-tokens.md).

---

## Step 1b — Decide who may reach what

Turning on `auth.type: jwt` also turns on **per-server authorization**. Before it, the
hub authenticated callers but did nothing with the distinction: anyone authenticated
could reach any registered server, using the hub's stored credential. That is the
privilege escalation on-behalf-of exists to prevent, so `jwt` mode now closes it.

Two rules:

- **A server is reachable only by callers holding its `required_scope`.** A server
  that declares none is reachable by nobody but an admin. That default is deliberate:
  an unlabelled server is ambiguous, and resolving ambiguity toward "everyone" is how
  the hole arose.
- **Administering the hub needs a separate scope.** Registering and deleting servers,
  editing credentials, and toggling fault injection require `mcp:admin` (override with
  `auth.jwt.admin_scope`). Fault injection especially — it is a denial-of-service
  primitive against every other caller of that server.

```jsonc
{
  "id": "files",
  "url": "https://files.example.com/mcp",
  "required_scope": "files:use"   // callers need this scope in their token
}
```

Admins may reach every server. That is honest rather than notional: an admin can edit
a server's `required_scope` or read its stored credential anyway.

Traces follow the same line — a caller sees only their own requests to a server;
admins see all. Refused attempts are recorded too, with the subject that made them,
because denied access is what you most want in an audit trail.

> **This is a breaking change if you already run `auth.type: jwt`.** Every server
> needs a `required_scope` before its callers can reach it again. `none` and `basic`
> are unaffected — they are single-user modes with no tenancy to enforce.

---

## Step 2 — Register a server for on-behalf-of

In the admin UI: **Add Server → Authentication → On-behalf-of**. Or by API:

```bash
curl -X POST http://localhost:8080/v1/register \
  -H "Authorization: Bearer $YOUR_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "files",
    "url": "https://files.example.com/mcp",
    "auth_type": "obo",
    "oauth_token_url": "https://idp.example.com/realms/mcp-hub/protocol/openid-connect/token",
    "oauth_client_id": "k5n-mcp-hub",
    "oauth_client_secret": "...",
    "obo_audience": "mcp-server-files"
  }'
```

| Field | What it is |
|---|---|
| `oauth_token_url` | your IdP's token endpoint — where the exchange happens |
| `oauth_client_id` / `oauth_client_secret` | **the hub's own** client credentials, which authenticate the exchange. Not the user's, not the backend's |
| `obo_audience` | who the exchanged token is *for*: the backend, as your IdP names it |
| `obo_resource` | optional RFC 8707 resource indicator, if your IdP uses them |
| `obo_scope` | optional scope to request on the exchanged token |
| `obo_actor_token_source` | `none` (default) or `client_credentials` for delegation — see below |

The secret is redacted everywhere it could be read back: the API sanitizes it, the
edit dialog leaves it blank (blank means "keep current"), and it never appears in a
trace.

### Impersonation or delegation?

Default is **impersonation**: the exchanged token's `sub` is the user, and its `azp`
is the hub — so the backend can still tell the hub brokered the call.

Setting `obo_actor_token_source: client_credentials` additionally sends the hub's own
token as an `actor_token`, which issuers that implement RFC 8693 delegation use to add
an `act` claim naming both parties. **Keycloak's supported exchange has no
`actor_token` parameter at all**, so leave this at `none` there.
[ADR 0002](adr/0002-impersonation-default-delegation-opt-in.md) has the full reasoning.

---

## Verifying it end to end

Call a tool through the proxy with a real user's token and check what the backend saw:

```bash
curl -X POST http://localhost:8080/mcp \
  -H "Authorization: Bearer $USER_ACCESS_TOKEN" \
  -H 'X-MCP-Target-Server: files' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"whoami","arguments":{}}}'
```

A correctly configured setup means the backend received a token whose `sub` is the
calling user, whose `aud` is the backend, and whose `azp` is the hub. The server card
in the admin UI shows a green **On-behalf-of** badge naming the audience.

The single most useful check: **point your MCP client straight at the backend with its
hub token.** It should be refused. If the backend accepts it, the backend isn't
validating its audience, and nothing about this setup is buying you anything.

---

## Troubleshooting

The server card shows the failure under its badge, and the same text lands in
`obo_error`. Match on what you see:

| What you see | What it means | Fix |
|---|---|---|
| Hub won't start: `auth.type is 'jwt' but auth.jwt.issuer is not set` | A required setting is missing. The hub fails closed rather than running unprotected | Set the named field |
| **401** with `WWW-Authenticate: Bearer ... resource_metadata=` | No valid caller identity — no token, expired, wrong audience, or wrong issuer | Check the token's `aud` names the hub, and `iss` matches `auth.jwt.issuer` exactly (a trailing slash counts) |
| **502** `On-behalf-of authentication failed: invalid_target...` | The IdP refused the exchange: the audience isn't a client it knows, or isn't permitted | Confirm `obo_audience` matches a client in your IdP, and that token exchange is enabled on the hub's client |
| `obo_error: no user identity available to act on behalf of` | The request reached an OBO server with no authenticated caller | The caller sent no token, or `auth.type` isn't `jwt` |
| `obo_error: missing oauth client credentials for the exchange` | `oauth_client_id`/`oauth_client_secret` are unset on the server record | Re-register with the hub's own client credentials |
| `obo_error: no oauth token endpoint configured` | `oauth_token_url` is unset and no OAuth metadata was discovered | Set `oauth_token_url` explicitly |
| Backend returns **401** even though the exchange succeeded | The exchanged token isn't valid there | Check the backend validates `aud` against the same name you put in `obo_audience`. The hub retries once with a fresh token before giving up |
| **403** `requires the 'x:use' scope` | The caller authenticated but lacks the server's `required_scope` | Grant the scope in your IdP, or change the server's `required_scope` |
| **403** `this server declares no required_scope, so only an admin may reach it` | The server has no rule, and `auth.type` is `jwt` | Set `required_scope` on the server |
| **403** `requires the 'mcp:admin' scope` | An admin operation attempted by a non-admin | Grant the admin scope, or set `auth.jwt.admin_scope` to one you already issue |
| Card says *"No service credential: health checks report reachability only"* | The server has OBO configured and nothing else, so background health and discovery have no identity to use | Expected. Add a static or client-credentials credential if you want background capability discovery |

**A failed exchange never falls back to another credential.** If it fails, the call
fails — the hub will not quietly run it as itself, because that would use broader
rights and look like success. If you *want* service-identity behaviour, register the
server as `auth_type: oauth` instead. See
[ADR 0003](adr/0003-fail-closed-on-token-exchange-failure.md).

---

## Enterprise-Managed Authorization (ID-JAG)

If a backend has its **own** authorization server rather than trusting your IdP, plain
OBO can't reach it — the hub can only mint tokens at one IdP. Register it with
`auth_type: "ema"` instead, which runs the two-leg ID-JAG flow: exchange at your IdP
for an assertion, then redeem that at the backend's authorization server.

Note before adopting it: **ID-JAG is an active IETF draft**, and Keycloak cannot issue
these assertions today (its support is receiver-side and experimental). See the Epic 8
notes in [`TODO.md`](../TODO.md) and
[ADR 0005](adr/0005-hub-is-the-mcp-client-in-ema.md).
