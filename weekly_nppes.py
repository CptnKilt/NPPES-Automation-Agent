"""Apply weekly NPPES releases after the current monthly baseline."""

import json
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZipFile

from download_nppes import MONTHLY_ARCHIVE, MONTHS, PROVIDER_CSV, WEEKLY_ARCHIVE, is_valid_archive
from promote_nppes import HISTORY_TABLE, release_name
from run_monthly_nppes import (
    JobError, LOG, acquire_archive, error_summary, process_release, save_state, sql_settings,
)


def weekly_range(filename: str):
    match = WEEKLY_ARCHIVE.fullmatch("/nppes/" + filename)
    if match is None:
        return None
    start, end = (datetime.strptime(value, "%m%d%y").date() for value in match.groups())
    if start > end:
        raise JobError("Weekly release has an invalid date range.")
    return start, end


def monthly_through(archive: Path):
    with ZipFile(archive) as source:
        members = [name for name in source.namelist() if PROVIDER_CSV.fullmatch(Path(name).name)]
    if len(members) != 1:
        raise JobError("Expected one provider CSV to determine monthly baseline coverage.")
    return datetime.strptime(Path(members[0]).stem.rsplit("-", 1)[1], "%Y%m%d").date()


def verify_weekly_dates(archive: Path, expected) -> None:
    with ZipFile(archive) as source:
        members = [name for name in source.namelist() if PROVIDER_CSV.fullmatch(Path(name).name)]
    if len(members) != 1:
        raise JobError("Expected one provider CSV in the weekly archive.")
    period = Path(members[0]).stem.removeprefix("npidata_pfile_")
    actual = tuple(datetime.strptime(value, "%Y%m%d").date() for value in period.split("-"))
    if actual != expected:
        raise JobError("Weekly provider CSV dates do not match the selected release.")


def history(settings: str):
    import pyodbc

    with closing(pyodbc.connect(settings, timeout=10, autocommit=True)) as connection:
        connection.timeout = 30
        with closing(connection.cursor()) as cursor:
            return cursor.execute(
                f"SELECT ReleaseName, SourceURL FROM {HISTORY_TABLE} ORDER BY ImportedUTC, ReleaseName"
            ).fetchall()


def check_monthly_coverage(settings: str, archive: Path) -> None:
    through = monthly_through(archive)
    for row in history(settings):
        dates = weekly_range(row[0])
        if dates and dates[1] > through:
            raise JobError("Monthly snapshot predates a committed weekly release. Review coverage before promotion.")


def select_weekly(links, through, committed: set[str]):
    candidates = []
    for (start, end), url in links.weekly_releases.items():
        if end > through and Path(urlsplit(url).path).name not in committed:
            candidates.append((end, start, url))
    for start, end in links.blocked_weeklies:
        if end > through:
            raise JobError("An eligible weekly link is outside the CMS download boundary.")
    if not candidates:
        return None
    end, start, url = min(candidates)
    if start > through + timedelta(days=1):
        raise JobError("A weekly coverage gap was detected. Recover the missing week before continuing.")
    return url


def run_weekly(args, now) -> int:
    settings = sql_settings(args)
    today = now()
    period = today.strftime("%Y-%m")
    rows = history(settings)
    baseline = None
    for row in rows:
        match = MONTHLY_ARCHIVE.fullmatch("/nppes/" + row[0])
        if match and (int(match[2]), MONTHS.get(match[1])) == (today.year, today.month):
            baseline = row
    if baseline is None:
        LOG.info("%s weekly processing waits for its monthly baseline", period)
        return 0
    if release_name(baseline[1]) != baseline[0]:
        raise JobError("Monthly history filename and source URL do not agree.")

    state_path = args.work_dir / "weekly-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if state.get("baseline") != baseline[0]:
        state = {"period": period, "baseline": baseline[0], "status": "waiting", "phase": "baseline",
                 "url": baseline[1]}
    # Reconcile a commit whose local state update was interrupted.
    committed = {row[0] for row in rows}
    if state.get("phase") == "weekly" and state.get("url"):
        name = release_name(state["url"])
        if name in committed:
            state.update(status="waiting", url=None, retry_at=None)
    if state["status"] in ("processing", "processing_failed") and not args.resume:
        LOG.error("%s weekly import requires operator recovery with --resume", period)
        return 1

    if "baseline_through" not in state:
        archive, code = acquire_archive(args, state_path, state, today, now)
        if archive is None:
            return code
        state.update(baseline_through=monthly_through(archive).isoformat(),
                     phase="weekly", status="waiting", url=None, retry_at=None)
        save_state(state_path, state)
    through = datetime.fromisoformat(state["baseline_through"]).date()
    for row in rows:
        dates = weekly_range(row[0])
        if dates and dates[1] > through:
            if dates[0] > through + timedelta(days=1):
                raise JobError("Committed weekly history has a coverage gap.")
            through = dates[1]

    while True:
        if state["status"] in ("processing", "processing_failed"):
            archive = args.work_dir / "archives" / release_name(state["url"])
            if not archive.is_file() or not is_valid_archive(archive):
                raise JobError("Restore the verified weekly ZIP before resuming.")
        else:
            archive, code = acquire_archive(
                args, state_path, state, today, now,
                lambda links: select_weekly(links, through, committed),
            )
            if archive is None:
                return code
        name = release_name(state["url"])
        dates = weekly_range(name)
        if dates is None or dates[1] <= through or dates[0] > through + timedelta(days=1):
            raise JobError("Saved weekly release is outside the next eligible coverage range.")
        state["status"] = "processing"
        save_state(state_path, state)
        try:
            verify_weekly_dates(archive, dates)
            # The label identifies the last committed release being backed up.
            label = rows[-1][0].removesuffix(".zip")
            count = process_release(args, settings, archive, state["url"], label)
        except Exception as error:
            state["status"] = "processing_failed"
            save_state(state_path, state)
            LOG.error("%s weekly import failed (%s)", name, error_summary(error))
            return 1
        LOG.info("%s weekly import committed (%d rows)", name, count)
        committed.add(name)
        rows.append((name, state["url"]))
        through = dates[1]
        state.update(status="waiting", url=None, retry_at=None)
        save_state(state_path, state)
