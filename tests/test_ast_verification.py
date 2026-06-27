import pytest
from src.self_modification import stage_and_test, stage_and_test_multi


def test_stage_and_test_raises_permission_error():
    """Verify stage_and_test raises PermissionError (V3-T3: direct modification disabled)."""
    with pytest.raises(PermissionError, match="Direct source modification is disabled"):
        stage_and_test("src/utils.py", "def broken_syntax(\n")


def test_stage_and_test_multi_raises_permission_error():
    """Verify stage_and_test_multi raises PermissionError (V3-T3: direct modification disabled)."""
    with pytest.raises(PermissionError, match="Direct source modification is disabled"):
        stage_and_test_multi({"src/valid.py": "def fine(): pass\n", "src/invalid.py": "def broken(\n"})
