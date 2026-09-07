"""Mapping a registry record onto a hub registration (Epic 10, Story 10.2).

Three things here are easy to get quietly wrong: the id (the hub's download route is
stricter about ids than registration is), the transport (this codebase's "sse" means
streamable HTTP, which is not what the registry means by "sse"), and secrets (a
record says a credential is *needed*, never what it is).
"""

from __future__ import annotations

import pytest

from mcp_hub.mcp.registry_client import RegistryRecord
from mcp_hub.mcp.registry_import import (
    derive_server_id,
    remote_options,
    required_credentials,
    stdio_suggestion,
    to_register_payload,
)


def _remote_record(**over: object) -> RegistryRecord:
    base = dict(
        name="ai.smithery/Hint-Services-obsidian-github-mcp",
        version="0.4.0",
        description="Connect AI assistants to a vault.",
        remotes=[
            {
                "type": "streamable-http",
                "url": "https://server.smithery.ai/@Hint-Services/x/mcp",
                "headers": [
                    {
                        "name": "Authorization",
                        "value": "Bearer {smithery_api_key}",
                        "isRequired": True,
                        "isSecret": True,
                        "description": "Bearer token for Smithery authentication",
                    }
                ],
            }
        ],
    )
    base.update(over)
    return RegistryRecord(**base)  # type: ignore[arg-type]


class TestServerId:
    @pytest.mark.parametrize(
        "registry_name,expected",
        [
            ("ai.smithery/foo", "ai.smithery.foo"),
            ("com.example/My_Server", "com.example.My_Server"),
            ("io.github.owner/repo-name", "io.github.owner.repo-name"),
        ],
    )
    def test_slash_becomes_a_dot(self, registry_name: str, expected: str) -> None:
        assert derive_server_id(registry_name) == expected

    def test_result_satisfies_the_download_route(self) -> None:
        """`ui_downloads.validate_ids` requires ^[A-Za-z0-9_.-]+$ -- stricter than
        registration, which only requires non-empty. An id carried over verbatim would
        register fine and then 400 on every script download."""
        import re

        for name in (
            "ai.smithery/a b c",
            "com.example/weird!id",
            "io.github.owner/repo@1",
            "x/../../etc/passwd",
        ):
            assert re.match(r"^[A-Za-z0-9_.-]+$", derive_server_id(name)), name

    def test_it_is_stable(self) -> None:
        assert derive_server_id("ai.smithery/foo") == derive_server_id("ai.smithery/foo")

    def test_empty_name_is_refused(self) -> None:
        with pytest.raises(ValueError):
            derive_server_id("   ")


class TestTransport:
    def test_transport_is_left_for_discovery(self) -> None:
        """The registry's "sse" means legacy SSE; this codebase's "sse" means
        *streamable HTTP* (see sdk_client and _health_badge.html). There is no honest
        mapping for the legacy value, and discovery determines the real transport from
        the handshake anyway -- so import must not write a guess that discovery would
        only have to correct."""
        payload = to_register_payload(_remote_record())
        assert payload.get("mcp_transport", "") == ""


class TestPayload:
    def test_carries_what_a_registration_needs(self) -> None:
        payload = to_register_payload(_remote_record())
        assert payload["id"] == "ai.smithery.Hint-Services-obsidian-github-mcp"
        assert payload["url"] == "https://server.smithery.ai/@Hint-Services/x/mcp"
        assert "vault" in payload["description"]
        assert payload["registration_type"] == "manual"

    def test_no_credential_is_ever_carried_across(self) -> None:
        # The record's header value is a placeholder like "Bearer {smithery_api_key}".
        # Storing it would put a fake credential on the server and make it look
        # configured when it is not.
        payload = to_register_payload(_remote_record())
        serialised = repr(payload)
        assert "smithery_api_key" not in serialised
        assert payload.get("bearer_token", "") == ""
        assert payload.get("auth_type", "") == ""

    def test_a_second_remote_can_be_chosen(self) -> None:
        record = _remote_record(
            remotes=[
                {"type": "streamable-http", "url": "https://one.example.com/mcp"},
                {"type": "streamable-http", "url": "https://two.example.com/mcp"},
            ]
        )
        assert to_register_payload(record, remote_index=1)["url"] == "https://two.example.com/mcp"

    def test_a_record_with_no_remote_cannot_be_registered(self) -> None:
        record = RegistryRecord(name="com.example/pkg", packages=[{"registryType": "npm"}])
        with pytest.raises(ValueError):
            to_register_payload(record)


class TestChoiceAndCredentials:
    def test_multiple_remotes_are_offered_rather_than_guessed(self) -> None:
        record = _remote_record(
            remotes=[
                {"type": "streamable-http", "url": "https://one.example.com/mcp"},
                {"type": "sse", "url": "https://two.example.com/mcp"},
            ]
        )
        options = remote_options(record)
        assert [o.url for o in options] == [
            "https://one.example.com/mcp",
            "https://two.example.com/mcp",
        ]
        assert options[1].transport == "sse"

    def test_required_secret_headers_are_reported_for_prompting(self) -> None:
        creds = required_credentials(_remote_record().remotes[0])
        assert len(creds) == 1
        assert creds[0].name == "Authorization"
        assert creds[0].is_secret is True
        assert creds[0].is_required is True
        assert "Smithery" in creds[0].description
        # The placeholder must not travel as if it were a value.
        assert not getattr(creds[0], "value", "")


class TestStdioRecords:
    def test_a_package_record_is_a_suggestion_not_a_registration(self) -> None:
        """ADR 0007: commands come from the operator's allowlist, never from a request
        -- and a registry record is exactly the untrusted input that rule refuses."""
        record = RegistryRecord(
            name="com.example/pretrip",
            version="1.0.1",
            packages=[
                {
                    "registryType": "npm",
                    "identifier": "pretrip-mcp",
                    "version": "1.0.1",
                    "transport": {"type": "stdio"},
                }
            ],
        )
        suggestion = stdio_suggestion(record)
        assert suggestion is not None
        assert "pretrip-mcp" in suggestion.command_hint
        assert "npx" in suggestion.command_hint
        assert "allowed_commands" in suggestion.instruction

    def test_a_remote_record_has_no_stdio_suggestion(self) -> None:
        assert stdio_suggestion(_remote_record()) is None
