from pathlib import Path

from fastapi.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.config import JSONStorageConfig, Settings, StorageConfig
from mcp_hub.storage import InMemoryStorage, JSONFileStorage


def _json_settings(path: Path) -> Settings:
    return Settings(storage=StorageConfig(type="json", json_=JSONStorageConfig(path=str(path))))


def test_defaults_to_inmemory_storage() -> None:
    app = create_app(Settings())
    assert isinstance(app.state.storage, InMemoryStorage)


def test_uses_jsonfile_storage_when_type_json(tmp_path: Path) -> None:
    app = create_app(_json_settings(tmp_path / "servers.json"))
    assert isinstance(app.state.storage, JSONFileStorage)


def test_json_storage_persists_registered_server_across_restart(tmp_path: Path) -> None:
    store_path = tmp_path / "servers.json"

    app1 = create_app(_json_settings(store_path))
    # TestClient as a context manager runs the lifespan (which calls storage.init()).
    with TestClient(app1) as client:
        resp = client.post(
            "/v1/register",
            json={
                "id": "persisted-server",
                "url": "http://unreachable-host:9999",
                "registration_type": "self",
            },
        )
        assert resp.status_code == 201

    assert store_path.exists(), "json storage should have written the server to disk"

    # A fresh app pointed at the same file must load the persisted server on init().
    app2 = create_app(_json_settings(store_path))
    with TestClient(app2) as client:
        resp = client.get("/v1/servers")
        assert resp.status_code == 200
        ids = [s["id"] for s in resp.json()["servers"]]
        assert "persisted-server" in ids
