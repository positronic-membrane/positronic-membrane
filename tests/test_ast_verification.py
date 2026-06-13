import pytest
from unittest.mock import patch, MagicMock
from src.self_modification import validate_python_ast, stage_and_test, stage_and_test_multi

def test_validate_python_ast_valid():
    code = "def test_func():\n    return 42\n"
    valid, err = validate_python_ast(code)
    assert valid is True
    assert err is None

def test_validate_python_ast_invalid():
    code = "def test_func(\n"
    valid, err = validate_python_ast(code)
    assert valid is False
    assert "SyntaxError" in err
    assert "line 1" in err

def test_validate_python_ast_indentation_error():
    # Indentation errors also raise SyntaxError subclasses
    code = "def test_func():\nreturn 42\n"
    valid, err = validate_python_ast(code)
    assert valid is False
    assert "SyntaxError" in err

@patch("src.self_modification.copy_project_structure")
@patch("src.self_modification.subprocess.run")
def test_stage_and_test_ast_fail_early(mock_run, mock_copy):
    """Verify that stage_and_test skips staging setup and pytest if AST validation fails."""
    rel_path = "src/utils.py"
    proposed = "def broken_syntax(\n"
    
    passed, logs, temp_dir = stage_and_test(rel_path, proposed)
    
    assert passed is False
    assert "AST Verification Failed" in logs
    assert "SyntaxError" in logs
    assert temp_dir == ""
    
    # Assert staging operations were bypassed completely
    mock_copy.assert_not_called()
    mock_run.assert_not_called()

@patch("src.self_modification.copy_project_structure")
@patch("src.self_modification.subprocess.run")
def test_stage_and_test_multi_ast_fail_early(mock_run, mock_copy):
    """Verify that stage_and_test_multi skips staging setup and pytest if any file has AST failure."""
    modifications = {
        "src/valid.py": "def fine(): pass\n",
        "src/invalid.py": "def broken(\n"
    }
    
    passed, logs, temp_dir = stage_and_test_multi(modifications)
    
    assert passed is False
    assert "AST Verification Failed" in logs
    assert "File: src/invalid.py" in logs
    assert temp_dir == ""
    
    # Assert staging operations were bypassed completely
    mock_copy.assert_not_called()
    mock_run.assert_not_called()


def test_validate_python_ast_unsafe_imports():
    """Verify that importing forbidden modules is caught by AST validation."""
    code_sub = "import subprocess\n"
    valid, err = validate_python_ast(code_sub)
    assert valid is False
    assert "Security Violation: Import of unsafe module 'subprocess'" in err

    code_sub_from = "from subprocess import Popen\n"
    valid, err = validate_python_ast(code_sub_from)
    assert valid is False
    assert "Security Violation: Import from unsafe module 'subprocess'" in err


def test_validate_python_ast_unsafe_calls():
    """Verify that unsafe function and system calls are rejected."""
    code_eval = "eval('1+1')\n"
    valid, err = validate_python_ast(code_eval)
    assert valid is False
    assert "Security Violation: Call to unsafe function 'eval'" in err

    code_exec = "exec('x = 5')\n"
    valid, err = validate_python_ast(code_exec)
    assert valid is False
    assert "Security Violation: Call to unsafe function 'exec'" in err

    code_system = "import os\nos.system('ls')\n"
    valid, err = validate_python_ast(code_system)
    assert valid is False
    assert "Security Violation: Calling os.system is forbidden." in err


def test_validate_python_ast_safe_os_calls():
    """Verify that safe os modules operations like os.path are allowed."""
    code_safe = "import os\nif os.path.exists('foo'):\n    os.makedirs('bar')\n"
    valid, err = validate_python_ast(code_safe)
    assert valid is True
    assert err is None
