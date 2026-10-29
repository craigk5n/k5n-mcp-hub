import os
import pathlib

import pytest
import yaml

from mcp_hub.config import (
    HealthCheckConfig,
    OpenAPIConfig,
    Settings,
    load_settings,
)


class TestEnvExampleDocumentation:
    def test_env_example_exists_and_documents_override_patterns(self, tmp_path: pathlib.Path):
        repo_root = pathlib.Path(__file__).parent.parent
        env_example_path = repo_root / ".env.example"

        assert env_example_path.exists(), ".env.example must exist at repository root"

        content = env_example_path.read_text()

        assert "SERVER_HTTP_PORT" in content, ".env.example must document SERVER_HTTP_PORT override"
        assert "MCPHUB_" in content, ".env.example must document MCPHUB_ nested-prefix pattern"


class TestDefaults:
    def test_defaults_no_config_no_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ):
        monkeypatch.chdir(tmp_path)
        for key in list(os.environ.keys()):
            if key.startswith("MCPHUB_") or key == "SERVER_HTTP_PORT":
                monkeypatch.delenv(key, raising=False)

        result = load_settings()

        assert result.server.http_port == 8080
        assert result.storage.type == "inmemory"
        # Safe-by-default: no auth and no built-in password unless the operator opts in.
        assert result.auth.type == "none"
        assert result.auth.basic_auth.register_user == "admin"
        assert result.auth.basic_auth.register_pass == ""


class TestYAMLLoading:
    def test_yaml_overrides_defaults(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
        for key in list(os.environ.keys()):
            if key.startswith("MCPHUB_") or key == "SERVER_HTTP_PORT":
                monkeypatch.delenv(key, raising=False)

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"server": {"http_port": 9000}}))

        result = load_settings(path=str(config_file))

        assert result.server.http_port == 9000


class TestEnvVarOverrides:
    def test_mcp_hub_env_overrides_yaml(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"server": {"http_port": 9000}}))

        monkeypatch.setenv("MCPHUB_SERVER__HTTP_PORT", "9100")

        result = load_settings(path=str(config_file))

        assert result.server.http_port == 9100

    def test_bare_env_overrides_everything(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"server": {"http_port": 9000}}))

        monkeypatch.setenv("MCPHUB_SERVER__HTTP_PORT", "9100")
        monkeypatch.setenv("SERVER_HTTP_PORT", "9200")

        result = load_settings(path=str(config_file))

        assert result.server.http_port == 9200

    def test_bare_env_zero_not_overrides(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"server": {"http_port": 9000}}))

        monkeypatch.setenv("SERVER_HTTP_PORT", "0")

        result = load_settings(path=str(config_file))

        assert result.server.http_port == 9000


class TestStorageTypeAliases:
    def test_json_alias(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
        for key in list(os.environ.keys()):
            if key.startswith("MCPHUB_") or key == "SERVER_HTTP_PORT":
                monkeypatch.delenv(key, raising=False)

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"storage": {"type": "json"}}))

        result = load_settings(path=str(config_file))

        assert result.storage.type == "json"

    def test_jsonfile_alias(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
        for key in list(os.environ.keys()):
            if key.startswith("MCPHUB_") or key == "SERVER_HTTP_PORT":
                monkeypatch.delenv(key, raising=False)

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"storage": {"type": "jsonfile"}}))

        result = load_settings(path=str(config_file))

        assert result.storage.type == "json"

    def test_file_alias(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
        for key in list(os.environ.keys()):
            if key.startswith("MCPHUB_") or key == "SERVER_HTTP_PORT":
                monkeypatch.delenv(key, raising=False)

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"storage": {"type": "file"}}))

        result = load_settings(path=str(config_file))

        assert result.storage.type == "json"


class TestRedisAccepted:
    def test_redis_type_accepted(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
        for key in list(os.environ.keys()):
            if key.startswith("MCPHUB_") or key == "SERVER_HTTP_PORT":
                monkeypatch.delenv(key, raising=False)

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"storage": {"type": "redis"}}))

        result = load_settings(path=str(config_file))

        assert result.storage.type == "redis"


class TestHealthCheckConfigValidator:
    def test_interval_negative_becomes_default(self):
        config = HealthCheckConfig(interval_seconds=-1)
        assert config.interval_seconds == 30

    def test_interval_zero_becomes_default(self):
        config = HealthCheckConfig(interval_seconds=0)
        assert config.interval_seconds == 30

    def test_timeout_negative_becomes_default(self):
        config = HealthCheckConfig(timeout_seconds=-1)
        assert config.timeout_seconds == 5

    def test_timeout_zero_becomes_default(self):
        config = HealthCheckConfig(timeout_seconds=0)
        assert config.timeout_seconds == 5

    def test_failure_threshold_negative_becomes_default(self):
        config = HealthCheckConfig(failure_threshold=-1)
        assert config.failure_threshold == 3

    def test_failure_threshold_zero_becomes_default(self):
        config = HealthCheckConfig(failure_threshold=0)
        assert config.failure_threshold == 3

    def test_string_env_values_are_coerced(self):
        # Env overrides (e.g. MCPHUB_HEALTHCHECK__INTERVAL_SECONDS=5) arrive as strings and
        # must be accepted, not raise "'<=' not supported between str and int".
        config = HealthCheckConfig(interval_seconds="5", timeout_seconds="2", failure_threshold="4")
        assert config.interval_seconds == 5
        assert config.timeout_seconds == 2
        assert config.failure_threshold == 4

    def test_string_non_positive_and_garbage_fall_back_to_default(self):
        assert HealthCheckConfig(interval_seconds="0").interval_seconds == 30
        assert HealthCheckConfig(interval_seconds="-3").interval_seconds == 30
        assert HealthCheckConfig(interval_seconds="abc").interval_seconds == 30

    def test_valid_values_preserved(self):
        config = HealthCheckConfig(interval_seconds=60, timeout_seconds=10, failure_threshold=5)
        assert config.interval_seconds == 60
        assert config.timeout_seconds == 10
        assert config.failure_threshold == 5


class TestOpenAPIConfig:
    """Story 13.1: spec size and fetch time are operator settings, not constants."""

    def test_defaults(self):
        config = Settings.from_defaults().openapi
        assert config.max_spec_bytes == 5 * 1024 * 1024
        assert config.fetch_timeout_seconds == 30
        assert config.validate_timeout_seconds == 120
        assert config.validate_memory_mb == 1024
        assert config.max_description_chars == 1024
        assert config.call_timeout_seconds == 30
        assert config.max_response_bytes == 1024 * 1024
        assert config.max_redirects == 5

    def test_max_redirects_may_be_zero(self):
        assert OpenAPIConfig(max_redirects=0).max_redirects == 0
        assert OpenAPIConfig(max_redirects=-1).max_redirects == 5
        assert OpenAPIConfig(max_redirects="x").max_redirects == 5

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MCPHUB_OPENAPI__MAX_SPEC_BYTES", "20971520")
        monkeypatch.setenv("MCPHUB_OPENAPI__FETCH_TIMEOUT_SECONDS", "90")
        config = load_settings().openapi
        assert config.max_spec_bytes == 20 * 1024 * 1024
        assert config.fetch_timeout_seconds == 90

    def test_yaml_is_read(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
        # Each section is wired into load_settings by hand; a section left out there
        # is silently ignored, and the shipped-config test cannot see that while the
        # shipped values equal the defaults.
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"openapi": {"max_spec_bytes": 1234}}))
        assert load_settings(path=str(config_file)).openapi.max_spec_bytes == 1234

    @pytest.mark.parametrize("bad", [0, -1, "0", "abc", None])
    def test_non_positive_and_garbage_fall_back_to_defaults(self, bad):
        config = OpenAPIConfig(
            max_spec_bytes=bad,
            fetch_timeout_seconds=bad,
            validate_timeout_seconds=bad,
            validate_memory_mb=bad,
            max_description_chars=bad,
        )
        assert config.max_spec_bytes == 5 * 1024 * 1024
        assert config.fetch_timeout_seconds == 30
        assert config.validate_timeout_seconds == 120
        assert config.validate_memory_mb == 1024
        assert config.max_description_chars == 1024


class TestShippedDefaultConfig:
    def test_shipped_config_is_noop(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
        import pathlib

        monkeypatch.chdir(tmp_path)
        for key in list(os.environ.keys()):
            if key.startswith("MCPHUB_") or key == "SERVER_HTTP_PORT":
                monkeypatch.delenv(key, raising=False)

        defaults = Settings.from_defaults()

        shipped_config_path = pathlib.Path(__file__).parent.parent.parent / "config.yaml"
        result = load_settings(path=str(shipped_config_path))

        assert result.server.http_port == defaults.server.http_port
        assert result.server.admin_ui == defaults.server.admin_ui
        assert result.storage.type == defaults.storage.type
        assert result.auth.type == defaults.auth.type
        assert result.auth.basic_auth.register_user == defaults.auth.basic_auth.register_user
        assert result.auth.basic_auth.register_pass == defaults.auth.basic_auth.register_pass
        assert result.healthcheck.enabled == defaults.healthcheck.enabled
        assert result.healthcheck.interval_seconds == defaults.healthcheck.interval_seconds
        assert result.healthcheck.timeout_seconds == defaults.healthcheck.timeout_seconds
        assert result.healthcheck.failure_threshold == defaults.healthcheck.failure_threshold
        assert result.healthcheck.auto_unregister == defaults.healthcheck.auto_unregister
        assert (
            result.healthcheck.response_fields.status == defaults.healthcheck.response_fields.status
        )
        assert (
            result.healthcheck.response_fields.uptime_seconds
            == defaults.healthcheck.response_fields.uptime_seconds
        )
        assert result.trace.body_limit == defaults.trace.body_limit
        assert result.trace.capture_sse == defaults.trace.capture_sse
        assert result.openapi.max_spec_bytes == defaults.openapi.max_spec_bytes
        assert result.openapi.fetch_timeout_seconds == defaults.openapi.fetch_timeout_seconds
        assert result.openapi.validate_timeout_seconds == defaults.openapi.validate_timeout_seconds
        assert result.openapi.validate_memory_mb == defaults.openapi.validate_memory_mb
        assert result.openapi.max_description_chars == defaults.openapi.max_description_chars
        assert result.openapi.call_timeout_seconds == defaults.openapi.call_timeout_seconds
        assert result.openapi.max_response_bytes == defaults.openapi.max_response_bytes
        assert result.openapi.max_redirects == defaults.openapi.max_redirects


class TestExposedWithoutAuthIsWarned:
    """The shipped config is local-first (no auth, private networks reachable). Copying
    it onto a host that binds a public interface is the realistic way to get burned,
    and nothing said so."""

    def _warnings(self, **overrides) -> list[str]:
        import logging
        from unittest.mock import patch

        from mcp_hub.app import warn_about_exposure
        from mcp_hub.config import AuthConfig, SecurityConfig, ServerConfig, Settings

        settings = Settings(
            server=ServerConfig(**overrides.pop("server", {})),
            auth=AuthConfig(**overrides.pop("auth", {})),
            security=SecurityConfig(**overrides.pop("security", {})),
        )
        records: list[str] = []
        with patch.object(
            logging.getLogger("mcp_hub.app"),
            "warning",
            side_effect=lambda msg, *a, **k: records.append(msg % a if a else msg),
        ):
            warn_about_exposure(settings)
        return records

    def test_loopback_without_auth_is_silent(self) -> None:
        # The default local case must not nag.
        assert self._warnings(server={"http_host": "127.0.0.1"}, auth={"type": "none"}) == []

    def test_public_bind_without_auth_warns(self) -> None:
        warnings = self._warnings(server={"http_host": "0.0.0.0"}, auth={"type": "none"})

        assert any("no authentication" in w for w in warnings)

    def test_public_bind_with_jwt_does_not_warn_about_auth(self) -> None:
        warnings = self._warnings(server={"http_host": "0.0.0.0"}, auth={"type": "jwt"})

        assert not any("no authentication" in w for w in warnings)

    def test_public_bind_with_private_networks_warns(self) -> None:
        # An exposed hub that will happily proxy to 169.254.169.254 is an SSRF pivot.
        warnings = self._warnings(
            server={"http_host": "0.0.0.0"},
            auth={"type": "jwt"},
            security={"allow_private_networks": True},
        )

        assert any("private" in w.lower() for w in warnings)

    def test_loopback_with_private_networks_is_silent(self) -> None:
        assert (
            self._warnings(
                server={"http_host": "127.0.0.1"}, security={"allow_private_networks": True}
            )
            == []
        )
