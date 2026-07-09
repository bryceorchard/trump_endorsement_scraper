"""
test_setup_db_password.py — Behavioural test for the PostgreSQL + venv setup flow
in scripts/setup.sh.

Drives the *real* setup.sh end-to-end in a throwaway sandbox with every
side-effecting command (sudo/psql/apt-get/systemctl/ollama/curl, plus
`python3 -m venv` and the venv's pip) replaced by a stub on PATH. Nothing on the
host is touched, so this is safe to run anywhere — including the Pi.

Stubs are instrumented:
  - psql logs every mutating statement, and answers the `SELECT 1 FROM
    pg_database/pg_roles` probes from FAKE_DB_EXISTS / FAKE_ROLE_EXISTS.
  - the venv's pip logs its invocations to $PIP_LOG, so we can assert whether a
    dependency install actually ran.

Covered behaviours:
  - Fresh install: prompt (hidden, confirm-retry, non-empty), CREATE + ALTER
    ROLE with SQL-escaped password, DATABASE_URL percent-encoded into .env, and
    pip install of requirements.
  - Existing DB/role → menu: keep / reset password / drop & recreate (typed
    DROP confirm).
  - A pre-existing src/.env is never rewritten.
  - Idempotency: an existing venv with a matching requirements hash skips the
    pip install entirely.

Runnable directly (exit 0 = pass, 1 = fail) or via pytest.
"""

import hashlib
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

_REAL_SED = shutil.which("sed") or "/usr/bin/sed"

# A pip that records its arguments (so tests can see if an install ran).
_PIP_STUB = '#!/bin/bash\n[ -n "$PIP_LOG" ] && echo "$@" >> "$PIP_LOG"\nexit 0\n'


def _example_database_url() -> str:
    for line in ENV_EXAMPLE.read_text().splitlines():
        if line.startswith("DATABASE_URL="):
            return line[len("DATABASE_URL="):]
    return ""


def _requirements_hash() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def _write_stubs(stub_dir: Path, psql_log: Path) -> None:
    stub_dir.mkdir(parents=True, exist_ok=True)

    def stub(name: str, body: str) -> None:
        p = stub_dir / name
        p.write_text(body)
        p.chmod(0o755)

    stub("sudo", '#!/bin/bash\n[ "$1" = "-u" ] && shift 2\nexec "$@"\n')

    stub("psql",
         '#!/bin/bash\n'
         'while [ $# -gt 0 ]; do\n'
         '  case "$1" in\n'
         '    -c|-tAc|-tc|-Ac)\n'
         '      sql="$2"; shift 2\n'
         '      case "$sql" in\n'
         '        *pg_database*) [ -n "$FAKE_DB_EXISTS" ] && echo 1 ;;\n'
         '        *pg_roles*)    [ -n "$FAKE_ROLE_EXISTS" ] && echo 1 ;;\n'
         '        *) printf "%s\\n" "$sql" >> "__PSQL_LOG__" ;;\n'
         '      esac ;;\n'
         '    *) shift ;;\n'
         '  esac\n'
         'done\nexit 0\n'.replace("__PSQL_LOG__", str(psql_log)))

    for name in ("apt-get", "systemctl", "ollama", "curl"):
        stub(name, "#!/bin/bash\nexit 0\n")

    stub("sed",
         '#!/bin/bash\n'
         'if [ "$1" = "-i" ]; then\n'
         '  shift\n'
         '  if "__SED__" --version >/dev/null 2>&1; then exec "__SED__" -i "$@"; '
         'else exec "__SED__" -i "" "$@"; fi\n'
         'fi\n'
         'exec "__SED__" "$@"\n'.replace("__SED__", _REAL_SED))

    stub("python3",
         '#!/bin/bash\n'
         'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
         '  d="$3"; mkdir -p "$d/bin"\n'
         "  cat > \"$d/bin/pip\" <<'PIPEOF'\n"
         + _PIP_STUB +
         'PIPEOF\n'
         '  printf "#!/bin/bash\\nexit 0\\n" > "$d/bin/python3"\n'
         '  chmod +x "$d/bin/pip" "$d/bin/python3"\n'
         '  exit 0\n'
         'fi\n'
         'exec "__REALPY__" "$@"\n'.replace("__REALPY__", sys.executable))


def _run_setup(tmp: Path, stdin: str, *, preexisting_env=None,
               db_exists=False, role_exists=False, preseed_deps=False):
    """Run setup.sh in a sandbox. Returns (psql SQL log, .env text, pip log)."""
    box = tmp / "box"
    (box / "scripts").mkdir(parents=True)
    (box / "src").mkdir(parents=True)
    shutil.copy(SETUP_SH, box / "scripts" / "setup.sh")
    shutil.copy(REQUIREMENTS, box / "scripts" / "requirements.txt")
    shutil.copy(ENV_EXAMPLE, box / "src" / ".env.example")
    env_path = box / "src" / ".env"
    if preexisting_env is not None:
        env_path.write_text(preexisting_env)

    if preseed_deps:
        venv_bin = box / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "pip").write_text(_PIP_STUB)
        (venv_bin / "pip").chmod(0o755)
        (venv_bin / "python3").write_text("#!/bin/bash\nexit 0\n")
        (venv_bin / "python3").chmod(0o755)
        (box / ".venv" / ".requirements-sha256").write_text(_requirements_hash())

    stub_dir = tmp / "stubs"
    psql_log = tmp / "psql.log"
    pip_log = tmp / "pip.log"
    psql_log.write_text("")
    pip_log.write_text("")
    _write_stubs(stub_dir, psql_log)

    env = dict(os.environ, PATH=f"{stub_dir}:{os.environ['PATH']}", PIP_LOG=str(pip_log))
    if db_exists:
        env["FAKE_DB_EXISTS"] = "1"
    if role_exists:
        env["FAKE_ROLE_EXISTS"] = "1"

    subprocess.run(
        ["bash", str(box / "scripts" / "setup.sh")],
        input=stdin, env=env, text=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )
    return psql_log.read_text(), (env_path.read_text() if env_path.exists() else ""), pip_log.read_text()


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


def _expected_url(pw: str) -> str:
    enc = urllib.parse.quote(pw, safe="")
    return f"postgresql://trump_tracker_user:{enc}@localhost/trump_tracker"


# ── Fresh install ─────────────────────────────────────────────────────────────

def test_fresh_password_is_escaped_and_encoded(tmp_path):
    pw = "pa's/w@rd $x"  # single-quote, slash, @, space, dollar
    sql_log, env_text, pip_log = _run_setup(tmp_path, f"{pw}\n{pw}\n")

    assert _alter_line(sql_log) == f"ALTER ROLE trump_tracker_user PASSWORD '{pw.replace(chr(39), chr(39) * 2)}';"
    assert "CREATE DATABASE trump_tracker;" in sql_log
    assert _database_url(env_text) == _expected_url(pw)
    assert "install -r" in pip_log  # deps installed on a fresh venv


def test_fresh_prompt_reprompts_on_empty_and_mismatch(tmp_path):
    pw = "s3cret!"
    sql_log, env_text, _ = _run_setup(tmp_path, f"\nX\na\nb\n{pw}\n{pw}\n")

    assert _alter_line(sql_log) == f"ALTER ROLE trump_tracker_user PASSWORD '{pw}';"
    assert _database_url(env_text) == _expected_url(pw)


def test_existing_env_is_left_untouched(tmp_path):
    original = "DATABASE_URL=postgresql://preexisting:DONOTCHANGE@localhost/trump_tracker\n"
    sql_log, env_text, _ = _run_setup(tmp_path, "newpass\nnewpass\n", preexisting_env=original)

    assert _alter_line(sql_log) == "ALTER ROLE trump_tracker_user PASSWORD 'newpass';"
    assert _database_url(env_text) == "postgresql://preexisting:DONOTCHANGE@localhost/trump_tracker"


# ── Idempotency ───────────────────────────────────────────────────────────────

def test_deps_skipped_when_requirements_unchanged(tmp_path):
    # Venv + a matching hash stamp already present → the pip install must be skipped.
    _, _, pip_log = _run_setup(tmp_path, "pw\npw\n", preseed_deps=True)
    assert "install -r" not in pip_log, f"expected deps install to be skipped; pip log:\n{pip_log}"


# ── Existing DB/role detected → menu ──────────────────────────────────────────

def test_detected_keep_leaves_password_and_db_alone(tmp_path):
    sql_log, env_text, _ = _run_setup(tmp_path, "1\n", db_exists=True, role_exists=True)

    assert "ALTER ROLE" not in sql_log
    assert "CREATE DATABASE" not in sql_log
    assert "DROP DATABASE" not in sql_log
    assert _database_url(env_text) == _example_database_url()


def test_detected_reset_password(tmp_path):
    pw = "res3t@me"
    sql_log, env_text, _ = _run_setup(tmp_path, f"2\n{pw}\n{pw}\n", db_exists=True, role_exists=True)

    assert _alter_line(sql_log) == f"ALTER ROLE trump_tracker_user PASSWORD '{pw}';"
    assert "CREATE DATABASE" not in sql_log
    assert "DROP DATABASE" not in sql_log
    assert _database_url(env_text) == _expected_url(pw)


def test_detected_drop_and_recreate_when_confirmed(tmp_path):
    pw = "brandnew1"
    sql_log, _, _ = _run_setup(tmp_path, f"3\nDROP\n{pw}\n{pw}\n", db_exists=True, role_exists=True)

    assert "DROP DATABASE IF EXISTS trump_tracker;" in sql_log
    assert "CREATE DATABASE trump_tracker;" in sql_log
    assert _alter_line(sql_log) == f"ALTER ROLE trump_tracker_user PASSWORD '{pw}';"


def test_detected_drop_aborts_without_confirmation(tmp_path):
    sql_log, _, _ = _run_setup(tmp_path, "3\nnope\n", db_exists=True, role_exists=True)

    assert "DROP DATABASE" not in sql_log
    assert "ALTER ROLE" not in sql_log
    assert "CREATE DATABASE" not in sql_log


if __name__ == "__main__":
    import tempfile

    tests = [
        test_fresh_password_is_escaped_and_encoded,
        test_fresh_prompt_reprompts_on_empty_and_mismatch,
        test_existing_env_is_left_untouched,
        test_deps_skipped_when_requirements_unchanged,
        test_detected_keep_leaves_password_and_db_alone,
        test_detected_reset_password,
        test_detected_drop_and_recreate_when_confirmed,
        test_detected_drop_aborts_without_confirmation,
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
