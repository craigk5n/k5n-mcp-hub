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


class TestStdioRequiresRealAuthentication:
    """`is_admin` returns True unconditionally when auth.type is not jwt, so
    `require_admin` on POST /v1/register is a no-op in the default configuration.
    That is tolerable when a hostile registration buys a fetch; it is not when it
    buys arbitrary code execution."""

    @pytest.mark.parametrize("auth_type", ["none", "basic", "noauth", ""])
    def test_refuses_to_start_without_jwt_auth(self, auth_type: str) -> None:
        with pytest.raises(ValueError) as exc:
            Settings(
                auth={"type": auth_type},
                stdio={"enabled": True},
            )

        message = str(exc.value)
        assert "stdio.enabled" in message, "must name the setting that caused this"
        assert "auth.type" in message, "must name the setting that fixes it"

    def test_permitted_with_jwt_auth(self) -> None:
        settings = Settings(auth={"type": "jwt"}, stdio={"enabled": True})
        assert settings.stdio.enabled is True

    def test_unauthenticated_hub_is_fine_while_stdio_is_off(self) -> None:
        # The default local-first posture must be entirely unaffected.
        settings = Settings(auth={"type": "none"}, stdio={"enabled": False})
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
