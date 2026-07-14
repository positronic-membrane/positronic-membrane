import json
import sqlite3
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

import src.config
from src.agent_sync import (
    _BLOCKER_TEXT_CHAR_LIMIT,
    STATUS_LABELS,
    _parse_status_comment,
    ensure_agent_status_labels,
    poll_agent_status,
)
from src.database import get_connection, init_db
from src.skills import SafeGitHub

REPO = "owner/repo"


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    temp_db = tmp_path / "test_janus_agent_sync.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path


@pytest.fixture(autouse=True)
def github_token(monkeypatch):
    monkeypatch.setattr("src.config.GITHUB_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr("src.config.GITHUB_REPO", REPO)


def _skip_label_bootstrap():
    conn = get_connection()
    conn.execute(
        "UPDATE system_config SET config_value = '1' WHERE config_key = 'agent_sync.labels_ensured';"
    )
    conn.commit()
    conn.close()


def _set_poll_interval(seconds: int):
    conn = get_connection()
    conn.execute(
        "UPDATE system_config SET config_value = ? WHERE config_key = 'agent_sync.poll_interval_seconds';",
        (str(seconds),),
    )
    conn.commit()
    conn.close()


def _urlopen_ctx(data):
    m = MagicMock()
    m.read.return_value = json.dumps(data).encode()
    m.__enter__ = lambda s: s
    m.__exit__ = MagicMock(return_value=False)
    return m


def _mock_urlopen(*payloads):
    contexts = [_urlopen_ctx(p) for p in payloads]
    return patch("urllib.request.urlopen", side_effect=contexts)


def _issue(number):
    return {"number": number}


def _comment(comment_id, login, body, author_association="COLLABORATOR", url="https://example/c"):
    return {
        "id": comment_id,
        "user": {"login": login},
        "body": body,
        "author_association": author_association,
        "html_url": url,
        "created_at": "2026-07-14T00:00:00Z",
    }


def _status_body(status, progress=None, blocker=None, agent=None):
    payload = {"status": status}
    if progress is not None:
        payload["progress"] = progress
    if blocker is not None:
        payload["blocker"] = blocker
    if agent is not None:
        payload["agent"] = agent
    return f"<!-- agent-status\n{json.dumps(payload)}\n-->"


def _empty_label_scan_payloads(exclude_label=None, matching_issue_number=None):
    """One payload per STATUS_LABELS key, in dict order — empty except
    `exclude_label`, which returns `matching_issue_number` if given."""
    payloads = []
    for name in STATUS_LABELS:
        if name == exclude_label and matching_issue_number is not None:
            payloads.append([_issue(matching_issue_number)])
        else:
            payloads.append([])
    return payloads


# ---------------------------------------------------------------------------
# _parse_status_comment
# ---------------------------------------------------------------------------

def test_parse_status_comment_valid():
    body = _status_body("in-progress", progress=60, blocker=None, agent="claude-code-1")
    parsed = _parse_status_comment(body)
    assert parsed == {"status": "in-progress", "progress": 60, "blocker": None, "agent": "claude-code-1"}


def test_parse_status_comment_malformed_json_returns_none():
    assert _parse_status_comment("<!-- agent-status\n{not json}\n-->") is None


def test_parse_status_comment_missing_status_returns_none():
    assert _parse_status_comment("<!-- agent-status\n{\"progress\": 50}\n-->") is None


def test_parse_status_comment_invalid_status_enum_returns_none():
    assert _parse_status_comment(_status_body("frobnicating")) is None


def test_parse_status_comment_no_wrapper_returns_none():
    assert _parse_status_comment("just a normal comment, no status here") is None


def test_parse_status_comment_progress_out_of_range_dropped():
    parsed = _parse_status_comment(_status_body("in-progress", progress=250))
    assert parsed is not None
    assert parsed["progress"] is None


# ---------------------------------------------------------------------------
# poll_agent_status
# ---------------------------------------------------------------------------

def test_poll_agent_status_persists_trusted_comment():
    _skip_label_bootstrap()
    label_payloads = _empty_label_scan_payloads(exclude_label="agent:in-progress", matching_issue_number=70)
    comments_payload = [_comment(1, "agentbot", _status_body("in-progress", progress=60, agent="agentbot"))]

    with _mock_urlopen(*label_payloads, comments_payload):
        result = poll_agent_status()

    assert result["status_updates"] == 1
    assert result["new_blockers"] == 0
    assert result["skipped_untrusted"] == 0

    conn = get_connection()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, progress, github_login FROM agent_work_status WHERE repo = ? AND issue_number = 70;",
        (REPO,),
    ).fetchone()
    conn.close()
    assert row["status"] == "in-progress"
    assert row["progress"] == 60
    assert row["github_login"] == "agentbot"


def test_poll_agent_status_ignores_untrusted_comment_entirely():
    _skip_label_bootstrap()
    label_payloads = _empty_label_scan_payloads(exclude_label="agent:blocked", matching_issue_number=70)
    comments_payload = [
        _comment(1, "randomstranger", _status_body("blocked", blocker="fake"), author_association="NONE")
    ]

    with _mock_urlopen(*label_payloads, comments_payload):
        result = poll_agent_status()

    assert result["status_updates"] == 0
    assert result["new_blockers"] == 0
    assert result["skipped_untrusted"] == 1

    conn = get_connection()
    row_count = conn.execute("SELECT COUNT(*) FROM agent_work_status;").fetchone()[0]
    escalation_count = conn.execute("SELECT COUNT(*) FROM pending_escalations;").fetchone()[0]
    memory_count = conn.execute(
        "SELECT COUNT(*) FROM episodic_memory WHERE message_content LIKE '%randomstranger%';"
    ).fetchone()[0]
    conn.close()
    assert row_count == 0
    assert escalation_count == 0
    assert memory_count == 0


def test_poll_agent_status_transition_to_blocked_enqueues_capped_escalation():
    _skip_label_bootstrap()
    long_blocker = "x" * 900
    label_payloads = _empty_label_scan_payloads(exclude_label="agent:blocked", matching_issue_number=70)
    comments_payload = [_comment(1, "agentbot", _status_body("blocked", blocker=long_blocker))]

    with _mock_urlopen(*label_payloads, comments_payload):
        result = poll_agent_status()

    assert result["new_blockers"] == 1

    conn = get_connection()
    conn.row_factory = sqlite3.Row
    esc = conn.execute("SELECT source, summary, detail FROM pending_escalations;").fetchone()
    conn.close()
    assert esc["source"] == "agent_status_blocked"
    assert "70" in esc["summary"]
    assert '<untrusted-data source="github-issue-comment"' in esc["detail"]
    # blocker text itself must be capped well below the raw 900-char input
    assert len(esc["detail"]) < 900 + 200
    assert "x" * _BLOCKER_TEXT_CHAR_LIMIT in esc["detail"]
    assert "x" * (_BLOCKER_TEXT_CHAR_LIMIT + 1) not in esc["detail"]


def test_poll_agent_status_non_blocked_transition_does_not_escalate():
    _skip_label_bootstrap()
    label_payloads = _empty_label_scan_payloads(exclude_label="agent:review-ready", matching_issue_number=70)
    comments_payload = [_comment(1, "agentbot", _status_body("review-ready"))]

    with _mock_urlopen(*label_payloads, comments_payload):
        result = poll_agent_status()

    assert result["status_updates"] == 1
    assert result["new_blockers"] == 0
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM pending_escalations;").fetchone()[0]
    conn.close()
    assert count == 0


def test_poll_agent_status_tracks_multiple_commenters_on_same_issue():
    """Regression: agent_work_status is keyed on (repo, issue_number,
    github_login), so two different trusted agents commenting on the same
    issue must each get their own persisted row — an earlier trusted comment
    from agent-a must not be discarded just because agent-b commented more
    recently on the same issue."""
    _skip_label_bootstrap()
    label_payloads = _empty_label_scan_payloads(exclude_label="agent:in-progress", matching_issue_number=70)
    comments_payload = [
        _comment(1, "agent-a", _status_body("in-progress", agent="agent-a")),
        _comment(2, "agent-b", _status_body("in-progress", agent="agent-b")),
    ]

    with _mock_urlopen(*label_payloads, comments_payload):
        result = poll_agent_status()

    assert result["status_updates"] == 2

    conn = get_connection()
    logins = {
        row[0]
        for row in conn.execute("SELECT github_login FROM agent_work_status WHERE issue_number = 70;").fetchall()
    }
    conn.close()
    assert logins == {"agent-a", "agent-b"}


def test_poll_agent_status_dedupes_same_comment_on_repoll():
    _skip_label_bootstrap()
    _set_poll_interval(0)
    label_payloads = _empty_label_scan_payloads(exclude_label="agent:in-progress", matching_issue_number=70)
    comments_payload = [_comment(1, "agentbot", _status_body("in-progress", progress=10))]

    with _mock_urlopen(*label_payloads, comments_payload):
        first = poll_agent_status()
    assert first["status_updates"] == 1

    label_payloads_2 = _empty_label_scan_payloads(exclude_label="agent:in-progress", matching_issue_number=70)
    with _mock_urlopen(*label_payloads_2, comments_payload):
        second = poll_agent_status()
    assert second["status_updates"] == 0


def test_poll_agent_status_throttles_second_call():
    _skip_label_bootstrap()
    label_payloads = _empty_label_scan_payloads()

    with _mock_urlopen(*label_payloads):
        first = poll_agent_status()
    assert "skipped" not in first

    with patch("urllib.request.urlopen") as mock_urlopen:
        second = poll_agent_status()
        mock_urlopen.assert_not_called()
    assert second == {"skipped": "throttled"}


# ---------------------------------------------------------------------------
# ensure_agent_status_labels
# ---------------------------------------------------------------------------

def test_ensure_agent_status_labels_creates_all_once():
    gh = SafeGitHub(party_id="system")
    payloads = [{"name": name} for name in STATUS_LABELS]
    with _mock_urlopen(*payloads):
        ok = ensure_agent_status_labels(gh, REPO)
    assert ok is True

    conn = get_connection()
    flag = conn.execute(
        "SELECT config_value FROM system_config WHERE config_key = 'agent_sync.labels_ensured';"
    ).fetchone()[0]
    conn.close()
    assert flag == "1"


def test_ensure_agent_status_labels_backs_off_after_permanent_failure():
    """Regression: a token permanently lacking label-write permission (e.g.
    403 Forbidden) must not retry all 4 label creations on every single poll
    forever — that would burn most of the shared 50/hr GitHub API budget on
    calls that can never succeed. The second attempt within the backoff
    window must make zero API calls."""
    gh = SafeGitHub(party_id="system")

    def _raise_403(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="https://api.github.com/repos/owner/repo/labels", code=403,
            msg="Forbidden", hdrs=None, fp=None,
        )

    with patch("urllib.request.urlopen", side_effect=_raise_403):
        first = ensure_agent_status_labels(gh, REPO)
    assert first is False

    conn = get_connection()
    flag = conn.execute(
        "SELECT config_value FROM system_config WHERE config_key = 'agent_sync.labels_ensured';"
    ).fetchone()[0]
    conn.close()
    assert flag == "0"

    with patch("urllib.request.urlopen") as mock_urlopen:
        second = ensure_agent_status_labels(gh, REPO)
        mock_urlopen.assert_not_called()
    assert second is False


def test_ensure_agent_status_labels_noop_once_flag_set():
    _skip_label_bootstrap()
    gh = SafeGitHub(party_id="system")
    with patch("urllib.request.urlopen") as mock_urlopen:
        ok = ensure_agent_status_labels(gh, REPO)
        mock_urlopen.assert_not_called()
    assert ok is True


def test_ensure_agent_status_labels_swallows_duplicate_label_error():
    gh = SafeGitHub(party_id="system")

    def _raise_422(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="https://api.github.com/repos/owner/repo/labels", code=422,
            msg="Unprocessable Entity", hdrs=None, fp=None,
        )

    with patch("urllib.request.urlopen", side_effect=_raise_422):
        ok = ensure_agent_status_labels(gh, REPO)
    assert ok is True

    conn = get_connection()
    flag = conn.execute(
        "SELECT config_value FROM system_config WHERE config_key = 'agent_sync.labels_ensured';"
    ).fetchone()[0]
    conn.close()
    assert flag == "1"
