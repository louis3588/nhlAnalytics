$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:TEMP = Join-Path $PWD '.work/tmp'
$env:TMP = $env:TEMP
$env:OMP_NUM_THREADS = '1'
$env:OPENBLAS_NUM_THREADS = '1'
$logDirectory = Join-Path $PWD '.work/logs/moneypuck-refresh'
New-Item -ItemType Directory -Force -Path $env:TEMP,$logDirectory | Out-Null
$runStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$refreshProcess = Start-Process -FilePath (Join-Path $PWD '.venv/Scripts/python.exe') -ArgumentList '-m','nhl_analytics.moneypuck_refresh' -WorkingDirectory $PWD -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput (Join-Path $logDirectory "$runStamp.out.log") -RedirectStandardError (Join-Path $logDirectory "$runStamp.err.log")
exit $refreshProcess.ExitCode
