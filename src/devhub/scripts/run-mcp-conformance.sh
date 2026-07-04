#!/bin/sh
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
        *[\;\|\&\`\$\(\)\<\>\!\{\}\\]*)
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
