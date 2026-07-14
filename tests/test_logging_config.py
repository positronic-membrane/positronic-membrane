import json
import logging

import src.config as config
from src.logging_config import setup_logging, JsonFormatter


def teardown_module(module):
    # setup_logging() replaces root handlers as a side effect — restore a
    # clean root logger so later test files aren't affected by JSON mode.
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.WARNING)


def test_json_formatter_emits_expected_fields():
    record = logging.LogRecord(
        name="JanusTest", level=logging.INFO, pathname=__file__, lineno=1,
        msg="hello %s", args=("world",), exc_info=None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "JanusTest"
    assert payload["message"] == "hello world"
    assert "timestamp" in payload
    assert "exception" not in payload


def test_json_formatter_includes_extra_fields():
    record = logging.LogRecord(
        name="JanusTest", level=logging.INFO, pathname=__file__, lineno=1,
        msg="msg", args=(), exc_info=None,
    )
    record.skill_id = "check_presence"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["extra"] == {"skill_id": "check_presence"}


def test_json_formatter_includes_traceback_on_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = logging.LogRecord(
            name="JanusTest", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="failed", args=(), exc_info=sys.exc_info(),
        )
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exception"]


def test_setup_logging_text_mode(monkeypatch):
    monkeypatch.setattr(config, "LOG_FORMAT", "text")
    monkeypatch.setattr(config, "LOG_LEVEL", "INFO")
    setup_logging()
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert not isinstance(root.handlers[0].formatter, JsonFormatter)
    assert root.level == logging.INFO


def test_setup_logging_json_mode(monkeypatch):
    monkeypatch.setattr(config, "LOG_FORMAT", "json")
    monkeypatch.setattr(config, "LOG_LEVEL", "DEBUG")
    setup_logging()
    root = logging.getLogger()
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    assert root.level == logging.DEBUG


def test_setup_logging_is_idempotent(monkeypatch):
    monkeypatch.setattr(config, "LOG_FORMAT", "text")
    setup_logging()
    setup_logging()
    setup_logging()
    root = logging.getLogger()
    assert len(root.handlers) == 1


def test_log_level_filters_records(monkeypatch):
    monkeypatch.setattr(config, "LOG_LEVEL", "WARNING")
    monkeypatch.setattr(config, "LOG_FORMAT", "text")
    setup_logging()
    logger = logging.getLogger("JanusTest.filter")

    assert not logger.isEnabledFor(logging.INFO)
    assert logger.isEnabledFor(logging.WARNING)
