"""
test_setup_db_password.py — Behavioural test for the DB-password prompt in
scripts/setup.sh.

Drives the *real* setup.sh end-to-end in a throwaway sandbox with every
side-effecting command (sudo/psql/apt-get/systemctl/ollama/curl, plus
`python3 -m venv`) replaced by a stub on PATH. Nothing on the host is touched,
so this is safe to run anywhere — including the Pi. It asserts three behaviours:

  A. A valid password (with SQL/URL-hostile characters) is escaped correctly for
     the `ALTER ROLE` SQL literal and percent-encoded into DATABASE_URL.
  B. The prompt loop reprompts on an empty entry and on a mismatch, accepting
     only a confirmed non-empty password.
  C. A pre-existing src/.env is left untouched (DATABASE_URL not rewritten).

Runnable directly (exit 0 = pass, 1 = fail) or via pytest:

    python3 test/test_setup_db_password.py
    pytest test/test_setup_db_password.py
"""

import os
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_SH = REPO_ROOT / "scripts" / "setup.sh"
REQUIREMENTS = REPO_ROOT / "scripts" / "requirements.txt"
ENV_EXAMPLE = REPO_ROOT / "src" / ".env.example"

# Absolute path to the real sed, resolved before our stub shadows it on PATH.
_REAL_SED = shutil.which("sed") or "/usr/bin/sed"


def _write_stubs(stub_dir: Path, psql_log: Path) -> None:
    """Create the fake external commands setup.sh calls, on a dir meant for PATH."""
    stub_dir.mkdir(parents=True, exist_ok=True)

    def stub(name: str, body: str) -> None:
        p = stub_dir / name
        p.write_text("#!/bin/bash\n" + body)
        p.chmod(0o755)

    # sudo: drop a leading "-u <user>", then run the rest (resolves via PATH to our stubs).
    stub("sudo", '[ "$1" = "-u" ] && shift 2\nexec "$@"\n')

    # psql: record each `-c` SQL statement, always succeed.
    stub("psql", (
        'while [ $# -gt 0 ]; do\n'
        '  if [ "$1" = "-c" ]; then echo "$2" >> "%s"; shift 2; else shift; fi\n'
        'done\nexit 0\n'
    ) % psql_log)

    for name in ("apt-get", "systemctl", "ollama", "curl"):
        stub(name, "exit 0\n")

    # sed shim: make the script's GNU-style `sed -i "expr" file` work on BSD too.
    stub("sed", (
        'if [ "$1" = "-i" ]; then\n'
        '  shift\n'
        '  if "%s" --version >/dev/null 2>&1; then exec "%s" -i "$@"; else exec "%s" -i "" "$@"; fi\n'
        'fi\n'
        'exec "%s" "$@"\n'
    ) % (_REAL_SED, _REAL_SED, _REAL_SED, _REAL_SED))

    # python3: intercept `-m venv <dir>` with a fake venv; delegate everything
    # else (e.g. the urllib percent-encoding) to the real interpreter.
    stub("python3", (
        'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
        '  d="$3"; mkdir -p "$d/bin"\n'
        '  printf "#!/bin/bash\\nexit 0\\n" > "$d/bin/pip";     chmod +x "$d/bin/pip"\n'
        '  printf "#!/bin/bash\\nexit 0\\n" > "$d/bin/python3"; chmod +x "$d/bin/python3"\n'
        '  exit 0\n'
        'fi\n'
        'exec "%s" "$@"\n'
    ) % sys.executable)


def _run_setup(tmp: Path, stdin: str, preexisting_env: str | None = None):
    """Run setup.sh in a sandbox; return (captured psql SQL, resulting .env text)."""
    box = tmp / "box"
    (box / "scripts").mkdir(parents=True)
    (box / "src").mkdir(parents=True)
    shutil.copy(SETUP_SH, box / "scripts" / "setup.sh")
    shutil.copy(REQUIREMENTS, box / "scripts" / "requirements.txt")
    shutil.copy(ENV_EXAMPLE, box / "src" / ".env.example")
    env_path = box / "src" / ".env"
    if preexisting_env is not None:
        env_path.write_text(preexisting_env)

    stub_dir = tmp / "stubs"
    psql_log = tmp / "psql.log"
    psql_log.write_text("")
    _write_stubs(stub_dir, psql_log)

    env = dict(os.environ, PATH=f"{stub_dir}:{os.environ['PATH']}")
    subprocess.run(
        ["bash", str(box / "scripts" / "setup.sh")],
        input=stdin, env=env, text=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )
    return psql_log.read_text(), (env_path.read_text() if env_path.exists() else "")


def _alter_line(sql_log: str) -> str:
    for line in sql_log.splitlines():
        if "ALTER ROLE" in line:
            return line
    return ""


def _database_url(env_text: str) -> str:
    for line in env_text.splitlines():
        if line.startswith("DATABASE_URL="):
            return line[len("DATABASE_URL="):]
    return ""


def test_valid_password_is_escaped_and_encoded(tmp_path):
    pw = "pa's/w@rd $x"  # single-quote, slash, @, space, dollar
    sql_log, env_text = _run_setup(tmp_path, f"{pw}\n{pw}\n")

    # SQL literal: single quotes doubled so the ALTER ROLE stays well-formed.
    assert _alter_line(sql_log) == f"ALTER ROLE trump_tracker_user PASSWORD '{pw.replace(chr(39), chr(39) * 2)}';"
    # DATABASE_URL: password percent-encoded.
    encoded = urllib.parse.quote(pw, safe="")
    assert _database_url(env_text) == f"postgresql://trump_tracker_user:{encoded}@localhost/trump_tracker"


def test_prompt_reprompts_on_empty_and_mismatch(tmp_path):
    # empty pair, then mismatched pair, then a confirmed valid one.
    pw = "s3cret!"
    sql_log, env_text = _run_setup(tmp_path, f"\nX\na\nb\n{pw}\n{pw}\n")

    assert _alter_line(sql_log) == f"ALTER ROLE trump_tracker_user PASSWORD '{pw}';"
    assert _database_url(env_text) == (
        f"postgresql://trump_tracker_user:{urllib.parse.quote(pw, safe='')}@localhost/trump_tracker"
    )


def test_existing_env_is_left_untouched(tmp_path):
    original = "DATABASE_URL=postgresql://preexisting:DONOTCHANGE@localhost/trump_tracker\n"
    sql_log, env_text = _run_setup(tmp_path, "newpass\nnewpass\n", preexisting_env=original)

    # Password is still (re)applied to the role...
    assert _alter_line(sql_log) == "ALTER ROLE trump_tracker_user PASSWORD 'newpass';"
    # ...but the existing .env's DATABASE_URL is preserved.
    assert _database_url(env_text) == "postgresql://preexisting:DONOTCHANGE@localhost/trump_tracker"


if __name__ == "__main__":
    import tempfile

    tests = [
        test_valid_password_is_escaped_and_encoded,
        test_prompt_reprompts_on_empty_and_mismatch,
        test_existing_env_is_left_untouched,
    ]
    failed = 0
    for fn in tests:
        with tempfile.TemporaryDirectory() as d:
            try:
                fn(Path(d))
                print(f"PASS: {fn.__name__}")
            except AssertionError as exc:
                print(f"FAIL: {fn.__name__}: {exc}")
                failed += 1
            except subprocess.CalledProcessError as exc:
                print(f"FAIL: {fn.__name__}: setup.sh exited {exc.returncode}")
                failed += 1
    sys.exit(1 if failed else 0)
