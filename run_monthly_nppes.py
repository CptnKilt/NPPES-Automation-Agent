"""Run one daily check for the current Eastern-time NPPES release."""

import argparse
import json
import logging
import msvcrt
import os
import re
import ssl
import time
from calendar import month_name
from contextlib import closing
from datetime import UTC, datetime, timedelta
from http.client import IncompleteRead
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from download_nppes import download_archive, fetch_archive_links, is_valid_archive
from inspect_nppes import ProviderRecordError, open_provider_csv
from promote_nppes import DETAIL_TARGET, HISTORY_TABLE, REFERENCE_TARGET, promote
from stage_nppes import DETAIL_TABLE, REFERENCE_TABLE, StagingOccupiedError, stage_records, write_rejection_report
from test_database import connection_string


WORK_DIR = Path(__file__).resolve().parent / "monthly-work"
MAX_ATTEMPTS = 5
RETRY_INTERVAL = timedelta(minutes=10)
LOG = logging.getLogger("nppes.monthly")


class JobError(ValueError):
    """An operational error with a message safe for the run log."""


def save_state(path: Path, state: dict) -> None:
    with NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(state, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
            output.close()
            temporary.replace(path)
        finally:
            output.close()
            temporary.unlink(missing_ok=True)


def retryable(error: Exception) -> bool:
    if isinstance(error, HTTPError):
        return error.code == 429 or 500 <= error.code <= 599
    return isinstance(error, (URLError, TimeoutError, ConnectionError, IncompleteRead, ssl.SSLError))


def error_summary(error: Exception) -> str:
    if isinstance(error, HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, (JobError, ProviderRecordError, StagingOccupiedError)):
        return str(error)
    return type(error).__name__


def sql_settings(args) -> str:
    server = (args.sql_server or os.environ.get("NPPES_SQL_SERVER", "")).strip()
    if not server:
        raise JobError("Set NPPES_SQL_SERVER or pass --sql-server for the scheduled account.")
    return connection_string(server)


def check_readiness(args, today: datetime) -> int:
    import pyodbc

    ready = True
    try:
        links = fetch_archive_links()
        period = today.year, today.month
        if period in links.blocked_releases or not links.releases:
            raise JobError("The CMS monthly listing could not be verified.")
        LOG.info("CMS reachable; current release listed: %s", period in links.releases)
    except Exception as error:
        LOG.error("CMS readiness failed (%s)", error_summary(error))
        ready = False

    with closing(pyodbc.connect(sql_settings(args), timeout=10, autocommit=True)) as connection:
        connection.timeout = 30
        with closing(connection.cursor()) as cursor:
            for table, permissions in (
                (REFERENCE_TABLE, ("SELECT", "INSERT", "ALTER")),
                (DETAIL_TABLE, ("SELECT", "INSERT", "ALTER")),
                (REFERENCE_TARGET, ("SELECT", "INSERT", "UPDATE")),
                (DETAIL_TARGET, ("SELECT", "INSERT", "UPDATE")),
                (HISTORY_TABLE, ("SELECT", "INSERT")),
            ):
                for permission in permissions:
                    granted = cursor.execute(
                        "SELECT HAS_PERMS_BY_NAME(?, 'OBJECT', ?)", table, permission
                    ).fetchone()[0]
                    if granted != 1:
                        LOG.error("Missing %s permission on %s", permission, table)
                        ready = False
                if table in (REFERENCE_TABLE, DETAIL_TABLE, HISTORY_TABLE):
                    count = cursor.execute(f"SELECT COUNT_BIG(*) FROM {table}").fetchone()[0]
                    LOG.info("%s contains %d rows", table, count)
                    if count and table != HISTORY_TABLE:
                        LOG.error("Reconcile existing staging before enabling the import.")
                        ready = False
            if args.backup_mode == "tables":
                permissions = cursor.execute("""
                    SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE TABLE'),
                           HAS_PERMS_BY_NAME('dedup', 'SCHEMA', 'ALTER')
                """).fetchone()
                if tuple(permissions) != (1, 1):
                    LOG.error("Table backups require CREATE TABLE in Dev and ALTER on dedup.")
                    ready = False
    LOG.info("Readiness checks %s", "passed" if ready else "found deployment blockers")
    return 0 if ready else 1


def acquire_archive(args, state_path: Path, state: dict, today: datetime, now):
    day = today.date().isoformat()
    if state.get("day") != day:
        state.update(day=day, attempts=0, status="waiting", retry_at=None)
    if state["status"] in ("unpublished", "download_error"):
        return None, int(state["status"] == "download_error")

    while state["attempts"] < MAX_ATTEMPTS:
        if state["retry_at"]:
            remaining = datetime.fromisoformat(state["retry_at"]) - now().astimezone(UTC)
            if remaining.total_seconds() > 0:
                time.sleep(remaining.total_seconds())
        if now().date() != today.date():
            return None, 0

        state["attempts"] += 1
        state["retry_at"] = (now().astimezone(UTC) + RETRY_INTERVAL).isoformat()
        save_state(state_path, state)
        step = "download" if state.get("url") else "listing"
        LOG.info("%s %s attempt %d", state["period"], step, state["attempts"])
        try:
            if step == "listing":
                links = fetch_archive_links()
                period = today.year, today.month
                if period in links.blocked_releases:
                    raise JobError("Current monthly link is outside the CMS download boundary.")
                if not links.releases:
                    raise JobError("Monthly Version 2 listing could not be recognized.")
                state["url"] = links.releases.get(period)
                if not state["url"]:
                    state["status"] = "unpublished"
                    save_state(state_path, state)
                    LOG.info("%s monthly release is not yet published", state["period"])
                    return None, 0
                step = "download"
                save_state(state_path, state)
            archive = download_archive(state["url"], args.work_dir / "archives")
        except Exception as error:
            LOG.error("%s %s attempt %d failed (%s)",
                      state["period"], step, state["attempts"], error_summary(error))
            if not retryable(error):
                state["status"] = "download_error"
                save_state(state_path, state)
                return None, 1
            state["retry_at"] = (now().astimezone(UTC) + RETRY_INTERVAL).isoformat()
            save_state(state_path, state)
        else:
            state.update(status="ready", archive=archive.name)
            save_state(state_path, state)
            LOG.info("%s archive verified: %s", state["period"], archive.name)
            return archive, 0
    LOG.error("%s daily CMS attempt budget exhausted", state["period"])
    return None, 1


def run(args, state_path: Path, now) -> int:
    today = now()
    period = today.strftime("%Y-%m")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if state.get("period") != period:
        state = {"period": period, "status": "waiting"}
    if state["status"] == "complete":
        return 0

    import pyodbc

    settings = sql_settings(args)
    expected_name = f"NPPES_Data_Dissemination_{month_name[today.month]}_{today.year}_V2.zip"
    with closing(pyodbc.connect(settings, timeout=10, autocommit=True)) as connection:
        with closing(connection.cursor()) as cursor:
            committed = cursor.execute(
                f"SELECT RecordCount FROM {HISTORY_TABLE} WHERE ReleaseName = ?", expected_name
            ).fetchone()
            if committed is not None:
                save_state(state_path, {"period": period, "status": "complete"})
                LOG.info("%s already committed in SQL history (%s rows)", period, committed[0])
                return 0
            prior = cursor.execute(f"""
                SELECT TOP (1) ReleaseName FROM {HISTORY_TABLE}
                ORDER BY ImportedUTC DESC, ReleaseName DESC
            """).fetchone()
    if state["status"] in ("processing", "processing_failed") and not args.resume:
        LOG.error("%s processing requires an operator rerun with --resume", period)
        return 1
    backup_label = prior[0].removesuffix(".zip") if prior is not None else args.initial_backup_label
    if not backup_label or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", backup_label):
        raise JobError("Provide a valid --initial-backup-label or verify the latest history entry.")

    if state["status"] in ("ready", "processing", "processing_failed"):
        archive = args.work_dir / "archives" / expected_name
        if not archive.is_file() or not is_valid_archive(archive):
            raise JobError("Restore the verified current-month ZIP before resuming processing.")
    else:
        archive, code = acquire_archive(args, state_path, state, today, now)
        if archive is None:
            return code
    state["status"] = "processing"
    save_state(state_path, state)
    step = "staging"
    try:
        with closing(pyodbc.connect(settings, timeout=10, autocommit=False)) as connection:
            try:
                with open_provider_csv(archive) as (member, _, records):
                    count = stage_records(connection, records)
                step = "promotion"
                promote(connection, count, backup_label, args.work_dir / "backups",
                        state["url"], args.backup_mode, clear_staging=True)
            except BaseException:
                connection.rollback()
                raise
    except Exception as error:
        state["status"] = "processing_failed"
        save_state(state_path, state)
        LOG.error("%s %s failed (%s)", period, step, error_summary(error))
        if isinstance(error, ProviderRecordError):
            LOG.info("Rejection report: %s", write_rejection_report(archive, member, error))
        return 1

    save_state(state_path, {"period": period, "status": "complete"})
    LOG.info("%s tables updated; committed %d provider records", period, count)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=WORK_DIR)
    parser.add_argument("--sql-server", help="SQL Server hostname, or set NPPES_SQL_SERVER.")
    parser.add_argument("--backup-mode", choices=("zip", "tables"), default="zip")
    parser.add_argument("--initial-backup-label", help="Prior release label if import history is empty.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true", help="Retry an interrupted current-month import.")
    mode.add_argument("--readiness-only", action="store_true", help="Check CMS and Dev with read-only requests.")
    args = parser.parse_args()
    try:
        eastern = ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        parser.error("Install the IANA timezone data with python -m pip install tzdata.")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(args.work_dir / "monthly.log", encoding="utf-8"),
                  logging.StreamHandler()],
    )
    with (args.work_dir / "monthly.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            LOG.info("Another run owns the monthly work directory lock.")
            return 0
        try:
            if args.readiness_only:
                return check_readiness(args, datetime.now(eastern))
            return run(args, args.work_dir / "state.json", lambda: datetime.now(eastern))
        except Exception as error:
            LOG.error("Monthly job stopped (%s)", error_summary(error))
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
