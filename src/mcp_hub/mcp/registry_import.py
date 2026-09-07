"""Turn a registry record into something the hub can register.

Everything here is a *proposal* for an operator to confirm, never an action. The
registry is a public index the hub does not control, so its records are untrusted
input: they name URLs the hub would go on to probe, and programs it would otherwise
be asked to run. Import therefore produces a registration payload that goes through
the ordinary `POST /v1/register` path — SSRF validation, admin check and all — rather
than writing to storage directly.

See `docs/adr/0008-registry-import-yes-publish-no.md`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from mcp_hub.mcp.registry_client import RegistryRecord

# `ui_downloads.validate_ids` accepts only these characters, which is stricter than
# registration itself (non-empty). An id carried over verbatim would register fine and
# then 400 on every script download, so ids are derived to satisfy the stricter rule.
_ID_ALLOWED = re.compile(r"[^A-Za-z0-9_.-]")

# How a package registry's identifier is usually run. Only used to *show* the operator
# what the command would be; nothing here reaches a process (ADR 0007).
_RUNNERS = {
    "npm": "npx -y {identifier}",
    "pypi": "uvx {identifier}",
    "oci": "docker run --rm -i {identifier}",
    "nuget": "dnx {identifier}",
}


@dataclass(frozen=True)
class RemoteOption:
    """One addressable endpoint a record offers."""

    url: str
    transport: str
    index: int


@dataclass(frozen=True)
class CredentialHint:
    """A header the server needs. What it is, never what its value should be."""

    name: str
    description: str = ""
    is_required: bool = False
    is_secret: bool = False


@dataclass(frozen=True)
class StdioSuggestion:
    """What an operator would have to add to config to use a package-based server."""

    command_hint: str
    instruction: str


def derive_server_id(registry_name: str) -> str:
    """A hub server id from a reverse-DNS registry name.

    `ai.smithery/foo` becomes `ai.smithery.foo`: the slash is the only structural
    difference, and a dot keeps the namespacing legible. Anything else outside the
    allowed set collapses to `-` so the result is always a usable id — including for
    names carrying path separators, which must not survive into an id that later
    reaches a route.
    """
    name = (registry_name or "").strip()
    if not name:
        raise ValueError("a registry record needs a name to derive a server id from")

    candidate = name.replace("/", ".")
    candidate = _ID_ALLOWED.sub("-", candidate)
    # Collapse runs and trim, so "x/../../etc" cannot leave a leading dot-run that
    # reads like a relative path.
    candidate = re.sub(r"-{2,}", "-", candidate)
    candidate = re.sub(r"\.{2,}", ".", candidate)
    candidate = candidate.strip(".-")

    if not candidate:
        raise ValueError(f"cannot derive a usable server id from {registry_name!r}")
    return candidate


def remote_options(record: RegistryRecord) -> list[RemoteOption]:
    """Every endpoint the record offers, in order, for the operator to choose from.

    A record with several remotes is offering alternatives (regions, tiers), and the
    hub has no basis for picking one. Guessing the first would silently register a
    different endpoint than the operator expected.
    """
    options: list[RemoteOption] = []
    for index, remote in enumerate(record.remotes):
        url = remote.get("url")
        if isinstance(url, str) and url:
            options.append(
                RemoteOption(url=url, transport=str(remote.get("type") or ""), index=index)
            )
    return options


def required_credentials(remote: dict[str, Any]) -> list[CredentialHint]:
    """Headers the endpoint expects, as prompts.

    A record's header `value` is a placeholder like `Bearer {smithery_api_key}`.
    Carrying it across would store a fake credential and make the server look
    configured when it is not — worse than storing nothing, because the failure then
    looks like the server rejecting a real token.
    """
    hints: list[CredentialHint] = []
    for header in remote.get("headers") or []:
        if not isinstance(header, dict):
            continue
        name = header.get("name")
        if not isinstance(name, str) or not name:
            continue
        hints.append(
            CredentialHint(
                name=name,
                description=str(header.get("description") or ""),
                is_required=bool(header.get("isRequired")),
                is_secret=bool(header.get("isSecret")),
            )
        )
    return hints


def stdio_suggestion(record: RegistryRecord) -> StdioSuggestion | None:
    """What to add to `stdio.allowed_commands` for a package-based record.

    Returns a suggestion, never a registration. ADR 0007 requires the command to come
    from operator config rather than from a request, and a registry record is exactly
    the untrusted input that rule exists to refuse — an import that could write to the
    allowlist would make the allowlist worthless.
    """
    if not record.needs_stdio:
        return None

    package = record.packages[0]
    identifier = str(package.get("identifier") or record.name)
    registry_type = str(package.get("registryType") or "").lower()
    template = _RUNNERS.get(registry_type, "{identifier}")
    command_hint = template.format(identifier=identifier)

    return StdioSuggestion(
        command_hint=command_hint,
        instruction=(
            f"{record.name} is distributed as a {registry_type or 'package'} program, not a "
            "URL. To use it, add an entry to stdio.allowed_commands in your config and "
            f"register that entry by name:\n\n"
            f"    stdio:\n"
            f"      allowed_commands:\n"
            f"        {derive_server_id(record.name)}:\n"
            f"          command: {command_hint.split()[0]}\n"
            f"          args: {command_hint.split()[1:]}\n\n"
            "The hub will not add this for you: commands come from your configuration, "
            "never from a registry record. See ADR 0007."
        ),
    )


def to_register_payload(record: RegistryRecord, *, remote_index: int = 0) -> dict[str, Any]:
    """The `POST /v1/register` body for a record's chosen endpoint.

    Carries identity and address only. `auth_type` and every credential field are left
    unset: the record can say a credential is needed, never what it is, and the
    operator supplies it in the same dialog they confirm the import in.

    `mcp_transport` is deliberately left empty. The registry's "sse" means legacy SSE,
    while this codebase's "sse" is its marker for *streamable HTTP* (see
    `sdk_client._open_transport` and `templates/_health_badge.html`), so there is no
    honest mapping for the legacy value. Discovery determines the real transport from
    the handshake through `record_protocol_metadata`, which is the single write path
    for it — importing a guess would only give discovery something to correct.
    """
    options = remote_options(record)
    if not options:
        raise ValueError(
            f"{record.name!r} has no remote endpoint to register; it is a package-based "
            "server (see stdio_suggestion)"
        )
    if not 0 <= remote_index < len(options):
        raise ValueError(f"no remote at index {remote_index} for {record.name!r}")

    chosen = options[remote_index]
    return {
        "id": derive_server_id(record.name),
        "url": chosen.url,
        "name": record.title or record.name,
        "description": record.description,
        "registration_type": "manual",
    }
