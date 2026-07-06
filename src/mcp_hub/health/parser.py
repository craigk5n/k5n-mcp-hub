from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

HEALTHY_STATUSES = {"healthy", "ok", "up", "running", "active", "pass", "warn"}


def format_uptime(seconds: float) -> str:
    if seconds <= 0:
        return "0m"
    if seconds < 60:
        return "< 1 min"

    total_minutes = int(seconds // 60)
    days = total_minutes // (24 * 60)
    remaining_minutes = total_minutes % (24 * 60)
    hours = remaining_minutes // 60
    minutes = remaining_minutes % 60

    if days >= 30:
        return f"{days}d"

    parts: list[str] = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")

    return " ".join(parts)


@dataclass
class HealthResponse:
    raw: dict[str, Any]
    status: str = ""
    uptime_secs: float = 0.0
    has_status: bool = False
    has_uptime: bool = False

    def is_healthy(self) -> bool:
        if not self.has_status:
            return False
        return self.status in HEALTHY_STATUSES


class HealthParser:
    def __init__(self, status_field: str = "status", uptime_field: str = "uptime_seconds") -> None:
        self.status_field = status_field
        self.uptime_field = uptime_field

    def parse(self, body: bytes | str) -> HealthResponse:
        if isinstance(body, bytes):
            body = body.decode("utf-8")

        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}") from e

        if not isinstance(data, dict):
            raise ValueError("JSON root must be an object")

        status = ""
        has_status = False
        if self.status_field in data and isinstance(data[self.status_field], str):
            status = data[self.status_field].lower().strip()
            has_status = True

        uptime_secs = 0.0
        has_uptime = False
        if self.uptime_field in data:
            try:
                uptime_secs = float(data[self.uptime_field])
                has_uptime = True
            except (ValueError, TypeError):
                pass

        return HealthResponse(
            raw=data,
            status=status,
            uptime_secs=uptime_secs,
            has_status=has_status,
            has_uptime=has_uptime,
        )
