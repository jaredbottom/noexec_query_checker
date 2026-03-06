#!/usr/bin/env python3
"""noexec_query_checker: Validate changed SQL queries via SET NOEXEC ON.

Finds .sql files that differ between the current branch and a base ref
(default: master), then tests each one by prepending:

    SET NOCOUNT ON;
    SET NOEXEC ON;

and executing the combined text through SQLAlchemy. SQL Server compiles
the query but does not run it, catching syntax and schema errors without
side effects.

Configuration (environment variables):
    DB_CONNECTION_STRING  SQLAlchemy URL, e.g.:
                            mssql+pyodbc://user:pass@server/db?driver=ODBC+Driver+17+for+SQL+Server
    BASE_REF              Git ref to compare against (default: master)
    MAX_WORKERS           Thread-pool size (default: 8)

Usage:
    DB_CONNECTION_STRING="mssql+pyodbc://..." python noexec_query_checker.py
"""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def get_repo_root() -> Path:
    """Return the root of the current git repository."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


def get_changed_sql_files(base_ref: str = "master") -> list[Path]:
    """Return .sql files changed between *base_ref* and HEAD that still exist."""
    repo_root = get_repo_root()
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
        capture_output=True,
        text=True,
        check=True,
        cwd=repo_root,
    )
    paths = [
        repo_root / line.strip()
        for line in result.stdout.splitlines()
        if line.strip().lower().endswith(".sql")
    ]
    return [p for p in paths if p.exists()]


# ---------------------------------------------------------------------------
# Query checker
# ---------------------------------------------------------------------------

def check_query(engine, sql_path: Path) -> tuple[Path, bool, str]:
    """Execute *sql_path* with NOEXEC ON; return (path, success, message)."""
    sql_text = sql_path.read_text(encoding="utf-8")
    wrapped = f"SET NOCOUNT ON;\nSET NOEXEC ON;\n{sql_text}"
    try:
        with engine.connect() as conn:
            conn.execute(text(wrapped))
        return (sql_path, True, "OK")
    except Exception as exc:  # noqa: BLE001
        return (sql_path, False, str(exc))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    connection_string = os.environ.get("DB_CONNECTION_STRING")
    if not connection_string:
        print(
            "Error: DB_CONNECTION_STRING environment variable is not set.",
            file=sys.stderr,
        )
        sys.exit(1)

    base_ref = os.environ.get("BASE_REF", "master")

    try:
        sql_files = get_changed_sql_files(base_ref)
    except subprocess.CalledProcessError as exc:
        print(f"Error running git diff: {exc.stderr}", file=sys.stderr)
        sys.exit(1)

    if not sql_files:
        print(f"No changed .sql files found compared to '{base_ref}'.")
        return

    print(f"Checking {len(sql_files)} changed SQL file(s) against '{base_ref}':\n")
    for f in sql_files:
        print(f"  {f}")
    print()

    workers = min(len(sql_files), int(os.environ.get("MAX_WORKERS", 8)))
    engine = create_engine(connection_string, pool_size=workers, max_overflow=0)

    failed: list[Path] = []

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(check_query, engine, f): f for f in sql_files}
            for future in as_completed(futures):
                sql_path, success, message = future.result()
                status = "PASS" if success else "FAIL"
                print(f"[{status}] {sql_path}")
                if not success:
                    print(f"       {message}")
                    failed.append(sql_path)
    finally:
        engine.dispose()

    print()
    if failed:
        print(f"{len(failed)} file(s) failed:")
        for f in failed:
            print(f"  {f}")
        sys.exit(1)
    else:
        print(f"All {len(sql_files)} file(s) passed.")


if __name__ == "__main__":
    main()
