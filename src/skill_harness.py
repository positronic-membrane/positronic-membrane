import ast
import json
import logging
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import src.config as config
from src.database import get_connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AST audit constants
# ---------------------------------------------------------------------------

SKILL_AST_BANNED_MODULES = {"subprocess", "pty", "commands", "ctypes"}
SKILL_AST_BANNED_CALLS   = {"eval", "exec"}

# ---------------------------------------------------------------------------
# Mock SDK source — written into every staging dir as mock_sdk.py
# ---------------------------------------------------------------------------

MOCK_SDK_SOURCE = textwrap.dedent("""\
    import logging

    class MockSafeDB:
        def __init__(self):
            self.calls = []
        def execute(self, sql, params=None):
            self.calls.append(("execute", sql, params))
            return []
        def fetchone(self, sql, params=None):
            self.calls.append(("fetchone", sql, params))
            return None
        def fetchall(self, sql, params=None):
            self.calls.append(("fetchall", sql, params))
            return []

    class MockSafeFS:
        def __init__(self):
            self.files = {}
        def read(self, path):
            return self.files.get(str(path), "")
        def write(self, path, content):
            self.files[str(path)] = content
            return True
        def exists(self, path):
            return str(path) in self.files
        def list_dir(self, path):
            return []

    class MockSafeMemory:
        def __init__(self):
            self.memories = []
            self.episodes = []
        def add_memory(self, text, metadata=None):
            self.memories.append({"text": text, "metadata": metadata or {}})
        def query_memories(self, query, n_results=5):
            return []
        def log_episode(self, content, role="background_thought"):
            self.episodes.append({"content": content, "role": role})

    def make_mock_sdk():
        return {
            "db":     MockSafeDB(),
            "fs":     MockSafeFS(),
            "memory": MockSafeMemory(),
            "logger": logging.getLogger("MockSkill"),
        }
""")

# ---------------------------------------------------------------------------
# Default test template — used when caller supplies no test_blob
# ---------------------------------------------------------------------------

DEFAULT_TEST_TEMPLATE = textwrap.dedent("""\
    import logging
    from mock_sdk import make_mock_sdk
    from skill import {entry_point}

    def test_skill_runs():
        sdk = make_mock_sdk()
        result = {entry_point}(sdk, {{}})
        assert result is not None
""")

# ---------------------------------------------------------------------------
# AST auditor
# ---------------------------------------------------------------------------

class SkillASTAuditor(ast.NodeVisitor):
    """Light AST audit for library skills.

    Blocks modules that have no legitimate use in a sandboxed skill
    (subprocess, pty, commands, ctypes) and the two most dangerous builtins
    (eval, exec).  os/socket/etc. are intentionally allowed — existing seeded
    skills use them directly and the trust boundary is the registry itself.
    """
    def __init__(self):
        self.errors: list[str] = []

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root in SKILL_AST_BANNED_MODULES:
                self.errors.append(f"banned import: {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module:
            root = node.module.split(".")[0]
            if root in SKILL_AST_BANNED_MODULES:
                self.errors.append(f"banned import: {node.module}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in SKILL_AST_BANNED_CALLS:
            self.errors.append(f"banned call: {node.func.id}()")
        self.generic_visit(node)


def audit_skill_ast(code_blob: str) -> tuple[bool, str | None]:
    """Return (ok, error_message). error_message is None when ok is True."""
    try:
        tree = ast.parse(code_blob)
    except SyntaxError as e:
        return False, f"SyntaxError on line {e.lineno}: {e.msg}"
    except Exception as e:
        return False, f"AST parse failed: {e}"

    auditor = SkillASTAuditor()
    auditor.visit(tree)
    if auditor.errors:
        return False, "; ".join(auditor.errors)
    return True, None


def _check_entry_point_defined(code_blob: str, entry_point: str) -> bool:
    """Return True if a top-level function named entry_point exists in code_blob."""
    try:
        tree = ast.parse(code_blob)
    except Exception:
        return False
    return any(
        isinstance(node, ast.FunctionDef) and node.name == entry_point
        for node in ast.walk(tree)
    )


# ---------------------------------------------------------------------------
# Pytest binary resolution (mirrors src/self_modification.py)
# ---------------------------------------------------------------------------

def _resolve_pytest() -> str:
    candidate = Path(sys.executable).parent / "pytest"
    if candidate.exists():
        return str(candidate)
    venv_candidate = config.ROOT_DIR / ".venv" / "bin" / "pytest"
    if venv_candidate.exists():
        return str(venv_candidate)
    return "pytest"


# ---------------------------------------------------------------------------
# Core staging pipeline
# ---------------------------------------------------------------------------

def stage_skill(
    skill_id: str,
    name: str,
    description: str,
    parameters_schema: str,
    code_blob: str,
    entry_point_function: str,
    test_blob: str | None = None,
    required_role: str = "contributor",
    trigger_type: str = "manual",
    trigger_config: str = "{}",
) -> tuple[bool, str]:
    """Verify and upsert a skill into agent_skills.

    Pipeline:
      1. AST audit code_blob (blocks banned modules/calls)
      2. Confirm entry_point_function is defined
      3. Write staging dir under .janus_sandboxes/temp_skills/<skill_id>/
      4. Run pytest with mock SDK
      5. If tests pass: upsert to agent_skills
      6. Always clean up staging dir

    Returns (success, message).
    """
    ok, err = audit_skill_ast(code_blob)
    if not ok:
        return False, f"AST audit failed for '{skill_id}': {err}"

    if not _check_entry_point_defined(code_blob, entry_point_function):
        return False, (
            f"Entry point '{entry_point_function}' not found as a function "
            f"definition in skill '{skill_id}'"
        )

    workspace = config.get_effective_workspace_root()
    staging_dir = workspace / ".janus_sandboxes" / "temp_skills" / skill_id
    staging_dir.mkdir(parents=True, exist_ok=True)

    try:
        (staging_dir / "skill.py").write_text(code_blob, encoding="utf-8")
        (staging_dir / "mock_sdk.py").write_text(MOCK_SDK_SOURCE, encoding="utf-8")

        resolved_test = test_blob or DEFAULT_TEST_TEMPLATE.format(
            entry_point=entry_point_function
        )
        (staging_dir / "test_skill.py").write_text(resolved_test, encoding="utf-8")

        env = {**os.environ, "JANUS_TEST_MODE": "1", "PYTHONPATH": str(staging_dir)}
        result = subprocess.run(
            [_resolve_pytest(), "-v", "--tb=short"],
            cwd=str(staging_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=config.SANDBOX_TEST_TIMEOUT,
        )
        logs = result.stdout + "\n" + result.stderr
        passed = result.returncode == 0

    except subprocess.TimeoutExpired:
        return False, f"Staging tests timed out ({config.SANDBOX_TEST_TIMEOUT}s) for '{skill_id}'"
    except Exception as e:
        return False, f"Staging error for '{skill_id}': {e}"
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    if not passed:
        return False, f"Tests failed for '{skill_id}':\n{logs}"

    # Upsert into agent_skills (INSERT OR REPLACE translates to ON CONFLICT DO UPDATE in Postgres)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO agent_skills
                (skill_id, name, description, parameters_schema, code_blob,
                 entry_point_function, required_role, trigger_type, trigger_config,
                 is_active, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now'))
            """,
            (
                skill_id, name, description, parameters_schema, code_blob,
                entry_point_function, required_role, trigger_type, trigger_config,
            ),
        )

    return True, f"Skill '{skill_id}' staged, tested, and upserted successfully.\n{logs}"


# ---------------------------------------------------------------------------
# Sibling registry sync
# ---------------------------------------------------------------------------

def sync_from_registry(
    repo_url: str | None = None,
    local_path: str | None = None,
) -> tuple[int, int, list[str]]:
    """Clone (or pull) janus-skills-library and compile verified skills into agent_skills.

    Pass local_path to skip the git clone step (useful in tests).

    Returns (synced_count, failed_count, error_messages).
    """
    if local_path is not None:
        source_dir = Path(local_path)
    else:
        url = repo_url or config.SKILLS_LIBRARY_REPO
        branch = config.SKILLS_LIBRARY_BRANCH
        cache_dir = config.ROOT_DIR / ".janus_sandboxes" / "skills_library"

        try:
            if cache_dir.exists():
                subprocess.run(
                    ["git", "-C", str(cache_dir), "pull", "--ff-only"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            else:
                cache_dir.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    ["git", "clone", "--branch", branch, "--depth", "1", url, str(cache_dir)],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
        except subprocess.CalledProcessError as e:
            err = e.stderr or str(e)
            return 0, 0, [f"git operation failed: {err}"]
        except subprocess.TimeoutExpired:
            return 0, 0, ["git operation timed out"]

        source_dir = cache_dir

    registry_path = source_dir / "registry.json"
    if not registry_path.exists():
        return 0, 0, [f"registry.json not found in {source_dir}"]

    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception as e:
        return 0, 0, [f"Failed to parse registry.json: {e}"]

    skills = registry.get("skills", [])
    synced = 0
    failed = 0
    errors: list[str] = []

    for descriptor in skills:
        skill_id = descriptor.get("skill_id", "<unknown>")
        try:
            skill_file = source_dir / descriptor["file"]
            code_blob = skill_file.read_text(encoding="utf-8")

            test_blob: str | None = None
            if descriptor.get("test_file"):
                test_file = source_dir / descriptor["test_file"]
                if test_file.exists():
                    test_blob = test_file.read_text(encoding="utf-8")

            ok, msg = stage_skill(
                skill_id=skill_id,
                name=descriptor.get("name", skill_id),
                description=descriptor.get("description", ""),
                parameters_schema=descriptor.get("parameters_schema", "{}"),
                code_blob=code_blob,
                entry_point_function=descriptor.get("entry_point_function", "run"),
                test_blob=test_blob,
                required_role=descriptor.get("required_role", "contributor"),
                trigger_type=descriptor.get("trigger_type", "manual"),
                trigger_config=descriptor.get("trigger_config", "{}"),
            )
            if ok:
                synced += 1
            else:
                failed += 1
                errors.append(msg)
        except Exception as e:
            failed += 1
            errors.append(f"Error processing skill '{skill_id}': {e}")

    return synced, failed, errors
