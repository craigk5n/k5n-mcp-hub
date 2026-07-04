import pytest

from devhub.agents.card import compare_card_to_expected
from devhub.models.agent import AgentCard


class TestCompareCardToExpected:
    def test_matching_subset_returns_empty_list(self) -> None:
        actual = AgentCard(
            name="test-agent",
            description="A test agent",
            version="1.0.0",
            url="https://example.com",
        )
        expected = {"name": "test-agent", "version": "1.0.0"}
        result = compare_card_to_expected(actual, expected)
        assert result == []

    def test_single_mismatch_emits_exact_drift_string(self) -> None:
        actual = AgentCard(
            name="test-agent",
            description="A test agent",
            version="1.0.0",
            url="https://example.com",
        )
        expected = {"name": "wrong-agent"}
        result = compare_card_to_expected(actual, expected)
        assert len(result) == 1
        assert result[0] == "drift: name expected='wrong-agent' actual='test-agent'"

    def test_missing_key_emits_drift_string(self) -> None:
        actual = AgentCard(
            name="test-agent",
            description="A test agent",
            version="1.0.0",
            url="https://example.com",
            capabilities={},
        )
        expected = {"capabilities": {"tools": True}}
        result = compare_card_to_expected(actual, expected)
        assert len(result) == 1
        assert result[0] == "drift: capabilities expected={'tools': True} actual={}"

    def test_multiple_mismatches_returns_multiple_drifts(self) -> None:
        actual = AgentCard(
            name="test-agent",
            description="A test agent",
            version="1.0.0",
            url="https://example.com",
        )
        expected = {"name": "wrong-agent", "version": "2.0.0"}
        result = compare_card_to_expected(actual, expected)
        assert len(result) == 2
        assert "drift: name expected='wrong-agent' actual='test-agent'" in result
        assert "drift: version expected='2.0.0' actual='1.0.0'" in result
