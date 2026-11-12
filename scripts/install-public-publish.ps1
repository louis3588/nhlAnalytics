$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
$publishScript = Join-Path $PSScriptRoot 'public-publish.ps1'
$accountName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$publishScript`"" -WorkingDirectory $projectPath
$trigger = New-ScheduledTaskTrigger -Once -At ([datetime]::Now.AddMinutes(5)) -RepetitionInterval (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId $accountName -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'NHL Analytics Public Publisher' -Description 'Publish queued NHL static snapshots to the existing free Pages project; never retrain or provision services. Requires the signed-in user and Wrangler credentials.' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Get-ScheduledTaskInfo -TaskName 'NHL Analytics Public Publisher' | Select-Object NextRunTime,LastRunTime,LastTaskResult
