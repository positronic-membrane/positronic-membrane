import pytest

from src.self_modification import (
    apply_search_replace_blocks,
    apply_staged_change,
    apply_staged_multi,
    generate_diff,
    generate_multi_diff,
    stage_and_test,
    stage_and_test_multi,
)

_DISABLED = "Direct source modification is disabled. Use the skill staging harness or a Project Sandbox."


def test_stage_and_test_raises():
    with pytest.raises(PermissionError, match="Direct source modification"):
        stage_and_test("src/foo.py", "x = 1")


def test_stage_and_test_multi_raises():
    with pytest.raises(PermissionError, match="Direct source modification"):
        stage_and_test_multi({"src/foo.py": "x = 1"})


def test_apply_staged_change_raises():
    with pytest.raises(PermissionError, match="Direct source modification"):
        apply_staged_change("/tmp/stage", "src/foo.py")


def test_apply_staged_multi_raises():
    with pytest.raises(PermissionError, match="Direct source modification"):
        apply_staged_multi("/tmp/stage", {"src/foo.py": "x = 1"})


def test_generate_diff_raises():
    with pytest.raises(PermissionError, match="Direct source modification"):
        generate_diff("src/foo.py", "x = 1")


def test_generate_multi_diff_raises():
    with pytest.raises(PermissionError, match="Direct source modification"):
        generate_multi_diff({"src/foo.py": "x = 1"})


def test_apply_search_replace_blocks_still_works():
    original = "def add(a, b): return a + b\n"
    block = "<<<<<<< SEARCH\ndef add(a, b): return a + b\n=======\ndef add(a, b): return a + b + 1\n>>>>>>> REPLACE"
    result = apply_search_replace_blocks(original, block)
    assert result == "def add(a, b): return a + b + 1\n"


def test_apply_search_replace_blocks_missing_raises():
    with pytest.raises(ValueError, match="No blocks"):
        apply_search_replace_blocks("content", "no blocks here")
