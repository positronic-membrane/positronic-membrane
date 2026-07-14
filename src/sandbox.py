import ast
import io
import logging
import multiprocessing
import sys

logger = logging.getLogger("JanusSandbox")

class SafetyAuditor(ast.NodeVisitor):
    def __init__(self):
        self.errors = []
        self.banned_modules = {
            "os", "sys", "subprocess", "socket", "shutil", "urllib",
            "requests", "ctypes", "platform", "importlib", "builtins"
        }

    def visit_Import(self, node):
        for name in node.names:
            root_module = name.name.split('.')[0]
            if root_module in self.banned_modules:
                self.errors.append(f"Import of module '{name.name}' is strictly forbidden.")
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module:
            root_module = node.module.split('.')[0]
            if root_module in self.banned_modules:
                self.errors.append(f"Import from module '{node.module}' is strictly forbidden.")
        self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name):
            if node.func.id in ("eval", "exec", "open", "getattr", "setattr", "compile"):
                self.errors.append(f"Built-in function '{node.func.id}' is strictly forbidden.")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr.startswith("__"):
            self.errors.append(f"Access to private attribute '{node.attr}' is strictly forbidden.")
        self.generic_visit(node)

def _run_restricted_code(code_str: str, pipe):
    """Worker function executed in isolated process context."""
    # Redirect stdout
    old_stdout = sys.stdout
    redirected = io.StringIO()
    sys.stdout = redirected

    import datetime
    import json
    import math
    import random
    import re
    import time

    # Safe import hook
    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        allowed_modules = {
            "math", "json", "re", "datetime", "time", "random",
            "collections", "itertools", "functools"
        }
        if name in allowed_modules:
            return __import__(name, globals, locals, fromlist, level)
        raise ImportError(f"Import of module '{name}' is not allowed in this sandbox.")

    safe_builtins = {
        "__import__": safe_import,
        "abs": abs, "all": all, "any": any, "bin": bin, "bool": bool,
        "chr": chr, "dict": dict, "dir": dir, "divmod": divmod,
        "enumerate": enumerate, "filter": filter, "float": float,
        "format": format, "frozenset": frozenset, "hash": hash,
        "hex": hex, "id": id, "int": int, "isinstance": isinstance,
        "issubclass": issubclass, "iter": iter, "len": len, "list": list,
        "map": map, "max": max, "min": min, "next": next, "object": object,
        "oct": oct, "ord": ord, "pow": pow, "print": print, "range": range,
        "repr": repr, "reversed": reversed, "round": round, "set": set,
        "slice": slice, "sorted": sorted, "str": str, "sum": sum,
        "tuple": tuple, "type": type, "zip": zip,
        "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
        "KeyError": KeyError, "IndexError": IndexError, "RuntimeError": RuntimeError,
        "ImportError": ImportError
    }

    safe_globals = {
        "__builtins__": safe_builtins,
        "math": math,
        "json": json,
        "re": re,
        "datetime": datetime,
        "time": time,
        "random": random
    }

    try:
        # Execute using safe_globals as both globals and locals to support recursion
        exec(code_str, safe_globals, safe_globals)
        output = redirected.getvalue()
        pipe.send((True, output))
    except Exception as e:
        pipe.send((False, f"Runtime Error: {e}"))
    finally:
        sys.stdout = old_stdout

def execute_code_safely(python_code: str, timeout_seconds: float = 3.0) -> str:
    """
    Validates Python code structure using AST, then compiles and executes it
    in an isolated subprocess context with a maximum timeout and restricted namespace.
    """
    logger.info("Sandbox auditing code block...")

    try:
        tree = ast.parse(python_code)
    except SyntaxError as se:
        return f"Syntax Error: {se}"

    # Perform AST Audit
    auditor = SafetyAuditor()
    auditor.visit(tree)
    if auditor.errors:
        logger.warning(f"Sandbox blocked code execution due to {len(auditor.errors)} errors.")
        return "Safety Violation:\n" + "\n".join([f"- {err}" for err in auditor.errors])

    # Set up process communication channel
    parent_conn, child_conn = multiprocessing.Pipe()

    # Run code execution in isolated process
    process = multiprocessing.Process(
        target=_run_restricted_code,
        args=(python_code, child_conn),
        daemon=True
    )
    process.start()

    # Wait with timeout
    process.join(timeout=timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join()
        logger.warning(f"Sandbox execution killed after exceeding timeout of {timeout_seconds}s.")
        return f"Timeout Error: Execution exceeded time limit of {timeout_seconds} seconds."

    if parent_conn.poll():
        success, result = parent_conn.recv()
        return result
    else:
        return "Execution Error: Subprocess terminated unexpectedly without returning output."
