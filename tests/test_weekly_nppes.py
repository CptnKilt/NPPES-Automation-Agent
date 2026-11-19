"""Regression checks for weekly coverage and monthly coordination."""

import json
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError
from zipfile import ZipFile

from download_nppes import ArchiveLinks
from run_monthly_nppes import JobError, acquire_archive
from weekly_nppes import check_monthly_coverage, monthly_through, run_weekly, select_weekly, verify_weekly_dates


BASELINE = "NPPES_Data_Dissemination_November_2026_V2.zip"
FIRST = "NPPES_Data_Dissemination_110226_110826_Weekly_V2.zip"
SECOND = "NPPES_Data_Dissemination_110926_111526_Weekly_V2.zip"
PREFIX = "https://download.cms.gov/nppes/"


def listing():
    links = ArchiveLinks()
    # Reverse listing order to ensure selection uses date coverage.
    links.feed(f'<a href="{PREFIX + SECOND}">second</a><a href="./{FIRST}">first</a>')
    return links


class WeeklyTests(unittest.TestCase):
    def test_monthly_and_weekly_share_daily_retry_budget(self):
        with TemporaryDirectory() as folder:
            args = SimpleNamespace(work_dir=Path(folder))
            clock = [datetime(2026, 11, 19, 6, tzinfo=timezone(timedelta(hours=-5)))]
            def sleep(seconds):
                self.assertEqual(seconds, 600)
                clock[0] += timedelta(seconds=seconds)
            with patch("run_monthly_nppes.fetch_archive_links", side_effect=URLError("offline")) as fetch, patch(
                "run_monthly_nppes.time.sleep", side_effect=sleep
            ):
                for filename in ("monthly.json", "weekly.json"):
                    state = {"period": "2026-11"}
                    self.assertEqual(
                        acquire_archive(args, Path(folder) / filename, state, clock[0], lambda: clock[0]),
                        (None, 1),
                    )
                self.assertEqual(fetch.call_count, 5)
                clock[0] += timedelta(days=1)
                state = {"period": "2026-11"}
                acquire_archive(args, Path(folder) / "weekly.json", state, clock[0], lambda: clock[0])
                self.assertEqual(fetch.call_count, 10)

    def test_oldest_unprocessed_and_overlap(self):
        links = listing()
        self.assertEqual(select_weekly(links, date(2026, 11, 2), set()), PREFIX + FIRST)
        self.assertEqual(select_weekly(links, date(2026, 11, 8), {FIRST}), PREFIX + SECOND)
        self.assertIsNone(select_weekly(links, date(2026, 11, 15), {FIRST, SECOND}))

    def test_gap_stops_before_next_week(self):
        links = listing()
        del links.weekly_releases[date(2026, 11, 2), date(2026, 11, 8)]
        with self.assertRaises(JobError):
            select_weekly(links, date(2026, 11, 2), set())

    def test_redirected_host_and_deactivations_excluded(self):
        links = ArchiveLinks()
        links.feed(f'<a href="https://example.org/nppes/{FIRST}">blocked</a>')
        links.feed('<a href="./NPPES_Deactivated_NPI_Report_110926_V2.zip">deactivated</a>')
        self.assertFalse(links.weekly_releases)
        with self.assertRaises(JobError):
            select_weekly(links, date(2026, 11, 2), set())

    def test_monthly_coverage_uses_csv_date(self):
        with TemporaryDirectory() as folder:
            archive = Path(folder) / BASELINE
            with ZipFile(archive, "w") as out:
                out.writestr("npidata_pfile_20050523-20261102.csv", "NPI\n")
            self.assertEqual(monthly_through(archive), date(2026, 11, 2))
            with patch("weekly_nppes.history", return_value=[(FIRST, PREFIX + FIRST)]):
                with self.assertRaises(JobError):
                    check_monthly_coverage("settings", archive)

    def test_weekly_csv_dates_must_match_release(self):
        with TemporaryDirectory() as folder:
            archive = Path(folder) / FIRST
            with ZipFile(archive, "w") as out:
                out.writestr("npidata_pfile_20261102-20261108.csv", "NPI\n")
            verify_weekly_dates(archive, (date(2026, 11, 2), date(2026, 11, 8)))
            with self.assertRaises(JobError):
                verify_weekly_dates(archive, (date(2026, 11, 9), date(2026, 11, 15)))

    def test_full_sequence_and_history_recovery(self):
        with TemporaryDirectory() as folder:
            args = SimpleNamespace(work_dir=Path(folder), resume=False)
            state_path = args.work_dir / "weekly-state.json"
            state_path.write_text(json.dumps({
                "period": "2026-11", "baseline": BASELINE, "status": "waiting", "phase": "weekly",
                "baseline_through": "2026-11-02",
            }))
            rows = [(BASELINE, PREFIX + BASELINE)]
            now = lambda: datetime(2026, 11, 19, 6, tzinfo=timezone(timedelta(hours=-5)))

            def acquire(args, path, state, today, clock, select):
                url = select(listing())
                if not url:
                    return None, 0
                state["url"] = url
                return Path(folder) / url.rsplit("/", 1)[1], 0

            with patch("weekly_nppes.sql_settings", return_value="settings"), patch(
                "weekly_nppes.history", return_value=rows
            ), patch("weekly_nppes.acquire_archive", side_effect=acquire), patch(
                "weekly_nppes.process_release", return_value=2
            ) as process, patch("weekly_nppes.verify_weekly_dates"):
                self.assertEqual(run_weekly(args, now), 0)
                self.assertEqual([c.args[3] for c in process.call_args_list], [PREFIX + FIRST, PREFIX + SECOND])
                # Recover an interrupted local state write after the second release committed.
                state = json.loads(state_path.read_text())
                state.update(status="processing", url=PREFIX + SECOND)
                state_path.write_text(json.dumps(state))
                process.reset_mock()
                self.assertEqual(run_weekly(args, now), 0)
                process.assert_not_called()

    def test_failure_prevents_later_week(self):
        with TemporaryDirectory() as folder:
            args = SimpleNamespace(work_dir=Path(folder), resume=False)
            state = {"period": "2026-11", "baseline": BASELINE, "status": "waiting",
                     "phase": "weekly", "baseline_through": "2026-11-02"}
            (args.work_dir / "weekly-state.json").write_text(json.dumps(state))

            def acquire(args, path, state, today, clock, select):
                state["url"] = select(listing())
                return Path(folder) / FIRST, 0

            with patch("weekly_nppes.sql_settings", return_value="settings"), patch(
                "weekly_nppes.history", return_value=[(BASELINE, PREFIX + BASELINE)]
            ), patch("weekly_nppes.acquire_archive", side_effect=acquire), patch(
                "weekly_nppes.process_release", side_effect=ValueError("failure")
            ) as process, patch("weekly_nppes.verify_weekly_dates"):
                self.assertEqual(run_weekly(args, lambda: datetime(2026, 11, 19)), 1)
                process.assert_called_once()
                self.assertEqual(json.loads((args.work_dir / "weekly-state.json").read_text())["status"],
                                 "processing_failed")


if __name__ == "__main__":
    unittest.main()
