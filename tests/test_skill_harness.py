import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from src.skill_harness import (
    MOCK_SDK_SOURCE,
    SkillASTAuditor,
    _check_entry_point_defined,
    audit_skill_ast,
    stage_skill,
    sync_from_registry,
)
from src.database import get_connection


# ---------------------------------------------------------------------------
# audit_skill_ast
# ---------------------------------------------------------------------------

def test_audit_passes_clean_skill():
    code = textwrap.dedent("""\
        def run(sdk, args):
            return {"ok": True}
    """)
    ok, err = audit_skill_ast(code)
    assert ok is True
    assert err is None


def test_audit_blocks_subprocess_import():
    code = textwrap.dedent("""\
        import subprocess
        def run(sdk, args):
            subprocess.run(["ls"])
    """)
    ok, err = audit_skill_ast(code)
    assert ok is False
    assert "subprocess" in err


def test_audit_blocks_from_subprocess_import():
    code = textwrap.dedent("""\
        from subprocess import run as sp_run
        def run(sdk, args):
            sp_run(["ls"])
    """)
    ok, err = audit_skill_ast(code)
    assert ok is False
    assert "subprocess" in err


def test_audit_blocks_eval_call():
    code = textwrap.dedent("""\
        def run(sdk, args):
            return eval(args["expr"])
    """)
    ok, err = audit_skill_ast(code)
    assert ok is False
    assert "eval" in err


def test_audit_blocks_exec_call():
    code = textwrap.dedent("""\
        def run(sdk, args):
            exec("import os")
    """)
    ok, err = audit_skill_ast(code)
    assert ok is False
    assert "exec" in err


def test_audit_syntax_error():
    code = "def run(sdk args):\n    return {}"  # missing comma
    ok, err = audit_skill_ast(code)
    assert ok is False
    assert "SyntaxError" in err


def test_audit_allows_os_import():
    # os is intentionally permitted (existing skills use it)
    code = textwrap.dedent("""\
        import os
        def run(sdk, args):
            return {"cwd": os.getcwd()}
    """)
    ok, err = audit_skill_ast(code)
    assert ok is True


# ---------------------------------------------------------------------------
# _check_entry_point_defined
# ---------------------------------------------------------------------------

def test_check_entry_point_found():
    code = "def run(sdk, args):\n    return {}"
    assert _check_entry_point_defined(code, "run") is True


def test_check_entry_point_missing():
    code = "def helper():\n    pass"
    assert _check_entry_point_defined(code, "run") is False


def test_check_entry_point_syntax_error():
    assert _check_entry_point_defined("def run(sdk args):", "run") is False


# ---------------------------------------------------------------------------
# stage_skill — the spec's key verification test
# ---------------------------------------------------------------------------

DESTRUCTIVE_WRITE_SKILL = textwrap.dedent("""\
    def run(sdk, args):
        sdk["db"].execute("DELETE FROM episodic_memory WHERE 1=1")
        return {"deleted": True}
""")

DESTRUCTIVE_WRITE_TEST = textwrap.dedent("""\
    from mock_sdk import make_mock_sdk
    from skill import run

    def test_destructive_write_absorbed_by_mock():
        sdk = make_mock_sdk()
        result = run(sdk, {})
        assert result["deleted"] is True
        # MockSafeDB recorded the call without touching any real DB
        assert any("DELETE" in str(call) for call in sdk["db"].calls)
""")

def test_stage_skill_destructive_write_happy_path(tmp_path):
    """Spec verification test: destructive-write skill passes with MockSafeDB,
    gets upserted into agent_skills, and staging dir is cleaned up."""
    with patch("src.skill_harness.config.get_effective_workspace_root", return_value=tmp_path):
        ok, msg = stage_skill(
            skill_id="test_destructive_write",
            name="Test Destructive Write",
            description="Skill that issues a DELETE via sdk['db']",
            parameters_schema="{}",
            code_blob=DESTRUCTIVE_WRITE_SKILL,
            entry_point_function="run",
            test_blob=DESTRUCTIVE_WRITE_TEST,
        )

    assert ok is True, f"Expected success but got: {msg}"

    # Skill must be in agent_skills
    with get_connection() as conn:
        cursor = conn.execute(
            "SELECT skill_id FROM agent_skills WHERE skill_id = ?",
            ("test_destructive_write",),
        )
        row = cursor.fetchone()
    assert row is not None, "Skill was not upserted into agent_skills"

    # Staging dir must have been cleaned up
    staging_dir = tmp_path / ".janus_sandboxes" / "temp_skills" / "test_destructive_write"
    assert not staging_dir.exists(), "Staging dir was not cleaned up"


def test_stage_skill_ast_blocked(tmp_path):
    """Skill with banned import is rejected before any pytest run."""
    code = textwrap.dedent("""\
        import subprocess
        def run(sdk, args):
            return subprocess.check_output(["ls"])
    """)
    with patch("src.skill_harness.config.get_effective_workspace_root", return_value=tmp_path):
        ok, msg = stage_skill(
            skill_id="test_banned_import",
            name="Banned",
            description="",
            parameters_schema="{}",
            code_blob=code,
            entry_point_function="run",
        )

    assert ok is False
    assert "subprocess" in msg

    # Must NOT be in agent_skills
    with get_connection() as conn:
        row = conn.execute(
            "SELECT skill_id FROM agent_skills WHERE skill_id = ?",
            ("test_banned_import",),
        ).fetchone()
    assert row is None


def test_stage_skill_missing_entry_point(tmp_path):
    code = "def helper():\n    return {}"
    with patch("src.skill_harness.config.get_effective_workspace_root", return_value=tmp_path):
        ok, msg = stage_skill(
            skill_id="test_no_entry",
            name="No Entry",
            description="",
            parameters_schema="{}",
            code_blob=code,
            entry_point_function="run",
        )
    assert ok is False
    assert "run" in msg


def test_stage_skill_test_failure_blocks_upsert(tmp_path):
    """A skill whose tests fail must not be upserted."""
    code = "def run(sdk, args):\n    return {}"
    failing_test = textwrap.dedent("""\
        from skill import run
        from mock_sdk import make_mock_sdk

        def test_always_fails():
            assert False, "intentional failure"
    """)
    with patch("src.skill_harness.config.get_effective_workspace_root", return_value=tmp_path):
        ok, msg = stage_skill(
            skill_id="test_failing_skill",
            name="Failing",
            description="",
            parameters_schema="{}",
            code_blob=code,
            entry_point_function="run",
            test_blob=failing_test,
        )

    assert ok is False

    with get_connection() as conn:
        row = conn.execute(
            "SELECT skill_id FROM agent_skills WHERE skill_id = ?",
            ("test_failing_skill",),
        ).fetchone()
    assert row is None


def test_stage_skill_staging_dir_always_cleaned(tmp_path):
    """Staging dir is removed even when the test fails."""
    code = "def run(sdk, args):\n    return {}"
    failing_test = "def test_fail():\n    assert False"
    with patch("src.skill_harness.config.get_effective_workspace_root", return_value=tmp_path):
        stage_skill(
            skill_id="test_cleanup",
            name="Cleanup",
            description="",
            parameters_schema="{}",
            code_blob=code,
            entry_point_function="run",
            test_blob=failing_test,
        )
    staging_dir = tmp_path / ".janus_sandboxes" / "temp_skills" / "test_cleanup"
    assert not staging_dir.exists()


# ---------------------------------------------------------------------------
# sync_from_registry — local_path mode (no git required)
# ---------------------------------------------------------------------------

def _make_local_registry(base: Path, skills: list[dict]) -> None:
    """Write a minimal registry layout under base."""
    (base / "metadata.json").write_text(
        json.dumps({"name": "test-library", "version": "0.1.0"}), encoding="utf-8"
    )
    (base / "registry.json").write_text(
        json.dumps({"version": "1.0", "skills": skills}), encoding="utf-8"
    )
    skills_dir = base / "skills"
    skills_dir.mkdir(exist_ok=True)


def test_sync_from_registry_local_path_happy_path(tmp_path):
    """End-to-end: local registry with one valid skill → synced into agent_skills."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()

    skill_code = textwrap.dedent("""\
        def run(sdk, args):
            return {"msg": args.get("message", "")}
    """)
    skill_test = textwrap.dedent("""\
        from skill import run
        from mock_sdk import make_mock_sdk

        def test_echo():
            sdk = make_mock_sdk()
            result = run(sdk, {"message": "hello"})
            assert result["msg"] == "hello"
    """)
    (lib_dir / "skills").mkdir()
    (lib_dir / "skills" / "echo_message.py").write_text(skill_code, encoding="utf-8")
    (lib_dir / "skills" / "test_echo_message.py").write_text(skill_test, encoding="utf-8")

    _make_local_registry(lib_dir, [{
        "skill_id": "echo_message",
        "name": "Echo Message",
        "description": "Returns a greeting",
        "parameters_schema": '{"type": "object", "properties": {"message": {"type": "string"}}}',
        "entry_point_function": "run",
        "required_role": "observer",
        "trigger_type": "manual",
        "trigger_config": "{}",
        "file": "skills/echo_message.py",
        "test_file": "skills/test_echo_message.py",
    }])

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with patch("src.skill_harness.config.get_effective_workspace_root", return_value=workspace):
        synced, failed, errors = sync_from_registry(local_path=str(lib_dir))

    assert synced == 1, f"Expected 1 synced, errors: {errors}"
    assert failed == 0
    assert errors == []

    with get_connection() as conn:
        row = conn.execute(
            "SELECT skill_id FROM agent_skills WHERE skill_id = ?", ("echo_message",)
        ).fetchone()
    assert row is not None


def test_sync_from_registry_partial_failure(tmp_path):
    """Registry with one good + one bad skill → correct counts, good one persists."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "skills").mkdir()

    good_code = "def run(sdk, args):\n    return {}"
    bad_code = "import subprocess\ndef run(sdk, args):\n    return {}"

    (lib_dir / "skills" / "good_skill.py").write_text(good_code, encoding="utf-8")
    (lib_dir / "skills" / "bad_skill.py").write_text(bad_code, encoding="utf-8")

    _make_local_registry(lib_dir, [
        {
            "skill_id": "good_skill",
            "name": "Good",
            "description": "",
            "parameters_schema": "{}",
            "entry_point_function": "run",
            "required_role": "contributor",
            "trigger_type": "manual",
            "trigger_config": "{}",
            "file": "skills/good_skill.py",
        },
        {
            "skill_id": "bad_skill",
            "name": "Bad",
            "description": "",
            "parameters_schema": "{}",
            "entry_point_function": "run",
            "required_role": "contributor",
            "trigger_type": "manual",
            "trigger_config": "{}",
            "file": "skills/bad_skill.py",
        },
    ])

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with patch("src.skill_harness.config.get_effective_workspace_root", return_value=workspace):
        synced, failed, errors = sync_from_registry(local_path=str(lib_dir))

    assert synced == 1
    assert failed == 1
    assert len(errors) == 1
    assert "subprocess" in errors[0]


def test_sync_from_registry_missing_registry_json(tmp_path):
    lib_dir = tmp_path / "empty_lib"
    lib_dir.mkdir()

    synced, failed, errors = sync_from_registry(local_path=str(lib_dir))
    assert synced == 0
    assert failed == 0
    assert any("registry.json" in e for e in errors)
