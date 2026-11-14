param([string]$Time = '12:30')
$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
$refreshScript = Join-Path $PSScriptRoot 'moneypuck-refresh.ps1'
$accountName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$refreshScript`"" -WorkingDirectory $projectPath
$trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.Add([timespan]::Parse($Time)))
$principal = New-ScheduledTaskPrincipal -UserId $accountName -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 4 -RestartInterval (New-TimeSpan -Minutes 30) -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'NHL Analytics MoneyPuck Refresh' -Description 'Daily published team-game xG download; current season only, chunked immutable imports, independent of frozen production forecasts. Requires signed-in user, awake PC and Docker.' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Get-ScheduledTaskInfo -TaskName 'NHL Analytics MoneyPuck Refresh' | Select-Object NextRunTime,LastRunTime,LastTaskResult
