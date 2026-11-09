"""Validate a monthly NPPES ZIP and optionally load both Dev staging tables."""

import argparse
import csv
import os
import sys
from contextlib import closing
from itertools import batched, islice
from pathlib import Path
from zipfile import BadZipFile

from inspect_nppes import DETAIL_FIELDS, REFERENCE_FIELDS, open_provider_csv
from test_database import connection_string


REFERENCE_TABLE = "[dedup].[NPPES_Reference_Staging]"
DETAIL_TABLE = "[dedup].[NPPES_Provider_Details_Staging]"
BATCH_SIZE = 1_000


def insert_sql(table: str, fields: dict[str, str]) -> str:
    columns = ", ".join(f"[{name}]" for name in fields)
    parameters = ", ".join("?" for _ in fields)
    return f"INSERT INTO {table} ({columns}) VALUES ({parameters})"


def stage_records(connection, records) -> int:
    with closing(connection.cursor()) as cursor:
        cursor.execute("SET LOCK_TIMEOUT 30000; SET XACT_ABORT ON;")
        for table in (REFERENCE_TABLE, DETAIL_TABLE):
            count = cursor.execute(
                f"SELECT COUNT_BIG(*) FROM {table} WITH (TABLOCKX, HOLDLOCK)"
            ).fetchone()[0]
            if count:
                raise ValueError(f"{table} already contains rows. Existing staging data was preserved.")

        reference_insert = insert_sql(REFERENCE_TABLE, REFERENCE_FIELDS)
        detail_insert = insert_sql(DETAIL_TABLE, DETAIL_FIELDS)
        cursor.fast_executemany = True
        count = 0
        for batch in batched(records, BATCH_SIZE):
            reference_values = [tuple(record[name] for name in REFERENCE_FIELDS) for record in batch]
            detail_values = [tuple(record[name] for name in DETAIL_FIELDS) for record in batch]
            cursor.executemany(reference_insert, reference_values)
            cursor.executemany(detail_insert, detail_values)
            count += len(batch)

        if not count:
            raise ValueError("The primary provider CSV has no data records.")
        for table in (REFERENCE_TABLE, DETAIL_TABLE):
            staged_count = cursor.execute(f"SELECT COUNT_BIG(*) FROM {table}").fetchone()[0]
            if staged_count != count:
                raise ValueError(f"{table} has {staged_count:,} rows. Expected {count:,}.")

        unmatched = cursor.execute(f"""
            SELECT TOP (1) 1
            FROM {REFERENCE_TABLE} AS src
            WHERE NOT EXISTS (SELECT 1 FROM {DETAIL_TABLE} AS dst WHERE dst.NPI = src.NPI)
        """).fetchone()
        if unmatched is not None:
            raise ValueError("The staging tables contain different NPI sets.")
    return count


def load_staging(records) -> int:
    server = os.environ.get("NPPES_SQL_SERVER", "").strip()
    if not server:
        raise ValueError("Set NPPES_SQL_SERVER to your SQL Server hostname.")
    try:
        import pyodbc
    except ImportError:
        raise ValueError("Install pyodbc with python -m pip install pyodbc.") from None

    try:
        with closing(pyodbc.connect(connection_string(server), timeout=10, autocommit=False)) as connection:
            try:
                count = stage_records(connection, records)
            except BaseException:
                connection.rollback()
                raise
            connection.commit()
    except pyodbc.Error as error:
        raise ValueError(f"SQL Server staging failed: {error}") from error
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="Path to the downloaded CMS ZIP.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stage", action="store_true", help="Write the full release to empty Dev staging tables.")
    mode.add_argument("--sample-rows", type=int, help="Limit the dry run to this many records.")
    args = parser.parse_args()
    if args.sample_rows is not None and args.sample_rows < 1:
        parser.error("--sample-rows must be positive")

    try:
        with open_provider_csv(args.archive) as (_, _, records):
            if args.stage:
                count = load_staging(records)
                print(f"Committed {count:,} rows to each Dev staging table.")
            else:
                count = sum(1 for _ in islice(records, args.sample_rows))
                if not count:
                    raise ValueError("The primary provider CSV has no data records.")
                print(f"Dry run validated {count:,} provider records. No database connection was made.")
    except (OSError, BadZipFile, csv.Error, ValueError) as error:
        print(f"Could not stage the NPPES release: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
