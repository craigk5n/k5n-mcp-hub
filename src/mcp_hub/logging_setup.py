import json
import logging
from datetime import datetime, timezone
from io import TextIOBase
from logging import LogRecord
from typing import Callable, Optional


def _make_log_record_factory(original_factory: Optional[Callable] = None) -> Callable:
    def factory(*args: object, **kwargs: object) -> LogRecord:
        if original_factory:
            record = original_factory(*args, **kwargs)  # type: ignore[arg-type]
        else:
            record = LogRecord(*args, **kwargs)  # type: ignore[arg-type]

        extra: dict[str, object] = kwargs.get("extra", {})  # type: ignore[assignment]
        for key, value in extra.items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return record

    return factory


def _get_log_level(level: str) -> int:
    level_upper = level.upper()
    level_value = getattr(logging, level_upper, None)
    if level_value is None or not isinstance(level_value, int):
        raise ValueError(f"Invalid log level: {level!r}")
    return level_value


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        reserved = {
            "name",
            "msg",
            "args",
            "created",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "module",
            "msecs",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "thread",
            "threadName",
            "exc_info",
            "exc_text",
            "stack_info",
            "msg",
            "level",
            "logger",
        }
        for key, value in record.__dict__.items():
            if key not in reserved and key not in log_data:
                try:
                    json.dumps(value)
                    log_data[key] = value
                except (TypeError, ValueError):
                    log_data[key] = str(value)

        return json.dumps(log_data)


def configure_logging(level: str = "INFO", stream: Optional[TextIOBase] = None) -> None:
    original_factory = logging.getLogRecordFactory()
    logging.setLogRecordFactory(_make_log_record_factory(original_factory))

    root_logger = logging.getLogger()
    root_logger.setLevel(_get_log_level(level))

    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())
    handler.setLevel(_get_log_level(level))

    root_logger.addHandler(handler)
