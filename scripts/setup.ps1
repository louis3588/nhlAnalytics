$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

# Keep installer temp files and pip downloads on the project drive (D:).
New-Item -ItemType Directory -Force -Path '.work/tmp', '.work/pip-cache' | Out-Null
$env:TEMP = Join-Path $PWD '.work/tmp'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = Join-Path $PWD '.work/pip-cache'

if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}
& ./.venv/Scripts/python.exe -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw 'Project installation failed.' }
