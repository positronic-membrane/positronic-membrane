def manage_sandbox(action, session_name=None):
    from src.sandbox_session import create_sandbox_session, run_sandbox_tests, ship_sandbox_session, abort_sandbox_session
    if action == "start":
        if not session_name:
            raise ValueError("session_name is required to start a sandbox.")
        path, branch = create_sandbox_session(session_name)
        return f"Sandbox spawned successfully at: {path} (Branch: {branch})"
    elif action == "test":
        passed, logs = run_sandbox_tests()
        status = "PASSED" if passed else "FAILED"
        return f"Sandbox test suite run completed: {status}.\nLogs:\n{logs}"
    elif action == "ship":
        copied = ship_sandbox_session()
        return f"Sandbox shipped and applied to active workspace. Files modified: {copied}"
    elif action == "abort":
        abort_sandbox_session()
        return "Sandbox session aborted and discarded."
    else:
        raise ValueError(f"Unknown sandbox action: {action}")
