import asyncio
import hashlib
import logging
import os
import re
import shlex
import shutil
import tempfile
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from devhub.mcp.constants import is_supported_protocol_version
from devhub.mcp.discovery import DiscoveryService
from devhub.mcp.validation import validate_tool_name
from devhub.models.server import RegisteredServer
from devhub.registry.service import Registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["ui"])

CONFORMANCE_SCRIPT = r"""#!/bin/sh
#
# This script is embedded in the dev-hub Python package and copied to a
# temp file at run time. It is invoked via subprocess.run(["sh", path], env=...).
#
set -eu

# ---------------------------------------------------------------------------
# Command resolution
# ---------------------------------------------------------------------------
if [ -z "${MCP_CONFORMANCE_CMD:-}" ]; then
    if [ -n "${MCP_CONFORMANCE_NPM_DIR:-}" ]; then
        _npm_prefix="$MCP_CONFORMANCE_NPM_DIR"
        # Build positional list for npm --prefix <dir> exec -- @modelcontextprotocol/conformance
        set -- npm --prefix "$_npm_prefix" exec -- @modelcontextprotocol/conformance
        _cmd_display="npm --prefix $MCP_CONFORMANCE_NPM_DIR exec -- @modelcontextprotocol/conformance"
    else
        set -- npx @modelcontextprotocol/conformance
        _cmd_display="npx @modelcontextprotocol/conformance"
    fi
else
    # Validate MCP_CONFORMANCE_CMD does not contain shell metacharacters.
    case "$MCP_CONFORMANCE_CMD" in
        *[\;\\|\\&\\`\\$\\(\\)\\<\\>\\!\\{\\}\\]*)
            echo "MCP_CONFORMANCE_CMD contains unsafe characters" >&2
            exit 2
            ;;
    esac
    # Word-split MCP_CONFORMANCE_CMD intentionally: it may be "npm --prefix /x exec --"
    # shellcheck disable=SC2086
    set -- $MCP_CONFORMANCE_CMD
    _cmd_display="$MCP_CONFORMANCE_CMD"
fi

# ---------------------------------------------------------------------------
# Validate required variable
# ---------------------------------------------------------------------------
if [ -z "${MCP_CONFORMANCE_TARGET:-}" ]; then
    echo "Set MCP_CONFORMANCE_TARGET to the target MCP base URL." >&2
    echo 'MCP_CONFORMANCE_TARGET="http://localhost:8080/mcp"' >&2
    exit 2
fi

subcmd="${MCP_CONFORMANCE_SUBCOMMAND:-server}"

# ---------------------------------------------------------------------------
# Temp file management: create a primary temp file and register a trap so it
# is always cleaned up on exit (including early exits).
# ---------------------------------------------------------------------------
tmpfile=$(mktemp) || { echo "Failed to create temp file" >&2; exit 1; }
trap 'rm -f "$tmpfile"' EXIT

# ---------------------------------------------------------------------------
# Helper: run a legacy retry shape.
# Usage: _run_retry "$cmd_arg1" "$cmd_arg2" ...
# Creates its own temp file; returns the exit status of the command.
# ---------------------------------------------------------------------------
_run_retry() {
    _rtmp=$(mktemp) || { echo "Failed to create retry temp file" >&2; return 1; }
    # Note: _rtmp is always removed before return on the normal path.
    # In the unlikely event cat or rm is interrupted, the EXIT trap on $tmpfile
    # (the primary temp file) is the only cleanup that fires; _rtmp may be leaked
    # in that extreme case, which is acceptable.
    echo "Retrying: $*"
    set +e
    "$@" > "$_rtmp" 2>&1
    _rstatus=$?
    set -e
    cat "$_rtmp"
    rm -f "$_rtmp"
    return "$_rstatus"
}

# ---------------------------------------------------------------------------
# Primary attempt: exec directly using positional parameters
# ---------------------------------------------------------------------------
if [ -n "${MCP_CONFORMANCE_ARGS:-}" ]; then
    # shellcheck disable=SC2086  # intentional word-split of pre-validated ARGS
    echo "Running: $_cmd_display $subcmd --url <redacted> $MCP_CONFORMANCE_ARGS"
    set +e
    "$@" "$subcmd" --url "$MCP_CONFORMANCE_TARGET" $MCP_CONFORMANCE_ARGS > "$tmpfile" 2>&1
    status=$?
    set -e
else
    echo "Running: $_cmd_display $subcmd --url <redacted>"
    set +e
    "$@" "$subcmd" --url "$MCP_CONFORMANCE_TARGET" > "$tmpfile" 2>&1
    status=$?
    set -e
fi

cat "$tmpfile"

if [ "$status" -eq 0 ]; then
    exit 0
fi

if ! grep -qE 'unknown option|unknown command' "$tmpfile"; then
    exit "$status"
fi

# ---------------------------------------------------------------------------
# Legacy CLI shape retries
# ---------------------------------------------------------------------------
if [ -n "${MCP_CONFORMANCE_ARGS:-}" ]; then
    # shellcheck disable=SC2086  # intentional word-split of pre-validated ARGS
    if _run_retry "$@" run --target "$MCP_CONFORMANCE_TARGET" $MCP_CONFORMANCE_ARGS; then
        exit 0
    fi
    # shellcheck disable=SC2086
    if _run_retry "$@" --target "$MCP_CONFORMANCE_TARGET" $MCP_CONFORMANCE_ARGS; then
        exit 0
    fi
    # shellcheck disable=SC2086
    _run_retry "$@" "$MCP_CONFORMANCE_TARGET" $MCP_CONFORMANCE_ARGS
else
    if _run_retry "$@" run --target "$MCP_CONFORMANCE_TARGET"; then
        exit 0
    fi
    if _run_retry "$@" --target "$MCP_CONFORMANCE_TARGET"; then
        exit 0
    fi
    _run_retry "$@" "$MCP_CONFORMANCE_TARGET"
fi
"""


def official_conformance_missing_deps() -> list[str]:
    """Returns list of missing executables among {'node','npx'} by probing shutil.which()."""
    required = {"node", "npx"}
    missing: list[str] = []
    for exe in required:
        if shutil.which(exe) is None:
            missing.append(exe)
    return missing


@dataclass
class OfficialScenario:
    name: str
    key: str
    passed: int
    failed: int
    ok: bool


def scenario_key(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")

    hash_bytes = hashlib.sha1(name.encode("utf-8")).digest()
    hex4 = hash_bytes[:4].hex()

    if slug:
        return f"{slug}-{hex4}"
    return f"scenario-{hex4}"


def parse_official_conformance_output(output: str) -> tuple[list[OfficialScenario], int, int]:
    scenarios: list[OfficialScenario] = []
    total_pass = 0
    total_fail = 0
    in_summary = False

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue

        if line == "=== SUMMARY ===":
            in_summary = True
            continue

        if not in_summary:
            continue

        if line.startswith("Total:"):
            match = re.match(r"Total:\s*(\d+)\s*passed,\s*(\d+)\s*failed", line)
            if match:
                total_pass = int(match.group(1))
                total_fail = int(match.group(2))
            continue

        if line[0] in ("✓", "✗"):
            ok = line[0] == "✓"
            rest = line[1:].strip()
            match = re.match(r"(.+?):\s*(\d+)\s*passed,\s*(\d+)\s*failed", rest)
            if match:
                name = match.group(1)
                passed = int(match.group(2))
                failed = int(match.group(3))
                key = scenario_key(name)
                scenarios.append(
                    OfficialScenario(name=name, key=key, passed=passed, failed=failed, ok=ok)
                )

    if total_pass == 0 and total_fail == 0 and scenarios:
        total_pass = sum(s.passed for s in scenarios)
        total_fail = sum(s.failed for s in scenarios)

    return scenarios, total_pass, total_fail


async def _probe_capabilities(
    request: Request, srv: RegisteredServer
) -> tuple[list[dict], list[dict], list[dict]]:
    tools: list[dict] = srv.tools or []
    prompts: list[dict] = srv.prompts or []
    resources: list[dict] = srv.resources or []

    if not tools and not prompts and not resources:
        discovery_service: DiscoveryService = request.app.state.discovery_service
        registry: Registry = request.app.state.registry
        try:
            await discovery_service.discover_immediately(srv)
            updated_srv = await registry.get(srv.id)
            if updated_srv is not None:
                srv = updated_srv
                tools = srv.tools or []
                prompts = srv.prompts or []
                resources = srv.resources or []
        except Exception as e:
            logger.warning("Discovery failed for server %s: %s", srv.id, str(e))

    return tools, prompts, resources


@router.get("/server/{server_id}/conformance", response_class=HTMLResponse)
async def get_server_conformance(request: Request, server_id: str) -> HTMLResponse:
    registry: Registry = request.app.state.registry
    templates = request.app.state.templates

    srv = await registry.get(server_id)
    if srv is None:
        raise HTTPException(status_code=404, detail="Server not found")

    protocol_version = srv.mcp_protocol_version or "unknown"
    protocol_conformant = is_supported_protocol_version(protocol_version)

    schema_conformant = srv.schema_conformant is True
    schema_checked = srv.schema_conformant is not None

    if srv.supports_health_endpoint is True:
        health_support = "supports /health"
    elif srv.supports_health_endpoint is False:
        health_support = "no /health endpoint"
    else:
        health_support = "unknown"

    if srv.auth_type == "oauth":
        if srv.oauth_token_status == "ok":
            oauth_status = "token ok"
        elif srv.oauth_token_status == "error":
            oauth_status = "token error"
        else:
            oauth_status = "unchecked"
    else:
        oauth_status = "n/a"

    tools, prompts, resources = await _probe_capabilities(request, srv)

    tool_name_issues: list[str] = []
    for t in tools:
        name = t.get("name")
        tool_label = name if name else "unnamed tool"
        issues = validate_tool_name(name) if name else ["is empty"]
        for issue in issues:
            tool_name_issues.append(f"{tool_label} {issue}")

    tool_count = len(tools)
    prompt_count = len(prompts)
    resource_count = len(resources)

    schema_issues = srv.schema_issues or []
    if not schema_issues and tools:
        from devhub.mcp.validation import validate_tool_schemas

        _, computed_issues = validate_tool_schemas(tools)
        schema_issues = computed_issues
        schema_checked = True

    template = templates.get_template("conformance.html")
    html = await template.render_async(
        server_id=server_id,
        protocol_version=protocol_version,
        protocol_conformant=protocol_conformant,
        schema_checked=schema_checked,
        schema_conformant=schema_conformant,
        schema_issues=schema_issues,
        tool_name_issues=tool_name_issues,
        health_support=health_support,
        oauth_status=oauth_status,
        tool_count=tool_count,
        prompt_count=prompt_count,
        resource_count=resource_count,
    )
    return HTMLResponse(content=html)


@router.get("/server/{server_id}/conformance/official/status", response_class=HTMLResponse)
async def get_server_conformance_official_status(request: Request, server_id: str) -> HTMLResponse:
    registry: Registry = request.app.state.registry
    templates = request.app.state.templates

    srv = await registry.get(server_id)
    if srv is None:
        raise HTTPException(status_code=404, detail="Server not found")

    missing_list = official_conformance_missing_deps()
    missing_deps = [{"name": m} for m in missing_list]
    available = len(missing_list) == 0

    template = templates.get_template("conformance_official.html")
    html = await template.render_async(
        server_id=server_id,
        target_url=srv.url,
        available=available,
        missing=missing_deps,
        command="",
        output="",
        exit_code=0,
        has_run=False,
        scenarios=[],
        total_pass=0,
        total_fail=0,
    )
    return HTMLResponse(content=html)


@router.post("/server/{server_id}/conformance/official/run", response_class=HTMLResponse)
async def run_server_conformance_official(
    request: Request,
    server_id: str,
    scenario: str = Query(""),
) -> HTMLResponse:
    registry: Registry = request.app.state.registry
    templates = request.app.state.templates

    srv = await registry.get(server_id)
    if srv is None:
        raise HTTPException(status_code=404, detail="Server not found")

    missing_list = official_conformance_missing_deps()
    if missing_list:
        missing_deps = [{"name": m} for m in missing_list]
        template = templates.get_template("conformance_official.html")
        html = await template.render_async(
            server_id=server_id,
            target_url=srv.url,
            available=False,
            missing=missing_deps,
            command="",
            output="",
            exit_code=0,
            has_run=False,
            scenarios=[],
            total_pass=0,
            total_fail=0,
        )
        return HTMLResponse(content=html)

    args = f"--filter {scenario}" if scenario else ""

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".sh") as tmp:
            tmp_path = tmp.name
            tmp.write(CONFORMANCE_SCRIPT.encode("utf-8"))
        os.chmod(tmp_path, 0o700)

        env = {**os.environ, "MCP_CONFORMANCE_TARGET": srv.url, "MCP_CONFORMANCE_ARGS": args}
        proc = await asyncio.create_subprocess_exec(
            "sh",
            tmp_path,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        raw_output = stdout.decode("utf-8") + stderr.decode("utf-8")
        exit_code = proc.returncode

        cmd_line = (
            f"MCP_CONFORMANCE_TARGET={srv.url}"
            + (f" MCP_CONFORMANCE_ARGS={shlex.quote(args)}" if args else "")
            + f" sh {tmp_path}"
        )

        parsed_scenarios, total_pass, total_fail = parse_official_conformance_output(raw_output)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    template = templates.get_template("conformance_official.html")
    html = await template.render_async(
        server_id=server_id,
        target_url=srv.url,
        available=True,
        missing=[],
        command=cmd_line,
        output=raw_output,
        exit_code=exit_code,
        has_run=True,
        scenarios=parsed_scenarios,
        total_pass=total_pass,
        total_fail=total_fail,
    )
    return HTMLResponse(content=html)


@router.post("/server/{server_id}/conformance/official/retest", response_class=HTMLResponse)
async def retest_server_conformance_official(
    request: Request,
    server_id: str,
    scenario: str = Query(""),
) -> HTMLResponse:
    if not scenario:
        raise HTTPException(status_code=400, detail="scenario query param is required")

    registry: Registry = request.app.state.registry
    templates = request.app.state.templates

    srv = await registry.get(server_id)
    if srv is None:
        raise HTTPException(status_code=404, detail="Server not found")

    missing_list = official_conformance_missing_deps()
    if missing_list:
        raise HTTPException(
            status_code=400,
            detail=f"Missing dependencies: {', '.join(missing_list)}",
        )

    args = f'--scenario "{scenario}"'

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".sh") as tmp:
            tmp_path = tmp.name
            tmp.write(CONFORMANCE_SCRIPT.encode("utf-8"))
        os.chmod(tmp_path, 0o700)

        env = {**os.environ, "MCP_CONFORMANCE_TARGET": srv.url, "MCP_CONFORMANCE_ARGS": args}
        proc = await asyncio.create_subprocess_exec(
            "sh",
            tmp_path,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        raw_output = stdout.decode("utf-8") + stderr.decode("utf-8")
        exit_code = proc.returncode

        parsed_scenarios, _, _ = parse_official_conformance_output(raw_output)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    matching = next((s for s in parsed_scenarios if s.name == scenario), None)
    if matching is None:
        key = scenario_key(scenario)
        matching = OfficialScenario(
            name=scenario,
            key=key,
            passed=0,
            failed=0,
            ok=exit_code == 0,
        )

    scenario_dict = {
        "name": matching.name,
        "key": matching.key,
        "passed": matching.passed,
        "failed": matching.failed,
        "ok": matching.ok,
    }

    template = templates.get_template("conformance_scenario.html")

    if matching.ok:
        failed_delete = await template.render_async(
            server_id=server_id,
            scenario=scenario_dict,
            swap_oob="delete",
        )
        passed_append = await template.render_async(
            server_id=server_id,
            scenario=scenario_dict,
            swap_oob=f"beforeend:#official-passed-{srv.id}",
        )
        return HTMLResponse(content=failed_delete + passed_append)
    else:
        html = await template.render_async(
            server_id=server_id,
            scenario=scenario_dict,
            swap_oob="",
        )
        return HTMLResponse(content=html)
