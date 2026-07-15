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
import re
import sys
from datetime import datetime, timezone
from typing import NamedTuple

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import config
from database.database import (
    init_db,
    get_unprocessed_items,
    count_unprocessed_items,
    save_endorsement,
    record_detection_attempt,
    DatabaseError,
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


class BatchResult(NamedTuple):
    """Outcome of one detection batch."""
    queue_empty: bool        # nothing left to fetch (beyond deferred items)
    ok: bool = True          # False = Ollama unreachable; stop the whole run
    progressed: int = 0      # items completed (analyzed or given up on)


# Tracks the non-empty→empty transition so a caught-up scheduled service logs
# "queue empty" once instead of every INTERVAL_DETECTION cycle forever.
_queue_was_empty = False


def run_detection(drain: bool = False) -> bool:
    """
    Pull unprocessed items from the DB (newest content first), run each through
    the endorsement detector, persist results, and log any actionable hits.

    One call processes a single batch of DETECTION_BATCH_SIZE items — the
    scheduled job drains the queue gradually. With drain=True (the --drain
    flag), keeps processing batches until the queue is empty. An item that
    times out is deferred for the rest of the run rather than retried
    back-to-back, so a transient Ollama slowdown can't burn through its whole
    DETECTION_MAX_ATTEMPTS budget within a single drain.

    Returns False if detection had to stop early (Ollama or the database
    unreachable), True otherwise — so one-shot CLI modes can exit non-zero
    instead of pretending the run succeeded.
    """
    if not config.DETECTION_ENABLED:
        logger.debug("Detection disabled (DETECTION_ENABLED=false), skipping.")
        return True

    ok = True
    deferred: set[int] = set()   # ids that timed out this run — skip until next run
    try:
        while True:
            result = _run_detection_batch(deferred)
            if result.queue_empty:
                break
            if not result.ok:
                ok = False
                break
            if not drain:
                break
            if result.progressed == 0:
                # Every item in the batch timed out and was deferred — Ollama is
                # too slow right now to make progress, so stop the drain rather
                # than burn a full timeout per item on the rest of the queue.
                logger.warning(
                    "[detection] --drain stopping: every item in this batch timed "
                    "out. Deferred items stay queued and are retried on later runs."
                )
                break

        remaining = 0 if (result.queue_empty and not deferred) else count_unprocessed_items()
        if remaining:
            if drain:
                logger.info(
                    "[detection] %d unprocessed item(s) remain (run stopped early) — "
                    "they'll be retried on later runs.", remaining,
                )
            else:
                logger.info(
                    "[detection] %d unprocessed item(s) remain — they'll be picked up "
                    "by the next detection cycle (scheduled mode) or the next "
                    "--detect-only/--run-once (add --drain to process the whole queue "
                    "in one go).", remaining,
                )
    except DatabaseError as exc:
        # DB-transport problem (server restarted, connection dropped) — not any
        # item's fault. Stop cleanly; unstamped items are simply retried later.
        logger.error(
            "[detection] database error — stopping detection: %s. Nothing is "
            "lost: items stay queued and are retried once PostgreSQL is "
            "reachable again (check DATABASE_URL in src/.env).", exc,
        )
        return False
    return ok


def _run_detection_batch(deferred: set[int]) -> BatchResult:
    """Process one batch, skipping `deferred` ids (they timed out earlier this
    run and stay queued for later runs). New timeouts are added to `deferred`."""
    global _queue_was_empty
    items = get_unprocessed_items(
        batch_size=config.DETECTION_BATCH_SIZE, exclude_ids=deferred
    )
    if not items:
        if deferred:
            logger.info(
                "[detection] only items deferred after timeouts remain (%d) — "
                "they'll be retried on later runs.", len(deferred),
            )
        elif not _queue_was_empty:
            logger.info("[detection] queue empty — all collected items have been analyzed.")
            _queue_was_empty = True
        else:
            logger.debug("[detection] queue still empty.")
        return BatchResult(queue_empty=True)
    _queue_was_empty = False

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

        except DatabaseError:
            # DB-transport problem, not this item's fault — never mark the item
            # processed for it. Bubbles up to run_detection's handler.
            raise
        except RuntimeError as exc:
            # Ollama unreachable / misconfigured — stop the loop, leave items
            # unprocessed so they're retried once the server is back. The
            # exception message carries the remediation steps.
            logger.error(
                "[detection] %s — stopping detection for now. Nothing is lost: "
                "unanalyzed items stay queued and are retried automatically once "
                "Ollama is reachable.", exc,
            )
            ok = False
            break
        except DetectionTimeout as exc:
            # A single call timed out. Usually transient — a cold model load
            # (~a minute on the Pi after an idle gap) or momentary overload — so
            # we retry rather than silently write the item off as "no
            # endorsement". But bound the retries: a genuinely too-long "poison"
            # item that always times out would otherwise pin the top of every
            # newest-first batch, wasting a full timeout each cycle. After
            # DETECTION_MAX_ATTEMPTS we give up and mark it processed so the
            # queue keeps moving. Within this run it's also deferred, so
            # retries land on later runs instead of back-to-back.
            attempts = record_detection_attempt(item["id"])
            if attempts >= config.DETECTION_MAX_ATTEMPTS:
                logger.warning(
                    "[detection] item %s timed out %d× (%s) — giving up, marking processed.",
                    item["id"], attempts, exc,
                )
                save_endorsement(item["id"], _make_error_result(item["content"]))
                gave_up += 1
            else:
                deferred.add(item["id"])
                logger.warning(
                    "[detection] item %s timed out (%s) — deferred; will retry on a "
                    "later run (%d/%d).",
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
    return BatchResult(queue_empty=False, ok=ok, progressed=analyzed + gave_up)


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


def _redact_db_url(dsn: str) -> str:
    """Hide the password when echoing DATABASE_URL in error messages.

    Handles both DSN forms psycopg2 accepts: URL style — where the password may
    itself contain ':' or '@' (libpq splits userinfo at the LAST '@'), and the
    user may be empty — and key=value style ('host=… password=… dbname=…').
    """
    if "://" in dsn:
        scheme, _, rest = dsn.partition("://")
        userinfo, at, hostpart = rest.rpartition("@")
        if at and ":" in userinfo:
            user = userinfo.split(":", 1)[0]
            return f"{scheme}://{user}:***@{hostpart}"
        return dsn  # no password present
    return re.sub(r"(password\s*=\s*)\S+", r"\1***", dsn)


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

    if args.drain and not (args.run_once or args.detect_only):
        parser.error("--drain requires --run-once or --detect-only")

    # Always initialise the DB first — every CLI mode needs the schema, and
    # init_db() is idempotent.
    try:
        init_db()
    except DatabaseError as exc:
        logger.error(
            "Cannot connect to PostgreSQL (DATABASE_URL=%s):\n  %s\n"
            "Is Postgres running and the URL well-formed? scripts/setup.sh creates "
            "the role and database (see docs/SETUP.md Step 2); check DATABASE_URL "
            "in src/.env.",
            _redact_db_url(config.DATABASE_URL), str(exc).strip(),
        )
        sys.exit(1)

    if not config.DETECTION_ENABLED:
        if args.detect_only:
            logger.error(
                "--detect-only requested, but DETECTION_ENABLED=false in src/.env — "
                "nothing to do. Set DETECTION_ENABLED=true (and have Ollama running) first."
            )
            sys.exit(1)
        if not args.collector:
            logger.warning(
                "Detection is disabled (DETECTION_ENABLED=false) — collected items "
                "will queue up unprocessed until it's re-enabled in src/.env."
            )

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

    # No detection job at all when disabled — scheduling a known no-op would
    # just wake the Pi every INTERVAL_DETECTION for nothing (the startup
    # warning above already tells the user items will queue up).
    if config.DETECTION_ENABLED:
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
