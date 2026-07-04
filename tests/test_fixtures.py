import json
import os
import pytest

from devhub.agents.fixtures import FixtureStore


@pytest.mark.asyncio
async def test_save_then_list_returns_fixture_name(tmp_path) -> None:
    store = FixtureStore(str(tmp_path / "fixtures"))
    await store.save("agent-1", "my-fixture", {"key": "value"})

    result = await store.list("agent-1")

    assert result == ["my-fixture"]


@pytest.mark.asyncio
async def test_list_returns_sorted_names(tmp_path) -> None:
    store = FixtureStore(str(tmp_path / "fixtures"))
    await store.save("agent-1", "zebra", {"z": 1})
    await store.save("agent-1", "alpha", {"a": 1})
    await store.save("agent-1", "mango", {"m": 1})

    result = await store.list("agent-1")

    assert result == ["alpha", "mango", "zebra"]


@pytest.mark.asyncio
async def test_load_returns_saved_fixture(tmp_path) -> None:
    store = FixtureStore(str(tmp_path / "fixtures"))
    body = {"key": "value", "number": 42}
    await store.save("agent-1", "my-fixture", body)

    result = await store.load("agent-1", "my-fixture")

    assert result == body


@pytest.mark.asyncio
async def test_load_missing_raises_file_not_found(tmp_path) -> None:
    store = FixtureStore(str(tmp_path / "fixtures"))

    with pytest.raises(FileNotFoundError):
        await store.load("agent-1", "nonexistent")


@pytest.mark.asyncio
async def test_delete_removes_fixture(tmp_path) -> None:
    store = FixtureStore(str(tmp_path / "fixtures"))
    await store.save("agent-1", "my-fixture", {"key": "value"})

    await store.delete("agent-1", "my-fixture")

    result = await store.list("agent-1")
    assert result == []


@pytest.mark.asyncio
async def test_delete_missing_raises_file_not_found(tmp_path) -> None:
    store = FixtureStore(str(tmp_path / "fixtures"))

    with pytest.raises(FileNotFoundError):
        await store.delete("agent-1", "nonexistent")


@pytest.mark.asyncio
async def test_sanitize_forward_slash_in_name(tmp_path) -> None:
    store = FixtureStore(str(tmp_path / "fixtures"))
    await store.save("agent-1", "foo/bar", {"key": "value"})

    result = await store.list("agent-1")

    assert result == ["foo_bar"]


@pytest.mark.asyncio
async def test_sanitize_double_dot_in_name(tmp_path) -> None:
    store = FixtureStore(str(tmp_path / "fixtures"))
    await store.save("agent-1", "foo..bar", {"key": "value"})

    result = await store.list("agent-1")

    assert result == ["foo_bar"]


@pytest.mark.asyncio
async def test_sanitize_null_byte_in_name(tmp_path) -> None:
    store = FixtureStore(str(tmp_path / "fixtures"))
    await store.save("agent-1", "foo\x00bar", {"key": "value"})

    result = await store.list("agent-1")

    assert result == ["foo_bar"]


@pytest.mark.asyncio
async def test_sanitize_backslash_in_name(tmp_path) -> None:
    store = FixtureStore(str(tmp_path / "fixtures"))
    await store.save("agent-1", "foo\\bar", {"key": "value"})

    result = await store.list("agent-1")

    assert result == ["foo_bar"]


@pytest.mark.asyncio
async def test_sanitize_backslash_in_agent_id(tmp_path) -> None:
    store = FixtureStore(str(tmp_path / "fixtures"))
    await store.save("agent\\1", "my-fixture", {"key": "value"})

    result = await store.list("agent\\1")

    assert result == ["my-fixture"]


@pytest.mark.asyncio
async def test_sanitize_forward_slash_in_agent_id(tmp_path) -> None:
    store = FixtureStore(str(tmp_path / "fixtures"))
    await store.save("agent/1", "my-fixture", {"key": "value"})

    result = await store.list("agent/1")

    assert result == ["my-fixture"]


@pytest.mark.asyncio
async def test_sanitize_double_dot_in_agent_id(tmp_path) -> None:
    store = FixtureStore(str(tmp_path / "fixtures"))
    await store.save("agent..1", "my-fixture", {"key": "value"})

    result = await store.list("agent..1")

    assert result == ["my-fixture"]


@pytest.mark.asyncio
async def test_path_traversal_sanitization_prevents_escape(tmp_path) -> None:
    base_dir = tmp_path / "fixtures"
    store = FixtureStore(str(base_dir))

    await store.save("agent-1", "../../../etc/passwd", {"key": "value"})

    safe_agent_dir = base_dir / "agent-1"
    assert safe_agent_dir.exists()
    safe_file = safe_agent_dir / "______etc_passwd.json"
    assert safe_file.exists()
    assert safe_file.is_relative_to(base_dir)
    assert not (tmp_path / "etc").exists()
    assert not (tmp_path / "passwd").exists()


@pytest.mark.asyncio
async def test_path_traversal_in_agent_id_prevents_escape(tmp_path) -> None:
    base_dir = tmp_path / "fixtures"
    store = FixtureStore(str(base_dir))

    await store.save("../../../etc/passwd", "fixture", {"key": "value"})

    assert not (tmp_path / "etc").exists()


@pytest.mark.asyncio
async def test_fixture_file_mode_is_0600(tmp_path) -> None:
    store = FixtureStore(str(tmp_path / "fixtures"))
    await store.save("agent-1", "my-fixture", {"key": "value"})

    agent_dir = tmp_path / "fixtures" / "agent-1"
    files = list(agent_dir.glob("*.json"))
    assert len(files) == 1

    mode = os.stat(files[0]).st_mode & 0o777
    assert mode == 0o600


@pytest.mark.asyncio
async def test_fixture_parent_dir_mode_is_0700(tmp_path) -> None:
    store = FixtureStore(str(tmp_path / "fixtures"))
    await store.save("agent-1", "my-fixture", {"key": "value"})

    agent_dir = tmp_path / "fixtures" / "agent-1"
    mode = os.stat(agent_dir).st_mode & 0o777
    assert mode == 0o700


@pytest.mark.asyncio
async def test_list_empty_for_nonexistent_agent(tmp_path) -> None:
    store = FixtureStore(str(tmp_path / "fixtures"))

    result = await store.list("nonexistent-agent")

    assert result == []
