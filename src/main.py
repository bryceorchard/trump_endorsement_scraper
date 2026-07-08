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
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import config
from database.database import init_db, get_unprocessed_items, save_endorsement
from detector.endorsement_detector import detect_endorsement, is_actionable
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

    logger.info("[detection] processing %d item(s)...", len(items))
    analyzed = 0
    hits = 0

    for item in items:
        try:
            result = detect_endorsement(item["content"], timeout=config.OLLAMA_TIMEOUT)
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
            # Ollama not running — stop the detection loop, don't mark item processed
            logger.error("[detection] %s — pausing detection.", exc)
            break
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
    logger.info("=== Running all collectors at %s ===", datetime.utcnow().isoformat())
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

    scheduler.add_job(
        lambda: run_collector("truth_social"),
        IntervalTrigger(seconds=config.INTERVAL_TRUTH_SOCIAL),
        id="truth_social",
        next_run_time=datetime.utcnow(),   # run immediately on startup
    )
    scheduler.add_job(
        lambda: run_collector("twitter"),
        IntervalTrigger(seconds=config.INTERVAL_TWITTER),
        id="twitter",
        next_run_time=datetime.utcnow(),
    )
    scheduler.add_job(
        lambda: run_collector("whitehouse"),
        IntervalTrigger(seconds=config.INTERVAL_WHITEHOUSE),
        id="whitehouse",
        next_run_time=datetime.utcnow(),
    )
    scheduler.add_job(
        lambda: run_collector("rss"),
        IntervalTrigger(seconds=config.INTERVAL_RSS),
        id="rss",
        next_run_time=datetime.utcnow(),
    )
    scheduler.add_job(
        run_detection,
        IntervalTrigger(seconds=config.INTERVAL_DETECTION),
        id="detection",
        next_run_time=datetime.utcnow(),
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
