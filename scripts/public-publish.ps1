$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:TEMP = Join-Path $PWD '.work/tmp'
$env:TMP = $env:TEMP
$env:npm_config_cache = Join-Path $PWD '.work/npm-cache'
$env:OMP_NUM_THREADS = '1'
$env:OPENBLAS_NUM_THREADS = '1'
$logDirectory = Join-Path $PWD '.work/logs/public-publisher'
New-Item -ItemType Directory -Force -Path $env:TEMP, $logDirectory | Out-Null
$publishStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
# Redirect at process level: normal Python stderr logs must not become terminating
# RemoteException records under Windows PowerShell's ErrorActionPreference=Stop.
$publishProcess = Start-Process -FilePath (Join-Path $PWD '.venv/Scripts/python.exe') -ArgumentList '-m', 'nhl_analytics.public_refresh', '--once' -WorkingDirectory $PWD -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput (Join-Path $logDirectory "$publishStamp.out.log") -RedirectStandardError (Join-Path $logDirectory "$publishStamp.err.log")
exit $publishProcess.ExitCode
