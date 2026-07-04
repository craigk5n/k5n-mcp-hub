import os
import pathlib

import pytest
import yaml

from devhub.config import (
    HealthCheckConfig,
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
        assert "DEVHUB_" in content, ".env.example must document DEVHUB_ nested-prefix pattern"


class TestDefaults:
    def test_defaults_no_config_no_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ):
        monkeypatch.chdir(tmp_path)
        for key in list(os.environ.keys()):
            if key.startswith("DEVHUB_") or key == "SERVER_HTTP_PORT":
                monkeypatch.delenv(key, raising=False)

        result = load_settings()

        assert result.server.http_port == 8080
        assert result.storage.type == "inmemory"
        assert result.auth.type == "basic"
        assert result.auth.basic_auth.register_user == "admin"
        assert result.auth.basic_auth.register_pass == "admin123"


class TestYAMLLoading:
    def test_yaml_overrides_defaults(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
        for key in list(os.environ.keys()):
            if key.startswith("DEVHUB_") or key == "SERVER_HTTP_PORT":
                monkeypatch.delenv(key, raising=False)

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"server": {"http_port": 9000}}))

        result = load_settings(path=str(config_file))

        assert result.server.http_port == 9000


class TestEnvVarOverrides:
    def test_devhub_env_overrides_yaml(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"server": {"http_port": 9000}}))

        monkeypatch.setenv("DEVHUB_SERVER__HTTP_PORT", "9100")

        result = load_settings(path=str(config_file))

        assert result.server.http_port == 9100

    def test_bare_env_overrides_everything(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"server": {"http_port": 9000}}))

        monkeypatch.setenv("DEVHUB_SERVER__HTTP_PORT", "9100")
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
            if key.startswith("DEVHUB_") or key == "SERVER_HTTP_PORT":
                monkeypatch.delenv(key, raising=False)

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"storage": {"type": "json"}}))

        result = load_settings(path=str(config_file))

        assert result.storage.type == "json"

    def test_jsonfile_alias(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
        for key in list(os.environ.keys()):
            if key.startswith("DEVHUB_") or key == "SERVER_HTTP_PORT":
                monkeypatch.delenv(key, raising=False)

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"storage": {"type": "jsonfile"}}))

        result = load_settings(path=str(config_file))

        assert result.storage.type == "json"

    def test_file_alias(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
        for key in list(os.environ.keys()):
            if key.startswith("DEVHUB_") or key == "SERVER_HTTP_PORT":
                monkeypatch.delenv(key, raising=False)

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"storage": {"type": "file"}}))

        result = load_settings(path=str(config_file))

        assert result.storage.type == "json"


class TestRedisAccepted:
    def test_redis_type_accepted(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
        for key in list(os.environ.keys()):
            if key.startswith("DEVHUB_") or key == "SERVER_HTTP_PORT":
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

    def test_valid_values_preserved(self):
        config = HealthCheckConfig(interval_seconds=60, timeout_seconds=10, failure_threshold=5)
        assert config.interval_seconds == 60
        assert config.timeout_seconds == 10
        assert config.failure_threshold == 5


class TestShippedDefaultConfig:
    def test_shipped_config_is_noop(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
        import pathlib

        monkeypatch.chdir(tmp_path)
        for key in list(os.environ.keys()):
            if key.startswith("DEVHUB_") or key == "SERVER_HTTP_PORT":
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
