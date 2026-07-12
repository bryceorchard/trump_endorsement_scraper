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

import psycopg2
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import config
from database.database import (
    init_db,
    get_unprocessed_items,
    count_unprocessed_items,
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


# Log the "detection disabled" reminder loudly once, then quietly — a scheduled
# service with detection off would otherwise warn every cycle or never at all.
_warned_detection_disabled = False


def run_detection(drain: bool = False) -> bool:
    """
    Pull unprocessed items from the DB (newest content first), run each through
    the endorsement detector, persist results, and log any actionable hits.

    One call processes a single batch of DETECTION_BATCH_SIZE items — the
    scheduled job drains the queue gradually. With drain=True (the --drain
    flag), keeps processing batches until the queue is empty.

    Returns False if detection had to stop early (Ollama unreachable), True
    otherwise — so one-shot CLI modes can exit non-zero instead of pretending
    the run succeeded.
    """
    global _warned_detection_disabled
    if not config.DETECTION_ENABLED:
        if not _warned_detection_disabled:
            _warned_detection_disabled = True
            logger.warning(
                "Detection is disabled (DETECTION_ENABLED=false) — collected items "
                "are queuing up unprocessed. Set DETECTION_ENABLED=true in src/.env "
                "once Ollama is available."
            )
        else:
            logger.debug("Detection disabled (DETECTION_ENABLED=false), skipping.")
        return True

    ok = True
    while True:
        batch_done = _run_detection_batch()
        if batch_done is None:          # queue empty
            break
        if not batch_done["ok"]:        # Ollama down — stop, don't spin
            ok = False
            break
        if not drain:
            break
        if batch_done["progressed"] == 0:
            # Every item in the batch was left for retry (timeouts) — looping
            # again would just re-run the same batch. Stop and say so.
            logger.warning(
                "[detection] --drain stopping: no items completed this batch "
                "(all left for timeout retry). Re-run later once Ollama is faster/warm."
            )
            break

    remaining = count_unprocessed_items()
    if remaining:
        logger.info(
            "[detection] %d unprocessed item(s) remain — they'll be picked up by the "
            "next detection cycle (scheduled mode) or the next --detect-only/--run-once "
            "(add --drain to process the whole queue in one go).",
            remaining,
        )
    return ok


def _run_detection_batch() -> dict | None:
    """Process one batch. Returns None if the queue was empty, else
    {"ok": bool (False = Ollama down), "progressed": int (items completed)}."""
    items = get_unprocessed_items(batch_size=config.DETECTION_BATCH_SIZE)
    if not items:
        logger.info("[detection] queue empty — all collected items have been analyzed.")
        return None

    total_unprocessed = count_unprocessed_items()
    total = len(items)
    if total_unprocessed > total:
        logger.info(
            "[detection] processing %d of %d unprocessed item(s) "
            "(batch size DETECTION_BATCH_SIZE=%d, newest first)...",
            total, total_unprocessed, config.DETECTION_BATCH_SIZE,
        )
    else:
        logger.info("[detection] processing %d item(s) (newest first)...", total)

    analyzed = 0
    hits = 0
    gave_up = 0
    ok = True

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
            logger.error(
                "[detection] %s — stopping detection for now. Nothing is lost: "
                "unanalyzed items stay queued and are retried automatically once "
                "Ollama is reachable (start it with `ollama serve`; ensure the "
                "model is pulled: `ollama pull %s` — see docs/SETUP.md Step 1).",
                exc, config.OLLAMA_MODEL,
            )
            ok = False
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
                gave_up += 1
            else:
                logger.warning(
                    "[detection] item %s timed out (%s) — leaving for retry (%d/%d).",
                    item["id"], exc, attempts, config.DETECTION_MAX_ATTEMPTS,
                )
            continue
        except Exception as exc:
            logger.warning(
                "[detection] error on item %s: %s — marking it processed (no detection) "
                "so it isn't retried forever.",
                item["id"], exc,
            )
            save_endorsement(item["id"], _make_error_result(item["content"]))
            gave_up += 1

    logger.info("[detection] analyzed=%d  actionable_hits=%d", analyzed, hits)
    return {"ok": ok, "progressed": analyzed + gave_up}


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


def run_all(drain: bool = False) -> bool:
    logger.info("=== Running all collectors at %s ===", datetime.now(timezone.utc).isoformat())
    for name in COLLECTORS:
        run_collector(name)
    return run_detection(drain=drain)


def _redact_db_url(url: str) -> str:
    """Hide the password when echoing DATABASE_URL in error messages."""
    import re
    return re.sub(r"(://[^:/@]+):[^@]*@", r"\1:***@", url)


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
    parser.add_argument(
        "--drain",
        action="store_true",
        help="With --run-once/--detect-only: keep processing detection batches "
             "until the queue is empty (default is one batch of "
             f"DETECTION_BATCH_SIZE={config.DETECTION_BATCH_SIZE})",
    )
    args = parser.parse_args()

    if args.detect_only and not config.DETECTION_ENABLED:
        logger.error(
            "--detect-only requested, but DETECTION_ENABLED=false in src/.env — "
            "nothing to do. Set DETECTION_ENABLED=true (and have Ollama running) first."
        )
        sys.exit(1)

    # Always initialise the DB first
    try:
        init_db()
    except psycopg2.OperationalError as exc:
        logger.error(
            "Cannot connect to PostgreSQL at %s:\n  %s\n"
            "Is Postgres running? scripts/setup.sh creates the role and database "
            "(see docs/SETUP.md Step 2); check DATABASE_URL in src/.env.",
            _redact_db_url(config.DATABASE_URL), str(exc).strip(),
        )
        sys.exit(1)

    if args.collector:
        run_collector(args.collector)
        return

    if args.detect_only:
        if not run_detection(drain=args.drain):
            sys.exit(1)   # Ollama was unreachable — don't pretend this succeeded
        return

    if args.run_once:
        if not run_all(drain=args.drain):
            sys.exit(1)   # collectors ran, but detection couldn't (Ollama down)
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
    try:
        main()
    except KeyboardInterrupt:
        # Progress is safe: processed items are stamped as they complete, and
        # anything in flight simply stays queued for the next run.
        logger.info("Interrupted — exiting. Progress so far is saved.")
        sys.exit(130)
