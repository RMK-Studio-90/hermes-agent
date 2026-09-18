<#
.SYNOPSIS
    RMK Hermes daily Git snapshot: commit + push the local hermes-agent
    worktree state once per day, only if there are actual changes.

.DESCRIPTION
    Safe, non-destructive, idempotent snapshot script.
    - Operates ONLY on E:\KI\Hermes\hermes-agent (hardcoded, ignores caller cwd).
    - Refuses to run unless the expected branch is checked out.
    - Never checks out, resets, cleans, or force-pushes anything.
    - Skips silently (exit 0) when there is nothing to commit.
    - Runs a basic secret/sensitive-file guard on newly untracked files
      before staging; aborts (no staging, no commit) on a hit.
    - Is safe to run manually at any time and is protected against
      concurrent execution via a named system Mutex.

.NOTES
    Exit codes:
      0 = OK (committed+pushed, OR nothing to commit, OR another instance
          already running the snapshot)
      2 = Wrong branch checked out - stopped safely, no action taken
      3 = Secret guard triggered - aborted, no staging/commit performed
      4 = Repository path invalid / not a git repository
      5 = Commit created locally but push failed (commit left intact)
      1 = Unexpected/other error
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
# NOTE: intentionally NOT 'Stop'. Native git commands routinely emit
# informational lines on stderr (e.g. "warning: adding embedded git
# repository: ..."). With ErrorActionPreference='Stop', PowerShell turns
# every such stderr line captured via 2>&1 into a terminating exception,
# aborting the script mid-`git add -A` and leaving a stale .git/index.lock
# behind. All git outcomes in this script are checked explicitly via
# $LASTEXITCODE (see Invoke-GitExit0 and the commit/push blocks below),
# so a non-terminating default is correct here.

# ---------------------------------------------------------------------------
# Fixed configuration - intentionally hardcoded, not parameterized, so the
# script can only ever operate on this one repository/branch/remote.
# ---------------------------------------------------------------------------
$RepoPath        = 'E:\KI\Hermes\hermes-agent'
$ExpectedBranch  = 'rmk/integration-current-upstream'
$RemoteName      = 'fork'   # writable RMK fork remote (origin is upstream, read-only for us)
$LogDir          = 'E:\KI\Hermes\logs\daily-git-snapshot'
$MutexName       = 'Global\RMK-Hermes-Daily-Git-Snapshot'

# Secret / sensitive-file guard patterns (matched against the file's
# basename only, case-insensitive). Defense-in-depth: .gitignore already
# excludes most of these from ever showing up as untracked.
$SecretPatterns = @(
    '^\.env(\..*)?$'
    '.*\.key$'
    '.*\.pem$'
    '^Credentials.*'
    '^Secrets.*'
    '.*token.*'
    '^Auth.*\.json$'
    '^Cookies.*\.json$'
)

function Get-Iso8601Now {
    (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
}

function Get-LogFile {
    if (-not (Test-Path -LiteralPath $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force -ErrorAction Stop | Out-Null
    }
    $dateStamp = (Get-Date).ToString('yyyy-MM-dd')
    Join-Path $LogDir "$dateStamp.log"
}

$script:LogFile = Get-LogFile

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Iso8601Now), $Message
    Add-Content -LiteralPath $script:LogFile -Value $line -Encoding utf8 -ErrorAction Stop
    Write-Host $line
}

function Invoke-GitExit0 {
    # Run git and require exit code 0; throw with captured output otherwise.
    param([string[]]$GitArgs)
    $out = & git -C $RepoPath @GitArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "git $($GitArgs -join ' ') failed (exit $LASTEXITCODE): $out"
    }
    return $out
}

# ---------------------------------------------------------------------------
# Concurrency guard - process-safe named Mutex. If another snapshot run is
# already in flight, exit cleanly without doing anything.
# ---------------------------------------------------------------------------
$mutex = New-Object System.Threading.Mutex($false, $MutexName)
$acquiredLock = $false
try {
    $acquiredLock = $mutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    # Previous holder crashed without releasing - we still got it.
    $acquiredLock = $true
}

if (-not $acquiredLock) {
    Write-Log "START"
    Write-Log "Repository: $RepoPath"
    Write-Log "Another snapshot instance already holds the lock. Exiting safely without action."
    Write-Log "END (skipped: concurrent run)"
    exit 0
}

try {
    Write-Log "START"
    Write-Log "Repository: $RepoPath"

    # -----------------------------------------------------------------
    # 1. Validate repository path + that it is actually a git repo.
    # -----------------------------------------------------------------
    if (-not (Test-Path -LiteralPath $RepoPath)) {
        Write-Log "FATAL: repository path does not exist: $RepoPath"
        Write-Log "END (repo path invalid)"
        exit 4
    }

    $isRepo = & git -C $RepoPath rev-parse --is-inside-work-tree 2>&1
    if ($LASTEXITCODE -ne 0 -or $isRepo -notmatch 'true') {
        Write-Log "FATAL: $RepoPath is not a valid git repository. Detail: $isRepo"
        Write-Log "END (not a git repository)"
        exit 4
    }

    # -----------------------------------------------------------------
    # 2. Record timestamp, branch, HEAD, status BEFORE any action.
    # -----------------------------------------------------------------
    $timestamp     = Get-Iso8601Now
    $currentBranch = (Invoke-GitExit0 @('rev-parse', '--abbrev-ref', 'HEAD')) | Select-Object -Last 1
    $previousHead  = (Invoke-GitExit0 @('rev-parse', 'HEAD')) | Select-Object -Last 1
    $statusBefore  = & git -C $RepoPath status --porcelain 2>&1

    Write-Log "Timestamp: $timestamp"
    Write-Log "Branch (current): $currentBranch"
    Write-Log "Previous HEAD: $previousHead"
    if ($statusBefore) {
        Write-Log "Git status before commit:"
        foreach ($line in $statusBefore) { Write-Log "  $line" }
    } else {
        Write-Log "Git status before commit: (clean)"
    }

    # -----------------------------------------------------------------
    # 3. Require the expected branch. Never checkout/switch/reset.
    # -----------------------------------------------------------------
    if ($currentBranch -ne $ExpectedBranch) {
        Write-Log "STOP: expected branch '$ExpectedBranch' but found '$currentBranch'. No action taken (no checkout, no reset)."
        Write-Log "END (branch mismatch)"
        exit 2
    }

    # -----------------------------------------------------------------
    # 4. No changes -> exit successfully, no empty commit.
    # -----------------------------------------------------------------
    if (-not $statusBefore -or $statusBefore.Count -eq 0) {
        Write-Log "No changes detected (git status --porcelain empty). Nothing to snapshot."
        Write-Log "Commit result: SKIPPED (no changes)"
        Write-Log "END (no changes)"
        exit 0
    }

    # -----------------------------------------------------------------
    # 5. Secret / sensitive-file guard on newly untracked files, BEFORE
    #    staging anything. Uses -z output for robust path parsing.
    # -----------------------------------------------------------------
    $statusZ = & git -C $RepoPath status --porcelain -z 2>&1
    $rawStatusZ = ($statusZ -join "`n")
    $entries = $rawStatusZ -split "`0" | Where-Object { $_ -ne '' }

    $suspicious = @()
    foreach ($entry in $entries) {
        if ($entry.Length -lt 3) { continue }
        $code = $entry.Substring(0, 2)
        $path = $entry.Substring(3)
        # Only newly untracked files are candidates for `git add -A` to
        # newly start tracking; that's what the guard targets.
        if ($code -eq '??') {
            $baseName = Split-Path -Path $path -Leaf
            foreach ($pattern in $SecretPatterns) {
                if ($baseName -imatch $pattern) {
                    $suspicious += $path
                    break
                }
            }
        }
    }

    if ($suspicious.Count -gt 0) {
        Write-Log "STOP: secret/sensitive-file guard triggered. Aborting BEFORE staging. No files were deleted or modified."
        Write-Log "Suspicious files:"
        foreach ($f in $suspicious) { Write-Log "  $f" }
        Write-Log "Commit result: ABORTED (secret guard)"
        Write-Log "END (secret guard block)"
        exit 3
    }

    # -----------------------------------------------------------------
    # 6. Stage everything, show staged files, commit.
    # -----------------------------------------------------------------
    Invoke-GitExit0 @('add', '-A') | Out-Null

    $stagedFiles = & git -C $RepoPath diff --cached --name-status 2>&1
    Write-Log "Staged files:"
    if ($stagedFiles) {
        foreach ($line in $stagedFiles) { Write-Log "  $line" }
    } else {
        Write-Log "  (none - unexpected, status showed changes but nothing staged)"
        Write-Log "Commit result: SKIPPED (nothing staged after add -A)"
        Write-Log "END (nothing staged)"
        exit 0
    }

    $dateStamp   = (Get-Date).ToString('yyyy-MM-dd')
    $subject     = "chore(snapshot): daily Hermes state $dateStamp"
    $body        = @(
        "Automated RMK Hermes daily snapshot."
        "Branch: $currentBranch"
        "Previous HEAD: $previousHead"
        "Created: $timestamp"
    ) -join "`n"

    $commitOut = & git -C $RepoPath commit -m $subject -m $body 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Log "FATAL: git commit failed: $commitOut"
        Write-Log "Commit result: FAILED"
        Write-Log "END (commit failed)"
        exit 1
    }

    $newHead = (Invoke-GitExit0 @('rev-parse', 'HEAD')) | Select-Object -Last 1
    Write-Log "Commit result: SUCCESS"
    Write-Log "New HEAD: $newHead"

    # -----------------------------------------------------------------
    # 7. Push ONLY the currently checked-out branch to its remote.
    #    Uses existing configured Git/GitHub credentials (credential
    #    manager) - never stores or reads tokens/passwords itself.
    # -----------------------------------------------------------------
    $pushOut = & git -C $RepoPath push $RemoteName "HEAD:refs/heads/$currentBranch" 2>&1
    $pushExit = $LASTEXITCODE
    if ($pushExit -ne 0) {
        Write-Log "Push result: FAILED (remote=$RemoteName, branch=$currentBranch). Local commit left intact."
        Write-Log "Push output: $pushOut"
        Write-Log "END (push failed)"
        Write-Error "Push to '$RemoteName' failed for branch '$currentBranch'. Commit $newHead is intact locally. See log: $script:LogFile"
        exit 5
    }

    Write-Log "Push result: SUCCESS (remote=$RemoteName, branch=$currentBranch)"
    Write-Log "END (success)"
    exit 0
}
catch {
    Write-Log "FATAL unexpected error: $($_.Exception.Message)"
    Write-Log "END (unexpected error)"
    exit 1
}
finally {
    if ($acquiredLock) {
        $mutex.ReleaseMutex() | Out-Null
    }
    $mutex.Dispose()
}
