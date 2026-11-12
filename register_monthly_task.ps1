param(
    [ValidatePattern('^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')]
    [string]$CheckTime = '06:00',

    [Parameter(Mandatory)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._,\\-]*$')]
    [string]$SqlServer,

    [ValidateSet('zip', 'tables')]
    [string]$BackupMode = 'zip',

    [switch]$ReadinessOnly
)

$ErrorActionPreference = 'Stop'
if ((Get-TimeZone).Id -ne 'Eastern Standard Time') {
    throw 'Register this daily task on a Windows host configured for Eastern time.'
}

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$runner = Join-Path $PSScriptRoot 'run_monthly_nppes.py'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Create the project virtual environment before registering the task.'
}

& $python -c "import pyodbc; from zoneinfo import ZoneInfo; ZoneInfo('America/New_York')"
if ($LASTEXITCODE -ne 0) {
    throw 'Install pyodbc and tzdata in the project virtual environment before registering the task.'
}

$arguments = '-B "{0}" --sql-server "{1}" --backup-mode {2}' -f $runner, $SqlServer, $BackupMode
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive
if ($ReadinessOnly) {
    $taskName = 'NPPES Monthly Import Readiness'
    $action = New-ScheduledTaskAction -Execute $python -WorkingDirectory $PSScriptRoot -Argument "$arguments --readiness-only"
    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal | Out-Null
    Start-ScheduledTask -TaskName $taskName
    return
}

$action = New-ScheduledTaskAction -Execute $python -WorkingDirectory $PSScriptRoot -Argument $arguments
$dailyTime = [datetime]::ParseExact($CheckTime, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
$trigger = New-ScheduledTaskTrigger -Daily -At $dailyTime
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 12)
Register-ScheduledTask -TaskName 'NPPES Monthly Import' -Action $action -Trigger $trigger -Settings $settings -Principal $principal
