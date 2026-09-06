"""`k5n-mcp-hub --dev` (local development mode).

Without it, a fresh `pip install` cannot register a localhost MCP server:
`allow_private_networks` defaults to False and only the repo's own config.yaml turns
it on, so anyone installing the CLI and pointing it at a local server gets
"URL validation failed" with nothing explaining why.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from mcp_hub.__main__ import (
    apply_dev_mode,
    export_cli_overrides,
    parse_args,
    resolve_settings,
)
from mcp_hub.config import AuthConfig, JWTAuthConfig, Settings


class TestFlagParsing:
    def test_dev_defaults_to_off(self) -> None:
        assert parse_args([]).dev is False

    def test_dev_can_be_enabled(self) -> None:
        assert parse_args(["--dev"]).dev is True

    def test_dev_combines_with_other_flags(self) -> None:
        parsed = parse_args(["--dev", "--port", "9000"])

        assert parsed.dev is True
        assert parsed.port == 9000


class TestEffect:
    def test_dev_mode_permits_localhost_and_lan_backends(self) -> None:
        settings = Settings.from_defaults()
        assert settings.security.allow_private_networks is False

        with patch.dict(os.environ, {}, clear=False):
            apply_dev_mode(settings)

        assert settings.security.allow_private_networks is True

    def test_dev_mode_reaches_the_app_through_the_environment(self) -> None:
        # uvicorn loads create_app as a factory, which re-reads settings itself, so
        # the flag has to travel via the documented env override or it would apply
        # only to the host/port the CLI passes to uvicorn.
        with patch.dict(os.environ, {}, clear=False):
            apply_dev_mode(Settings.from_defaults())

            assert os.environ["MCPHUB_SECURITY__ALLOW_PRIVATE_NETWORKS"] == "true"

    def test_dev_mode_does_not_disable_configured_authentication(self) -> None:
        # Relaxing the SSRF guard is the papercut being fixed. Silently switching off
        # authentication someone deliberately configured would be a different, much
        # worse thing to do.
        settings = Settings(
            auth=AuthConfig(
                type="jwt",
                jwt=JWTAuthConfig(
                    issuer="https://idp.example.com",
                    audience="k5n-mcp-hub",
                    jwks_uri="https://idp.example.com/certs",
                ),
            )
        )

        with patch.dict(os.environ, {}, clear=False):
            apply_dev_mode(settings)

        assert settings.auth.type == "jwt"

    def test_dev_mode_warns_on_stderr(self, capsys: pytest.CaptureFixture) -> None:
        with patch.dict(os.environ, {}, clear=False):
            apply_dev_mode(Settings.from_defaults())

        message = capsys.readouterr().err.lower()
        assert "dev mode" in message
        assert "not for" in message or "do not" in message


class TestWithoutTheFlag:
    def test_resolve_settings_has_no_side_effects(self, tmp_path) -> None:
        # It leaked into a neighbouring test when it did.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCPHUB_SERVER__HTTP_PORT", None)
            resolve_settings(str(tmp_path / "none.yaml"), "0.0.0.0", 9000)

            assert "MCPHUB_SERVER__HTTP_PORT" not in os.environ

    def test_settings_are_untouched(self, tmp_path) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCPHUB_SECURITY__ALLOW_PRIVATE_NETWORKS", None)
            settings = resolve_settings(str(tmp_path / "none.yaml"), None, None)

        assert settings.security.allow_private_networks is False

    def test_an_explicit_config_value_is_preserved(self, tmp_path) -> None:
        config = tmp_path / "config.yaml"
        config.write_text("security:\n  allow_private_networks: true\n")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCPHUB_SECURITY__ALLOW_PRIVATE_NETWORKS", None)
            settings = resolve_settings(str(config), None, None)

        assert settings.security.allow_private_networks is True


class TestCLIOverridesReachTheApp:
    """uvicorn loads create_app as a factory that re-reads settings, so mutating the
    Settings object only told uvicorn where to listen. The app itself still believed
    config.yaml — which silenced the exposure warning and made generated download
    scripts advertise the wrong host and port."""

    def test_host_override_is_exported(self, tmp_path) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCPHUB_SERVER__HTTP_HOST", None)
            export_cli_overrides("0.0.0.0", None)

            assert os.environ["MCPHUB_SERVER__HTTP_HOST"] == "0.0.0.0"

    def test_port_override_is_exported(self, tmp_path) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCPHUB_SERVER__HTTP_PORT", None)
            export_cli_overrides(None, 9000)

            assert os.environ["MCPHUB_SERVER__HTTP_PORT"] == "9000"

    def test_the_app_then_sees_the_overridden_host(self, tmp_path) -> None:
        from mcp_hub.config import load_settings

        with patch.dict(os.environ, {}, clear=False):
            export_cli_overrides("0.0.0.0", None)
            # What create_app does: re-read from scratch.
            assert load_settings(str(tmp_path / "none.yaml")).server.http_host == "0.0.0.0"

    def test_no_override_exports_nothing(self, tmp_path) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCPHUB_SERVER__HTTP_HOST", None)
            export_cli_overrides(None, None)

            assert "MCPHUB_SERVER__HTTP_HOST" not in os.environ
