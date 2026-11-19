"""Inspect or add the provider columns required for the full NPPES CSV."""

import argparse
import os
import sys
from contextlib import closing

from inspect_nppes import ADDITIONAL_DETAIL_COLUMNS, DETAIL_FIELDS
from test_database import connection_string


TABLES = ("[dedup].[NPPES_Provider_Details]", "[dedup].[NPPES_Provider_Details_Staging]")


def provider_columns(cursor, table: str) -> dict:
    catalog = "tempdb.sys" if table.startswith("#") else "sys"
    object_name = "tempdb.." + table if table.startswith("#") else table
    rows = cursor.execute(f"""
        SELECT c.name, ty.name, c.max_length, c.is_nullable
        FROM {catalog}.columns c JOIN {catalog}.types ty ON ty.user_type_id=c.user_type_id
        WHERE c.object_id=OBJECT_ID(?, 'U')
    """, object_name).fetchall()
    return {row[0]: tuple(row[1:]) for row in rows}


def validate_provider_columns(cursor, table: str) -> None:
    existing = provider_columns(cursor, table)
    missing = set(DETAIL_FIELDS) - set(existing)
    if missing:
        raise ValueError(f"{table} is missing {len(missing)} provider columns. Run the provider schema migration.")
    for name, length in ADDITIONAL_DETAIL_COLUMNS.items():
        actual = existing[name]
        if actual[0] != "nvarchar" or (actual[1] != -1 and actual[1] < length * 2) or not actual[2]:
            raise ValueError(f"{table}.[{name}] has an incompatible definition.")


def migrate(connection, apply: bool) -> dict[str, int]:
    changes = {}
    try:
        with closing(connection.cursor()) as cursor:
            cursor.execute("SET XACT_ABORT ON; SET LOCK_TIMEOUT 30000;")
            for table in TABLES:
                existing = provider_columns(cursor, table)
                if not existing:
                    raise ValueError(f"Cannot read column definitions for {table}.")
                required = set(DETAIL_FIELDS) - set(ADDITIONAL_DETAIL_COLUMNS)
                if not required.issubset(existing):
                    raise ValueError(f"{table} is missing existing mapped provider columns.")
                additions = []
                for name, length in ADDITIONAL_DETAIL_COLUMNS.items():
                    actual = existing.get(name)
                    if actual is None:
                        additions.append(f"[{name}] nvarchar({length}) NULL")
                    elif actual[0] != "nvarchar" or (actual[1] != -1 and actual[1] < length * 2) or not actual[2]:
                        raise ValueError(f"{table}.[{name}] has an incompatible definition.")
                changes[table] = len(additions)
                if apply and additions:
                    cursor.execute(f"ALTER TABLE {table} ADD " + ", ".join(additions))
                if apply:
                    validate_provider_columns(cursor, table)
        if apply:
            connection.commit()
        else:
            connection.rollback()
    except BaseException:
        connection.rollback()
        raise
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sql-server", default=os.environ.get("NPPES_SQL_SERVER"))
    parser.add_argument("--apply", action="store_true", help="Add the missing nullable columns to both Dev tables.")
    args = parser.parse_args()
    if not args.sql_server:
        parser.error("Provide --sql-server or set NPPES_SQL_SERVER.")
    import pyodbc

    try:
        with closing(pyodbc.connect(connection_string(args.sql_server), timeout=10, autocommit=False)) as connection:
            for table, count in migrate(connection, args.apply).items():
                print(f"{table}: {count} columns {'added' if args.apply else 'to add'}")
    except (pyodbc.Error, ValueError) as error:
        print(f"Provider schema migration failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
