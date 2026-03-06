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
    ALL_RESULTS_FILE      Path to write all results (optional)
    ERRORS_FILE           Path to write only failing results (optional)

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


def get_current_branch() -> str:
    """Return the name of the current git branch."""
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def get_changed_sql_files(base_ref: str = "master") -> list[Path]:
    """Return .sql files added or modified between *base_ref* and HEAD."""
    repo_root = get_repo_root()
    result = subprocess.run(
        ["git", "diff", "--diff-filter=AM", "--name-only", f"{base_ref}...HEAD"],
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
    sql_text = sql_path.read_text(encoding="latin-1")
    wrapped = f"SET NOCOUNT ON;\nSET NOEXEC ON;\n{sql_text}"
    try:
        with engine.connect() as conn:
            conn.execute(text(wrapped))
        return (sql_path, True, "OK")
    except Exception as exc:  # noqa: BLE001
        orig = getattr(exc, "orig", exc)
        return (sql_path, False, str(orig))


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
        source_branch = get_current_branch()
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
    engine = create_engine(
        connection_string,
        pool_size=workers,
        max_overflow=0,
        isolation_level="READ UNCOMMITTED",
        connect_args={"autocommit": True},
    )

    all_results_path = os.environ.get("ALL_RESULTS_FILE")
    errors_path = os.environ.get("ERRORS_FILE")

    failed: list[Path] = []

    header = f"# source branch: {source_branch} -> {base_ref}\n\n"

    try:
        all_fh = open(all_results_path, "w", encoding="utf-8") if all_results_path else None  # noqa: SIM115
        err_fh = open(errors_path, "w", encoding="utf-8") if errors_path else None  # noqa: SIM115
        if all_fh:
            all_fh.write(header)
        if err_fh:
            err_fh.write(header)
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(check_query, engine, f): f for f in sql_files}
                for future in as_completed(futures):
                    sql_path, success, message = future.result()
                    status = "PASS" if success else "FAIL"
                    print(f"[{status}] {sql_path}")
                    if all_fh:
                        all_fh.write(f"[{status}] {sql_path}\n")
                    if not success:
                        print(f"       {message}")
                        if all_fh:
                            all_fh.write(f"       {message}\n")
                        if err_fh:
                            err_fh.write(f"[FAIL] {sql_path}\n       {message}\n")
                        failed.append(sql_path)
            print()
            if failed:
                summary = f"{len(failed)} file(s) failed:\n" + "".join(f"  {f}\n" for f in failed)
            else:
                summary = f"All {len(sql_files)} file(s) passed.\n"
            print(summary, end="")
            if all_fh:
                all_fh.write(f"\n{summary}")
            if err_fh:
                err_fh.write(f"\n{summary}")
        finally:
            if all_fh:
                all_fh.close()
            if err_fh:
                err_fh.close()
    finally:
        engine.dispose()

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
