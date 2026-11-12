"""Verify and promote a staged monthly NPPES release into the Dev tables."""

import argparse
import os
import re
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import BadZipFile

from backup_nppes import DEFAULT_BACKUP_DIR, write_backup, write_table_backup
from download_nppes import MONTHLY_ARCHIVE, MONTHS
from inspect_nppes import DETAIL_FIELDS, REFERENCE_FIELDS
from stage_nppes import DETAIL_TABLE, REFERENCE_TABLE
from test_database import connection_string


REFERENCE_TARGET = "[dedup].[NPPES_Reference]"
DETAIL_TARGET = "[dedup].[NPPES_Provider_Details]"
HISTORY_TABLE = "[dedup].[NPPES_Import_History]"


def release_name(source_url: str) -> str:
    url = urlsplit(source_url)
    archive = MONTHLY_ARCHIVE.fullmatch(url.path)
    if (
        url.scheme != "https" or url.netloc != "download.cms.gov"
        or archive is None or archive[1] not in MONTHS
        or len(source_url.encode("utf-16-le")) // 2 > 2_000
    ):
        raise ValueError("Use the official monthly CMS Version 2 ZIP URL as --source-url.")
    return url.path.rsplit("/", 1)[1]


def validate_staging(cursor, expected_rows: int) -> None:
    for table in (REFERENCE_TABLE, DETAIL_TABLE):
        count = cursor.execute(f"SELECT COUNT_BIG(*) FROM {table} WITH (TABLOCK, HOLDLOCK)").fetchone()[0]
        if count != expected_rows:
            raise ValueError(f"{table} has {count:,} rows. Expected {expected_rows:,}.")

    mismatch = cursor.execute(f"""
        SELECT TOP (1) 1
        FROM (
            SELECT NPI, EntityType, DeactivationDate FROM {REFERENCE_TABLE}
            EXCEPT
            SELECT NPI, EntityType, DeactivationDate FROM {DETAIL_TABLE}
        ) AS mismatched
    """).fetchone()
    if mismatch is not None:
        raise ValueError("Staging NPI sets or shared values do not agree.")


def upsert(cursor, staging: str, target: str, fields: dict[str, str], timestamp: str) -> None:
    columns = ", ".join(f"[{name}]" for name in fields)
    values = ", ".join(f"src.[{name}]" for name in fields)
    data_columns = [name for name in fields if name != "NPI"]
    assignments = ", ".join(f"[{name}] = src.[{name}]" for name in data_columns)

    # Compare the encoded values so case, accents, and trailing spaces remain significant.
    source_values = ", ".join(f"CONVERT(varbinary(max), src.[{name}])" for name in data_columns)
    target_values = ", ".join(f"CONVERT(varbinary(max), dst.[{name}])" for name in data_columns)
    cursor.execute(f"""
        UPDATE dst
        SET {assignments}, [{timestamp}] = SYSUTCDATETIME()
        FROM {target} AS dst WITH (TABLOCKX, HOLDLOCK)
        JOIN {staging} AS src ON src.NPI = dst.NPI
        WHERE EXISTS (SELECT {source_values} EXCEPT SELECT {target_values})
    """)
    cursor.execute(f"""
        INSERT INTO {target} ({columns})
        SELECT {values}
        FROM {staging} AS src
        WHERE NOT EXISTS (
            SELECT 1 FROM {target} AS dst WITH (TABLOCKX, HOLDLOCK) WHERE dst.NPI = src.NPI
        )
    """)
    missing = cursor.execute(f"""
        SELECT TOP (1) 1
        FROM {staging} AS src
        WHERE NOT EXISTS (SELECT 1 FROM {target} AS dst WHERE dst.NPI = src.NPI)
    """).fetchone()
    if missing is not None:
        raise ValueError(f"{target} is missing staged NPIs after promotion.")


def promote(
    connection, expected_rows: int, backup_label: str | None, backup_dir: Path,
    source_url: str | None, backup_mode: str, *, clear_staging: bool = False,
) -> None:
    try:
        with closing(connection.cursor()) as cursor:
            cursor.execute("SET LOCK_TIMEOUT 30000; SET XACT_ABORT ON;")
            validate_staging(cursor, expected_rows)
            if backup_label is not None:
                if source_url is None:
                    raise ValueError("Live promotion requires the CMS source URL.")
                release = release_name(source_url)
                prior = cursor.execute(f"""
                    SELECT 1 FROM {HISTORY_TABLE} WITH (UPDLOCK, HOLDLOCK)
                    WHERE ReleaseName = ?
                """, release).fetchone()
                if prior is not None:
                    raise ValueError(f"{release} is already in import history.")
                print("Staging checks passed. Backing up both Dev final tables.", flush=True)
                targets = (REFERENCE_TARGET, DETAIL_TARGET)
                if backup_mode == "tables":
                    snapshots = write_table_backup(cursor, targets, backup_label)
                    print(f"Verified backup tables (pending commit): {', '.join(snapshots)}", flush=True)
                else:
                    backup = write_backup(cursor, targets, backup_label, backup_dir)
                    print(f"Verified backup: {backup}", flush=True)
                upsert(cursor, REFERENCE_TABLE, REFERENCE_TARGET, REFERENCE_FIELDS, "LastUpdated")
                print("Reference checks passed. Updating the Dev provider details table.", flush=True)
                upsert(cursor, DETAIL_TABLE, DETAIL_TARGET, DETAIL_FIELDS, "ImportedUTC")
                cursor.execute(f"""
                    INSERT INTO {HISTORY_TABLE} (ReleaseName, RecordCount, SourceURL)
                    VALUES (?, ?, ?)
                """, release, expected_rows, source_url)
                if clear_staging:
                    cursor.execute(f"TRUNCATE TABLE {DETAIL_TABLE}")
                    cursor.execute(f"TRUNCATE TABLE {REFERENCE_TABLE}")
        if backup_label is not None:
            connection.commit()
        else:
            connection.rollback()
    except BaseException:
        connection.rollback()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-rows", required=True, type=int, help="Committed staging row count per table.")
    parser.add_argument("--apply", action="store_true", help="Commit the verified release to the Dev final tables.")
    parser.add_argument("--backup-label", help="Prior release label, required with --apply.")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR, help="Local backup directory.")
    parser.add_argument("--backup-mode", choices=("zip", "tables"), default="zip",
                        help="Backup destination for --apply (default: zip).")
    parser.add_argument("--source-url", help="Official CMS monthly ZIP URL, required with --apply.")
    args = parser.parse_args()
    if args.expected_rows < 1:
        parser.error("--expected-rows must be positive")
    if args.apply and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", args.backup_label or ""):
        parser.error("--apply requires a --backup-label of up to 64 letters, digits, underscores, or hyphens")
    if not args.apply and args.backup_label is not None:
        parser.error("--backup-label is only used with --apply")
    if args.apply:
        try:
            release_name(args.source_url or "")
        except ValueError as error:
            parser.error(str(error))
    elif args.source_url is not None or args.backup_mode != "zip":
        parser.error("--source-url and --backup-mode tables are only used with --apply")

    server = os.environ.get("NPPES_SQL_SERVER", "").strip()
    if not server:
        print("Set NPPES_SQL_SERVER to your SQL Server hostname.", file=sys.stderr)
        return 1
    try:
        import pyodbc
    except ImportError:
        print("Install pyodbc with python -m pip install pyodbc.", file=sys.stderr)
        return 1

    try:
        with closing(pyodbc.connect(connection_string(server), timeout=10, autocommit=False)) as connection:
            promote(
                connection, args.expected_rows, args.backup_label, args.backup_dir,
                args.source_url, args.backup_mode,
            )
    except (pyodbc.Error, OSError, BadZipFile, TypeError, ValueError) as error:
        print(f"Could not promote the NPPES release: {error}", file=sys.stderr)
        return 1

    if args.apply:
        print(f"Promotion committed. Both Dev final tables contain all {args.expected_rows:,} staged NPIs.")
    else:
        print(f"Preflight passed for {args.expected_rows:,} staged NPIs. Final tables were not updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
