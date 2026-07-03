import json
import time
from unittest.mock import MagicMock, patch

import pytest

from src.database import get_connection
from src.skills import SafeGitHub

REPO = "owner/repo"


def _urlopen_ctx(data):
    """Return a mock context manager yielding a fake urllib response."""
    m = MagicMock()
    m.read.return_value = json.dumps(data).encode()
    m.__enter__ = lambda s: s
    m.__exit__ = MagicMock(return_value=False)
    return m


@pytest.fixture(autouse=True)
def github_token(monkeypatch):
    monkeypatch.setattr("src.config.GITHUB_ACCESS_TOKEN", "test-token")


# ---------------------------------------------------------------------------
# Endpoint correctness
# ---------------------------------------------------------------------------

def test_create_issue_posts_to_correct_endpoint():
    gh = SafeGitHub(party_id="system")
    with patch("urllib.request.urlopen", return_value=_urlopen_ctx({"number": 42})) as mock_open:
        result = gh.create_issue(REPO, "New bug", "Some body")
    req = mock_open.call_args[0][0]
    assert req.full_url == f"https://api.github.com/repos/{REPO}/issues"
    assert req.method == "POST"
    payload = json.loads(req.data)
    assert payload["title"] == "New bug"
    assert result["number"] == 42


def test_add_comment_targets_correct_endpoint():
    gh = SafeGitHub(party_id="system")
    with patch("urllib.request.urlopen", return_value=_urlopen_ctx({"id": 99})) as mock_open:
        result = gh.add_comment(REPO, 7, "Nice work")
    req = mock_open.call_args[0][0]
    assert req.full_url == f"https://api.github.com/repos/{REPO}/issues/7/comments"
    assert req.method == "POST"
    assert result["id"] == 99


def test_list_open_issues_uses_get():
    gh = SafeGitHub(party_id="system")
    with patch("urllib.request.urlopen", return_value=_urlopen_ctx([{"number": 1}])) as mock_open:
        result = gh.list_open_issues(REPO)
    req = mock_open.call_args[0][0]
    assert f"/repos/{REPO}/issues?state=open&per_page=100" in req.full_url
    assert req.method == "GET"
    assert result == [{"number": 1}]


def test_get_issue_uses_get():
    gh = SafeGitHub(party_id="system")
    with patch("urllib.request.urlopen", return_value=_urlopen_ctx({"number": 5})) as mock_open:
        result = gh.get_issue(REPO, 5)
    req = mock_open.call_args[0][0]
    assert req.full_url == f"https://api.github.com/repos/{REPO}/issues/5"
    assert req.method == "GET"
    assert result["number"] == 5


def test_create_pr_posts_to_correct_endpoint():
    gh = SafeGitHub(party_id="system")
    with patch("urllib.request.urlopen", return_value=_urlopen_ctx({"number": 10})) as mock_open:
        result = gh.create_pr(REPO, "My PR", "body text", "feature-branch")
    req = mock_open.call_args[0][0]
    assert req.full_url == f"https://api.github.com/repos/{REPO}/pulls"
    assert req.method == "POST"
    payload = json.loads(req.data)
    assert payload["head"] == "feature-branch"
    assert payload["base"] == "main"
    assert result["number"] == 10


def test_update_issue_patches_only_provided_fields():
    gh = SafeGitHub(party_id="system")
    with patch("urllib.request.urlopen", return_value=_urlopen_ctx({"number": 8})) as mock_open:
        result = gh.update_issue(REPO, 8, title="New title", labels=["bug"])
    req = mock_open.call_args[0][0]
    assert req.full_url == f"https://api.github.com/repos/{REPO}/issues/8"
    assert req.method == "PATCH"
    payload = json.loads(req.data)
    assert payload == {"title": "New title", "labels": ["bug"]}
    assert result["number"] == 8


def test_update_issue_accepts_state_change():
    gh = SafeGitHub(party_id="system")
    with patch("urllib.request.urlopen", return_value=_urlopen_ctx({"state": "closed"})) as mock_open:
        gh.update_issue(REPO, 8, state="closed")
    payload = json.loads(mock_open.call_args[0][0].data)
    assert payload == {"state": "closed"}


def test_update_issue_rejects_invalid_state():
    gh = SafeGitHub(party_id="system")
    with pytest.raises(ValueError, match="open"):
        gh.update_issue(REPO, 8, state="reopened")


def test_update_issue_requires_at_least_one_field():
    gh = SafeGitHub(party_id="system")
    with pytest.raises(ValueError, match="at least one"):
        gh.update_issue(REPO, 8)


def test_create_label_posts_to_correct_endpoint():
    gh = SafeGitHub(party_id="system")
    with patch("urllib.request.urlopen", return_value=_urlopen_ctx({"name": "triage"})) as mock_open:
        result = gh.create_label(REPO, "triage", color="#ff0000", description="Needs triage")
    req = mock_open.call_args[0][0]
    assert req.full_url == f"https://api.github.com/repos/{REPO}/labels"
    assert req.method == "POST"
    payload = json.loads(req.data)
    assert payload == {"name": "triage", "color": "ff0000", "description": "Needs triage"}
    assert result["name"] == "triage"


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------

def _insert_party(conn, party_id: str, role: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO parties (id, name, role) VALUES (?, ?, ?)",
        (party_id, f"Test {party_id}", role),
    )
    conn.commit()


def test_close_issue_blocked_for_user_role():
    conn = get_connection()
    try:
        _insert_party(conn, "u_close", "user")
    finally:
        conn.close()

    gh = SafeGitHub(party_id="u_close")
    with pytest.raises(PermissionError, match="contributor"):
        gh.close_issue(REPO, 31)


def test_create_pr_blocked_for_user_role():
    conn = get_connection()
    try:
        _insert_party(conn, "u_pr", "user")
    finally:
        conn.close()

    gh = SafeGitHub(party_id="u_pr")
    with pytest.raises(PermissionError, match="contributor"):
        gh.create_pr(REPO, "My PR", "body", "feature-branch")


def test_update_issue_blocked_for_user_role():
    conn = get_connection()
    try:
        _insert_party(conn, "u_update", "user")
    finally:
        conn.close()

    gh = SafeGitHub(party_id="u_update")
    with pytest.raises(PermissionError, match="contributor"):
        gh.update_issue(REPO, 8, title="hijacked")


def test_create_label_blocked_for_user_role():
    conn = get_connection()
    try:
        _insert_party(conn, "u_label", "user")
    finally:
        conn.close()

    gh = SafeGitHub(party_id="u_label")
    with pytest.raises(PermissionError, match="contributor"):
        gh.create_label(REPO, "triage")


def test_close_issue_allowed_for_contributor():
    conn = get_connection()
    try:
        _insert_party(conn, "contrib1", "contributor")
    finally:
        conn.close()

    gh = SafeGitHub(party_id="contrib1")
    with patch("urllib.request.urlopen", return_value=_urlopen_ctx({"state": "closed"})):
        result = gh.close_issue(REPO, 31)
    assert result["state"] == "closed"


def test_close_issue_blocked_for_no_party():
    gh = SafeGitHub(party_id=None)
    with pytest.raises(PermissionError, match="contributor"):
        gh.close_issue(REPO, 31)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def _set_rate_state(calls: int, window_start: float) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable) "
            "VALUES ('github.api_calls_this_hour', ?, 0)",
            (str(calls),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO system_config (config_key, config_value, is_agent_modifiable) "
            "VALUES ('github.rate_limit_window_start', ?, 0)",
            (str(window_start),),
        )
        conn.commit()
    finally:
        conn.close()


def test_rate_limit_blocks_at_threshold():
    _set_rate_state(calls=50, window_start=time.time())

    gh = SafeGitHub(party_id="system")
    with pytest.raises(RuntimeError, match="rate limit"):
        gh.list_open_issues(REPO)


def test_rate_limit_resets_after_window():
    _set_rate_state(calls=50, window_start=time.time() - 4000)

    gh = SafeGitHub(party_id="system")
    with patch("urllib.request.urlopen", return_value=_urlopen_ctx([])):
        result = gh.list_open_issues(REPO)
    assert result == []

    # Counter must have been reset and incremented to 1
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT config_value FROM system_config WHERE config_key='github.api_calls_this_hour'"
        ).fetchone()
    finally:
        conn.close()
    assert int(row[0]) == 1


def test_rate_limit_counter_increments():
    _set_rate_state(calls=3, window_start=time.time())

    gh = SafeGitHub(party_id="system")
    with patch("urllib.request.urlopen", return_value=_urlopen_ctx([])):
        gh.list_open_issues(REPO)

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT config_value FROM system_config WHERE config_key='github.api_calls_this_hour'"
        ).fetchone()
    finally:
        conn.close()
    assert int(row[0]) == 4


# ---------------------------------------------------------------------------
# Token guard
# ---------------------------------------------------------------------------

def test_missing_token_raises(monkeypatch):
    monkeypatch.setattr("src.config.GITHUB_ACCESS_TOKEN", "")
    monkeypatch.setattr("src.config.GITHUB_PM_TOKEN", "")
    gh = SafeGitHub(party_id="system")
    with pytest.raises(PermissionError, match="GITHUB_ACCESS_TOKEN"):
        gh.list_open_issues(REPO)


# ---------------------------------------------------------------------------
# HTTP error surfacing
# ---------------------------------------------------------------------------

def _http_error(code, reason, body):
    import io
    import urllib.error
    return urllib.error.HTTPError(
        url="https://api.github.com/x", code=code, msg=reason, hdrs=None,
        fp=io.BytesIO(body),
    )


def test_http_error_includes_response_body():
    gh = SafeGitHub(party_id="system")
    err = _http_error(403, "Forbidden", b'{"message": "Resource not accessible by personal access token"}')
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(RuntimeError, match="Resource not accessible"):
            gh.create_issue(REPO, "New bug")


def test_http_error_body_truncated_to_500_chars():
    gh = SafeGitHub(party_id="system")
    err = _http_error(422, "Unprocessable Entity", b"x" * 2000)
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(RuntimeError) as exc_info:
            gh.get_issue(REPO, 1)
    assert "x" * 500 in str(exc_info.value)
    assert "x" * 501 not in str(exc_info.value)


def test_http_error_without_body_still_raises():
    gh = SafeGitHub(party_id="system")
    err = _http_error(404, "Not Found", b"")
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(RuntimeError, match="GitHub API error 404 .* Not Found$"):
            gh.get_issue(REPO, 1)


# ---------------------------------------------------------------------------
# Token routing
# ---------------------------------------------------------------------------

def test_pm_token_used_for_non_restricted_repo(monkeypatch):
    monkeypatch.setattr("src.config.GITHUB_PM_TOKEN", "pm-token")
    monkeypatch.setattr("src.config.GITHUB_READONLY_REPOS", [])
    gh = SafeGitHub(party_id="system")
    with patch("urllib.request.urlopen", return_value=_urlopen_ctx([])) as mock_open:
        gh.list_open_issues("other/repo")
    req = mock_open.call_args[0][0]
    assert req.get_header("Authorization") == "Bearer pm-token"


def test_access_token_used_for_restricted_repo(monkeypatch):
    monkeypatch.setattr("src.config.GITHUB_PM_TOKEN", "pm-token")
    monkeypatch.setattr("src.config.GITHUB_READONLY_REPOS", [REPO])
    gh = SafeGitHub(party_id="system")
    with patch("urllib.request.urlopen", return_value=_urlopen_ctx([])) as mock_open:
        gh.list_open_issues(REPO)
    req = mock_open.call_args[0][0]
    assert req.get_header("Authorization") == "Bearer test-token"


def test_access_token_used_when_pm_token_absent(monkeypatch):
    monkeypatch.setattr("src.config.GITHUB_PM_TOKEN", "")
    monkeypatch.setattr("src.config.GITHUB_READONLY_REPOS", [])
    gh = SafeGitHub(party_id="system")
    with patch("urllib.request.urlopen", return_value=_urlopen_ctx([])) as mock_open:
        gh.list_open_issues(REPO)
    req = mock_open.call_args[0][0]
    assert req.get_header("Authorization") == "Bearer test-token"


# ---------------------------------------------------------------------------
# create_repo
# ---------------------------------------------------------------------------

def test_create_repo_defaults_to_org_from_config(monkeypatch):
    monkeypatch.setattr("src.config.GITHUB_REPO", "some-org/some-repo")
    gh = SafeGitHub(party_id="system")
    with patch("urllib.request.urlopen", return_value=_urlopen_ctx({"name": "new-repo", "full_name": "some-org/new-repo"})) as mock_open:
        result = gh.create_repo("new-repo", description="A test repo")
    req = mock_open.call_args[0][0]
    assert req.full_url == "https://api.github.com/orgs/some-org/repos"
    assert req.method == "POST"
    payload = json.loads(req.data)
    assert payload["name"] == "new-repo"
    assert payload["private"] is True
    assert payload["auto_init"] is True
    assert result["full_name"] == "some-org/new-repo"


def test_create_repo_explicit_org_overrides_config(monkeypatch):
    monkeypatch.setattr("src.config.GITHUB_REPO", "some-org/some-repo")
    gh = SafeGitHub(party_id="system")
    with patch("urllib.request.urlopen", return_value=_urlopen_ctx({"full_name": "other-org/new-repo"})) as mock_open:
        gh.create_repo("new-repo", org="other-org")
    req = mock_open.call_args[0][0]
    assert req.full_url == "https://api.github.com/orgs/other-org/repos"


def test_create_repo_falls_back_to_user_repos_without_org(monkeypatch):
    monkeypatch.setattr("src.config.GITHUB_REPO", "")
    gh = SafeGitHub(party_id="system")
    with patch("urllib.request.urlopen", return_value=_urlopen_ctx({"full_name": "pm/new-repo"})) as mock_open:
        gh.create_repo("new-repo", private=False)
    req = mock_open.call_args[0][0]
    assert req.full_url == "https://api.github.com/user/repos"
    payload = json.loads(req.data)
    assert payload["private"] is False


def test_create_repo_empty_org_forces_user_repos(monkeypatch):
    monkeypatch.setattr("src.config.GITHUB_REPO", "some-org/some-repo")
    gh = SafeGitHub(party_id="system")
    with patch("urllib.request.urlopen", return_value=_urlopen_ctx({"full_name": "pm/new-repo"})) as mock_open:
        gh.create_repo("new-repo", org="")
    req = mock_open.call_args[0][0]
    assert req.full_url == "https://api.github.com/user/repos"


def test_create_repo_blocked_for_user_role():
    conn = get_connection()
    try:
        conn.execute("INSERT OR REPLACE INTO parties (id, name, role) VALUES (?, ?, ?)", ("u_repo", "Test", "user"))
        conn.commit()
    finally:
        conn.close()
    gh = SafeGitHub(party_id="u_repo")
    with pytest.raises(PermissionError, match="contributor"):
        gh.create_repo("new-repo")
