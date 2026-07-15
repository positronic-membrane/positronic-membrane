"""Unit coverage for benchmarks.run.run_layer1(), with the `pytest -m e2e`
subprocess call mocked out -- actually invoking it would recursively spawn
pytest from inside a pytest run. Not marked e2e; runs under the default
suite."""
import os
from unittest.mock import MagicMock, patch

from benchmarks.run import run_layer1


def _proc_result(returncode):
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = ""
    proc.stderr = ""
    return proc


def test_run_layer1_e2e_pass_conformance_not_available(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("subprocess.run", return_value=_proc_result(0)) as mock_run:
        result = run_layer1()

    assert result == {"e2e_suite": "pass", "conformance_suite": "not_available"}
    mock_run.assert_called_once()


def test_run_layer1_e2e_fail():
    with patch("subprocess.run", return_value=_proc_result(1)):
        result = run_layer1()

    assert result["e2e_suite"] == "fail"


def test_run_layer1_detects_conformance_suite_when_present(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs(tmp_path / "tests" / "conformance")

    with patch("subprocess.run", side_effect=[_proc_result(0), _proc_result(0)]) as mock_run:
        result = run_layer1()

    assert result == {"e2e_suite": "pass", "conformance_suite": "pass"}
    assert mock_run.call_count == 2


def test_run_layer1_conformance_suite_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs(tmp_path / "tests" / "conformance")

    with patch("subprocess.run", side_effect=[_proc_result(0), _proc_result(1)]):
        result = run_layer1()

    assert result == {"e2e_suite": "pass", "conformance_suite": "fail"}
