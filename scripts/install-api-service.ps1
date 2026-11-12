$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
$serviceScript = Join-Path $PSScriptRoot 'api-service.ps1'
$accountName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$serviceScript`"" -WorkingDirectory $projectPath
$logon = New-ScheduledTaskTrigger -AtLogOn -User $accountName
$recovery = New-ScheduledTaskTrigger -Once -At ([datetime]::Now.AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $accountName -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([timespan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'NHL Analytics API' -Description 'Local API, live collector and frozen prospective forecasts. Restarts independently of PyCharm; requires signed-in user and Docker.' -Action $action -Trigger @($logon, $recovery) -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName 'NHL Analytics API'
Get-ScheduledTaskInfo -TaskName 'NHL Analytics API' | Select-Object NextRunTime, LastRunTime, LastTaskResult
