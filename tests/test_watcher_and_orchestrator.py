import os
import time
import json
import pytest
import shutil
from pathlib import Path
import src.config
from src.watcher import DirectoryWatcher
from src.memory import orchestrate_workspace_snapshot

@pytest.fixture
def temp_workspace(tmp_path):
    """Setup temporary workspace with dummy code files."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    src_dir = workspace / "src"
    src_dir.mkdir()
    (src_dir / "calc.py").write_text("def add(a, b): return a + b\n")
    
    orig_root = src.config.ROOT_DIR
    src.config.ROOT_DIR = workspace
    yield workspace
    src.config.ROOT_DIR = orig_root

def test_directory_watcher_detects_changes(temp_workspace):
    watcher = DirectoryWatcher(str(temp_workspace))
    
    # Capture initial state
    initial_state = watcher._get_state()
    assert str(temp_workspace / "src" / "calc.py") in initial_state
    
    # 1. Test Add file
    new_file = temp_workspace / "src" / "new.py"
    new_file.write_text("def hello(): pass\n")
    
    # 2. Test Modify file
    (temp_workspace / "src" / "calc.py").write_text("def add(a, b): return a + b + 1\n")
    # Update modified time to be distinct
    os.utime(temp_workspace / "src" / "calc.py", (time.time() + 10, time.time() + 10))
    
    # Get changes by simulating watch check
    current_state = watcher._get_state()
    added = set(current_state.keys()) - set(initial_state.keys())
    removed = set(initial_state.keys()) - set(current_state.keys())
    modified = {
        k for k in set(current_state.keys()) & set(initial_state.keys())
        if current_state[k] != initial_state[k]
    }
    
    assert str(new_file) in added
    assert str(temp_workspace / "src" / "calc.py") in modified
    assert len(removed) == 0

def test_directory_watcher_ignores_restricted_dirs(temp_workspace):
    watcher = DirectoryWatcher(str(temp_workspace))
    
    # Create ignored folders
    git_dir = temp_workspace / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("dummy git config\n")
    
    snapshot_dir = temp_workspace / ".janus_snapshots"
    snapshot_dir.mkdir()
    (snapshot_dir / "snapshot.json").write_text("{}\n")
    
    state = watcher._get_state()
    assert str(git_dir / "config") not in state
    assert str(snapshot_dir / "snapshot.json") not in state

def test_orchestrator_creates_snapshots(temp_workspace):
    # Setup mock changes dictionary
    calc_path = temp_workspace / "src" / "calc.py"
    changes = {
        "added": [],
        "removed": [],
        "modified": [str(calc_path)]
    }
    
    # Run snapshot orchestration
    orchestrate_workspace_snapshot(changes)
    
    snapshot_dir = temp_workspace / ".janus_snapshots"
    assert snapshot_dir.exists()
    
    snapshots = list(snapshot_dir.glob("snapshot_*.json"))
    assert len(snapshots) == 1
    
    # Read snapshot and verify structure
    with open(snapshots[0], "r", encoding="utf-8") as f:
        data = json.load(f)
        
    assert "timestamp" in data
    assert "changes" in data
    assert data["changes"]["modified"] == ["src/calc.py"]
    assert "contents" in data
    assert data["contents"]["src/calc.py"] == "def add(a, b): return a + b\n"