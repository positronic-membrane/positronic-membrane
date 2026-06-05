import pytest
from pathlib import Path
from src.external_context_loader import ExternalContextLoader

def test_external_context_loader_success(tmp_path):
    """Verify that a valid text file is successfully ingested and formatted."""
    # Create a dummy source file
    source_file = tmp_path / "my_source.txt"
    source_file.write_text("Hello from sandbox source!", encoding="utf-8")
    
    loader = ExternalContextLoader(workspace_root=tmp_path)
    context = loader.ingest_file(
        source_path="my_source.txt",
        target_relative_path="my_target.txt"
    )
    
    assert context["status"] == "success"
    assert context["target_path"] == "my_target.txt"
    assert context["content"] == "Hello from sandbox source!"
    assert context["size_bytes"] == len("Hello from sandbox source!".encode("utf-8"))
    assert context["content_type"] == "text/plain"

def test_external_context_loader_file_not_found(tmp_path):
    """Verify that loader returns a structured error when source file does not exist."""
    loader = ExternalContextLoader(workspace_root=tmp_path)
    context = loader.ingest_file(
        source_path="nonexistent.txt",
        target_relative_path="target.txt"
    )
    
    assert context["status"] == "error"
    assert "Source file not found" in context["message"]

def test_external_context_loader_binary_file(tmp_path):
    """Verify that loader rejects non-UTF-8 binary files gracefully."""
    # Create a non-UTF8 binary file
    binary_file = tmp_path / "binary.dat"
    with open(binary_file, "wb") as f:
        f.write(b"\x80\x81\x82\x83")
        
    loader = ExternalContextLoader(workspace_root=tmp_path)
    context = loader.ingest_file(
        source_path="binary.dat",
        target_relative_path="target.dat"
    )
    
    assert context["status"] == "error"
    assert "File is not valid UTF-8 text" in context["message"]
