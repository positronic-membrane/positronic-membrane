from src.sandbox import execute_code_safely


def test_sandbox_safe_execution():
    """Verify that safe Python code compiles and runs, yielding correct stdout."""
    code = """
def fib(n):
    if n <= 1: return n
    return fib(n-1) + fib(n-2)
print("Fib 6 is:", fib(6))
"""
    result = execute_code_safely(code)
    assert "Fib 6 is: 8" in result

def test_sandbox_blocked_imports():
    """Verify that importing banned modules like os or sys is blocked."""
    code_os = "import os; os.system('echo hack')"
    result_os = execute_code_safely(code_os)
    assert "Safety Violation" in result_os
    assert "Import of module 'os'" in result_os

    code_sys = "from sys import exit; exit(1)"
    result_sys = execute_code_safely(code_sys)
    assert "Safety Violation" in result_sys
    assert "Import from module 'sys'" in result_sys

def test_sandbox_blocked_builtins():
    """Verify that dangerous built-in functions like open or exec are blocked."""
    code_open = "open('secret.txt', 'r')"
    result_open = execute_code_safely(code_open)
    assert "Safety Violation" in result_open
    assert "Built-in function 'open'" in result_open

def test_sandbox_blocked_private_attributes():
    """Verify that private attribute access like __globals__ is blocked."""
    code_globals = "print(int.__class__.__globals__)"
    result_globals = execute_code_safely(code_globals)
    assert "Safety Violation" in result_globals
    assert "private attribute '__class__'" in result_globals

def test_sandbox_timeout():
    """Verify that infinite loops are terminated by the timeout limit."""
    code_infinite = """
import time
while True:
    pass
"""
    result_infinite = execute_code_safely(code_infinite, timeout_seconds=1.0)
    assert "Timeout Error" in result_infinite
