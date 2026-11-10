"""Write a verified archive of the Dev final tables before promotion."""

import json
import os
from datetime import UTC, date, datetime
from io import TextIOWrapper
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile


DEFAULT_BACKUP_DIR = Path(__file__).resolve().parent / "backups"


def encode_value(value):
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Unsupported backup value type: {type(value).__name__}")


def write_backup(cursor, tables: tuple[str, ...], label: str, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    created = datetime.now(UTC)
    prefix = f"NPPES-{label}-{created:%Y%m%dT%H%M%SZ}-"
    with NamedTemporaryFile(dir=directory, prefix=prefix, suffix=".part", delete=False) as temporary:
        partial = Path(temporary.name)

    try:
        # Lock both tables before exporting either so the files describe one prior state.
        expected = {
            table: cursor.execute(
                f"SELECT COUNT_BIG(*) FROM {table} WITH (TABLOCKX, HOLDLOCK)"
            ).fetchone()[0]
            for table in tables
        }
        manifest = {"format_version": 1, "label": label, "created_utc": created.isoformat(), "tables": {}}
        with ZipFile(partial, "w", compression=ZIP_DEFLATED, compresslevel=1) as archive:
            for table, expected_count in expected.items():
                name = table.replace("[", "").replace("]", "")
                member = f"{name}.jsonl"
                cursor.execute(f"SELECT * FROM {table}")
                columns = [{"name": column[0], "type": column[1].__name__} for column in cursor.description]
                count = 0
                with archive.open(member, "w", force_zip64=True) as entry:
                    with TextIOWrapper(entry, encoding="utf-8", newline="\n") as output:
                        while rows := cursor.fetchmany(1_000):
                            for row in rows:
                                output.write(json.dumps(tuple(row), default=encode_value, ensure_ascii=False))
                                output.write("\n")
                            count += len(rows)
                if count != expected_count:
                    raise ValueError(f"{table} backup has {count:,} rows. Expected {expected_count:,}.")
                manifest["tables"][name] = {"member": member, "columns": columns, "rows": count}
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))

        with partial.open("r+b") as backup_file:
            with ZipFile(backup_file) as archive:
                bad_member = archive.testzip()
                if bad_member is not None:
                    raise ValueError(f"Backup integrity check failed for {bad_member}.")
            os.fsync(backup_file.fileno())
        destination = partial.with_suffix(".zip")
        partial.replace(destination)
        return destination
    finally:
        partial.unlink(missing_ok=True)
