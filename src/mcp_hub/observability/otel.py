"""OpenTelemetry wiring, kept behind a shim.

Off by default and an optional install (ADR 0009). This is the only feature that
sends data about every request to a third party, so the module's job is as much about
what it refuses to export as about what it emits.

The SDK is imported lazily through `_import_sdk`, so a default install pays nothing
for a feature it is not using, and an SDK API change has one place to break rather
than one per call site.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

from mcp_hub.config import OtelConfig

logger = logging.getLogger(__name__)

# Names the operator sees in their collector. Stable on purpose: a dashboard built
# against these should keep working.
SPAN_SERVER_ID = "mcp.server.id"
SPAN_SUBJECT = "mcp.caller.subject"
SPAN_REQUEST_ID = "mcp.request.id"


# Attribute keys that must never be exported, matched as substrings on a lowercased
# key. Deliberately broad: an exporter ships continuously to a system the hub's
# operator may not even run, so a false positive costs one missing attribute while a
# false negative costs a credential. The hub has already learned twice that secrets
# turn up where they were not expected -- inside IdP error text, and in header values
# -- which is what `sanitize_trace_body` exists for.
_FORBIDDEN_KEY_PARTS = (
    "authorization",
    "cookie",
    "token",
    "password",
    "secret",
    "credential",
    "api_key",
    "apikey",
    "passwd",
    "session",
)

# Keys whose values are URLs, and so need query strings removed rather than dropping.
_URL_KEY_PARTS = ("url", "endpoint", "uri")


def sanitize_url(url: str) -> str:
    """A URL safe to export: no query string, no userinfo.

    Both routinely carry credentials -- `?api_key=`, `https://user:pw@host` -- and a
    span attribute is a worse place to leak one than a log, because it leaves
    continuously and lands somewhere the operator may not control.
    """
    if not url:
        return ""
    parsed = urlsplit(url)
    if not parsed.netloc:
        # Opaque forms like `stdio:echo` have nothing to strip.
        return url.split("?", 1)[0]
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def safe_attributes(attributes: dict[str, Any] | None) -> dict[str, Any]:
    """Drop what must not be exported, and clean up what may be.

    Dropping rather than raising: a wrong attribute should not take down a proxied
    call. It is logged, so the mistake is findable rather than silent.
    """
    if not attributes:
        return {}

    cleaned: dict[str, Any] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        lowered = key.lower()
        if any(part in lowered for part in _FORBIDDEN_KEY_PARTS):
            logger.warning("otel: refusing to export attribute %r; it looks like a credential", key)
            continue
        if isinstance(value, str) and any(part in lowered for part in _URL_KEY_PARTS):
            cleaned[key] = sanitize_url(value)
            continue
        cleaned[key] = value
    return cleaned


def record_error(span: Any, error: BaseException) -> None:
    """Mark a span failed, recording the error's *type* and nothing else.

    Not `record_exception`, which attaches the message and stack trace. IdP error
    descriptions echo the token that was rejected -- `sanitize_trace_body` carries a
    prose pattern for precisely that -- so the text is the one part that cannot be
    allowed out. The type plus the failing operation is enough to find the request in
    the hub's own trace view, where the detail is already available to an admin.
    """
    try:
        span.set_attribute("error.type", type(error).__name__)
        span.set_status(_error_status())
    except Exception:  # noqa: BLE001 - telemetry must never break the caller
        logger.debug("otel: could not record error on span", exc_info=True)


def _error_status() -> Any:
    """The SDK's error status, or a placeholder when the SDK is absent."""
    try:
        from opentelemetry.trace import Status, StatusCode

        return Status(StatusCode.ERROR)
    except ImportError:
        return "ERROR"


class _NoOpSpan:
    """Accepts everything a real span does and keeps none of it."""

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        return None

    def set_status_error(self, message: str = "") -> None:
        return None

    def record_exception(self, *args: Any, **kwargs: Any) -> None:
        return None


class _RealSpan:
    """Thin wrapper so call sites use one API whether or not telemetry is on."""

    def __init__(self, span: Any) -> None:
        self._span = span

    def set_attribute(self, key: str, value: Any) -> None:
        for safe_key, safe_value in safe_attributes({key: value}).items():
            self._span.set_attribute(safe_key, safe_value)

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        self._span.set_status(*args, **kwargs)

    def set_status_error(self, message: str = "") -> None:
        # `message` is accepted and ignored on purpose: see record_error.
        self._span.set_status(_error_status())

    def record_exception(self, *args: Any, **kwargs: Any) -> None:
        return None


class OtelUnavailableError(RuntimeError):
    """OpenTelemetry was enabled but the SDK is not installed."""


def _import_sdk() -> Any:
    """Import the SDK. Indirected so tests can simulate its absence.

    Not imported at module scope: with `otel.enabled` false this must never run, and
    the default install does not have these packages at all.
    """
    from opentelemetry import trace as otel_trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    return otel_trace, OTLPSpanExporter, Resource, TracerProvider, BatchSpanProcessor


class OtelProvider:
    """What the rest of the hub talks to.

    A disabled provider is a working object whose spans do nothing, so call sites
    never branch on whether telemetry is on — the branch is here, once.
    """

    def __init__(self, config: OtelConfig, tracer: Any = None) -> None:
        self.config = config
        self._tracer = tracer

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled and self._tracer is not None)

    @property
    def include_subject(self) -> bool:
        return bool(self.config.include_subject)

    def subject_attributes(self, subject: str) -> dict[str, Any]:
        """The caller's identity, if the operator opted into exporting it.

        Gated here rather than at each call site so the decision lives in one place --
        and so adding a new span cannot accidentally start exporting identities.
        """
        if not subject or not self.include_subject:
            return {}
        return {SPAN_SUBJECT: subject}

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
        """Open a span, or hand back a no-op that behaves the same.

        Call sites never ask whether telemetry is on: a branch repeated at every call
        site is a chance to get it wrong at every call site. Telemetry also never
        breaks the caller -- if the SDK raises, the work continues untraced.
        """
        if not self.enabled:
            yield _NoOpSpan()
            return

        try:
            with self._tracer.start_as_current_span(name) as raw:
                wrapped = _RealSpan(raw)
                for key, value in safe_attributes(attributes).items():
                    raw.set_attribute(key, value)
                yield wrapped
        except Exception:  # noqa: BLE001 - see docstring
            logger.warning("otel: span %r failed; continuing untraced", name, exc_info=True)
            yield _NoOpSpan()


def disabled_provider() -> OtelProvider:
    """A provider that traces nothing.

    So a subsystem constructed without telemetry -- in a test, or by a caller that
    predates this feature -- still has the same object to talk to, rather than every
    call site growing a `if self._otel is not None` branch.
    """
    return OtelProvider(OtelConfig())


def build_provider(config: OtelConfig) -> OtelProvider:
    """Build a provider, or a disabled one.

    Enabling without the SDK installed raises rather than degrading to a no-op:
    telemetry that silently does nothing is worse than telemetry that is off, because
    the operator believes they have visibility they do not have.
    """
    if not config.enabled:
        return OtelProvider(config)

    try:
        otel_trace, exporter_cls, resource_cls, provider_cls, processor_cls = _import_sdk()
    except ImportError as e:
        raise OtelUnavailableError(
            "otel.enabled is true but the OpenTelemetry SDK is not installed. "
            "Install the extra: pip install 'k5n-mcp-hub[otel]'. "
            "Refusing to start rather than exporting nothing, which would look like "
            "working telemetry."
        ) from e

    resource = resource_cls.create({"service.name": config.service_name})
    provider = provider_cls(resource=resource)
    provider.add_span_processor(
        processor_cls(
            exporter_cls(
                endpoint=f"{config.endpoint.rstrip('/')}/v1/traces",
                headers=dict(config.headers),
            )
        )
    )
    otel_trace.set_tracer_provider(provider)
    logger.info("OpenTelemetry enabled, exporting to %s", config.endpoint)

    return OtelProvider(config, tracer=provider.get_tracer("mcp_hub"))
