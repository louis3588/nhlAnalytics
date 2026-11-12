$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
# The task owns the process, independently of the IDE. A mutex also protects manual starts.
$serviceMutex = New-Object System.Threading.Mutex($false, 'Local\NhlAnalyticsApiSupervisor')
if (-not $serviceMutex.WaitOne(0)) { exit 0 }
try {
    New-Item -ItemType Directory -Force -Path '.work/tmp', '.work/logs/api-service' | Out-Null
    $env:TEMP = Join-Path $PWD '.work/tmp'
    $env:TMP = $env:TEMP
    $env:OMP_NUM_THREADS = '1'
    $env:OPENBLAS_NUM_THREADS = '1'
    $env:PYTHONUNBUFFERED = '1'
    while ($true) {
        # Never attach to or stop another process that already owns our port.
        if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
            Start-Sleep -Seconds 30
            continue
        }
        $serviceLog = Join-Path $PWD ('.work/logs/api-service/' + (Get-Date -Format 'yyyy-MM-dd') + '.log')
        ('{0:o} Starting API' -f (Get-Date)) | Out-File $serviceLog -Append -Encoding utf8
        try {
            # Redirect native stderr directly: Uvicorn INFO messages are not PowerShell errors.
            $serviceStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
            $apiProcess = Start-Process -FilePath (Join-Path $PWD '.venv/Scripts/python.exe') -ArgumentList '-m', 'nhl_analytics.api' -WorkingDirectory $PWD -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput (Join-Path $PWD ".work/logs/api-service/$serviceStamp.out.log") -RedirectStandardError (Join-Path $PWD ".work/logs/api-service/$serviceStamp.err.log")
            ('{0:o} API exited ({1}); retrying in 30 seconds' -f (Get-Date), $apiProcess.ExitCode) | Out-File $serviceLog -Append -Encoding utf8
        } catch {
            $ErrorActionPreference = 'Stop'
            # Exception text may contain provider configuration; log only the type.
            ('{0:o} Launch failed ({1}); retrying in 30 seconds' -f (Get-Date), $_.Exception.GetType().Name) | Out-File $serviceLog -Append -Encoding utf8
        }
        Start-Sleep -Seconds 30
    }
} finally {
    $serviceMutex.ReleaseMutex()
    $serviceMutex.Dispose()
}
