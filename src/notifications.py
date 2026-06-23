import json
import logging
import urllib.error
import urllib.request

from src.database import get_connection

logger = logging.getLogger("JanusNotifications")

WEBHOOK_CONFIG_KEYS = {
    "slack": "webhooks.slack_url",
    "discord": "webhooks.discord_url",
}


def _get_webhook_urls() -> dict:
    """Reads configured webhook target URLs from system_config. Unset/empty entries are skipped."""
    urls = {}
    conn = get_connection(read_only_constitution=True)
    try:
        cursor = conn.cursor()
        for platform, config_key in WEBHOOK_CONFIG_KEYS.items():
            cursor.execute("SELECT config_value FROM system_config WHERE config_key = ?;", (config_key,))
            row = cursor.fetchone()
            if row and row[0]:
                urls[platform] = row[0]
    finally:
        conn.close()
    return urls


def _build_payload(platform: str, message: str) -> dict:
    if platform == "discord":
        return {"content": message}
    return {"text": message}  # Slack-compatible incoming webhook format


def send_webhook_notification(event_type: str, message: str) -> bool:
    """
    Dispatches `message` to every webhook target configured in system_config.
    Each target is independent and failures are logged, not raised, so a single
    unreachable webhook can't block the caller (a veto, halt, or proposal flow).
    Returns True if at least one target accepted the notification.
    """
    urls = _get_webhook_urls()
    if not urls:
        return False

    full_message = f"[{event_type}] {message}"
    any_success = False
    for platform, url in urls.items():
        try:
            data = json.dumps(_build_payload(platform, full_message)).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                if 200 <= response.status < 300:
                    any_success = True
                else:
                    logger.warning(f"Webhook dispatch to {platform} returned status {response.status}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            logger.warning(f"Webhook dispatch to {platform} failed: {e}")
    return any_success
