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
def test_compress_episodic_memory_trigger(mock_add_memory, mock_query_agent):
    """Verify compress_episodic_memory triggers, processes LLM summary, and deletes rows."""
    mock_query_agent.return_value = "Synthesized primary concept summary."

    # 1. Seed 15 episodic memories
    for i in range(15):
        log_episodic_memory(
            speaker="persona" if i % 2 == 0 else "user",
            message_content=f"Dummy message content {i}",
            context_type="background_thought" if i % 3 == 0 else "user_visible"
        )

    # Verify we seeded 15 items
    from src.database import get_connection
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM episodic_memory;").fetchone()[0]
    assert count == 15
    conn.close()

    # 2. Trigger compression with limit 10 and keeping recent 3.
    # This should compress 15 - 3 = 12 items.
    compress_episodic_memory(limit=10, keep_recent=3)

    # 3. Verify database count is now 3
    conn = get_connection()
    new_count = conn.execute("SELECT COUNT(*) FROM episodic_memory;").fetchone()[0]
    assert new_count == 3
    conn.close()

    # 4. Verify vector DB add_memory was called to store synthesized concept
    mock_add_memory.assert_called_once()
    stored_concept = mock_add_memory.call_args[0][0]
    assert stored_concept == "Synthesized primary concept summary."

    # 5. Verify query_agent was called for the archivist role
    mock_query_agent.assert_called_once()
    assert mock_query_agent.call_args[0][0] == "archivist"
    prompt_sent = mock_query_agent.call_args[0][1]
    assert "Dummy message content 0" in prompt_sent
    assert "Dummy message content 11" in prompt_sent
    # Message 12, 13, 14 should not be in the prompt because they are the 3 kept recent ones
    assert "Dummy message content 12" not in prompt_sent

def test_apply_staged_change_raises_permission_error(tmp_path):
    """Verify that apply_staged_change raises PermissionError (V3-T3: direct modification disabled)."""
    with pytest.raises(PermissionError, match="Direct source modification is disabled"):
        apply_staged_change(str(tmp_path), "src/utils.py")
