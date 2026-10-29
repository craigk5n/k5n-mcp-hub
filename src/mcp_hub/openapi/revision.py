"""A pending spec revision: what changes, and accepting it (Story 13.7, ADR 0011 §2).

Pure. `diff_revision` maps the accepted and pending snapshots with the server's
current selection and compares them operation by operation. Descriptions and schemas
are compared *before* overrides, so a spec change is visible even where the admin
renamed or reworded a tool. `accept_revision` carries the choices over by
`(METHOD, path template)` (ADR 0011 §3): operations new in the revision start
disabled, and selections, overrides or a health probe that no longer have a servable
operation are dropped and reported. The result is validated as a whole before
anything is returned, so an accept that cannot be served changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp_hub.models.openapi import OpenAPIOperationRef, OpenAPIOverride, OpenAPISnapshot
from mcp_hub.models.server import RegisteredServer
from mcp_hub.openapi.importer import check_choices, health_probe_problem
from mcp_hub.openapi.mapping import preview
from mcp_hub.openapi.model import MappedOperation, MappingError, OperationKey
from mcp_hub.openapi.swagger2 import swagger2_to_openapi3


@dataclass(frozen=True)
class OperationChange:
    key: OperationKey
    changes: tuple[str, ...]


def _ref_json(ref: OpenAPIOperationRef) -> dict[str, str]:
    return {"method": ref.method, "path": ref.path}


@dataclass(frozen=True)
class RevisionDiff:
    added: tuple[MappedOperation, ...]
    removed: tuple[MappedOperation, ...]
    changed: tuple[OperationChange, ...]
    servers: tuple[list[str], list[str]] | None
    orphaned_selection: tuple[OpenAPIOperationRef, ...]
    orphaned_overrides: tuple[OpenAPIOverride, ...]
    orphaned_health_probe: OpenAPIOperationRef | None
    # What would stop `accept_revision` from succeeding as things stand.
    problems: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed or self.servers)

    def to_json(self) -> dict[str, Any]:
        return {
            "added": [
                {
                    "method": m.key.method,
                    "path": m.key.path,
                    "tool": m.tool.name if m.tool else None,
                    "unsupported": list(m.unsupported),
                }
                for m in self.added
            ],
            "removed": [{"method": m.key.method, "path": m.key.path} for m in self.removed],
            "changed": [
                {"method": c.key.method, "path": c.key.path, "changes": list(c.changes)}
                for c in self.changed
            ],
            "servers": {"before": self.servers[0], "after": self.servers[1]}
            if self.servers
            else None,
            "orphaned_selection": [_ref_json(r) for r in self.orphaned_selection],
            "orphaned_overrides": [_ref_json(r) for r in self.orphaned_overrides],
            "orphaned_health_probe": _ref_json(self.orphaned_health_probe)
            if self.orphaned_health_probe
            else None,
            "problems": list(self.problems),
        }


@dataclass(frozen=True)
class AcceptReport:
    dropped_selection: tuple[OpenAPIOperationRef, ...]
    dropped_overrides: tuple[OpenAPIOverride, ...]
    dropped_health_probe: OpenAPIOperationRef | None

    def to_json(self) -> dict[str, Any]:
        return {
            "selection": [_ref_json(r) for r in self.dropped_selection],
            "overrides": [_ref_json(r) for r in self.dropped_overrides],
            "health_probe": _ref_json(self.dropped_health_probe)
            if self.dropped_health_probe
            else None,
        }


def _mapped(
    snapshot: OpenAPISnapshot, server: RegisteredServer, limit: int
) -> dict[OperationKey, MappedOperation]:
    selection = frozenset(ref.key() for ref in server.openapi_selection)
    operations = preview(
        snapshot.document,
        snapshot.dialect,
        overrides={},
        description_limit=limit,
        selection=selection,
    )
    return {m.key: m for m in operations}


def _servers(snapshot: OpenAPISnapshot) -> list[str]:
    document = snapshot.document
    if snapshot.dialect == "2.0":
        document = swagger2_to_openapi3(document)
    return [s.get("url", "") for s in document.get("servers", []) if isinstance(s, dict)]


def _changes(before: MappedOperation, after: MappedOperation) -> tuple[str, ...]:
    if before.tool is not None and after.tool is None:
        return (f"now unsupported: {'; '.join(after.unsupported)}",)
    if before.tool is None and after.tool is not None:
        return ("now supported",)
    if before.tool is None or after.tool is None:
        return ()
    changes = []
    if before.tool.name != after.tool.name:
        changes.append("tool name")
    if before.tool.input_schema != after.tool.input_schema:
        changes.append("input schema")
    if before.tool.output_schema != after.tool.output_schema:
        changes.append("output schema")
    if before.tool.description != after.tool.description:
        changes.append("description")
    return tuple(changes)


def _pending(server: RegisteredServer) -> OpenAPISnapshot:
    if server.openapi_pending is None or server.openapi_snapshot is None:
        raise ValueError(f"server {server.id!r} has no pending revision")
    return server.openapi_pending


def _carried(
    server: RegisteredServer, after: dict[OperationKey, MappedOperation], limit: int
) -> tuple[RegisteredServer, AcceptReport]:
    """The server on the pending snapshot with every choice that still has a servable
    operation, and a report of the ones that do not. Not yet validated."""

    def servable(ref: OpenAPIOperationRef) -> bool:
        mapped = after.get(ref.key())
        return mapped is not None and mapped.tool is not None

    selection = [r for r in server.openapi_selection if servable(r)]
    overrides = [o for o in server.openapi_overrides if servable(o)]
    candidate = server.model_copy(
        update={
            "openapi_snapshot": server.openapi_pending,
            "openapi_pending": None,
            "openapi_selection": selection,
            "openapi_overrides": overrides,
        }
    )
    probe = server.openapi_health_probe
    dropped_probe = None
    if probe is not None and health_probe_problem(candidate, probe, description_limit=limit):
        dropped_probe = probe
        candidate = candidate.model_copy(update={"openapi_health_probe": None})
    report = AcceptReport(
        dropped_selection=tuple(r for r in server.openapi_selection if not servable(r)),
        dropped_overrides=tuple(o for o in server.openapi_overrides if not servable(o)),
        dropped_health_probe=dropped_probe,
    )
    return candidate, report


def diff_revision(server: RegisteredServer, *, description_limit: int) -> RevisionDiff:
    pending = _pending(server)
    accepted = server.openapi_snapshot
    assert accepted is not None  # checked by _pending
    before = _mapped(accepted, server, description_limit)
    after = _mapped(pending, server, description_limit)
    changed = tuple(
        OperationChange(key, changes)
        for key in after
        if key in before and (changes := _changes(before[key], after[key]))
    )
    old_servers, new_servers = _servers(accepted), _servers(pending)
    _, report = _carried(server, after, description_limit)
    try:
        accept_revision(server, description_limit=description_limit)
        problems: tuple[str, ...] = ()
    except MappingError as exc:
        problems = exc.problems
    return RevisionDiff(
        added=tuple(m for key, m in after.items() if key not in before),
        removed=tuple(m for key, m in before.items() if key not in after),
        changed=changed,
        servers=(old_servers, new_servers) if old_servers != new_servers else None,
        orphaned_selection=report.dropped_selection,
        orphaned_overrides=report.dropped_overrides,
        orphaned_health_probe=report.dropped_health_probe,
        problems=problems,
    )


def accept_revision(
    server: RegisteredServer, *, description_limit: int
) -> tuple[RegisteredServer, AcceptReport]:
    """The server with its pending revision accepted, or `MappingError` (and nothing
    changed) when the carried-over choices cannot be served from it."""
    pending = _pending(server)
    after = _mapped(pending, server, description_limit)
    candidate, report = _carried(server, after, description_limit)
    check_choices(candidate, description_limit=description_limit)
    return candidate, report
