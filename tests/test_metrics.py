import re
import threading

from mcp_hub.metrics import metrics


def test_concurrent_increment_requests_total() -> None:
    metrics.reset()

    def worker() -> None:
        metrics.inc_requests_total()

    threads = [threading.Thread(target=worker) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert metrics.requests_total == 100


def test_render_prometheus_format() -> None:
    metrics.reset()
    metrics.inc_in_flight()
    metrics.inc_requests_total()
    metrics.inc_errors_total()
    metrics.add_duration_ms_sum(42)

    output = metrics.render_prometheus()
    pattern = r"^mcp_hub_requests_in_flight \d+\nmcp_hub_requests_total \d+\nmcp_hub_request_errors_total \d+\nmcp_hub_request_duration_ms_sum [\d.]+\n$"
    assert re.match(pattern, output) is not None


def test_render_prometheus_values() -> None:
    metrics.reset()
    metrics.inc_in_flight()
    metrics.inc_requests_total()
    metrics.inc_errors_total()
    metrics.add_duration_ms_sum(100)

    output = metrics.render_prometheus()
    expected = "mcp_hub_requests_in_flight 1\nmcp_hub_requests_total 1\nmcp_hub_request_errors_total 1\nmcp_hub_request_duration_ms_sum 100.0\n"
    assert output == expected


def test_in_flight_counter() -> None:
    metrics.reset()
    metrics.inc_in_flight()
    metrics.inc_in_flight()
    assert metrics.in_flight == 2
    metrics.dec_in_flight()
    assert metrics.in_flight == 1
