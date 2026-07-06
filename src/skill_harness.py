import ast
import json
import logging
import os
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import src.config as config
from src.database import get_connection
from src.explorer import _get_config_int

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
    import importlib.util
    import logging
    from pathlib import Path
    from mock_sdk import make_mock_sdk

    def test_skill_entry_point_callable():
        spec = importlib.util.spec_from_file_location(
            "skill", Path(__file__).parent / "skill.py"
        )
        mod = importlib.util.module_from_spec(spec)
        mod.sdk = make_mock_sdk()
        spec.loader.exec_module(mod)
        assert hasattr(mod, "{entry_point}")
        assert callable(getattr(mod, "{entry_point}"))
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
    import re as _re
    if not _re.match(r'^[a-zA-Z0-9_-]+$', skill_id):
        return False, f"Invalid skill_id '{skill_id}': must match [a-zA-Z0-9_-]+"

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
        (staging_dir / f"{skill_id}.py").write_text(code_blob, encoding="utf-8")
        (staging_dir / "mock_sdk.py").write_text(MOCK_SDK_SOURCE, encoding="utf-8")

        resolved_test = test_blob or DEFAULT_TEST_TEMPLATE.format(
            entry_point=entry_point_function
        )
        (staging_dir / "test_skill.py").write_text(resolved_test, encoding="utf-8")

        env = {**os.environ, "JANUS_TEST_MODE": "1", "PYTHONPATH": str(staging_dir) + os.pathsep + str(config.ROOT_DIR)}
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

def _sync_result(
    synced: list[str] | None = None,
    failed: list[dict] | None = None,
    fatal_error: str | None = None,
) -> dict:
    return {
        "synced": synced or [],
        "failed": failed or [],
        "fatal_error": fatal_error,
    }


def format_sync_summary(result: dict) -> str:
    """Render a sync_from_registry() result as a human-readable summary."""
    if result["fatal_error"]:
        return f"Sync FAILED before any skill was processed: {result['fatal_error']}"
    lines = [f"Synced: {len(result['synced'])}  Failed: {len(result['failed'])}"]
    if result["synced"]:
        lines.append("Imported: " + ", ".join(result["synced"]))
    if result["failed"]:
        lines.append("Skipped:")
        lines.extend(f"  - {f['skill_id']}: {f['reason']}" for f in result["failed"])
    return "\n".join(lines)


def sync_from_registry(
    repo_url: str | None = None,
    local_path: str | None = None,
) -> dict:
    """Clone (or pull) janus-skills-library and compile verified skills into agent_skills.

    Pass local_path to skip the git clone step (useful in tests).

    Returns a structured result:
      {"synced": [skill_id, ...],
       "failed": [{"skill_id": ..., "reason": ...}, ...],
       "fatal_error": None | str}

    fatal_error is set when the sync could not run at all (git failure, missing or
    unparseable registry.json) — distinct from per-skill failures, so callers can
    tell "nothing to sync" apart from "sync never happened".
    """
    if local_path is not None:
        source_dir = Path(local_path)
    else:
        url = repo_url or config.SKILLS_LIBRARY_REPO
        branch = config.SKILLS_LIBRARY_REF
        cache_dir = config.ROOT_DIR / ".janus_sandboxes" / "skills_library"

        try:
            if cache_dir.exists():
                # Fetch then checkout the configured ref before pulling, so a change
                # in SKILLS_LIBRARY_REF between boots is honoured.
                subprocess.run(
                    ["git", "-C", str(cache_dir), "fetch", "origin"],
                    check=True, capture_output=True, text=True, timeout=60,
                )
                subprocess.run(
                    ["git", "-C", str(cache_dir), "checkout", branch],
                    check=True, capture_output=True, text=True, timeout=30,
                )
                subprocess.run(
                    ["git", "-C", str(cache_dir), "pull", "--ff-only"],
                    check=True, capture_output=True, text=True, timeout=60,
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
            return _sync_result(fatal_error=f"git operation failed: {err}")
        except subprocess.TimeoutExpired:
            return _sync_result(fatal_error="git operation timed out")

        source_dir = cache_dir

    registry_path = source_dir / "registry.json"
    if not registry_path.exists():
        return _sync_result(fatal_error=f"registry.json not found in {source_dir}")

    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception as e:
        return _sync_result(fatal_error=f"Failed to parse registry.json: {e}")

    skills = registry.get("skills", [])
    synced: list[str] = []
    failed: list[dict] = []

    source_dir_resolved = source_dir.resolve()

    for descriptor in skills:
        skill_id = descriptor.get("skill_id", "<unknown>")
        try:
            skill_file = (source_dir / descriptor["file"]).resolve()
            if not skill_file.is_relative_to(source_dir_resolved):
                failed.append({"skill_id": skill_id, "reason": "file path escapes library root"})
                continue
            code_blob = skill_file.read_text(encoding="utf-8")

            test_blob: str | None = None
            if descriptor.get("test_file"):
                test_file = (source_dir / descriptor["test_file"]).resolve()
                if not test_file.is_relative_to(source_dir_resolved):
                    failed.append({"skill_id": skill_id, "reason": "test_file path escapes library root"})
                    continue
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
                synced.append(skill_id)
            else:
                failed.append({"skill_id": skill_id, "reason": msg})
        except Exception as e:
            failed.append({"skill_id": skill_id, "reason": f"Error processing skill: {e}"})

    return _sync_result(synced=synced, failed=failed)


# ---------------------------------------------------------------------------
# Circuit breaker (issue #59) — trips a skill after repeated execution
# failures, auto-resetting after a cooldown period elapses.
# ---------------------------------------------------------------------------

DEFAULT_MAX_FAILURES = 5
DEFAULT_COOLDOWN_MINUTES = 15

# check_presence is the sole mechanism (src/daemon.py) that resets the Loop
# Safety Valve / Smart Governor via human-presence detection. Tripping its
# breaker would silently disable a more fundamental safety mechanism than the
# one this breaker protects, so it is exempt from enforcement (failures are
# still recorded and visible via /circuit status, just never enforced).
_BREAKER_EXEMPT_SKILLS = frozenset({"check_presence"})


def check_circuit(skill_id: str, tripped_at: Optional[str] = None) -> bool:
    """Returns False if skill_id's circuit breaker is tripped and still within
    its cooldown window; True otherwise. A skill whose cooldown has elapsed is
    auto-reset as a side effect of this check.

    Pass `tripped_at` when the caller has already fetched it alongside other
    skill data (e.g. a joined query) to avoid a redundant lookup; otherwise it
    is fetched here.
    """
    if skill_id in _BREAKER_EXEMPT_SKILLS:
        return True

    if tripped_at is None:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT tripped_at FROM circuit_breaker_state WHERE skill_id = ?;",
                (skill_id,),
            ).fetchone()
        finally:
            conn.close()
        tripped_at = row[0] if row else None

    if not tripped_at:
        return True

    cooldown_minutes = _get_config_int("circuit_breaker.cooldown_minutes", DEFAULT_COOLDOWN_MINUTES)
    cutoff_str = (datetime.utcnow() - timedelta(minutes=cooldown_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    if str(tripped_at) >= cutoff_str:
        return False

    conn = get_connection()
    try:
        conn.execute(
            "UPDATE circuit_breaker_state SET consecutive_failures = 0, tripped_at = NULL WHERE skill_id = ?;",
            (skill_id,),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info(f"Circuit breaker for '{skill_id}' auto-reset after {cooldown_minutes}m cooldown.")
    return True


def record_skill_failure(skill_id: str) -> None:
    """Increments the consecutive-failure count for skill_id, tripping the
    breaker once the configured threshold is exceeded.

    The increment and the trip decision are applied in a single UPDATE so that
    concurrent failures of the same skill_id can't both independently observe
    an untripped breaker and both announce a trip (SQLite serializes writers,
    so a second writer only proceeds after seeing the first's committed
    tripped_at)."""
    max_failures = _get_config_int("circuit_breaker.max_failures", DEFAULT_MAX_FAILURES)
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO circuit_breaker_state (skill_id) VALUES (?);",
            (skill_id,),
        )
        conn.execute(
            """
            UPDATE circuit_breaker_state
            SET consecutive_failures = consecutive_failures + 1,
                last_failure_at = ?,
                tripped_at = CASE
                    WHEN tripped_at IS NULL AND consecutive_failures + 1 >= ? THEN ?
                    ELSE tripped_at
                END
            WHERE skill_id = ?;
            """,
            (now_str, max_failures, now_str, skill_id),
        )
        row = conn.execute(
            "SELECT consecutive_failures, tripped_at, last_failure_at FROM circuit_breaker_state WHERE skill_id = ?;",
            (skill_id,),
        ).fetchone()
        conn.commit()
    finally:
        conn.close()

    # tripped_at == last_failure_at (both just written to now_str) iff the
    # CASE branch fired on this call, i.e. this call is the one that tripped it.
    if row and row[1] is not None and row[1] == row[2] and row[0] >= max_failures:
        announce_trip(skill_id, row[0])


def record_skill_success(skill_id: str) -> None:
    """Resets the consecutive-failure count for skill_id after a successful run.

    Only applies when the breaker isn't currently tripped, so a success from a
    long-running execution that started before a concurrent failure tripped
    the breaker doesn't mask that trip."""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE circuit_breaker_state SET consecutive_failures = 0 WHERE skill_id = ? AND tripped_at IS NULL;",
            (skill_id,),
        )
        conn.commit()
    finally:
        conn.close()


def announce_trip(skill_id: str, failure_count: int) -> None:
    """Logs and notifies that skill_id's breaker was just tripped. The
    tripped_at write itself happens atomically inside record_skill_failure()."""
    from src.database import log_episodic_memory
    from src.notifications import send_webhook_notification

    cooldown_minutes = _get_config_int("circuit_breaker.cooldown_minutes", DEFAULT_COOLDOWN_MINUTES)
    message = (
        f"Circuit breaker tripped for skill '{skill_id}' after {failure_count} "
        f"consecutive failures. It will be skipped for {cooldown_minutes} minute(s) "
        f"or until '/circuit reset {skill_id}' is run."
    )
    logger.warning(message)
    log_episodic_memory(speaker="system", message_content=message, context_type="background_thought")
    send_webhook_notification("circuit_breaker_tripped", message)


def reset_breaker(skill_id: str) -> bool:
    """Manually clears breaker state for skill_id. Returns True if a row existed."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE circuit_breaker_state SET consecutive_failures = 0, tripped_at = NULL WHERE skill_id = ?;",
            (skill_id,),
        )
        existed = cursor.rowcount > 0
        conn.commit()
    finally:
        conn.close()

    if existed:
        from src.database import log_episodic_memory
        log_episodic_memory(
            speaker="system",
            message_content=f"Circuit breaker for skill '{skill_id}' manually reset.",
            context_type="background_thought",
        )
    return existed
