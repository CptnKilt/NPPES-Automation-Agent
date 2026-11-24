"""Preview or add audit timestamps to the final Dev provider details table."""

import argparse
import os
from contextlib import closing

from migrate_provider_columns import provider_columns
from test_database import connection_string

TABLE = "[dedup].[NPPES_Provider_Details]"
AUDIT_COLUMNS = ("InsertedUTC", "LastUpdatedUTC")


def validate_timestamps(cursor):
    existing = provider_columns(cursor, TABLE)
    for name in AUDIT_COLUMNS:
        actual = existing.get(name)
        if actual is None or actual[0] != "datetime2" or not actual[2]:
            raise ValueError("Run migrate_provider_timestamps.py --apply before importing providers.")


def migrate(connection, apply=False):
    try:
        with closing(connection.cursor()) as cursor:
            cursor.execute("SET XACT_ABORT ON; SET LOCK_TIMEOUT 30000;")
            existing = provider_columns(cursor, TABLE)
            if "ImportedUTC" not in existing:
                raise ValueError("Cannot find the existing provider import timestamp.")
            missing = []
            for name in AUDIT_COLUMNS:
                actual = existing.get(name)
                if actual is None:
                    missing.append(name)
                elif actual[0] != "datetime2" or not actual[2]:
                    raise ValueError(f"Incompatible audit column: {name}")
            if apply:
                for name in missing:
                    cursor.execute(f"ALTER TABLE {TABLE} ADD [{name}] datetime2(7) NULL")
                if "LastUpdatedUTC" in missing:
                    cursor.execute(f"UPDATE {TABLE} SET [LastUpdatedUTC] = [ImportedUTC]")
                validate_timestamps(cursor)
        if apply:
            connection.commit()
        else:
            connection.rollback()
        return missing
    except BaseException:
        connection.rollback()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sql-server", default=os.environ.get("NPPES_SQL_SERVER"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.sql_server:
        parser.error("Provide --sql-server or set NPPES_SQL_SERVER.")
    import pyodbc
    try:
        with closing(pyodbc.connect(connection_string(args.sql_server), timeout=10, autocommit=False)) as connection:
            missing = migrate(connection, args.apply)
        print(f"Audit columns {'added' if args.apply else 'to add'}: {', '.join(missing) or 'none'}")
    except (pyodbc.Error, ValueError) as error:
        print(f"Timestamp migration failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
