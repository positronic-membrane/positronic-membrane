from unittest.mock import MagicMock, patch

import pytest

import src.config
from src.database import get_connection, init_db
from src.notifications import send_webhook_notification


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB settings for testing."""
    temp_db = tmp_path / "test_janus_notifications.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path


def _set_webhook_url(config_key: str, url: str):
    conn = get_connection()
    conn.execute(
        "UPDATE system_config SET config_value = ? WHERE config_key = ?;", (url, config_key)
    )
    conn.commit()
    conn.close()


def test_send_webhook_notification_noop_when_unconfigured():
    """No webhook URLs configured -> no HTTP call is attempted, returns False."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        result = send_webhook_notification("critic_veto", "test message")
    assert result is False
    mock_urlopen.assert_not_called()


@patch("urllib.request.urlopen")
def test_send_webhook_notification_slack_payload(mock_urlopen):
    _set_webhook_url("webhooks.slack_url", "https://hooks.slack.example/T000/B000/xxx")

    mock_response = MagicMock()
    mock_response.status = 200
    mock_urlopen.return_value.__enter__.return_value = mock_response

    result = send_webhook_notification("critic_veto", "Critic vetoed action 'x'")

    assert result is True
    request_obj = mock_urlopen.call_args[0][0]
    assert request_obj.full_url == "https://hooks.slack.example/T000/B000/xxx"
    assert b'"text"' in request_obj.data
    assert b"critic_veto" in request_obj.data


@patch("urllib.request.urlopen")
def test_send_webhook_notification_discord_payload(mock_urlopen):
    _set_webhook_url("webhooks.discord_url", "https://discord.example/api/webhooks/1/xxx")

    mock_response = MagicMock()
    mock_response.status = 204
    mock_urlopen.return_value.__enter__.return_value = mock_response

    result = send_webhook_notification("governor_halt", "Background automations paused")

    assert result is True
    request_obj = mock_urlopen.call_args[0][0]
    assert b'"content"' in request_obj.data


@patch("urllib.request.urlopen")
def test_send_webhook_notification_dispatches_to_all_configured_targets(mock_urlopen):
    _set_webhook_url("webhooks.slack_url", "https://hooks.slack.example/T000/B000/xxx")
    _set_webhook_url("webhooks.discord_url", "https://discord.example/api/webhooks/1/xxx")

    mock_response = MagicMock()
    mock_response.status = 200
    mock_urlopen.return_value.__enter__.return_value = mock_response

    result = send_webhook_notification("goal_proposal", "New proposal")

    assert result is True
    assert mock_urlopen.call_count == 2


@patch("urllib.request.urlopen")
def test_send_webhook_notification_one_target_failing_does_not_block_others(mock_urlopen):
    _set_webhook_url("webhooks.slack_url", "https://hooks.slack.example/T000/B000/xxx")
    _set_webhook_url("webhooks.discord_url", "https://discord.example/api/webhooks/1/xxx")

    import urllib.error

    mock_response = MagicMock()
    mock_response.status = 200
    success_cm = MagicMock()
    success_cm.__enter__.return_value = mock_response

    mock_urlopen.side_effect = [urllib.error.URLError("unreachable"), success_cm]

    result = send_webhook_notification("critic_veto", "test message")

    assert result is True
    assert mock_urlopen.call_count == 2


def test_webhook_urls_are_not_agent_modifiable_by_default():
    """Webhook target URLs must require admin-level config writes, not agent-driven ones."""
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT is_agent_modifiable FROM system_config "
        "WHERE config_key IN ('webhooks.slack_url', 'webhooks.discord_url');"
    )
    rows = cursor.fetchall()
    conn.close()
    assert len(rows) == 2
    assert all(row[0] == 0 for row in rows)
