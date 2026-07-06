import json
import logging


class TestConfigureLogging:
    def test_idempotent_no_duplicate_handlers(self):
        from mcp_hub.logging_setup import configure_logging

        configure_logging()
        configure_logging()

        root_logger = logging.getLogger()
        handlers = root_logger.handlers
        assert len(handlers) == 1

    def test_json_output_valid_parseable(self):
        from mcp_hub.logging_setup import configure_logging
        from io import StringIO

        stream = StringIO()
        configure_logging(stream=stream)

        logging.getLogger("test").info("hello", extra={"x": 1})

        output = stream.getvalue()
        lines = [line.strip() for line in output.strip().split("\n") if line.strip()]
        assert len(lines) == 1

        log_data = json.loads(lines[0])
        assert log_data["msg"] == "hello"
        assert log_data["x"] == 1
        assert "ts" in log_data
        assert "level" in log_data
        assert "logger" in log_data

    def test_json_output_contains_standard_fields(self):
        from mcp_hub.logging_setup import configure_logging
        from io import StringIO

        stream = StringIO()
        configure_logging(stream=stream)

        logging.getLogger("mylogger").warning("test message", extra={"request_id": "abc123"})

        output = stream.getvalue()
        lines = [line.strip() for line in output.strip().split("\n") if line.strip()]
        assert len(lines) == 1

        log_data = json.loads(lines[0])
        assert log_data["msg"] == "test message"
        assert log_data["level"] == "WARNING"
        assert log_data["logger"] == "mylogger"
        assert log_data["request_id"] == "abc123"

    def test_default_level_info(self):
        from mcp_hub.logging_setup import configure_logging
        from io import StringIO

        stream = StringIO()
        configure_logging(stream=stream)

        logging.getLogger("test").debug("debug message")

        output = stream.getvalue()
        assert output.strip() == ""

    def test_custom_level(self):
        from mcp_hub.logging_setup import configure_logging
        from io import StringIO

        stream = StringIO()
        configure_logging(level="DEBUG", stream=stream)

        logging.getLogger("test").debug("debug message")

        output = stream.getvalue()
        lines = [line.strip() for line in output.strip().split("\n") if line.strip()]
        assert len(lines) == 1

        log_data = json.loads(lines[0])
        assert log_data["msg"] == "debug message"
        assert log_data["level"] == "DEBUG"
