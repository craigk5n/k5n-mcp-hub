"""OpenTelemetry configuration (Epic 11, Story 11.1).

This is the one feature that sends data about every request to a third party, so the
defaults matter more than the plumbing: off unless asked for, loud when it cannot do
what it was asked to do, and silent about user identities unless the operator says
otherwise in as many words.
"""

from __future__ import annotations

import pytest

from mcp_hub.config import Settings


class TestDefaults:
    def test_it_is_off(self) -> None:
        assert Settings.from_defaults().otel.enabled is False

    def test_subjects_are_not_exported_by_default(self) -> None:
        # A user identity leaving for someone else's collector is a policy decision,
        # not a default.
        assert Settings.from_defaults().otel.include_subject is False

    def test_the_service_name_has_a_sensible_default(self) -> None:
        assert Settings.from_defaults().otel.service_name == "k5n-mcp-hub"

    def test_config_yaml_ships_it_disabled(self) -> None:
        from mcp_hub.config import load_settings

        assert load_settings("config.yaml").otel.enabled is False
        assert load_settings("config.production.example.yaml").otel.enabled is False


class TestEnabling:
    def test_an_endpoint_is_required_when_enabled(self) -> None:
        # Enabled with nowhere to send is the same silent nothing the startup check
        # below exists to prevent.
        with pytest.raises(ValueError) as exc:
            Settings(otel={"enabled": True, "endpoint": ""})
        assert "otel.endpoint" in str(exc.value)

    def test_a_valid_configuration_is_accepted(self) -> None:
        settings = Settings(
            otel={"enabled": True, "endpoint": "http://localhost:4318", "service_name": "hub"}
        )
        assert settings.otel.enabled is True
        assert settings.otel.endpoint == "http://localhost:4318"

    def test_the_endpoint_must_be_an_absolute_http_url(self) -> None:
        with pytest.raises(ValueError):
            Settings(otel={"enabled": True, "endpoint": "localhost:4318"})

    def test_headers_carry_collector_credentials(self) -> None:
        settings = Settings(
            otel={
                "enabled": True,
                "endpoint": "http://localhost:4318",
                "headers": {"authorization": "Bearer x"},
            }
        )
        assert settings.otel.headers == {"authorization": "Bearer x"}


class TestMissingDependency:
    def test_enabling_without_the_sdk_fails_loudly(self) -> None:
        """A telemetry feature that silently does nothing is worse than one that is
        off: the operator believes they have visibility they do not have."""
        from unittest.mock import patch

        from mcp_hub.observability.otel import OtelUnavailableError, build_provider

        settings = Settings(otel={"enabled": True, "endpoint": "http://localhost:4318"})

        with patch("mcp_hub.observability.otel._import_sdk", side_effect=ImportError("no sdk")):
            with pytest.raises(OtelUnavailableError) as exc:
                build_provider(settings.otel)

        message = str(exc.value)
        assert "otel" in message and "install" in message.lower(), (
            "the error must name the extra to install"
        )

    def test_disabled_never_touches_the_sdk(self) -> None:
        # The default install must not pay for a feature it is not using.
        from unittest.mock import patch

        from mcp_hub.observability.otel import build_provider

        with patch("mcp_hub.observability.otel._import_sdk") as imported:
            provider = build_provider(Settings.from_defaults().otel)

        assert imported.call_count == 0
        assert provider is not None
        assert provider.enabled is False


class TestPackaging:
    def test_the_extra_is_declared(self) -> None:
        import tomllib
        from pathlib import Path

        pyproject = tomllib.loads(Path("pyproject.toml").read_text())
        extras = pyproject["project"]["optional-dependencies"]
        assert "otel" in extras, "an `otel` install extra must exist"
        joined = " ".join(extras["otel"])
        assert "opentelemetry-sdk" in joined
        assert "opentelemetry-exporter-otlp" in joined

    def test_the_sdk_is_not_a_base_dependency(self) -> None:
        # The common case is one developer with no collector; they should not pay for
        # this dependency tree, and CI's clean-install gate should stay honest.
        import tomllib
        from pathlib import Path

        pyproject = tomllib.loads(Path("pyproject.toml").read_text())
        assert not [d for d in pyproject["project"]["dependencies"] if "opentelemetry" in d]

    def test_the_dev_extra_exists_and_covers_optional_features(self) -> None:
        """CLAUDE.md has documented `pip install -e .[dev]` for a while, but no such
        extra existed -- pip warned and installed less than the command claimed."""
        import tomllib
        from pathlib import Path

        pyproject = tomllib.loads(Path("pyproject.toml").read_text())
        dev = " ".join(pyproject["project"]["optional-dependencies"]["dev"])
        assert "opentelemetry" in dev
