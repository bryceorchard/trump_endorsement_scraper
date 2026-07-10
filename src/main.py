"""
main.py — Entry point for trump_tracker.

Run once:
    python main.py --run-once

Run on a schedule (blocking):
    python main.py

Run a single collector:
    python main.py --collector truth_social

Run detection only (useful for testing Ollama separately):
    python main.py --detect-only
"""

import argparse
import logging
import sys
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import config
from database.database import (
    init_db,
    get_unprocessed_items,
    save_endorsement,
    record_detection_attempt,
)
from detector.endorsement_detector import detect_endorsement, is_actionable, DetectionTimeout
from collectors import (
    TruthSocialCollector,
    TwitterCollector,
    WhiteHouseCollector,
    RSSCollector,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("trump_tracker")

# APScheduler logs every job add + every run at INFO, which buries the app's own
# output. Keep only its warnings (missed runs, overlapping jobs skipped).
logging.getLogger("apscheduler").setLevel(logging.WARNING)

COLLECTORS = {
    "truth_social": TruthSocialCollector,
    "twitter":      TwitterCollector,
    "whitehouse":   WhiteHouseCollector,
    "rss":          RSSCollector,
}


def run_collector(name: str):
    cls = COLLECTORS.get(name)
    if cls is None:
        logger.error("Unknown collector: %s", name)
        return
    try:
        summary = cls().run()
        logger.info(
            "%-14s  found=%-4d  new=%-4d  %s",
            summary["source"],
            summary["found"],
            summary["new"],
            f"ERROR: {summary['error']}" if summary["error"] else "OK",
        )
    except Exception as exc:
        logger.error("[%s] unhandled error: %s", name, exc, exc_info=True)


def run_detection():
    """
    Pull unprocessed items from the DB, run each through the endorsement
    detector, persist results, and log any actionable hits.
    """
    if not config.DETECTION_ENABLED:
        logger.debug("Detection disabled (DETECTION_ENABLED=false), skipping.")
        return

    items = get_unprocessed_items(batch_size=config.DETECTION_BATCH_SIZE)
    if not items:
        logger.debug("[detection] no unprocessed items.")
        return

    total = len(items)
    logger.info("[detection] processing %d item(s)...", total)
    analyzed = 0
    hits = 0

    for idx, item in enumerate(items, 1):
        # Per-item heartbeat: each inference takes ~30s on a Pi and only
        # actionable hits log below, so without this the loop looks hung.
        preview = " ".join(item["content"].split())[:70]
        logger.info("[detection] %d/%d id=%s (%s) %r",
                    idx, total, item["id"], item["source_name"], preview)
        try:
            result = detect_endorsement(item["content"])
            save_endorsement(item["id"], result)
            analyzed += 1

            if is_actionable(result):
                hits += 1
                logger.warning(
                    "🚨 ENDORSEMENT DETECTED | company=%-20s ticker=%-6s "
                    "confidence=%-6s type=%-10s source=%s | %s",
                    result.company or "?",
                    result.ticker or "-",
                    result.confidence,
                    result.endorsement_type,
                    item["source_name"],
                    item.get("url") or "(no url)",
                )
                if result.quote:
                    logger.warning('   Quote: "%s"', result.quote)

        except RuntimeError as exc:
            # Ollama unreachable / misconfigured — stop the loop, leave items
            # unprocessed so they're retried once the server is back.
            logger.error("[detection] %s — pausing detection.", exc)
            break
        except DetectionTimeout as exc:
            # A single call timed out. Usually transient — a cold model load
            # (~a minute on the Pi after an idle gap) or momentary overload — so
            # we retry rather than silently write the item off as "no
            # endorsement". But bound the retries: a genuinely too-long "poison"
            # item that always times out would otherwise sit unprocessed forever
            # and, since the batch is oldest-first, fill every batch and starve
            # newer items. After DETECTION_MAX_ATTEMPTS we give up and mark it
            # processed so the queue keeps moving.
            attempts = record_detection_attempt(item["id"])
            if attempts >= config.DETECTION_MAX_ATTEMPTS:
                logger.warning(
                    "[detection] item %s timed out %d× (%s) — giving up, marking processed.",
                    item["id"], attempts, exc,
                )
                save_endorsement(item["id"], _make_error_result(item["content"]))
            else:
                logger.warning(
                    "[detection] item %s timed out (%s) — leaving for retry (%d/%d).",
                    item["id"], exc, attempts, config.DETECTION_MAX_ATTEMPTS,
                )
            continue
        except Exception as exc:
            logger.warning("[detection] error on item %s: %s", item["id"], exc)
            # Still mark processed so we don't retry a permanently broken item
            save_endorsement(item["id"], _make_error_result(item["content"]))

    logger.info("[detection] analyzed=%d  actionable_hits=%d", analyzed, hits)


def _make_error_result(text: str):
    """Fallback result used when the LLM call fails, so we don't retry forever."""
    from detector.endorsement_detector import EndorsementResult
    return EndorsementResult(
        endorsement_detected=False,
        company=None,
        ticker=None,
        confidence="low",
        quote=None,
        endorsement_type="none",
        raw_text=text,
    )


def run_all():
    logger.info("=== Running all collectors at %s ===", datetime.now(timezone.utc).isoformat())
    for name in COLLECTORS:
        run_collector(name)
    run_detection()


def main():
    parser = argparse.ArgumentParser(description="Trump statement tracker")
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run all collectors once and exit",
    )
    parser.add_argument(
        "--collector",
        choices=list(COLLECTORS.keys()),
        help="Run a single named collector once and exit",
    )
    parser.add_argument(
        "--detect-only",
        action="store_true",
        help="Run the endorsement detector on unprocessed items and exit",
    )
    args = parser.parse_args()

    # Always initialise the DB first
    init_db()

    if args.collector:
        run_collector(args.collector)
        return

    if args.detect_only:
        run_detection()
        return

    if args.run_once:
        run_all()
        return

    # ── Scheduled mode ───────────────────────────────────────────────────────
    scheduler = BlockingScheduler(timezone="UTC")
    now = datetime.now(timezone.utc)   # every job also fires immediately on startup
    # misfire_grace_time=None disables the default 1s grace window: if a slow Pi
    # boot delays the scheduler past `now`, the immediate startup fire still runs
    # instead of being dropped as a "missed" run.

    # One interval job per collector. Pass the name via args + name= (rather than
    # a lambda) so the logs read "truth_social"/"twitter"/… instead of four
    # identical "main.<locals>.<lambda>" lines.
    collector_intervals = {
        "truth_social": config.INTERVAL_TRUTH_SOCIAL,
        "twitter":      config.INTERVAL_TWITTER,
        "whitehouse":   config.INTERVAL_WHITEHOUSE,
        "rss":          config.INTERVAL_RSS,
    }
    for name, interval in collector_intervals.items():
        scheduler.add_job(
            run_collector,
            IntervalTrigger(seconds=interval),
            args=[name],
            id=name,
            name=name,
            next_run_time=now,
            misfire_grace_time=None,
        )

    scheduler.add_job(
        run_detection,
        IntervalTrigger(seconds=config.INTERVAL_DETECTION),
        id="detection",
        name="detection",
        next_run_time=now,
        misfire_grace_time=None,
    )

    logger.info(
        "Scheduler started. Intervals: truth_social=%ds twitter=%ds "
        "whitehouse=%ds rss=%ds detection=%ds",
        config.INTERVAL_TRUTH_SOCIAL,
        config.INTERVAL_TWITTER,
        config.INTERVAL_WHITEHOUSE,
        config.INTERVAL_RSS,
        config.INTERVAL_DETECTION,
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
