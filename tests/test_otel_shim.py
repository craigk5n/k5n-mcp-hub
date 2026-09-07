"""The span API the rest of the hub talks to (Epic 11, Story 11.2).

Two jobs. It must be a no-op when telemetry is off, so no call site ever has to ask
whether it is on — a branch repeated at twenty call sites is twenty chances to get it
wrong. And it must refuse to export the things this codebase has already learned turn
up in unexpected places: credentials inside error text, tokens in query strings.
"""

from __future__ import annotations

from typing import Any

import pytest

from mcp_hub.config import Settings
from mcp_hub.observability.otel import (
    build_provider,
    record_error,
    safe_attributes,
    sanitize_url,
)


def _enabled_provider(**over: Any) -> Any:
    """An enabled provider without needing the SDK installed.

    Built directly rather than through `build_provider`, which -- correctly -- refuses
    to return a half-working provider when the SDK is absent. These tests are about
    the subject gate, which is pure config, so requiring the optional dependency to
    exercise it would mean the default install could not run its own tests.
    """
    from mcp_hub.observability.otel import OtelProvider

    config = Settings(otel={"enabled": True, "endpoint": "http://localhost:4318", **over}).otel
    return OtelProvider(config, tracer=object())


class TestDisabledIsANoOp:
    def test_a_span_can_be_opened_and_used_when_disabled(self) -> None:
        provider = build_provider(Settings.from_defaults().otel)

        with provider.span("mcp.proxy", {"mcp.server.id": "files"}) as span:
            span.set_attribute("anything", "at all")
            span.set_status_error("boom")

    def test_it_costs_nothing_and_touches_no_sdk(self) -> None:
        from unittest.mock import patch

        provider = build_provider(Settings.from_defaults().otel)
        with patch("mcp_hub.observability.otel._import_sdk") as imported:
            with provider.span("mcp.proxy"):
                pass
        assert imported.call_count == 0


class TestUrlSanitisation:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://e.example.com/mcp?token=secret", "https://e.example.com/mcp"),
            ("https://e.example.com/mcp", "https://e.example.com/mcp"),
            ("https://user:pw@e.example.com/mcp", "https://e.example.com/mcp"),
            ("stdio:echo", "stdio:echo"),
        ],
    )
    def test_query_strings_and_userinfo_are_stripped(self, url: str, expected: str) -> None:
        # A credential in a query string is a real pattern, and an exporter is a much
        # worse place to leak one than a log an admin has to go and read.
        assert sanitize_url(url) == expected

    def test_a_password_never_survives(self) -> None:
        assert "pw" not in sanitize_url("https://user:pw@e.example.com/mcp?api_key=pw")


class TestAttributeFiltering:
    @pytest.mark.parametrize(
        "key",
        [
            "authorization",
            "Authorization",
            "bearer_token",
            "basic_password",
            "oauth_client_secret",
            "api_key",
            "cookie",
            "http.request.header.authorization",
            "credential",
        ],
    )
    def test_credential_shaped_keys_are_dropped(self, key: str) -> None:
        cleaned = safe_attributes({key: "super-secret", "mcp.server.id": "files"})
        assert key not in cleaned
        assert "super-secret" not in str(cleaned)
        assert cleaned["mcp.server.id"] == "files"

    def test_ordinary_attributes_survive(self) -> None:
        cleaned = safe_attributes(
            {"mcp.server.id": "files", "mcp.method": "tools/call", "http.status_code": 200}
        )
        assert cleaned == {
            "mcp.server.id": "files",
            "mcp.method": "tools/call",
            "http.status_code": 200,
        }

    def test_url_attributes_are_sanitised_in_passing(self) -> None:
        cleaned = safe_attributes({"url.full": "https://e.example.com/mcp?token=abc"})
        assert cleaned["url.full"] == "https://e.example.com/mcp"

    def test_none_values_are_dropped_rather_than_exported_as_none(self) -> None:
        assert safe_attributes({"a": None, "b": 1}) == {"b": 1}


class TestSubjectGate:
    def test_the_subject_is_withheld_by_default(self) -> None:
        provider = _enabled_provider()
        assert provider.subject_attributes("alice") == {}

    def test_the_subject_is_included_only_when_asked_for(self) -> None:
        provider = _enabled_provider(include_subject=True)
        assert provider.subject_attributes("alice") == {"mcp.caller.subject": "alice"}

    def test_an_anonymous_caller_adds_nothing(self) -> None:
        provider = _enabled_provider(include_subject=True)
        assert provider.subject_attributes("") == {}


class TestErrorRecording:
    def test_the_error_type_is_recorded_but_not_its_text(self) -> None:
        """IdP error descriptions echo the token that was rejected -- which is why
        sanitize_trace_body has a prose pattern for exactly that. An exporter must not
        become the place that leak reappears."""
        recorded: dict[str, Any] = {}

        class _Span:
            def set_attribute(self, key: str, value: Any) -> None:
                recorded[key] = value

            def set_status(self, *a: Any, **k: Any) -> None:
                recorded["status"] = "error"

            def record_exception(self, *a: Any, **k: Any) -> None:
                recorded["exception_recorded"] = True

        error = ValueError("subject_token 'eyJhbGciOi.secret.token' is not active")
        record_error(_Span(), error)

        blob = str(recorded)
        assert "ValueError" in blob
        assert "eyJhbGciOi" not in blob, "the error text must not reach the exporter"
        assert "exception_recorded" not in recorded, (
            "record_exception attaches the message and stack; the type is enough"
        )


class TestEnabledSpans:
    """Against the real SDK with an in-memory exporter.

    Not the OTLP exporter: pointing it at a collector that is not there produced
    retry storms in the test output and background threads outliving the test. An
    in-memory exporter also lets these assert what was actually *exported*, which is
    the only claim that matters -- filtering that happens on the way to a span nobody
    inspects proves nothing.
    """

    def _recording_provider(self, **over: Any) -> tuple[Any, Any]:
        pytest.importorskip("opentelemetry.sdk")
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        from mcp_hub.observability.otel import OtelProvider

        exporter = InMemorySpanExporter()
        sdk_provider = TracerProvider()
        sdk_provider.add_span_processor(SimpleSpanProcessor(exporter))

        config = Settings(otel={"enabled": True, "endpoint": "http://localhost:4318", **over}).otel
        return OtelProvider(config, tracer=sdk_provider.get_tracer("test")), exporter

    def test_a_span_is_exported_with_its_attributes(self) -> None:
        provider, exporter = self._recording_provider()

        with provider.span("mcp.proxy", {"mcp.server.id": "files"}) as span:
            span.set_attribute("mcp.method", "tools/list")

        spans = exporter.get_finished_spans()
        assert [s.name for s in spans] == ["mcp.proxy"]
        assert spans[0].attributes["mcp.server.id"] == "files"
        assert spans[0].attributes["mcp.method"] == "tools/list"

    def test_a_credential_attribute_never_reaches_the_exporter(self) -> None:
        provider, exporter = self._recording_provider()

        with provider.span("mcp.proxy", {"authorization": "Bearer super-secret"}) as span:
            span.set_attribute("bearer_token", "also-secret")

        exported = str(exporter.get_finished_spans()[0].attributes)
        assert "super-secret" not in exported
        assert "also-secret" not in exported

    def test_a_url_attribute_loses_its_query_string_on_the_way_out(self) -> None:
        provider, exporter = self._recording_provider()

        with provider.span("mcp.proxy", {"url.full": "https://e.example.com/mcp?token=abc"}):
            pass

        assert exporter.get_finished_spans()[0].attributes["url.full"] == (
            "https://e.example.com/mcp"
        )

    def test_an_error_records_its_type_and_not_its_text(self) -> None:
        from mcp_hub.observability.otel import record_error

        provider, exporter = self._recording_provider()

        with provider.span("mcp.proxy") as span:
            record_error(span, ValueError("subject_token 'eyJhbGciOi.secret' is not active"))

        span_data = exporter.get_finished_spans()[0]
        assert span_data.attributes["error.type"] == "ValueError"
        assert "eyJhbGciOi" not in str(span_data.attributes)
        assert span_data.status.is_ok is False
