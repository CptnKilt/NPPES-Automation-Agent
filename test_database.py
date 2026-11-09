"""Check the Dev database connection and sample the NPPES tables."""

import os
import sys
from contextlib import closing


DATABASE = "Dev"
DRIVER = "ODBC Driver 17 for SQL Server"
SCHEMA = "dedup"
TABLES = (
    "NPPES_reference",
    "NPPES_provider_details",
    "NPPES_import_history",
)
SAMPLE_SIZE = 3
TABLE_EXISTS_SQL = """
SELECT TOP (1) 1
FROM sys.tables AS t
JOIN sys.schemas AS s ON s.schema_id = t.schema_id
WHERE s.name = ? AND t.name = ?
"""


def connection_string(server: str) -> str:
    return (
        f"DRIVER={{{DRIVER}}};"
        f"SERVER={{{server.replace('}', '}}')}}};"
        f"DATABASE={DATABASE};"
        "Trusted_Connection=yes;"
        "Encrypt=yes;"
        "TrustServerCertificate=yes;"
    )


def main() -> int:
    server = os.environ.get("NPPES_SQL_SERVER", "").strip()
    if not server:
        print("Set NPPES_SQL_SERVER to your SQL Server hostname.", file=sys.stderr)
        return 1

    try:
        import pyodbc
    except ImportError as error:
        print(
            f"Could not load pyodbc ({error}). Install it with python -m pip install pyodbc.",
            file=sys.stderr,
        )
        return 1

    if DRIVER not in pyodbc.drivers():
        print(f"Required ODBC driver is unavailable: {DRIVER}", file=sys.stderr)
        return 1

    try:
        connection = pyodbc.connect(connection_string(server), timeout=10, autocommit=True)
    except pyodbc.Error as error:
        print(f"Could not connect to {DATABASE} on {server}: {error}", file=sys.stderr)
        return 1

    try:
        with closing(connection):
            print(f"Successfully connected to {DATABASE} on {server}.", flush=True)
            connection.timeout = 15
            with closing(connection.cursor()) as cursor:
                sample_failed = False
                for table in TABLES:
                    cursor.execute(TABLE_EXISTS_SQL, SCHEMA, table)
                    if cursor.fetchone() is None:
                        print(f"{SCHEMA}.{table}: not found (missing or hidden by metadata permissions)")
                        continue

                    print(f"{SCHEMA}.{table}: exists")
                    try:
                        # The identifiers come from the fixed SCHEMA and TABLES constants.
                        cursor.execute(f"SELECT TOP ({SAMPLE_SIZE}) * FROM [{SCHEMA}].[{table}]")
                        columns = ", ".join(column[0] for column in cursor.description)
                        rows = cursor.fetchall()
                        print(f"  Columns: {columns}")
                        print(f"  Sample rows: {len(rows)}")
                        for row in rows:
                            print(f"    {tuple(row)!r}")
                    except pyodbc.Error as error:
                        sample_failed = True
                        print(f"Could not read {SCHEMA}.{table}: {error}", file=sys.stderr)
    except pyodbc.Error as error:
        print(f"Database table check failed: {error}", file=sys.stderr)
        return 1

    return 1 if sample_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
