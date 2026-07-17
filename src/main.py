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
    redact_dsn,
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


def run_collector(name: str) -> bool:
    """Run one collector. True only if the run completed without errors —
    one-shot CLI modes exit non-zero on failure instead of hiding it."""
    cls = COLLECTORS.get(name)
    if cls is None:
        logger.error("Unknown collector: %s", name)
        return False
    try:
        summary = cls().run()
        logger.info(
            "%-14s  found=%-4d  new=%-4d  %s",
            summary["source"],
            summary["found"],
            summary["new"],
            f"ERROR: {summary['error']}" if summary["error"] else "OK",
        )
        return summary["error"] is None
    except Exception as exc:
        logger.error("[%s] unhandled error: %s", name, exc, exc_info=True)
        return False


class BatchResult(NamedTuple):
    """Outcome of one detection batch."""
    queue_empty: bool        # nothing left to fetch (beyond deferred items)
    ok: bool = True          # False = Ollama unreachable; stop the whole run
    progressed: int = 0      # items completed (analyzed or given up on)


# Latch: log the idle state (queue empty / all items cooling down) once per
# transition instead of every INTERVAL_DETECTION cycle forever.
_idle_logged = False


def run_detection(drain: bool = False) -> bool:
    """
    Pull unprocessed items from the DB (newest content first), run each through
    the endorsement detector, persist results, and log any actionable hits.

    One call processes a single batch of DETECTION_BATCH_SIZE items — the
    scheduled job drains the queue gradually. With drain=True (the --drain
    flag), keeps processing batches until nothing eligible is left. An item
    that times out gets a DETECTION_RETRY_COOLDOWN before it's eligible again
    (persisted in items.next_attempt_at), so a transient Ollama slowdown can't
    burn through its DETECTION_MAX_ATTEMPTS budget back-to-back — in any run
    mode, across restarts.

    Returns False if detection stopped early — Ollama or the database
    unreachable, or a --drain that stopped making progress — so one-shot CLI
    modes can exit non-zero instead of pretending the run succeeded.
    """
    if not config.DETECTION_ENABLED:
        logger.debug("Detection disabled (DETECTION_ENABLED=false), skipping.")
        return True

    try:
        while True:
            result = _run_detection_batch()
            if result.queue_empty or not result.ok or not drain:
                break
            if result.progressed == 0:
                # Every item in the batch timed out — Ollama is too slow right
                # now to make progress, so stop the drain (non-zero exit)
                # rather than burn a full timeout per item on the whole queue.
                logger.warning(
                    "[detection] --drain stopping: every item in this batch timed "
                    "out. They'll be retried after their cooldown."
                )
                result = result._replace(ok=False)
                break
    except DatabaseError as exc:
        # DB-transport problem (server restarted, connection dropped) — not any
        # item's fault. Stop cleanly; unstamped items are simply retried later.
        logger.error(
            "[detection] database error — stopping detection: %s. Nothing is "
            "lost: items stay queued and are retried once PostgreSQL is "
            "reachable again (check DATABASE_URL in src/.env).", exc,
        )
        return False
    return result.ok


def _run_detection_batch() -> BatchResult:
    """Process one batch of eligible items (cooling-down timeouts excluded by
    the query itself — see get_unprocessed_items)."""
    global _idle_logged
    items = get_unprocessed_items(batch_size=config.DETECTION_BATCH_SIZE)
    if not items:
        pending = count_unprocessed_items()
        if _idle_logged:
            logger.debug("[detection] queue still idle (%d cooling down).", pending)
        elif pending:
            logger.info(
                "[detection] %d unprocessed item(s) are cooling down after "
                "timeouts — retried once their cooldown (%ds) expires.",
                pending, config.DETECTION_RETRY_COOLDOWN,
            )
            _idle_logged = True
        else:
            logger.info("[detection] queue empty — all collected items have been analyzed.")
            _idle_logged = True
        return BatchResult(queue_empty=True)
    _idle_logged = False

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
            # endorsement". record_detection_attempt starts the item's cooldown
            # (persisted in the DB, so retries are spaced out in every run
            # mode) and bounds the retries: a genuinely too-long "poison" item
            # that always times out would otherwise pin the top of every
            # newest-first batch. After DETECTION_MAX_ATTEMPTS we give up and
            # mark it processed so the queue keeps moving.
            attempts = record_detection_attempt(item["id"])
            if attempts is None:
                logger.warning(
                    "[detection] item %s vanished mid-run — skipping.", item["id"]
                )
            elif attempts >= config.DETECTION_MAX_ATTEMPTS:
                logger.warning(
                    "[detection] item %s timed out %d× (%s) — giving up, marking processed.",
                    item["id"], attempts, exc,
                )
                save_endorsement(item["id"], _make_error_result(item["content"]))
                gave_up += 1
            else:
                logger.warning(
                    "[detection] item %s timed out (%s) — will retry after a %ds "
                    "cooldown (%d/%d).",
                    item["id"], exc, config.DETECTION_RETRY_COOLDOWN,
                    attempts, config.DETECTION_MAX_ATTEMPTS,
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
    """Run every collector once, then a detection pass. True only if all of it
    succeeded — one-shot mode exits non-zero on any component failure."""
    logger.info("=== Running all collectors at %s ===", datetime.now(timezone.utc).isoformat())
    # List, not a generator: every collector must run even after one fails.
    collectors_ok = all([run_collector(name) for name in COLLECTORS])
    detection_ok = run_detection(drain=drain)
    return collectors_ok and detection_ok


def _remaining_hint() -> None:
    """One-shot modes only: say how much of the queue is left and how to drain
    it. (Scheduled mode gets no advisory — its per-batch 'X of Y' lines already
    show queue depth, and the CLI flags don't apply to a systemd service.)"""
    if not config.DETECTION_ENABLED:
        return
    try:
        remaining = count_unprocessed_items()
    except DatabaseError:
        return
    if remaining:
        logger.info(
            "[detection] %d unprocessed item(s) remain — run again, or use "
            "--drain to process the whole queue in one go.", remaining,
        )


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
            redact_dsn(config.DATABASE_URL), str(exc).strip(),
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
        if not run_collector(args.collector):
            sys.exit(1)   # the run failed — don't pretend it succeeded
        return

    if args.detect_only:
        ok = run_detection(drain=args.drain)
        if not args.drain:
            _remaining_hint()
        if not ok:
            sys.exit(1)   # Ollama/DB unreachable or drain stalled — surface it
        return

    if args.run_once:
        ok = run_all(drain=args.drain)
        if not args.drain:
            _remaining_hint()
        if not ok:
            sys.exit(1)   # a collector or detection failed — surface it
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
