"""
webhook.py — Post actionable-endorsement alerts to a Discord channel.

The webhook URL is a secret and comes from config (DISCORD_WEBHOOK_URL in
src/.env) — never hard-coded here. If it's unset, alerting is simply skipped;
detections are still logged by the caller.

send_discord_message() never raises: alerting is best-effort and must not be
able to disturb the detection loop that calls it (a failed alert must not undo
a saved detection). It returns True only when Discord accepted the message.
"""

import logging

import requests

from config import config

logger = logging.getLogger(__name__)

# Discord returns 204 No Content on a successful webhook post.
_DISCORD_SUCCESS = 204
_TIMEOUT = 10  # seconds — never block the detection loop on a slow Discord


def send_discord_message(message: str, username: str = "Pi Bot") -> bool:
    """Post `message` to the configured Discord webhook. Returns True on
    success, False if unconfigured or the send failed (logged, never raised)."""
    if not config.DISCORD_WEBHOOK_URL:
        logger.debug("Discord webhook not configured (DISCORD_WEBHOOK_URL unset) — skipping alert.")
        return False

    try:
        response = requests.post(
            config.DISCORD_WEBHOOK_URL,
            json={"content": message, "username": username},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Discord alert failed to send: %s", exc)
        return False

    if response.status_code == _DISCORD_SUCCESS:
        return True
    logger.warning(
        "Discord alert rejected (HTTP %s): %s",
        response.status_code, response.text[:200],
    )
    return False
