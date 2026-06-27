import re

import pytest


def test_regex_extract_failing_tests():
    """Verify the regex used to find failing test files from pytest output."""
    logs = """
    ============================= test session starts ==============================
    collected 3 items

    tests/test_memory.py::test_add_and_query_memory FAILED
    tests/test_database.py::test_database_initialization PASSED
    tests/test_persona.py::test_detect_metacognitive_intent FAILED
    """
    failing_tests = []
    for match in re.findall(
        r"(?:FAILED|ERROR)\s+(tests/test_[a-zA-Z0-9_-]+\.py)"
        r"|(tests/test_[a-zA-Z0-9_-]+\.py)::\S+\s+(?:FAILED|ERROR)",
        logs,
    ):
        failing_tests.append(match[0] or match[1])
    failing_tests = sorted(list(set(failing_tests)))
    assert failing_tests == ["tests/test_memory.py", "tests/test_persona.py"]
