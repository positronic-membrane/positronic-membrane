from unittest.mock import patch

import src.config
from src.config import get_effective_workspace_root


def test_get_effective_workspace_root_fallback(tmp_path):
    """Verify that get_effective_workspace_root falls back to ROOT_DIR when no staging or sandbox is active."""
    orig_root = src.config.ROOT_DIR
    src.config.ROOT_DIR = tmp_path

    try:
        with patch("src.database.get_pending_modification", return_value=None), \
             patch("src.sandbox_session.get_active_sandbox", return_value=None):
            resolved = get_effective_workspace_root()
            assert resolved == tmp_path
    finally:
        src.config.ROOT_DIR = orig_root

def test_get_effective_workspace_root_staging(tmp_path):
    """Verify that get_effective_workspace_root returns staging directory if active."""
    orig_root = src.config.ROOT_DIR
    src.config.ROOT_DIR = tmp_path

    staging_dir = tmp_path / "staging_folder"
    mock_pending = {
        "pending_mod_dir": str(staging_dir),
        "pending_mod_file": "src/utils.py"
    }

    try:
        with patch("src.database.get_pending_modification", return_value=mock_pending), \
             patch("src.sandbox_session.get_active_sandbox", return_value=None):
            resolved = get_effective_workspace_root()
            assert resolved == staging_dir
    finally:
        src.config.ROOT_DIR = orig_root

def test_get_effective_workspace_root_sandbox(tmp_path):
    """Verify that get_effective_workspace_root returns sandbox directory if active and no staging is active."""
    orig_root = src.config.ROOT_DIR
    src.config.ROOT_DIR = tmp_path

    sandbox_dir = tmp_path / "sandbox_folder"
    mock_active_sandbox = {
        "active_sandbox_path": str(sandbox_dir),
        "active_sandbox_branch": "janus/sandbox-feat"
    }

    try:
        with patch("src.database.get_pending_modification", return_value=None), \
             patch("src.sandbox_session.get_active_sandbox", return_value=mock_active_sandbox):
            resolved = get_effective_workspace_root()
            assert resolved == sandbox_dir
    finally:
        src.config.ROOT_DIR = orig_root

def test_get_effective_workspace_root_exception_handling(tmp_path):
    """Verify that get_effective_workspace_root falls back to ROOT_DIR if database or session queries fail."""
    orig_root = src.config.ROOT_DIR
    src.config.ROOT_DIR = tmp_path

    try:
        with patch("src.database.get_pending_modification", side_effect=Exception("Database error")), \
             patch("src.sandbox_session.get_active_sandbox", side_effect=Exception("Session error")):
            resolved = get_effective_workspace_root()
            assert resolved == tmp_path
    finally:
        src.config.ROOT_DIR = orig_root
