import json
import logging

from src import config

TEXT_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

_RESERVED_RECORD_ATTRS = frozenset(logging.LogRecord(
    name="", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None,
).__dict__.keys()) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Emits one JSON object per log record: timestamp, level, logger,
    message, plus any caller-supplied `extra` fields, plus a formatted
    traceback under "exception" when exc_info is present."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        extra = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_RECORD_ATTRS
        }
        if extra:
            payload["extra"] = extra

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def setup_logging() -> None:
    """
    Configures the root logger's level and output format from
    src.config.LOG_LEVEL / LOG_FORMAT. Every other logger in this codebase
    calls logging.getLogger(name) with no handler of its own, so it
    propagates to and is governed by this root configuration.

    Reads config values at call time (not import time) so tests can
    monkeypatch src.config before calling this, and is safe to call more
    than once (e.g. once from main.py's main thread, once from
    web_server.py's run_server() on the background web thread main.py
    spawns) since it replaces rather than accumulates root handlers. The
    new handler is added before old ones are removed — never the other way
    around — so a concurrent log call from the other thread can at worst
    be emitted twice, never dropped by landing in a zero-handler window.
    """
    level_name = (config.LOG_LEVEL or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    if (config.LOG_FORMAT or "text").lower() == "json":
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(TEXT_FORMAT)

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    root.addHandler(handler)
    for existing in previous_handlers:
        root.removeHandler(existing)
    root.setLevel(level)
