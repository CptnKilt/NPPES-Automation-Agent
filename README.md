# NPPES Automation Agent

## Roadmap

| Phase | Status | Scope |
| --- | --- | --- |
| 1. Database connectivity | **Complete** | Verify Windows Authentication and read access to all three NPPES tables in Dev. |
| 2. Monthly CMS download | **Complete** | Find and download the monthly NPPES Version 2 full replacement ZIP, validate the archive, and skip releases already downloaded and verified. [CMS NPI download files](https://download.cms.gov/nppes/NPI_Files.html) |
| 3. File inspection and mapping | **Complete** | Map the primary provider CSV to Dev, verify keys and column widths, and check a sample of the November release. |
| 4. SQL Server import | **In progress** | Load and validate both Dev staging tables, then promote the validated release to the main tables in one transaction. |
| 5. Import verification and history | Planned | Reconcile row counts, report rejected rows, and record each release and its outcome in the import history table. |
| 6. Monthly scheduling and monitoring | Planned | Schedule the end-to-end job and document how to monitor results, respond to failures, and rerun a release. |

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

The November sample dry run passed. Live staging and promotion to the main tables are pending.

## Branches

Use `main` for verified milestones and `development` for ongoing work. See [AGENTS.md](AGENTS.md) for push and merge permissions.
