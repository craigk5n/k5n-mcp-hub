import asyncio
import json
import os
from pathlib import Path
from typing import Any

from mcp_hub.utils import sanitize_filename


class FixtureStore:
    def __init__(self, base_dir: str = ".mcp_hub/fixtures") -> None:
        self._base_dir = Path(base_dir).resolve()
        self._lock = asyncio.Lock()

    async def list(self, agent_id: str) -> list[str]:
        async with self._lock:
            safe_agent_dir = self._agent_dir(agent_id)
            if not safe_agent_dir.exists():
                return []
            files = []
            for f in safe_agent_dir.iterdir():
                if f.is_file() and f.suffix == ".json":
                    resolved = f.resolve()
                    if not resolved.is_relative_to(self._base_dir):
                        continue
                    files.append(f.stem)
            return sorted(files)

    async def load(self, agent_id: str, name: str) -> dict[str, Any]:
        async with self._lock:
            safe_agent_dir = self._agent_dir(agent_id)
            safe_name = sanitize_filename(name)
            file_path = (safe_agent_dir / f"{safe_name}.json").resolve()
            if not file_path.is_relative_to(self._base_dir):
                raise FileNotFoundError(f"Fixture not found: {name}")
            try:
                with open(file_path, "r") as f:
                    return json.load(f)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid fixture JSON: {name}") from e
            except FileNotFoundError:
                raise FileNotFoundError(f"Fixture not found: {name}")

    async def save(self, agent_id: str, name: str, body: dict[str, Any]) -> None:
        async with self._lock:
            safe_agent_dir = self._agent_dir(agent_id)
            safe_name = sanitize_filename(name)
            file_path = (safe_agent_dir / f"{safe_name}.json").resolve()
            if not file_path.is_relative_to(self._base_dir):
                raise ValueError("Invalid fixture name")
            safe_agent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            json_str = json.dumps(body, indent=2)
            tmp_path = safe_agent_dir / f"{safe_name}.tmp"
            try:
                tmp_fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    with os.fdopen(tmp_fd, "wb") as f:
                        f.write(json_str.encode("utf-8"))
                        f.flush()
                        os.fsync(f.fileno())
                except Exception:
                    os.close(tmp_fd)
                    raise
                os.replace(tmp_path, str(file_path))
            except FileExistsError:
                raise ValueError("Invalid fixture name")
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

    async def delete(self, agent_id: str, name: str) -> None:
        async with self._lock:
            safe_agent_dir = self._agent_dir(agent_id)
            safe_name = sanitize_filename(name)
            file_path = (safe_agent_dir / f"{safe_name}.json").resolve()
            if not file_path.is_relative_to(self._base_dir):
                raise FileNotFoundError(f"Fixture not found: {name}")
            try:
                file_path.unlink()
            except FileNotFoundError:
                raise FileNotFoundError(f"Fixture not found: {name}")

    def _agent_dir(self, agent_id: str) -> Path:
        safe_agent_id = sanitize_filename(agent_id)
        return self._base_dir / safe_agent_id
