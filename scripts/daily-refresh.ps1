$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
New-Item -ItemType Directory -Force -Path '.work/tmp', '.work/pip-cache' | Out-Null
$env:TEMP = Join-Path $PWD '.work/tmp'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = Join-Path $PWD '.work/pip-cache'
$env:OMP_NUM_THREADS = '1'
$env:OPENBLAS_NUM_THREADS = '1'
$env:PYTHONUNBUFFERED = '1'
& ./.venv/Scripts/python.exe -m nhl_analytics.refresh @args
exit $LASTEXITCODE
