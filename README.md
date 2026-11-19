# NPPES Automation Agent

## Roadmap

| Phase | Status | Scope |
| --- | --- | --- |
| 1. Database connectivity | **Complete** | Verify Windows Authentication and read access to all three NPPES tables in Dev. |
| 2. Monthly CMS download | **Complete** | Find and download the monthly NPPES Version 2 full replacement ZIP, validate the archive, and skip releases already downloaded and verified. [CMS NPI download files](https://download.cms.gov/nppes/NPI_Files.html) |
| 3. File inspection and mapping | **Complete** | Map the primary provider CSV to Dev, verify keys and column widths, and check a sample of the November release. |
| 4. SQL Server import | **Complete** | Load and validate both Dev staging tables, then promote the validated release to the main tables in one transaction. |
| 5. Import verification, history, and backups | **Complete** | Reconcile row counts, report rejected rows, record the import outcome, and verify a recoverable backup of the prior release before future promotions. |
| 6. Monthly scheduling and monitoring | **In progress** | Run the pipeline from Windows Task Scheduler and monitor daily attempts, release state, and import outcomes. |
| 7. Preserve deactivated provider details | **In progress** | Preserve existing provider details when an NPI deactivates, apply its deactivation information, and handle reactivation. |
| 8. Weekly incremental updates | **In progress** | Discover and process weekly releases in order, track their coverage, and coordinate them with the monthly baseline. |
| 9. Import all provider columns | **In progress** | Import all 330 main-provider CSV columns, expand the final and staging schemas, and verify wider imports and backups. |
| 10. Completion notifications | Planned | Notify a designated recipient after the monthly import succeeds. Choose the recipient and delivery channel during this phase. |

## Phase 1 — Database connectivity

The database check uses Python 3.14.4, `pyodbc`, Microsoft ODBC Driver 17 for SQL Server, and Windows Authentication. Run it from the company network or while connected to the company VPN. The connection to the Dev database uses encryption and skips server certificate validation.

In the PyCharm terminal with the project virtual environment active, run:

```powershell
python -m pip install pyodbc
$env:NPPES_SQL_SERVER = "your-sql-server-hostname"
python test_database.py
```

For the PyCharm Run button, set `NPPES_SQL_SERVER` in the script's run configuration.

The script confirms the connection and checks these tables:

- `dedup.NPPES_reference`
- `dedup.NPPES_provider_details`
- `dedup.NPPES_import_history`

For each visible table, it prints the column names and up to three sample rows without a defined sort order. All queries are read-only. A live run verified read access to all three tables.

## Phase 2 — Monthly CMS download

Run release discovery from the PyCharm terminal:

```powershell
python download_nppes.py
```

The script prints the latest monthly Version 2 full replacement release and its ZIP URL. To download it, run:

```powershell
python download_nppes.py --download
```

The archive is about 1 GB and is saved under the Git-ignored `downloads/` folder. The script checks for the provider CSV and verifies the ZIP's integrity before saving it under its final name. On a rerun, it reuses a local copy that passes the same checks. The ZIP stays compressed for the file inspection phase.

## Phase 3 — File inspection and mapping

The November 2026 primary provider CSV has 330 columns and is about 11.8 GB uncompressed. The inspection script reads the CSV directly from the ZIP and checks the header, row shape, NPI, entity type, and dates for the first 10,000 records by default:

```powershell
python inspect_nppes.py downloads/NPPES_Data_Dissemination_November_2026_V2.zip
```

The initial 3 reference mappings and 115 provider-detail mappings matched the live Dev column names. The first 10,000 November records passed inspection and fit the Dev text column widths. Phase 9 expands the provider-detail mapping to all 330 source columns.

| Dev table | Source and key |
| --- | --- |
| `dedup.NPPES_reference` | Take NPI, entity type, and deactivation date from the primary provider CSV. The primary key is NPI and SQL Server supplies the default for `LastUpdated`. |
| `dedup.NPPES_provider_details` | Use the primary provider CSV for names, addresses, dates, authorized officials, and the 15 taxonomy and license groups. The primary key is NPI and SQL Server supplies the default for `ImportedUTC`. |
| `dedup.NPPES_import_history` | Use release metadata and the final imported row count. The primary key is `ReleaseName` and SQL Server supplies the default for `ImportedUTC`. |

The initial mapping covers the primary provider CSV. The endpoint, secondary practice location, and other-name CSVs have no dedicated destination among these three tables. The loader will need to validate each record as it streams the full release.

## Phase 4 — SQL Server import

To validate the first 10,000 provider records without connecting to SQL Server:

```powershell
python stage_nppes.py downloads/NPPES_Data_Dissemination_November_2026_V2.zip --sample-rows 10000
```

Omit `--sample-rows` to validate the full CSV locally. The explicit `--stage` option instead streams the full release into `dedup.NPPES_Reference_Staging` and `dedup.NPPES_Provider_Details_Staging`. It requires the Dev connection settings from Phase 1 and both staging tables to be empty. It commits only after row counts and NPI sets agree. A failure rolls back both inserts.

The full November 2026 dry run passed CSV structure and format checks for 9,891,116 provider records. The live Dev load committed 9,891,116 rows to each staging table after verifying row counts and matching NPI sets. The load took about 2 hours and 19 minutes. Promotion then committed in about 3 minutes and 19 seconds after confirming that both final tables contained all staged NPIs.

Check the committed staging tables before promotion:

```powershell
python promote_nppes.py --expected-rows 9891116
```

Use the row count reported by the staging loader for the release being promoted. To promote, add `--apply --backup-label <prior-release> --source-url <staged-release-url>`. Use the CMS ZIP URL printed by the downloader for the release you staged. The backup label identifies the release currently in the final tables.

Before updating either final table, the script locks both tables and streams their current rows into a ZIP under the Git-ignored `backups/` folder. Use `--backup-dir` to select a different approved location. The ZIP contains a manifest with the release label, column names and types, and row counts. Each table is stored as UTF-8 JSON Lines with values in manifest order, ISO dates and timestamps, and explicit JSON nulls. The script verifies the row counts and ZIP integrity before it updates either final table. Plan for extra time and local disk space because both tables are exported while the locks are held.

Promotion updates changed rows and inserts new NPIs in one transaction. It checks that every staged NPI is present before committing. Existing NPIs absent from staging are preserved, and staging remains available for reconciliation. The backup remains available if a subsequent promotion step fails. Backup creation was added after the November promotion and verified with fixtures. A live backup and restore rehearsal are pending.

## Phase 5 — Import history and table backups

A successful promotion records the ZIP filename, source URL, and validated staging row count in `dedup.NPPES_Import_History`. SQL Server supplies the UTC import time. The history record and both final-table updates commit together. A release already in history is rejected before any backup or update.

ZIP is the default backup mode. Add `--backup-mode tables` to copy both prior final tables into new, release-labeled and UTC-timestamped snapshot tables in `dedup`. The script verifies both snapshot counts before updating the final tables, then commits the snapshots, updates, and import history together. The snapshots copy the columns and data. Their source indexes and constraints stay on the original tables. Table mode requires permission to create tables in Dev and alter the `dedup` schema.

The live Dev rehearsal reused the committed November 2026 staging data. It finished in about 5 minutes and 30 seconds. A fresh connection verified 9,891,116 rows in each backup table and the November import history row with the same count and its CMS source URL. Both snapshots restored into session-local tables, with matching counts, unique NPIs, and matching field values. The temporary tables were removed after verification.

A separate end-to-end rehearsal downloaded and validated a fresh November ZIP, then loaded and promoted all 9,891,116 provider records through isolated Dev tables. A fresh connection verified both final row counts, exact backups of the 100-row baseline in each table, and one matching import-history entry. The SQL phase took about 2 hours and 40 minutes. The rehearsal tables were removed afterward.

If CSV record validation fails, the staging loader stops and writes a JSON report under the Git-ignored `reports/` folder. It identifies the first rejected record by archive, CSV member, record number, starting line, field when available, and reason. Provider values are omitted. During live staging, the load rolls back. Archive, header, and database errors are reported separately because they may have no identifiable CSV record.

## Phase 6 — Monthly scheduling and monitoring

The [monthly import skill](skills/nppes-monthly-import/SKILL.md) describes the operating rules. The Windows runner checks the current Eastern-time month, retries transient CMS failures up to four times with ten minutes between attempts, and stops polling once the archive is verified. A completed month exits before contacting CMS.

On the intended Windows host, install the dependencies in the project virtual environment. The scheduled check runs daily at 6:00 a.m. Eastern. The registration script uses the signed-in Windows account, which must have access to the company network or VPN. After a reboot, sign in again if Windows has returned to the sign-in screen.

```powershell
python -m pip install pyodbc tzdata
.\register_monthly_task.ps1 -SqlServer 'your-sql-server-hostname' -BackupMode tables -ReadinessOnly
```

Review the readiness task's last run result and `monthly-work/monthly.log`. Exit code 0 indicates the CMS and Dev checks passed. Exit code 1 indicates a deployment blocker. To repeat the test, start the existing `NPPES Monthly Import Readiness` task from Task Scheduler. After the checks pass, register the daily task:

```powershell
.\register_monthly_task.ps1 -CheckTime '06:00' -SqlServer 'your-sql-server-hostname' -BackupMode tables
```

Before enabling the schedule, reconcile any staging rows left by earlier manual imports. The automated loader starts with empty staging. It loads and validates the release, backs up both final tables, promotes the rows, records import history, and truncates both staging tables before committing the SQL transaction. If any step fails, SQL Server rolls back the transaction. The scheduled account needs `ALTER` on both staging tables, plus the existing load, promotion, and backup permissions. If import history is empty, pass `--initial-backup-label` on the first live run to identify the data being backed up.

Review `monthly-work/monthly.log`, `monthly-work/state.json`, and Task Scheduler's last run result. A processing failure requires operator review. After resolving the issue, rerun `python run_monthly_nppes.py --resume` with the same SQL Server, backup mode, and work directory. The runner checks committed import history before retrying. Rejected CSV records are recorded under `reports/`. Outbound notices remain phase-ten work.

A one-time Task Scheduler test on the development PC launched Python 3.14.4 under the intended Windows account, reached the CMS listing, and wrote a local readiness log. Before the one-time Dev staging cleanup, the November history entry and staged NPIs were checked against the final tables. A fresh connection verified that both staging tables were empty and November history remained intact. The follow-up readiness check passed on the PC. The scheduled readiness test on the server is pending.

## Phase 7 — Preserve deactivated provider details

For an existing NPI, promotion treats an incoming record as deactivated when it has a deactivation date and its reactivation date is absent or no later than that deactivation date. It preserves the existing provider details and applies the incoming deactivation date. It applies a supplied deactivation reason and retains the existing reason when the source reason is missing. The reference table retains its existing entity type when a deactivated source omits it.

An incoming record with a reactivation date later than its deactivation date follows normal provider updates. A record with no deactivation date also follows normal updates, including clearing fields when the active source contains blanks. Previously unseen NPIs are inserted with the information available in the source.

The update timestamp changes only when an effective stored value changes. Synthetic SQL tests using the complete Dev column definitions passed for field preservation, missing reasons, repeated imports, reactivation, subsequent deactivation, active updates, and previously unseen deactivated NPIs. The test used session-local tables and rolled back afterward. Phase 7 remains in progress until its behavior is verified in a live import. The change still needs deployment to the scheduled server before December's import. Existing data already absent from the final tables can only be recovered from an earlier source or backup.

## Phase 8 — Weekly incremental updates

The daily runner accepts `--include-weekly`. The scheduler registration script accepts `-IncludeWeekly` when weekly processing is ready to deploy. The same task and work-directory lock coordinate both monthly and weekly imports.

After the current month's full release is recorded in history, weekly processing reads the monthly provider CSV's end date to establish coverage. On its first run, it may download the imported monthly ZIP to determine that date. It then selects all available Version 2 weekly files whose end dates extend coverage and imports them oldest first. An overlapping first week is eligible. A missing week stops processing before any later week is applied. Each weekly release receives its own committed history entry.

Each weekly import uses the existing staging, validation, prior-data backup, promotion, and transactional staging truncation. Deactivated records retain the provider details required by Phase 7. The configured backup mode applies to every weekly import, so table mode creates a pair of full prior-data snapshots for each imported week.

The job continues checking for eligible weeklies after the monthly import completes. At the start of a new month, it waits for that month's baseline before applying further weekly files. A monthly snapshot that predates a previously committed weekly end date requires operator review before promotion. This prevents the monthly snapshot from overwriting newer weekly updates. The shared `cms-attempts.json` file limits CMS attempts across both workflows to five per day.

Weekly release state is stored in `monthly-work/weekly-state.json`. A weekly processing failure stops subsequent weeks and requires operator review followed by `--resume --include-weekly`. Committed SQL history resolves a restart after commit. The weekly option is pending server deployment and live-run verification.

Both November weekly archives passed full CSV validation, with 31,562 and 29,288 provider records. A SQL rehearsal imported them in date order through uniquely named temporary tables. It verified 59,934 distinct final NPIs, two import-history entries, preservation of an existing deactivated provider's details, and empty staging after each promotion. All temporary tables were removed afterward. Automated regression checks cover ordered selection, overlapping coverage, missing weeks, blocked hosts, shared retry limits, monthly coverage protection, commit recovery, and stopping on a failed week.

## Phase 9 — Import all provider columns

The provider-details mapping now covers every one of the 330 columns in the main provider CSV. The 215 added fields contain 50 groups of other-provider identifiers, type codes, states, and issuers, plus 15 taxonomy groups. Both provider-details tables have these source columns plus `ImportedUTC`, for a total of 331 columns. Leading zeroes in identifiers and type codes are stored as text.

The migration adds nullable `nvarchar` fields to the existing tables and verifies compatible definitions on reruns. The added fields use CMS's documented maximum lengths of 20, 2, 2, and 80 for identifier groups and 70 for taxonomy groups. The CSV validator checks these limits in UTF-16 units and reports the first overlength record. [CMS file layout](https://www.cms.gov/regulations-and-guidance/administrative-simplification/nationalprovidentstand/downloads/data_dissemination_file-readme.pdf)

Inspect the required changes before deploying to another database environment:

```powershell
python migrate_provider_columns.py --sql-server your-sql-server-hostname
python migrate_provider_columns.py --sql-server your-sql-server-hostname --apply
```

Apply the schema migration before running the expanded importer. The migration targets the configured Dev database and changes both provider-details tables in one transaction. Readiness checks and import entry points verify the expanded definitions.

An isolated SQL rehearsal verified migration, idempotent reruns, preservation of existing rows, a 1,001-record import, active-source values, a 331-column backup, and preservation of all added fields during deactivation. All 330 headers matched the monthly and weekly files. The monthly sample and both complete weekly files passed CSV validation.

The Dev migration added 215 nullable fields to each provider-details table. Verification found 331 columns in each table, 9,891,116 rows in the final table, and zero staging rows. A second migration inspection reported zero columns to add. An isolated failure test confirmed that an incompatible definition in the second table rolls back the first table's additions.

Existing provider rows receive NULL in the newly added columns until they are imported again. A full monthly import is required to populate the added fields for the full active-provider population. Weekly imports populate only the NPIs in those updates. Previously suppressed details for deactivated NPIs require an earlier source or backup. Full-volume performance and server deployment remain pending, so Phase 9 stays in progress.

## Branches

Use `main` for verified milestones and `development` for ongoing work. See [AGENTS.md](AGENTS.md) for push and merge permissions.
