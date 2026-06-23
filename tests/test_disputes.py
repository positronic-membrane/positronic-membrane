from unittest.mock import patch

import pytest

import src.config
from src.database import (
    get_connection,
    get_consecutive_critic_vetoes,
    get_dispute,
    get_open_disputes,
    init_db,
    log_deliberation,
    resolve_dispute,
)


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB settings for testing."""
    temp_db = tmp_path / "test_janus_disputes.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path


def _veto(action="modify_code: src/foo.py", justification="Violates constitution rule X"):
    log_deliberation(
        proposed_action=action,
        debate_json={"proposer_output": "x", "critic_output": "y"},
        critic_decision=0,
        utility_score=0.0,
        justification=justification,
    )


def _approve(action="scan_workspace"):
    log_deliberation(
        proposed_action=action,
        debate_json={"proposer_output": "x", "critic_output": "y"},
        critic_decision=1,
        utility_score=1.0,
        justification="Safe and compliant",
    )


def _dispute_paused() -> bool:
    conn = get_connection(read_only_constitution=True)
    row = conn.execute("SELECT config_value FROM system_config WHERE config_key = 'dispute_paused';").fetchone()
    conn.close()
    return row[0] == "true"


@patch("src.notifications.send_webhook_notification")
def test_three_consecutive_vetoes_create_open_dispute_and_pause(mock_webhook):
    """3 consecutive Critic vetoes must log a swarm_disputes row, pause the loop, and fire a webhook."""
    _veto("modify_code: src/foo.py", "Violates rule A")
    _veto("modify_code: src/foo.py", "Violates rule A")
    assert not _dispute_paused()

    _veto("modify_code: src/foo.py", "Violates rule A")
    assert _dispute_paused()

    disputes = get_open_disputes()
    assert len(disputes) == 1
    assert disputes[0]["proposed_action"] == "modify_code: src/foo.py"
    assert disputes[0]["veto_count"] == 3
    assert disputes[0]["status"] == "open"

    dispute_webhook_calls = [c for c in mock_webhook.call_args_list if c[0][0] == "dispute_detected"]
    assert len(dispute_webhook_calls) == 1


@patch("src.notifications.send_webhook_notification")
def test_dispute_transcript_captures_consecutive_vetoes(mock_webhook):
    """The dispute's debate_transcript must contain exactly the 3 consecutive vetoed entries, in order."""
    _veto("action_1", "reason 1")
    _veto("action_2", "reason 2")
    _veto("action_3", "reason 3")

    disputes = get_open_disputes()
    dispute = get_dispute(disputes[0]["id"])
    transcript = dispute["debate_transcript"]

    assert len(transcript) == 3
    assert [entry["proposed_action"] for entry in transcript] == ["action_1", "action_2", "action_3"]
    assert [entry["justification"] for entry in transcript] == ["reason 1", "reason 2", "reason 3"]


@patch("src.notifications.send_webhook_notification")
def test_approval_resets_consecutive_veto_counter(mock_webhook):
    """An approved action between vetoes must reset the streak, so no dispute is created."""
    _veto()
    _veto()
    _approve()
    _veto()

    assert get_consecutive_critic_vetoes() == 1
    assert get_open_disputes() == []
    assert not _dispute_paused()


@patch("src.notifications.send_webhook_notification")
def test_dispute_creation_resets_counter_for_next_streak(mock_webhook):
    """After a dispute is logged, the counter resets so a 4th immediate veto doesn't re-trigger a second dispute."""
    _veto()
    _veto()
    _veto()
    assert len(get_open_disputes()) == 1

    _veto()
    assert len(get_open_disputes()) == 1
    assert get_consecutive_critic_vetoes() == 1


@patch("src.notifications.send_webhook_notification")
def test_get_dispute_unknown_id_returns_none(mock_webhook):
    assert get_dispute(999) is None


@patch("src.notifications.send_webhook_notification")
def test_resolve_dispute_override_clears_pause_and_resets_counter(mock_webhook):
    _veto()
    _veto()
    _veto()
    dispute_id = get_open_disputes()[0]["id"]

    result = resolve_dispute(dispute_id, "override")

    assert result == {"success": True, "dispute_id": dispute_id, "resolution": "override"}
    assert get_open_disputes() == []
    assert not _dispute_paused()
    assert get_consecutive_critic_vetoes() == 0

    dispute = get_dispute(dispute_id)
    assert dispute["status"] == "resolved"
    assert dispute["resolution"] == "override"
    assert dispute["resolved_at"] is not None


@patch("src.notifications.send_webhook_notification")
def test_resolve_dispute_abort_and_rewrite_rules(mock_webhook):
    _veto()
    _veto()
    _veto()
    dispute_id = get_open_disputes()[0]["id"]
    resolve_dispute(dispute_id, "abort")
    assert get_dispute(dispute_id)["resolution"] == "abort"

    _veto()
    _veto()
    _veto()
    other_dispute_id = get_open_disputes()[0]["id"]
    resolve_dispute(other_dispute_id, "rewrite_rules", notes="some_rule | new text")
    resolved = get_dispute(other_dispute_id)
    assert resolved["resolution"] == "rewrite_rules"
    assert resolved["resolution_notes"] == "some_rule | new text"


@patch("src.notifications.send_webhook_notification")
def test_resolve_dispute_rejects_invalid_resolution(mock_webhook):
    _veto()
    _veto()
    _veto()
    dispute_id = get_open_disputes()[0]["id"]
    with pytest.raises(ValueError):
        resolve_dispute(dispute_id, "force_execute")


@patch("src.notifications.send_webhook_notification")
def test_resolve_dispute_unknown_id_raises(mock_webhook):
    with pytest.raises(ValueError):
        resolve_dispute(999, "override")


@patch("src.notifications.send_webhook_notification")
def test_resolve_dispute_already_resolved_raises(mock_webhook):
    _veto()
    _veto()
    _veto()
    dispute_id = get_open_disputes()[0]["id"]
    resolve_dispute(dispute_id, "abort")
    with pytest.raises(ValueError):
        resolve_dispute(dispute_id, "override")
