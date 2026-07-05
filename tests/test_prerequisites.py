from unittest.mock import MagicMock, patch

import pytest

import src.config
from src.database import init_db, log_episodic_memory
from src.memory import compress_episodic_memory
from src.sandbox_session import DockerSandboxExecutor, LocalSandboxExecutor
from src.self_modification import apply_staged_change


@pytest.fixture(autouse=True)
def setup_isolated_db(tmp_path):
    """Isolate DB settings for testing prerequisites."""
    temp_db = tmp_path / "test_janus_prereq.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path

def test_docker_sandbox_executor_network_none():
    """Verify DockerSandboxExecutor passes the configured --network parameter and hardening flags."""
    executor = DockerSandboxExecutor()

    # Temporarily override DOCKER_NETWORK config value
    orig_net = getattr(src.config, "DOCKER_NETWORK", "none")
    src.config.DOCKER_NETWORK = "test_isolated_net"

    with patch("shutil.which", return_value="/usr/local/bin/docker"), \
         patch("subprocess.run") as mock_run:

        mock_run.return_value = MagicMock(returncode=0, stdout="success", stderr="")

        executor.run_tests("/mock/sandbox/root", 30, {})

        # Verify the daemon-reachability and image-existence preflight checks both ran
        # before the final `docker run ...` invocation.
        assert mock_run.call_count == 3

        # Verify subprocess.run command includes --network test_isolated_net
        called_args = mock_run.call_args[0][0]
        assert "--network" in called_args
        assert "test_isolated_net" in called_args

        # Verify resource-limit and hardening flags are present
        assert "--memory" in called_args
        assert "--cpus" in called_args
        assert "--pids-limit" in called_args
        assert "--cap-drop=ALL" in called_args
        assert "--security-opt=no-new-privileges" in called_args

    src.config.DOCKER_NETWORK = orig_net


def test_local_sandbox_executor_blocked_by_default():
    """Verify LocalSandboxExecutor refuses to run unless explicitly allowed."""
    executor = LocalSandboxExecutor()

    orig_allow_local = src.config.ALLOW_LOCAL_SANDBOX_EXEC
    src.config.ALLOW_LOCAL_SANDBOX_EXEC = False

    try:
        with patch("subprocess.run") as mock_run:
            passed, logs = executor.run_tests("/mock/sandbox/root", 30, {})

            assert passed is False
            assert "ALLOW_LOCAL_SANDBOX_EXEC" in logs
            mock_run.assert_not_called()
    finally:
        src.config.ALLOW_LOCAL_SANDBOX_EXEC = orig_allow_local


def test_local_sandbox_executor_allowed_when_flag_set():
    """Verify LocalSandboxExecutor runs pytest when explicitly allowed via the override flag."""
    executor = LocalSandboxExecutor()

    orig_allow_local = src.config.ALLOW_LOCAL_SANDBOX_EXEC
    src.config.ALLOW_LOCAL_SANDBOX_EXEC = True

    try:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="passed", stderr="")

            passed, logs = executor.run_tests("/mock/sandbox/root", 30, {})

            assert passed is True
            mock_run.assert_called_once()
            called_args = mock_run.call_args[0][0]
            assert "pytest" in called_args[0]
    finally:
        src.config.ALLOW_LOCAL_SANDBOX_EXEC = orig_allow_local


def test_docker_sandbox_executor_daemon_unreachable():
    """Verify DockerSandboxExecutor fails fast when the Docker daemon is unreachable."""
    executor = DockerSandboxExecutor()

    with patch("shutil.which", return_value="/usr/local/bin/docker"), \
         patch("subprocess.run") as mock_run:

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Cannot connect to the Docker daemon")

        passed, logs = executor.run_tests("/mock/sandbox/root", 30, {})

        assert passed is False
        assert "Docker daemon is not reachable" in logs
        assert mock_run.call_count == 1


def test_docker_sandbox_executor_image_missing():
    """Verify DockerSandboxExecutor fails fast with an actionable error when the image is missing."""
    executor = DockerSandboxExecutor()

    with patch("shutil.which", return_value="/usr/local/bin/docker"), \
         patch("subprocess.run") as mock_run:

        info_ok = MagicMock(returncode=0, stdout="", stderr="")
        inspect_fail = MagicMock(returncode=1, stdout="", stderr="No such image")
        mock_run.side_effect = [info_ok, inspect_fail]

        passed, logs = executor.run_tests("/mock/sandbox/root", 30, {})

        assert passed is False
        assert "docker build -t" in logs
        assert mock_run.call_count == 2

@patch("src.memory.query_agent")
@patch("src.memory.add_memory")
def test_compress_episodic_memory_background_thought_only(mock_add_memory, mock_query_agent):
    """Verify compress_episodic_memory compresses only background_thought rows,
    leaving user_visible rows untouched (issue #54: per-context_type thresholds)."""
    mock_query_agent.return_value = "Synthesized primary concept summary."

    # 12 background_thought rows + 5 user_visible rows
    for i in range(12):
        log_episodic_memory(
            speaker="persona",
            message_content=f"Thought {i}",
            context_type="background_thought"
        )
    for i in range(5):
        log_episodic_memory(
            speaker="user",
            message_content=f"Chat {i}",
            context_type="user_visible"
        )

    # Trigger with limit=10/keep_recent=3 for background_thought (compresses 12-3=9),
    # and thresholds high enough that the user_visible pass is a no-op.
    compress_episodic_memory(limit=10, keep_recent=3, chat_min_rows=1000, chat_min_age_days=9999)

    from src.database import get_connection
    conn = get_connection()
    bg_count = conn.execute(
        "SELECT COUNT(*) FROM episodic_memory WHERE context_type = 'background_thought';"
    ).fetchone()[0]
    visible_count = conn.execute(
        "SELECT COUNT(*) FROM episodic_memory WHERE context_type = 'user_visible';"
    ).fetchone()[0]
    conn.close()

    assert bg_count == 3
    assert visible_count == 5

    mock_add_memory.assert_called_once()
    mock_query_agent.assert_called_once()
    assert mock_query_agent.call_args[0][0] == "archivist"
    prompt_sent = mock_query_agent.call_args[0][1]
    assert "Thought 0" in prompt_sent
    assert "Thought 8" in prompt_sent
    assert "Thought 9" not in prompt_sent  # kept recent
    assert "Chat 0" not in prompt_sent  # user_visible must never be in the same batch


def _insert_backdated_episodic_row(speaker: str, message: str, context_type: str, days_ago: int):
    from datetime import datetime, timedelta

    from src.database import get_connection
    ts = (datetime.utcnow() - timedelta(days=days_ago)).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_connection()
    conn.execute(
        "INSERT INTO episodic_memory (speaker, message_content, context_type, timestamp) VALUES (?, ?, ?, ?);",
        (speaker, message, context_type, ts)
    )
    conn.commit()
    conn.close()


@patch("src.memory.query_agent")
@patch("src.memory.add_memory")
def test_compress_episodic_memory_user_visible_row_and_age_threshold(mock_add_memory, mock_query_agent):
    """user_visible rows are only compressed if BOTH beyond the row-count
    keep-window AND older than min_age_days (issue #54)."""
    mock_query_agent.return_value = "Synthesized primary concept summary."

    for i in range(7):
        _insert_backdated_episodic_row("user", f"Old chat {i}", "user_visible", days_ago=35)
    for i in range(8):
        _insert_backdated_episodic_row("user", f"Recent chat {i}", "user_visible", days_ago=5)

    compress_episodic_memory(limit=50, keep_recent=10, chat_min_rows=8, chat_min_age_days=30)

    from src.database import get_connection
    conn = get_connection()
    remaining = conn.execute("SELECT COUNT(*) FROM episodic_memory WHERE context_type = 'user_visible';").fetchone()[0]
    conn.close()

    assert remaining == 8

    mock_add_memory.assert_called_once()
    prompt_sent = mock_query_agent.call_args[0][1]
    for i in range(7):
        assert f"Old chat {i}" in prompt_sent
    for i in range(8):
        assert f"Recent chat {i}" not in prompt_sent


@patch("src.memory.query_agent")
@patch("src.memory.add_memory")
def test_compress_episodic_memory_both_passes_trigger_together(mock_add_memory, mock_query_agent):
    """When both background_thought and user_visible thresholds are exceeded
    in the same compress_episodic_memory() call, each runs its own independent
    Archivist summarization pass (issue #54: passes must never be merged)."""
    mock_query_agent.return_value = "Synthesized primary concept summary."

    for i in range(12):
        log_episodic_memory(speaker="persona", message_content=f"Thought {i}", context_type="background_thought")
    for i in range(7):
        _insert_backdated_episodic_row("user", f"Old chat {i}", "user_visible", days_ago=35)

    compress_episodic_memory(limit=10, keep_recent=3, chat_min_rows=0, chat_min_age_days=30)

    from src.database import get_connection
    conn = get_connection()
    bg_count = conn.execute(
        "SELECT COUNT(*) FROM episodic_memory WHERE context_type = 'background_thought';"
    ).fetchone()[0]
    visible_count = conn.execute(
        "SELECT COUNT(*) FROM episodic_memory WHERE context_type = 'user_visible';"
    ).fetchone()[0]
    conn.close()

    assert bg_count == 3
    assert visible_count == 0

    assert mock_query_agent.call_count == 2
    assert mock_add_memory.call_count == 2
    prompts_sent = [call.args[1] for call in mock_query_agent.call_args_list]
    assert any("Thought 0" in p for p in prompts_sent)
    assert any("Old chat 0" in p for p in prompts_sent)
    # Each Archivist call summarizes exactly one batch — never both types mixed together.
    for p in prompts_sent:
        assert not ("Thought 0" in p and "Old chat 0" in p)


@patch("src.memory.query_agent")
@patch("src.memory.add_memory")
def test_compress_episodic_memory_user_visible_age_guard(mock_add_memory, mock_query_agent):
    """A large volume of recent user_visible rows must never be compressed
    purely due to row count if none are older than min_age_days (issue #54)."""
    mock_query_agent.return_value = "Synthesized primary concept summary."

    for i in range(15):
        log_episodic_memory(speaker="user", message_content=f"Chat {i}", context_type="user_visible")

    compress_episodic_memory(limit=50, keep_recent=10, chat_min_rows=8, chat_min_age_days=30)

    from src.database import get_connection
    conn = get_connection()
    remaining = conn.execute("SELECT COUNT(*) FROM episodic_memory WHERE context_type = 'user_visible';").fetchone()[0]
    conn.close()

    assert remaining == 15
    mock_add_memory.assert_not_called()
    mock_query_agent.assert_not_called()

def test_apply_staged_change_raises_permission_error(tmp_path):
    """Verify that apply_staged_change raises PermissionError (V3-T3: direct modification disabled)."""
    with pytest.raises(PermissionError, match="Direct source modification is disabled"):
        apply_staged_change(str(tmp_path), "src/utils.py")
