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
