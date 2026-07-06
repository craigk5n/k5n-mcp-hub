import threading
from dataclasses import dataclass, field


@dataclass
class Metrics:
    in_flight: int = 0
    requests_total: int = 0
    errors_total: int = 0
    duration_ms_sum: float = 0.0

    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)

    def inc_in_flight(self) -> None:
        with self._lock:
            self.in_flight += 1

    def dec_in_flight(self) -> None:
        with self._lock:
            self.in_flight -= 1

    def inc_requests_total(self) -> None:
        with self._lock:
            self.requests_total += 1

    def inc_errors_total(self) -> None:
        with self._lock:
            self.errors_total += 1

    def add_duration_ms_sum(self, value: float) -> None:
        with self._lock:
            self.duration_ms_sum += value

    def render_prometheus(self) -> str:
        with self._lock:
            return (
                f"mcp_hub_requests_in_flight {self.in_flight}\n"
                f"mcp_hub_requests_total {self.requests_total}\n"
                f"mcp_hub_request_errors_total {self.errors_total}\n"
                f"mcp_hub_request_duration_ms_sum {self.duration_ms_sum}\n"
            )

    def reset(self) -> None:
        with self._lock:
            self.in_flight = 0
            self.requests_total = 0
            self.errors_total = 0
            self.duration_ms_sum = 0.0


metrics = Metrics()
