import pytest
from devhub.health import HealthParser, HealthResponse
from devhub.health.parser import format_uptime


class TestFormatUptime:
    def test_format_uptime_zero(self) -> None:
        assert format_uptime(0) == "0m"

    def test_format_uptime_negative(self) -> None:
        assert format_uptime(-10) == "0m"

    def test_format_uptime_under_one_minute(self) -> None:
        assert format_uptime(30) == "< 1 min"

    def test_format_uptime_exactly_one_minute(self) -> None:
        assert format_uptime(90) == "1m"

    def test_format_uptime_one_hour_one_minute(self) -> None:
        assert format_uptime(3661) == "1h 1m"

    def test_format_uptime_one_day(self) -> None:
        assert format_uptime(86400) == "1d 0h 0m"

    def test_format_uptime_45_days(self) -> None:
        assert format_uptime(45 * 86400) == "45d"


class TestHealthResponse:
    def test_is_healthy_without_status(self) -> None:
        response = HealthResponse(raw={})
        assert response.is_healthy() is False

    def test_is_healthy_with_healthy_status(self) -> None:
        response = HealthResponse(raw={}, status="healthy", has_status=True)
        assert response.is_healthy() is True

    def test_is_healthy_with_ok_status(self) -> None:
        response = HealthResponse(raw={}, status="ok", has_status=True)
        assert response.is_healthy() is True

    def test_is_healthy_with_up_status(self) -> None:
        response = HealthResponse(raw={}, status="up", has_status=True)
        assert response.is_healthy() is True

    def test_is_healthy_with_running_status(self) -> None:
        response = HealthResponse(raw={}, status="running", has_status=True)
        assert response.is_healthy() is True

    def test_is_healthy_with_active_status(self) -> None:
        response = HealthResponse(raw={}, status="active", has_status=True)
        assert response.is_healthy() is True

    def test_is_healthy_with_pass_status(self) -> None:
        response = HealthResponse(raw={}, status="pass", has_status=True)
        assert response.is_healthy() is True

    def test_is_healthy_with_warn_status(self) -> None:
        response = HealthResponse(raw={}, status="warn", has_status=True)
        assert response.is_healthy() is True

    def test_is_healthy_with_unhealthy_status(self) -> None:
        response = HealthResponse(raw={}, status="down", has_status=True)
        assert response.is_healthy() is False

    def test_is_healthy_with_case_insensitive_healthy_status(self) -> None:
        parser = HealthParser()
        response = parser.parse(b'{"status":"PASS"}')
        assert response.status == "pass"
        assert response.is_healthy() is True


class TestHealthParser:
    def test_parse_with_status_and_uptime(self) -> None:
        parser = HealthParser()
        response = parser.parse(b'{"status":"PASS","uptime_seconds":42.5}')
        assert response.status == "pass"
        assert response.uptime_secs == 42.5
        assert response.has_status is True
        assert response.has_uptime is True
        assert response.is_healthy() is True

    def test_parse_with_unhealthy_status(self) -> None:
        parser = HealthParser()
        response = parser.parse(b'{"status":"down"}')
        assert response.status == "down"
        assert response.has_status is True
        assert response.is_healthy() is False

    def test_parse_with_custom_fields(self) -> None:
        parser = HealthParser(status_field="state", uptime_field="up")
        response = parser.parse(b'{"state":"ok","up":10}')
        assert response.status == "ok"
        assert response.uptime_secs == 10.0
        assert response.has_status is True
        assert response.has_uptime is True
        assert response.is_healthy() is True

    def test_parse_invalid_json_raises_valueerror(self) -> None:
        parser = HealthParser()
        with pytest.raises(ValueError) as exc_info:
            parser.parse(b"not valid json")
        assert "Invalid JSON" in str(exc_info.value)

    def test_parse_missing_status(self) -> None:
        parser = HealthParser()
        response = parser.parse(b'{"uptime_seconds":100}')
        assert response.status == ""
        assert response.has_status is False
        assert response.uptime_secs == 100.0
        assert response.has_uptime is True
        assert response.is_healthy() is False

    def test_parse_missing_uptime(self) -> None:
        parser = HealthParser()
        response = parser.parse(b'{"status":"healthy"}')
        assert response.status == "healthy"
        assert response.has_status is True
        assert response.uptime_secs == 0.0
        assert response.has_uptime is False
        assert response.is_healthy() is True

    def test_parse_status_not_string(self) -> None:
        parser = HealthParser()
        response = parser.parse(b'{"status":123}')
        assert response.status == ""
        assert response.has_status is False

    def test_parse_uptime_not_parseable(self) -> None:
        parser = HealthParser()
        response = parser.parse(b'{"uptime_seconds":"not_a_number"}')
        assert response.uptime_secs == 0.0
        assert response.has_uptime is False

    def test_parse_string_body(self) -> None:
        parser = HealthParser()
        response = parser.parse('{"status":"ok"}')
        assert response.status == "ok"
        assert response.has_status is True
        assert response.is_healthy() is True

    def test_parse_stores_raw_dict(self) -> None:
        parser = HealthParser()
        response = parser.parse(b'{"status":"ok","extra":"data"}')
        assert response.raw == {"status": "ok", "extra": "data"}

    def test_parse_empty_json_object(self) -> None:
        parser = HealthParser()
        response = parser.parse(b"{}")
        assert response.raw == {}
        assert response.status == ""
        assert response.has_status is False
        assert response.uptime_secs == 0.0
        assert response.has_uptime is False

    def test_parse_json_array_raises_valueerror(self) -> None:
        parser = HealthParser()
        with pytest.raises(ValueError):
            parser.parse(b"[1,2,3]")

    def test_parse_integer_root_raises_valueerror(self) -> None:
        parser = HealthParser()
        with pytest.raises(ValueError):
            parser.parse(b"123")
