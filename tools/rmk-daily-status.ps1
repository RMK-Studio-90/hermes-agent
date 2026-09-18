<#
.SYNOPSIS
    Thin wrapper: regenerate docs/rmk/HERMES_DAILY_STATUS.md.

.DESCRIPTION
    Calls the canonical Python generator (E:\KI\Hermes\scripts\rmk_daily_status.py),
    which reads state/rmk-project-status.json (machine-readable source of truth,
    never modified here) and the RMK Daily Priority Engine, and writes the
    generated markdown into docs/rmk/HERMES_DAILY_STATUS.md inside this repo.

    No logic lives in this file on purpose - it exists only so
    tools/rmk-daily-git-snapshot.ps1 has a single, stable, PowerShell-native
    entry point to call, without duplicating the Python generation logic.

    Never throws on failure; exits non-zero instead, so a calling script can
    treat status generation as best-effort and continue.
#>

[CmdletBinding()]
param()

$GeneratorScript = 'E:\KI\Hermes\scripts\rmk_daily_status.py'

try {
    $out = & python $GeneratorScript 2>&1
    $exit = $LASTEXITCODE
    $out | ForEach-Object { Write-Host "[rmk-daily-status] $_" }
    if ($exit -ne 0) {
        Write-Warning "rmk-daily-status: generator exited with code $exit"
    }
    exit $exit
}
catch {
    Write-Warning "rmk-daily-status: failed to invoke generator: $($_.Exception.Message)"
    exit 1
}
