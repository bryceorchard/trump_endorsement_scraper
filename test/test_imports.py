"""
test_imports.py — Import-wiring smoke test for the src/ subpackage layout.

Guards the package-qualified import convention (see CLAUDE.md → "Module layout &
import convention"): every module under src/ must import cleanly with src/ as the
import root. Catches a reintroduced flat import (`import config`,
`from database import ...`) or a missing `__init__.py` before it reaches the Pi.

Runnable directly (exit 0 = pass, 1 = fail) or via pytest:

    python3 test/test_imports.py          # from the repo root
    pytest test/test_imports.py

The heavy third-party deps that only live in the Pi's venv
(apscheduler / feedparser / twscrape / psycopg2) are stubbed *only when not
installed*, so this runs on a dev machine without them yet still exercises the
real deps on the Pi. It verifies OUR import wiring, not the environment.
"""

import importlib
import importlib.util
import sys
import types
from pathlib import Path

# src/ is a sibling of test/ and is the import root — put it on sys.path first,
# exactly as `python3 main.py` (run from src/) does via sys.path[0].
SRC = Path(__file__).resolve().parent.parent / "src"


def _stub(name: str, **attrs: object) -> None:
    """Register a placeholder module so an `import name` succeeds off-Pi."""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


def _stub_missing_pi_deps() -> None:
    """Stub Pi-only third-party deps, but only if they aren't actually installed."""
    if importlib.util.find_spec("apscheduler") is None:
        _stub("apscheduler")
        _stub("apscheduler.schedulers")
        _stub("apscheduler.schedulers.blocking", BlockingScheduler=object)
        _stub("apscheduler.triggers")
        _stub("apscheduler.triggers.interval", IntervalTrigger=object)
    if importlib.util.find_spec("feedparser") is None:
        _stub("feedparser")
    if importlib.util.find_spec("twscrape") is None:
        _stub("twscrape")
    if importlib.util.find_spec("psycopg2") is None:
        # Error must be a real exception class: database.py aliases it
        # (DatabaseError = psycopg2.Error) and callers use it in `except`.
        _psycopg2_error = type("Error", (Exception,), {})
        _stub("psycopg2", connect=lambda *a, **k: None, Error=_psycopg2_error)
        _stub("psycopg2.extras", RealDictCursor=object, Json=object)
        _stub("psycopg2.extensions",
              parse_dsn=lambda dsn: {}, make_dsn=lambda **kw: "")
        sys.modules["psycopg2"].extras = sys.modules["psycopg2.extras"]
        sys.modules["psycopg2"].extensions = sys.modules["psycopg2.extensions"]


# Modules the pipeline imports, in dependency order. Each entry is imported and,
# where given, the listed attributes must resolve on the imported module.
_TARGETS = [
    ("config.config", ["OLLAMA_MODEL", "OLLAMA_URL"]),
    ("database.database", ["init_db", "get_unprocessed_items", "save_endorsement",
                           "upsert_item", "log_run", "finish_run"]),
    ("detector.endorsement_detector", ["detect_endorsement", "is_actionable",
                                        "EndorsementResult"]),
    ("collectors", ["TruthSocialCollector", "TwitterCollector",
                    "WhiteHouseCollector", "RSSCollector"]),
    ("main", []),
]


def test_package_qualified_imports_resolve() -> None:
    """Every src/ module imports cleanly and exposes its expected public names."""
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    _stub_missing_pi_deps()

    for name, attrs in _TARGETS:
        module = importlib.import_module(name)
        missing = [a for a in attrs if not hasattr(module, a)]
        assert not missing, f"{name} is missing expected names: {missing}"

    # `from config import config` rebinds the name to the module, so attribute
    # access (config.FOO) must still work — this is the crux of the convention.
    config = importlib.import_module("config.config")
    assert isinstance(config.OLLAMA_MODEL, str) and config.OLLAMA_MODEL


if __name__ == "__main__":
    try:
        test_package_qualified_imports_resolve()
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)
    except ImportError as exc:
        print(f"FAIL: import wiring broken — {exc}")
        sys.exit(1)
    print(f"OK: all {len(_TARGETS)} import targets resolve (src root: {SRC})")
