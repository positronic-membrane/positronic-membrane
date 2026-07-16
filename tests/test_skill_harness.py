import json
import textwrap
from pathlib import Path
from unittest.mock import patch

from src.database import get_connection
from src.skill_harness import (
    _check_entry_point_defined,
    audit_skill_ast,
    stage_skill,
    sync_from_registry,
)

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

def _make_local_registry(base: Path, skills: list[dict], sdk_version: str | None = None) -> None:
    """Write a minimal registry layout under base."""
    (base / "metadata.json").write_text(
        json.dumps({"name": "test-library", "version": "0.1.0"}), encoding="utf-8"
    )
    registry_body = {"version": "1.0", "skills": skills}
    if sdk_version is not None:
        registry_body["sdk_version"] = sdk_version
    (base / "registry.json").write_text(
        json.dumps(registry_body), encoding="utf-8"
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
        result = sync_from_registry(local_path=str(lib_dir))

    assert result["synced"] == ["echo_message"], f"Expected echo_message synced, got: {result}"
    assert result["failed"] == []
    assert result["fatal_error"] is None

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
        result = sync_from_registry(local_path=str(lib_dir))

    assert result["synced"] == ["good_skill"]
    assert len(result["failed"]) == 1
    assert result["failed"][0]["skill_id"] == "bad_skill"
    assert "subprocess" in result["failed"][0]["reason"]
    assert result["fatal_error"] is None


def test_sync_from_registry_missing_registry_json(tmp_path):
    lib_dir = tmp_path / "empty_lib"
    lib_dir.mkdir()

    result = sync_from_registry(local_path=str(lib_dir))
    assert result["synced"] == []
    assert result["failed"] == []
    assert "registry.json" in result["fatal_error"]


def test_sync_from_registry_blocks_sibling_prefix_escape(tmp_path):
    """A file path resolving to a sibling dir sharing the root's name prefix is rejected."""
    lib_dir = tmp_path / "lib"
    (lib_dir / "skills").mkdir(parents=True)
    evil_dir = tmp_path / "lib_evil"
    evil_dir.mkdir()
    (evil_dir / "evil.py").write_text("def run(sdk, args):\n    return {}", encoding="utf-8")

    _make_local_registry(lib_dir, [{
        "skill_id": "evil_skill",
        "name": "Evil",
        "description": "",
        "parameters_schema": "{}",
        "entry_point_function": "run",
        "required_role": "contributor",
        "trigger_type": "manual",
        "trigger_config": "{}",
        "file": "../lib_evil/evil.py",
    }])

    result = sync_from_registry(local_path=str(lib_dir))
    assert result["synced"] == []
    assert result["failed"][0]["skill_id"] == "evil_skill"
    assert "escapes library root" in result["failed"][0]["reason"]


# ---------------------------------------------------------------------------
# sync_from_registry — sdk_version compatibility gate (issue #104)
# ---------------------------------------------------------------------------

def test_sync_from_registry_rejects_mismatched_top_level_sdk_version(tmp_path):
    """A registry-wide sdk_version that doesn't match this instance is rejected
    at sync time (never staged, never inserted into agent_skills)."""
    import src.config as config

    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "skills").mkdir()
    (lib_dir / "skills" / "future_skill.py").write_text(
        "def run(sdk, args):\n    return {}", encoding="utf-8"
    )

    mismatched_version = f"not-{config.SDK_MAJOR_VERSION}"
    _make_local_registry(
        lib_dir,
        [{
            "skill_id": "future_skill",
            "name": "Future",
            "description": "",
            "parameters_schema": "{}",
            "entry_point_function": "run",
            "required_role": "contributor",
            "trigger_type": "manual",
            "trigger_config": "{}",
            "file": "skills/future_skill.py",
        }],
        sdk_version=mismatched_version,
    )

    result = sync_from_registry(local_path=str(lib_dir))

    assert result["synced"] == []
    assert result["failed"] == [{
        "skill_id": "future_skill",
        "reason": (
            f"sdk_version mismatch: skill targets '{mismatched_version}', "
            f"this instance runs '{config.SDK_MAJOR_VERSION}'"
        ),
    }]
    assert result["fatal_error"] is None

    with get_connection() as conn:
        row = conn.execute(
            "SELECT skill_id FROM agent_skills WHERE skill_id = ?", ("future_skill",)
        ).fetchone()
    assert row is None


def test_sync_from_registry_descriptor_sdk_version_overrides_registry_level(tmp_path):
    """A per-skill sdk_version takes precedence over the registry-wide default."""
    import src.config as config

    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "skills").mkdir()
    (lib_dir / "skills" / "compatible_skill.py").write_text(
        "def run(sdk, args):\n    return {}", encoding="utf-8"
    )

    mismatched_version = f"not-{config.SDK_MAJOR_VERSION}"
    _make_local_registry(
        lib_dir,
        [{
            "skill_id": "compatible_skill",
            "name": "Compatible",
            "description": "",
            "parameters_schema": "{}",
            "entry_point_function": "run",
            "required_role": "contributor",
            "trigger_type": "manual",
            "trigger_config": "{}",
            "file": "skills/compatible_skill.py",
            "sdk_version": config.SDK_MAJOR_VERSION,
        }],
        sdk_version=mismatched_version,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with patch("src.skill_harness.config.get_effective_workspace_root", return_value=workspace):
        result = sync_from_registry(local_path=str(lib_dir))

    assert result["synced"] == ["compatible_skill"]
    assert result["failed"] == []
    assert result["fatal_error"] is None


def test_sync_from_registry_missing_sdk_version_is_treated_as_compatible(tmp_path):
    """A legacy/pre-feature registry with no sdk_version field anywhere still syncs."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "skills").mkdir()
    (lib_dir / "skills" / "legacy_skill.py").write_text(
        "def run(sdk, args):\n    return {}", encoding="utf-8"
    )

    _make_local_registry(lib_dir, [{
        "skill_id": "legacy_skill",
        "name": "Legacy",
        "description": "",
        "parameters_schema": "{}",
        "entry_point_function": "run",
        "required_role": "contributor",
        "trigger_type": "manual",
        "trigger_config": "{}",
        "file": "skills/legacy_skill.py",
    }])

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with patch("src.skill_harness.config.get_effective_workspace_root", return_value=workspace):
        result = sync_from_registry(local_path=str(lib_dir))

    assert result["synced"] == ["legacy_skill"]
    assert result["failed"] == []
    assert result["fatal_error"] is None


def test_sync_from_registry_explicit_null_descriptor_sdk_version_falls_through(tmp_path):
    """An explicit `"sdk_version": null` on a descriptor must NOT bypass a
    mismatched registry-level default — it should fall through to it, not be
    treated as "no override present"."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "skills").mkdir()
    (lib_dir / "skills" / "future_skill.py").write_text(
        "def run(sdk, args):\n    return {}", encoding="utf-8"
    )

    _make_local_registry(
        lib_dir,
        [{
            "skill_id": "future_skill",
            "name": "Future",
            "description": "",
            "parameters_schema": "{}",
            "entry_point_function": "run",
            "required_role": "contributor",
            "trigger_type": "manual",
            "trigger_config": "{}",
            "file": "skills/future_skill.py",
            "sdk_version": None,
        }],
        sdk_version="v2",
    )

    result = sync_from_registry(local_path=str(lib_dir))

    assert result["synced"] == []
    assert len(result["failed"]) == 1
    assert result["failed"][0]["skill_id"] == "future_skill"
    assert "sdk_version mismatch" in result["failed"][0]["reason"]


def test_format_sync_summary_fatal():
    from src.skill_harness import format_sync_summary
    summary = format_sync_summary(
        {"synced": [], "failed": [], "fatal_error": "git operation failed: x"}
    )
    assert summary == "Sync FAILED before any skill was processed: git operation failed: x"


def test_format_sync_summary_mixed():
    from src.skill_harness import format_sync_summary
    summary = format_sync_summary({
        "synced": ["a", "b"],
        "failed": [{"skill_id": "c", "reason": "tests failed"}],
        "fatal_error": None,
    })
    assert summary.splitlines() == [
        "Synced: 2  Failed: 1",
        "Imported: a, b",
        "Skipped:",
        "  - c: tests failed",
    ]


def test_sync_from_registry_git_failure_is_fatal(tmp_path, monkeypatch):
    """A failing git step must surface as fatal_error, not as an empty success."""
    import subprocess as sp

    import src.skill_harness as harness

    monkeypatch.setattr(harness.config, "ROOT_DIR", tmp_path)

    def _failing_git(cmd, **kwargs):
        raise sp.CalledProcessError(
            128, cmd, stderr="fatal: detected dubious ownership in repository"
        )

    monkeypatch.setattr(harness.subprocess, "run", _failing_git)

    result = sync_from_registry()
    assert result["synced"] == []
    assert result["failed"] == []
    assert "dubious ownership" in result["fatal_error"]


def test_sync_from_registry_uses_pinned_ref_from_system_config(tmp_path, monkeypatch):
    """The git clone must use the skills.library_ref system_config pin, not the
    env-configured SKILLS_LIBRARY_REF default (issue #104)."""
    import src.skill_harness as harness
    from src.database import set_system_config_value

    set_system_config_value("skills.library_ref", "v7-custom-pin", is_agent=False)

    monkeypatch.setattr(harness.config, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(harness.config, "SKILLS_LIBRARY_REF", "should-not-be-used")

    captured_cmds = []

    def _fake_git(cmd, **kwargs):
        captured_cmds.append(cmd)
        raise __import__("subprocess").CalledProcessError(128, cmd, stderr="stop after capture")

    monkeypatch.setattr(harness.subprocess, "run", _fake_git)

    sync_from_registry()

    assert captured_cmds, "expected at least one git subprocess call to be captured"
    clone_cmd = captured_cmds[0]
    assert "v7-custom-pin" in clone_cmd
    assert "should-not-be-used" not in clone_cmd

# ---------------------------------------------------------------------------
# sync_from_registry — git path (issue #139)
# ---------------------------------------------------------------------------

def _git(args, cwd):
    import subprocess
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
             "HOME": str(cwd)},
    )


def _write_echo_library(lib_dir: Path, marker: str) -> None:
    """Write a complete one-skill library layout whose code carries `marker`."""
    (lib_dir / "skills").mkdir(exist_ok=True)
    (lib_dir / "skills" / "echo_message.py").write_text(
        f'def run(sdk, args):\n    return {{"msg": "{marker}"}}\n', encoding="utf-8"
    )
    (lib_dir / "skills" / "test_echo_message.py").write_text(
        textwrap.dedent(f"""\
            from skill import run
            from mock_sdk import make_mock_sdk

            def test_echo():
                assert run(make_mock_sdk(), {{}})["msg"] == "{marker}"
        """),
        encoding="utf-8",
    )
    _make_local_registry(lib_dir, [{
        "skill_id": "echo_message",
        "name": "Echo Message",
        "description": "Returns a marker",
        "parameters_schema": '{"type": "object", "properties": {}}',
        "entry_point_function": "run",
        "required_role": "observer",
        "trigger_type": "manual",
        "trigger_config": "{}",
        "file": "skills/echo_message.py",
        "test_file": "skills/test_echo_message.py",
    }])


def _make_git_library_remote(tmp_path: Path) -> Path:
    """A real git repo with 'main' (marker from-main) and 'v1' (marker from-v1)."""
    remote = tmp_path / "remote_lib"
    remote.mkdir()
    _git(["init", "-b", "main"], remote)
    _write_echo_library(remote, "from-main")
    _git(["add", "-A"], remote)
    _git(["commit", "-m", "main content"], remote)
    _git(["checkout", "-b", "v1"], remote)
    _write_echo_library(remote, "from-v1")
    _git(["add", "-A"], remote)
    _git(["commit", "-m", "v1 content"], remote)
    _git(["checkout", "main"], remote)
    return remote


def _synced_code_blob() -> str:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT code_blob FROM agent_skills WHERE skill_id = ?", ("echo_message",)
        ).fetchone()
    assert row is not None
    return row[0]


def test_sync_git_path_single_branch_cache_still_fetches_pinned_ref(tmp_path, monkeypatch):
    """A pre-existing cache cloned single-branch from 'main' must not break the
    'v1' pin — the production failure behind issue #139."""
    import subprocess

    import src.config

    remote = _make_git_library_remote(tmp_path)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(src.config, "ROOT_DIR", workspace)

    # Recreate the bad state: cache exists but only knows origin/main
    cache_dir = workspace / ".janus_sandboxes" / "skills_library"
    cache_dir.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "--branch", "main", "--single-branch", "--depth", "1",
         str(remote), str(cache_dir)],
        check=True, capture_output=True, text=True,
    )

    with patch("src.skill_harness.config.get_effective_workspace_root", return_value=workspace):
        result = sync_from_registry(repo_url=str(remote))

    assert result["fatal_error"] is None, f"sync failed: {result}"
    assert result["synced"] == ["echo_message"]
    assert "from-v1" in _synced_code_blob()


def test_sync_git_path_honours_ref_change_between_syncs(tmp_path, monkeypatch):
    """Changing skills.library_ref between syncs must switch the fetched content."""
    import src.config

    remote = _make_git_library_remote(tmp_path)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(src.config, "ROOT_DIR", workspace)

    with patch("src.skill_harness.config.get_effective_workspace_root", return_value=workspace):
        result = sync_from_registry(repo_url=str(remote))
        assert result["fatal_error"] is None, f"first sync failed: {result}"
        assert "from-v1" in _synced_code_blob()

        with get_connection() as conn:
            conn.execute(
                "UPDATE system_config SET config_value = ? WHERE config_key = ?",
                ("main", "skills.library_ref"),
            )
            conn.commit()

        result = sync_from_registry(repo_url=str(remote))
        assert result["fatal_error"] is None, f"re-sync failed: {result}"
        assert "from-main" in _synced_code_blob()
