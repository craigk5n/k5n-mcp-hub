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
from typing import Any

from mcp_hub.config import OtelConfig

logger = logging.getLogger(__name__)

# Names the operator sees in their collector. Stable on purpose: a dashboard built
# against these should keep working.
SPAN_SERVER_ID = "mcp.server.id"
SPAN_SUBJECT = "mcp.caller.subject"
SPAN_REQUEST_ID = "mcp.request.id"


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
