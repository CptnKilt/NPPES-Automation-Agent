# NPPES Automation Agent

## Roadmap

| Phase | Status | Scope |
| --- | --- | --- |
| 1. Database connectivity | **Complete** | Verify Windows Authentication and read access to all three NPPES tables in Dev. |
| 2. Monthly CMS download | **Complete** | Find and download the monthly NPPES Version 2 full replacement ZIP, validate the archive, and skip releases already downloaded and verified. [CMS NPI download files](https://download.cms.gov/nppes/NPI_Files.html) |
| 3. File inspection and mapping | **Complete** | Map the primary provider CSV to Dev, verify keys and column widths, and check a sample of the November release. |
| 4. SQL Server import | **Complete** | Load and validate both Dev staging tables, then promote the validated release to the main tables in one transaction. |
| 5. Import verification, history, and backups | **Complete** | Reconcile row counts, report rejected rows, record the import outcome, and verify a recoverable backup of the prior release before future promotions. |
| 6. Monthly scheduling and monitoring | Planned | Schedule the end-to-end job and document how to monitor results, respond to failures, and rerun a release. |
| 7. Completion notifications | Planned | Notify a designated recipient after the monthly import succeeds. Choose the recipient and delivery channel during this phase. |

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

All 3 reference mappings and 115 provider-detail mappings matched the live Dev column names. The first 10,000 November records passed inspection and fit the Dev text column widths.

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

## Branches

Use `main` for verified milestones and `development` for ongoing work. See [AGENTS.md](AGENTS.md) for push and merge permissions.
