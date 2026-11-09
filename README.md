# NPPES Automation Agent

## Project status

- **Phase 1 — Complete.** Verified live Windows-authenticated connectivity and read access to all three NPPES tables in Dev.
- **Phase 2 — In progress.** Automate discovery, download, and ZIP validation for the monthly NPPES Version 2 full replacement file. [CMS NPI download files](https://download.cms.gov/nppes/NPI_Files.html)

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

## Branches

Use `main` for verified milestones and `development` for ongoing work. See [AGENTS.md](AGENTS.md) for push and merge permissions.
