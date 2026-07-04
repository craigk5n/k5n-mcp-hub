from datetime import datetime, timezone

import pytest
from devhub.models import FaultInjection, RegisteredServer


class TestRegisteredServer:
    def test_persisted_fields_default_values(self) -> None:
        srv = RegisteredServer(id="test-id", url="http://test.example.com")
        assert srv.id == "test-id"
        assert srv.url == "http://test.example.com"
        assert srv.name == ""
        assert srv.version == ""
        assert srv.description == ""
        assert srv.tags == []
        assert srv.created_at is None
        assert srv.updated_at is None
        assert srv.registration_type == ""
        assert srv.mcp_protocol_version == ""
        assert srv.mcp_transport == ""
        assert srv.mcp_conformant is None
        assert srv.auth_type == ""
        assert srv.bearer_token == ""
        assert srv.oauth_discovery_url == ""
        assert srv.oauth_issuer == ""
        assert srv.oauth_token_url == ""
        assert srv.oauth_client_id == ""
        assert srv.oauth_client_secret == ""
        assert srv.oauth_scope == ""
        assert srv.oauth_resource == ""
        assert srv.oauth_metadata is None
        assert srv.oauth_last_checked is None
        assert srv.trace_verbose is False
        assert srv.fault_injection == FaultInjection()

    def test_volatile_fields_default_values(self) -> None:
        srv = RegisteredServer(id="test-id", url="http://test.example.com")
        assert srv.healthy is False
        assert srv.last_checked is None
        assert srv.uptime_seconds == 0.0
        assert srv.supports_health_endpoint is None
        assert srv.schema_conformant is None
        assert srv.schema_issues == []
        assert srv.oauth_token_status == ""
        assert srv.oauth_token_error == ""
        assert srv.tools is None
        assert srv.prompts is None
        assert srv.resources is None
        assert srv.last_capability_sync is None

    def test_roundtrip_json(self) -> None:
        srv = RegisteredServer(
            id="test-id",
            url="http://test.example.com",
            name="Test Server",
            version="1.0.0",
            description="A test server",
            tags=["test", "example"],
        )
        json_str = srv.model_dump_json()
        restored = RegisteredServer.model_validate_json(json_str)
        assert srv.id == restored.id
        assert srv.url == restored.url
        assert srv.name == restored.name
        assert srv.version == restored.version
        assert srv.description == restored.description
        assert srv.tags == restored.tags

    def test_sanitize_for_api_clears_secrets(self) -> None:
        srv = RegisteredServer(
            id="test-id",
            url="http://test.example.com",
            bearer_token="secret-token",
            oauth_client_secret="client-secret",
            oauth_token_error="some error",
        )
        sanitized = srv.sanitize_for_api()
        assert sanitized.bearer_token == ""
        assert sanitized.oauth_client_secret == ""
        assert sanitized.oauth_token_error == ""
        assert sanitized.id == srv.id
        assert sanitized.url == srv.url

    def test_sanitize_for_persistence_resets_volatile_fields(self) -> None:
        srv = RegisteredServer(
            id="test-id",
            url="http://test.example.com",
            tools=[{"name": "foo"}],
            prompts=[{"name": "bar"}],
            resources=[{"name": "baz"}],
            last_capability_sync=datetime.now(timezone.utc),
            schema_conformant=True,
            schema_issues=["issue1"],
            healthy=True,
            last_checked=datetime.now(timezone.utc),
            uptime_seconds=100.0,
            supports_health_endpoint=True,
            oauth_token_status="ok",
            oauth_token_error="some error",
        )
        sanitized = srv.sanitize_for_persistence()
        assert sanitized.tools is None
        assert sanitized.prompts is None
        assert sanitized.resources is None
        assert sanitized.last_capability_sync is None
        assert sanitized.schema_conformant is None
        assert sanitized.schema_issues == []
        assert sanitized.healthy is False
        assert sanitized.last_checked is None
        assert sanitized.uptime_seconds == 0.0
        assert sanitized.supports_health_endpoint is None
        assert sanitized.oauth_token_status == ""
        assert sanitized.oauth_token_error == ""
        assert sanitized.id == srv.id
        assert sanitized.url == srv.url

    def test_datetime_serializes_to_rfc3339_with_z(self) -> None:
        dt = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        srv = RegisteredServer(id="test-id", url="http://test.example.com", created_at=dt)
        json_str = srv.model_dump_json()
        assert '"created_at":"2024-01-15T10:30:00Z"' in json_str

    def test_datetime_naive_serializes_to_rfc3339_with_z(self) -> None:
        dt = datetime(2024, 1, 15, 10, 30, 0)
        srv = RegisteredServer(id="test-id", url="http://test.example.com", created_at=dt)
        json_str = srv.model_dump_json()
        assert '"created_at":"2024-01-15T10:30:00Z"' in json_str

    def test_consecutive_fails_in_dump(self) -> None:
        srv = RegisteredServer(id="test-id", url="http://test.example.com", consecutive_fails=5)
        dump = srv.model_dump()
        assert "consecutive_fails" in dump
        assert dump["consecutive_fails"] == 5
        json_str = srv.model_dump_json()
        assert "consecutive_fails" in json_str

    def test_fault_injection_default_factory(self) -> None:
        srv1 = RegisteredServer(id="test-id", url="http://test.example.com")
        srv2 = RegisteredServer(id="test-id2", url="http://test.example.com")
        assert srv1.fault_injection is not srv2.fault_injection

    def test_oauth_metadata_dict_serializes(self) -> None:
        srv = RegisteredServer(
            id="test-id",
            url="http://test.example.com",
            oauth_metadata={"key": "value", "nested": {"a": 1}},
        )
        json_str = srv.model_dump_json()
        restored = RegisteredServer.model_validate_json(json_str)
        assert restored.oauth_metadata == {"key": "value", "nested": {"a": 1}}

    def test_datetime_with_non_utc_timezone_converts_to_utc(self) -> None:
        dt_str = "2024-01-15T10:30:00+05:00"
        srv = RegisteredServer(id="test-id", url="http://test.example.com", created_at=dt_str)
        assert srv.created_at == datetime(2024, 1, 15, 5, 30, 0, tzinfo=timezone.utc)

    def test_datetime_naive_string_converts_to_utc(self) -> None:
        dt_str = "2024-01-15T10:30:00"
        srv = RegisteredServer(id="test-id", url="http://test.example.com", created_at=dt_str)
        assert srv.created_at == datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        json_str = srv.model_dump_json()
        assert '"created_at":"2024-01-15T10:30:00Z"' in json_str

    def test_datetime_naive_object_converts_to_utc(self) -> None:
        dt = datetime(2024, 1, 15, 10, 30, 0)
        srv = RegisteredServer(id="test-id", url="http://test.example.com", created_at=dt)
        assert srv.created_at == datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)


class TestFaultInjection:
    def test_default_values(self) -> None:
        fi = FaultInjection()
        assert fi.enabled is False
        assert fi.timeout_enabled is False
        assert fi.timeout_millis == 0
        assert fi.malformed_json is False
        assert fi.invalid_method is False
        assert fi.sse_interrupt is False

    def test_custom_values(self) -> None:
        fi = FaultInjection(
            enabled=True,
            timeout_enabled=True,
            timeout_millis=500,
            malformed_json=True,
            invalid_method=True,
            sse_interrupt=True,
        )
        assert fi.enabled is True
        assert fi.timeout_enabled is True
        assert fi.timeout_millis == 500
        assert fi.malformed_json is True
        assert fi.invalid_method is True
        assert fi.sse_interrupt is True

    def test_roundtrip_json(self) -> None:
        fi = FaultInjection(
            enabled=True,
            timeout_millis=100,
        )
        json_str = fi.model_dump_json()
        restored = FaultInjection.model_validate_json(json_str)
        assert fi.enabled == restored.enabled
        assert fi.timeout_millis == restored.timeout_millis
