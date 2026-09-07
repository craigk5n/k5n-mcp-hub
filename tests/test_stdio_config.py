"""Configuration gate for stdio servers (ADR 0007, Story 9.2).

Registering a stdio server means running a program, so the settings that enable it
carry more weight than the rest of the config surface. These tests pin the three
properties that make the feature safe to ship: off unless asked for, impossible to
enable while registration is unauthenticated, and never turned on as a side effect.
"""

from __future__ import annotations

import pytest

from mcp_hub.config import Settings


class TestStdioDefaults:
    def test_stdio_is_off_by_default(self) -> None:
        # An existing deployment must not acquire an exec primitive by upgrading.
        assert Settings.from_defaults().stdio.enabled is False

    def test_no_commands_are_allowed_by_default(self) -> None:
        assert Settings.from_defaults().stdio.allowed_commands == {}


class TestStdioGate:
    """stdio.enabled requires one of three things (ADR 0007).

    `is_admin` returns True unconditionally when auth.type is not jwt, so
    `require_admin` on POST /v1/register does not actually restrict anything in the
    default configuration. Tolerable when a hostile registration buys an SSRF-pinned
    fetch; not when it buys fork+exec. Any one of these closes that:

      - auth.type: jwt        -- registration genuinely requires an admin scope
      - a loopback bind       -- nothing off-box can reach the endpoint at all
      - trusted_network: true -- the operator states the claim explicitly, which is
                                 what a container published to host loopback needs,
                                 since the hub binds 0.0.0.0 inside it and cannot see
                                 how the port was published
    """

    @pytest.mark.parametrize("auth_type", ["none", "basic", "noauth", ""])
    def test_exposed_and_unauthenticated_is_refused(self, auth_type: str) -> None:
        with pytest.raises(ValueError) as exc:
            Settings(
                server={"http_host": "0.0.0.0"},
                auth={"type": auth_type},
                stdio={"enabled": True},
            )
        message = str(exc.value)
        assert "stdio.enabled" in message, "must name the setting that caused this"
        assert "auth.type" in message and "trusted_network" in message, (
            "must name every way out, not just one"
        )

    def test_jwt_auth_permits_any_bind(self) -> None:
        settings = Settings(
            server={"http_host": "0.0.0.0"}, auth={"type": "jwt"}, stdio={"enabled": True}
        )
        assert settings.stdio.enabled is True

    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
    def test_loopback_bind_permits_any_auth(self, host: str) -> None:
        # Nothing off-box can reach it, so there is no one to escalate against.
        settings = Settings(
            server={"http_host": host}, auth={"type": "none"}, stdio={"enabled": True}
        )
        assert settings.stdio.enabled is True

    def test_trusted_network_is_an_explicit_opt_out(self) -> None:
        # The container case: 0.0.0.0 inside, published to host loopback outside.
        settings = Settings(
            server={"http_host": "0.0.0.0"},
            auth={"type": "none"},
            stdio={"enabled": True, "trusted_network": True},
        )
        assert settings.stdio.enabled is True

    def test_trusted_network_defaults_to_false(self) -> None:
        assert Settings.from_defaults().stdio.trusted_network is False

    def test_unauthenticated_exposed_hub_is_fine_while_stdio_is_off(self) -> None:
        settings = Settings(
            server={"http_host": "0.0.0.0"}, auth={"type": "none"}, stdio={"enabled": False}
        )
        assert settings.stdio.enabled is False


class TestAllowedCommands:
    def test_an_entry_carries_its_command_and_fixed_arguments(self) -> None:
        settings = Settings(
            auth={"type": "jwt"},
            stdio={
                "enabled": True,
                "allowed_commands": {
                    "everything": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-everything"],
                        "env": {"LOG_LEVEL": "info"},
                    }
                },
            },
        )
        entry = settings.stdio.allowed_commands["everything"]
        assert entry.command == "npx"
        assert entry.args == ["-y", "@modelcontextprotocol/server-everything"]
        assert entry.env == {"LOG_LEVEL": "info"}
        assert entry.cwd == ""

    def test_an_entry_needs_a_command(self) -> None:
        with pytest.raises(ValueError):
            Settings(
                auth={"type": "jwt"},
                stdio={"enabled": True, "allowed_commands": {"broken": {"command": "  "}}},
            )


class TestDevModeDoesNotEnableStdio:
    def test_dev_mode_leaves_stdio_alone(self) -> None:
        # `--dev` relaxes the SSRF guard and deliberately nothing else. Enabling an
        # exec primitive as a side effect of a convenience flag would be indefensible.
        from mcp_hub.__main__ import apply_dev_mode

        settings = Settings.from_defaults()
        apply_dev_mode(settings)

        assert settings.stdio.enabled is False
        assert settings.security.allow_private_networks is True
