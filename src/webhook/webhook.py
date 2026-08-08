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
from typing import TYPE_CHECKING

import requests

from config import config

if TYPE_CHECKING:
    from detector.endorsement_detector import EndorsementResult

logger = logging.getLogger(__name__)

# Discord returns 204 No Content on a successful webhook post.
_DISCORD_SUCCESS = 204
_TIMEOUT = 10  # seconds — never block the detection loop on a slow Discord
# Discord rejects a content field over 2000 chars with HTTP 400; truncate so a
# long model-returned quote can't silently drop the whole alert.
_MAX_CONTENT = 2000
_TRUNCATED_SUFFIX = "\n…[truncated]"


def send_discord_message(message: str, username: str = "Pi Bot") -> bool:
    """Post `message` to the configured Discord webhook. Returns True on
    success, False if unconfigured or the send failed (logged, never raised)."""
    if not config.DISCORD_WEBHOOK_URL:
        logger.debug("Discord webhook not configured (DISCORD_WEBHOOK_URL unset) — skipping alert.")
        return False

    if len(message) > _MAX_CONTENT:
        message = message[: _MAX_CONTENT - len(_TRUNCATED_SUFFIX)] + _TRUNCATED_SUFFIX

    try:
        response = requests.post(
            config.DISCORD_WEBHOOK_URL,
            # allowed_mentions parse=[] — the alert embeds untrusted scraped text
            # (company/quote); without this, an @everyone/@here/role mention in
            # that text would ping the whole server.
            json={
                "content": message,
                "username": username,
                "allowed_mentions": {"parse": []},
            },
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


def format_endorsement_alert(
    result: "EndorsementResult", source_name: str, url: str | None
) -> str:
    """Render an actionable endorsement as the alert text posted to Discord (and
    logged). Single source of truth shared by the detection loop (main._alert)
    and the webhook self-test below, so the two can't drift apart."""
    return (
        "🚨 ENDORSEMENT DETECTED\n"
        f"company: {result.company or '?'}\n"
        f"ticker:  {result.ticker or '-'} (unverified — model-guessed)\n"
        f"confidence: {result.confidence}   type: {result.endorsement_type}\n"
        f"source: {source_name}   {url or '(no url)'}\n"
        f"quote: {result.quote or '(no quote)'}"
    )


# ── Manual end-to-end test of the alerting path ───────────────────────────────
# Run from src/:  python3 -m webhook.webhook   (scripts/test_detector.sh wraps this)
#
# Runs a known-endorsement sample through the real detector, then posts the
# resulting alert to Discord — exercising detector → format → send end to end.
# Requires Ollama running (for detection) and DISCORD_WEBHOOK_URL set in src/.env.
if __name__ == "__main__":
    import sys

    _SAMPLE = (
        "Just had a GREAT meeting with Tim Cook. Apple is doing TREMENDOUS "
        "things for America!"
    )

    if not config.DISCORD_WEBHOOK_URL:
        print(
            "DISCORD_WEBHOOK_URL is not set — nothing to test. Set it in src/.env "
            "(see src/.env.example → Alerting) and re-run.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Imported here (not at module top) so the app's normal startup import of
    # this module stays lightweight and free of a detector dependency.
    from detector.endorsement_detector import detect_endorsement, is_actionable

    print(f"Sample post:\n  {_SAMPLE}\n\nRunning detector (needs Ollama)…")
    try:
        result = detect_endorsement(_SAMPLE)
    except Exception as exc:  # RuntimeError/DetectionTimeout carry remediation text
        print(f"\nDetector unavailable: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"  detected:   {result.endorsement_detected}")
    print(f"  company:    {result.company}")
    print(f"  ticker:     {result.ticker}")
    print(f"  confidence: {result.confidence}")
    print(f"  type:       {result.endorsement_type}")
    print(f"  actionable: {is_actionable(result)}")

    if not is_actionable(result):
        print(
            "\nThe model did not flag this sample as an actionable endorsement, so the "
            "real pipeline would have nothing to alert on — not sending to Discord. "
            "Is Ollama serving the expected model (OLLAMA_MODEL)?",
            file=sys.stderr,
        )
        sys.exit(1)

    message = format_endorsement_alert(
        result, source_name="webhook-test", url="https://example.com/sample-post"
    )
    print(f"\nPosting this alert to Discord:\n{message}\n")
    if send_discord_message(message):
        print("✅ Discord accepted the message (HTTP 204). Check your channel.")
        sys.exit(0)
    print(
        "❌ Discord did not accept the message — see the warning logged above.",
        file=sys.stderr,
    )
    sys.exit(1)
