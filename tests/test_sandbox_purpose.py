import shutil
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import src.config
from src.sandbox_session import (
    create_sandbox_session,
    promote_evolution_sandbox,
    spawn_evolution_daemon,
)


@patch("src.sandbox_session.subprocess.run")
@patch("src.sandbox_session.save_sandbox_session")
def test_create_project_sandbox_runs_git_init(mock_save, mock_run, tmp_path):
    """Project sandboxes get a plain git-init'd folder -- no worktree, branch, or DB copy."""
    orig_root = src.config.ROOT_DIR
    src.config.ROOT_DIR = tmp_path

    mock_run_instance = MagicMock()
    mock_run_instance.returncode = 0
    mock_run.return_value = mock_run_instance

    try:
        path, branch = create_sandbox_session("demo", purpose="project", app_name="demo-app")

        assert branch == ""
        assert "projects" in path
        assert "demo-app" in path

        # Exactly one subprocess call: git init -- no worktree/branch/DB-copy calls.
        mock_run.assert_called_once()
        init_args = mock_run.call_args[0][0]
        assert init_args[:2] == ["git", "init"]

        mock_save.assert_called_once_with(path, "", "active", purpose="project", app_name="demo-app")
    finally:
        src.config.ROOT_DIR = orig_root


def test_create_project_sandbox_existing_dir_raises_without_overwrite(tmp_path):
    """An existing project dir must not be silently clobbered -- it may hold real app work."""
    orig_root = src.config.ROOT_DIR
    src.config.ROOT_DIR = tmp_path

    project_dir = tmp_path / ".janus_sandboxes" / "projects" / "demo-app"
    project_dir.mkdir(parents=True)

    try:
        with pytest.raises(RuntimeError, match="already exists"):
            create_sandbox_session("demo", purpose="project", app_name="demo-app")
    finally:
        src.config.ROOT_DIR = orig_root


def test_project_sandbox_safefs_confinement(tmp_path):
    """
    The literal spec test: SafeFS must confine reads/writes to an active project
    sandbox root, with zero changes to SafeFS itself -- it already resolves its
    root via get_effective_workspace_root(), which a project sandbox feeds into
    via the same active-sandbox mechanism evolution sessions use.
    """
    from src.skills import SafeFS

    project_dir = tmp_path / "myapp"
    project_dir.mkdir()
    mock_active_sandbox = {
        "active_sandbox_path": str(project_dir),
        "active_sandbox_branch": "",
        "active_sandbox_purpose": "project",
        "active_sandbox_app_name": "myapp",
    }

    with patch("src.sandbox_session.get_active_sandbox", return_value=mock_active_sandbox):
        fs = SafeFS()

        fs.write("notes.txt", "hello")
        assert (project_dir / "notes.txt").read_text() == "hello"

        with pytest.raises(PermissionError):
            fs.write("../outside.txt", "escape!")

        assert not (tmp_path / "outside.txt").exists()


@patch("src.sandbox_session._create_project_sandbox")
@patch("src.sandbox_session._create_evolution_sandbox")
def test_create_sandbox_session_dispatches_by_purpose(mock_evolution, mock_project):
    """purpose defaults to 'evolution' so every existing caller is unaffected."""
    mock_evolution.return_value = ("/evo/path", "evolution/sandbox-x")
    mock_project.return_value = ("/proj/path", "")

    path, branch = create_sandbox_session("x")
    mock_evolution.assert_called_once_with("x")
    mock_project.assert_not_called()
    assert (path, branch) == ("/evo/path", "evolution/sandbox-x")

    mock_evolution.reset_mock()
    path2, branch2 = create_sandbox_session("y", purpose="project", app_name="myapp")
    mock_project.assert_called_once_with("y", "myapp")
    mock_evolution.assert_not_called()
    assert (path2, branch2) == ("/proj/path", "")


@patch("src.sandbox_session.subprocess.Popen")
@patch("src.sandbox_session._find_free_evolution_port", return_value=5001)
def test_spawn_evolution_daemon_sets_port_and_env(mock_port, mock_popen, tmp_path):
    """Verify the child gets DB/port/parent-DB env vars and a spawn_log bookkeeping row."""
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    (sandbox_dir / "janus_test.db").write_text("")

    mock_process = MagicMock()
    mock_process.pid = 4242
    mock_popen.return_value = mock_process

    result = spawn_evolution_daemon(sandbox_dir, "evolution/sandbox-feat")

    assert result["child_pid"] == 4242
    assert result["port"] == 5001
    assert result["self_party_id"] == "evolution_feat"

    launched_env = mock_popen.call_args.kwargs["env"]
    assert launched_env["DB_PATH"] == str(sandbox_dir / "janus_test.db")
    assert launched_env["JANUS_EVOLUTION_PORT"] == "5001"
    assert launched_env["JANUS_PARENT_DB_PATH"] == str(Path(src.config.DB_PATH).resolve())
    assert launched_env["JANUS_ROLE"] == "evolution_child"
    assert launched_env["JANUS_PARENT_PARTY_ID"] == "parent"
    assert launched_env["JANUS_SELF_PARTY_ID"] == "evolution_feat"

    from src.database import get_connection
    conn = get_connection(read_only_constitution=True)
    row = conn.execute(
        "SELECT child_pid, status FROM spawn_log WHERE child_path = ?;", (str(sandbox_dir),)
    ).fetchone()
    conn.close()
    assert row[0] == 4242
    assert row[1] == "alive"


def test_swarm_bus_connection_crosses_process_boundary(tmp_path, monkeypatch):
    """
    Core proof that swarm_messages can actually cross the parent/child DB
    boundary: a message sent while JANUS_PARENT_DB_PATH points at a DB other
    than the local DB_PATH lands in that other DB, not the local one.
    """
    from src.database import get_pending_swarm_messages, init_db, send_swarm_message

    child_db_path = tmp_path / "child.db"
    parent_db_path = tmp_path / "parent.db"

    orig_db_path = src.config.DB_PATH

    # Initialize the "parent" DB's schema (the fixture only initialized the
    # default/"child" DB_PATH).
    src.config.DB_PATH = str(parent_db_path)
    init_db()
    src.config.DB_PATH = str(child_db_path)
    init_db()

    try:
        # Simulate the evolution child: local DB_PATH is the child DB, but
        # JANUS_PARENT_DB_PATH redirects swarm bus traffic to the parent DB.
        monkeypatch.setenv("JANUS_PARENT_DB_PATH", str(parent_db_path))
        send_swarm_message("evolution_feat", "parent", "status_update", "child says hi")

        # The message must NOT be visible in the child's own local DB.
        child_conn = sqlite3.connect(str(child_db_path))
        local_count = child_conn.execute("SELECT COUNT(*) FROM swarm_messages;").fetchone()[0]
        child_conn.close()
        assert local_count == 0

        # Simulate the parent: no JANUS_PARENT_DB_PATH, local DB_PATH is the parent DB.
        monkeypatch.delenv("JANUS_PARENT_DB_PATH", raising=False)
        src.config.DB_PATH = str(parent_db_path)
        pending = get_pending_swarm_messages("parent")
        assert len(pending) == 1
        assert pending[0][1] == "evolution_feat"
        assert pending[0][3] == "child says hi"
    finally:
        src.config.DB_PATH = orig_db_path


@patch("src.sandbox_session.ship_sandbox_session")
@patch("src.sandbox_session.get_active_sandbox")
def test_promote_evolution_sandbox_detects_schema_delta_without_applying(
    mock_get_active, mock_ship, tmp_path
):
    """Schema deltas are queued for manual review -- never auto-applied to the parent DB."""
    mock_ship.return_value = ["src/foo.py"]

    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    child_db_path = sandbox_dir / "janus_test.db"
    shutil.copy2(src.config.DB_PATH, child_db_path)

    child_conn = sqlite3.connect(str(child_db_path))
    child_conn.execute("CREATE TABLE evolved_feature (id INTEGER PRIMARY KEY);")
    child_conn.commit()
    child_conn.close()

    mock_get_active.return_value = {
        "active_sandbox_path": str(sandbox_dir),
        "active_sandbox_branch": "evolution/sandbox-feat",
        "active_sandbox_purpose": "evolution",
    }

    result = promote_evolution_sandbox()

    assert result["queued_migrations"] == 1
    mock_ship.assert_called_once()

    from src.database import get_connection
    conn = get_connection(read_only_constitution=True)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='evolved_feature';"
    ).fetchone()
    queued = conn.execute("SELECT ddl_statement, status FROM pending_schema_migrations;").fetchall()
    conn.close()

    assert row is None  # parent DB must NOT gain the table automatically
    assert len(queued) == 1
    assert "evolved_feature" in queued[0][0]
    assert queued[0][1] == "pending_review"


@patch("src.sandbox_session.ship_sandbox_session")
@patch("src.sandbox_session.get_active_sandbox")
def test_promote_evolution_sandbox_ports_tagged_episodic_memory(
    mock_get_active, mock_ship, tmp_path
):
    """Only the child's own party-tagged, post-spawn episodic_memory rows are ported."""
    mock_ship.return_value = []

    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    child_db_path = sandbox_dir / "janus_test.db"
    shutil.copy2(src.config.DB_PATH, child_db_path)

    child_party_id = "evolution_feat"

    from src.database import get_connection
    conn = get_connection(read_only_constitution=True)
    conn.execute(
        "INSERT INTO spawn_log (child_path, status, spawned_at) VALUES (?, 'alive', '2020-01-01 00:00:00');",
        (str(sandbox_dir),)
    )
    conn.commit()
    conn.close()

    child_conn = sqlite3.connect(str(child_db_path))
    child_conn.execute(
        "INSERT INTO episodic_memory (speaker, message_content, context_type, party_id, timestamp) "
        "VALUES ('evolution_feat', 'tagged after spawn', 'background_thought', ?, '2020-06-01 00:00:00');",
        (child_party_id,)
    )
    child_conn.execute(
        "INSERT INTO episodic_memory (speaker, message_content, context_type, party_id, timestamp) "
        "VALUES ('evolution_feat', 'tagged before spawn', 'background_thought', ?, '2019-01-01 00:00:00');",
        (child_party_id,)
    )
    child_conn.execute(
        "INSERT INTO episodic_memory (speaker, message_content, context_type, timestamp) "
        "VALUES ('someone_else', 'untagged after spawn', 'background_thought', '2020-06-01 00:00:00');"
    )
    child_conn.commit()
    child_conn.close()

    mock_get_active.return_value = {
        "active_sandbox_path": str(sandbox_dir),
        "active_sandbox_branch": "evolution/sandbox-feat",
        "active_sandbox_purpose": "evolution",
    }

    result = promote_evolution_sandbox()
    assert result["ported_memories"] == 1

    conn = get_connection(read_only_constitution=True)
    rows = conn.execute(
        "SELECT message_content FROM episodic_memory WHERE party_id = ?;", (child_party_id,)
    ).fetchall()
    conn.close()
    contents = [r[0] for r in rows]
    assert "tagged after spawn" in contents
    assert "tagged before spawn" not in contents
    assert "untagged after spawn" not in contents
