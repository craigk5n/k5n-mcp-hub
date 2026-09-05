"""Principal — the authenticated caller identity (Story 5.1).

The hub cannot exchange a token on a user's behalf (Epic 6) without first knowing
who the caller is. Today `Authenticator` answers only yes/no; `Principal` is the
"who".
"""

import pytest

from mcp_hub.auth import ANONYMOUS_SUBJECT, Principal


class TestPrincipal:
    def test_carries_the_fields_obo_needs(self) -> None:
        principal = Principal(
            subject="alice",
            issuer="https://idp.example.com/realms/mcp-hub",
            scopes=frozenset({"mcp:read", "mcp:invoke"}),
            token="header.payload.signature",
        )

        assert principal.subject == "alice"
        assert principal.issuer == "https://idp.example.com/realms/mcp-hub"
        assert principal.scopes == frozenset({"mcp:read", "mcp:invoke"})
        # The raw token is retained because RFC 8693 sends it as `subject_token`.
        assert principal.token == "header.payload.signature"

    def test_is_immutable(self) -> None:
        # A principal is passed down into the outbound auth path; nothing there may
        # rewrite whose identity is in play.
        principal = Principal(subject="alice")

        with pytest.raises(Exception):
            principal.subject = "bob"  # type: ignore[misc]

    def test_defaults_are_empty_not_none(self) -> None:
        principal = Principal(subject="alice")

        assert principal.issuer == ""
        assert principal.scopes == frozenset()
        assert principal.token == ""
        assert principal.is_anonymous is False

    def test_anonymous_constructor(self) -> None:
        principal = Principal.anonymous()

        assert principal.is_anonymous is True
        assert principal.subject == ANONYMOUS_SUBJECT
        assert principal.token == ""

    def test_anonymous_principal_is_not_usable_as_an_obo_subject(self) -> None:
        # ADR 0001/0004: `auth.type: none` yields an anonymous principal. It must never
        # be mistaken for a real user whose token could be exchanged.
        assert Principal.anonymous().can_act_as_obo_subject() is False

    def test_principal_with_a_token_is_usable_as_an_obo_subject(self) -> None:
        principal = Principal(subject="alice", token="header.payload.signature")

        assert principal.can_act_as_obo_subject() is True

    def test_principal_without_a_token_is_not_usable_as_an_obo_subject(self) -> None:
        # Basic auth authenticates a caller but yields no bearer token to exchange.
        principal = Principal(subject="admin")

        assert principal.can_act_as_obo_subject() is False

    def test_has_scope(self) -> None:
        principal = Principal(subject="alice", scopes=frozenset({"mcp:read"}))

        assert principal.has_scope("mcp:read") is True
        assert principal.has_scope("mcp:invoke") is False
