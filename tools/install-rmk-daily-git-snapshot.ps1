<#
.SYNOPSIS
    Installs (or updates) the Windows Scheduled Task that runs the RMK
    Hermes daily Git snapshot script once per day.

.DESCRIPTION
    Creates/updates a Scheduled Task named 'RMK-Hermes-Daily-Git-Snapshot'
    that runs daily at 21:30 local Windows time as the current user, via:

        powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<snapshot script>"

    Idempotent: re-running this installer updates the existing task
    definition (schedule, action, principal) instead of erroring out or
    creating a duplicate task.

    Does NOT store any GitHub token/password/credential anywhere. The
    snapshot script relies entirely on the current user's existing Git
    credential configuration (git credential.helper / Windows Credential
    Manager) when it pushes.

.NOTES
    Run this installer manually, once, as the user who should own the
    scheduled task (no elevation required — the task runs under the
    invoking user's own Windows account, "run only when user is logged
    on" style trigger is NOT used; this uses a simple daily time trigger
    under the current user).
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$TaskName    = 'RMK-Hermes-Daily-Git-Snapshot'
$ScriptPath  = 'E:\KI\Hermes\hermes-agent\tools\rmk-daily-git-snapshot.ps1'
$TriggerTime = '21:30'

if (-not (Test-Path -LiteralPath $ScriptPath)) {
    Write-Error "Snapshot script not found at expected path: $ScriptPath. Aborting install."
    exit 1
}

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""

$trigger = New-ScheduledTaskTrigger -Daily -At $TriggerTime

# Run as the current interactive user, using their normal (non-elevated)
# logon token, only when the user is logged on — no stored password
# required, no "run whether user is logged on or not" secret storage.
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($existing) {
    Write-Host "Updating existing scheduled task '$TaskName'..."
    Set-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
} else {
    Write-Host "Registering new scheduled task '$TaskName'..."
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Daily local snapshot commit+push of the Hermes repository worktree state (RMK).' | Out-Null
}

$task = Get-ScheduledTask -TaskName $TaskName
Write-Host ""
Write-Host "Scheduled task '$TaskName' is installed:"
Write-Host "  State:    $($task.State)"
Write-Host "  Schedule: Daily at $TriggerTime"
Write-Host "  Action:   powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""
Write-Host "  Principal: $($task.Principal.UserId) (LogonType=$($task.Principal.LogonType), RunLevel=$($task.Principal.RunLevel))"
Write-Host ""
Write-Host "To run it immediately for a manual test:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To disable it:                             Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host "To re-enable it:                           Enable-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove it entirely:                     Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
