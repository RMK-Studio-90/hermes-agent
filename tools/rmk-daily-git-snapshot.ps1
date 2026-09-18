<#
.SYNOPSIS
    RMK Hermes daily Git snapshot: commit + push ONLY explicitly
    allowlisted state/documentation files, once per day, only if any
    of them actually changed.

.DESCRIPTION
    Safe, non-destructive, idempotent, FAIL-CLOSED snapshot script.

    HARDENED (2026-09-18 safety incident): this script no longer uses
    `git add -A` / `git add .`. It stages ONLY exact paths present in
    $AllowlistExactPaths below. Application/source code is never
    automatically staged, no matter what is dirty in the working tree.

    - Operates ONLY on E:\KI\Hermes\hermes-agent (hardcoded, ignores caller cwd).
    - Refuses to run unless the expected branch is checked out.
    - Never checks out, resets, cleans, or force-pushes anything.
    - Never runs `git add -A` / `git add .` — stages an explicit,
      hardcoded allowlist of exact paths only.
    - FAILS CLOSED if the Git index already has staged changes when
      the run starts (ambiguous state — could sweep unrelated staged
      work into the snapshot commit otherwise).
    - FAILS CLOSED if an unexpected embedded-repo/gitlink (mode
      160000) is present anywhere in the index.
    - Explicitly detects and reports (never stages, never touches)
      dirty files under the protected Smart Model Routing paths.
    - Skips silently (exit 0) when none of the allowlisted paths have
      actually changed — no empty commit, and unrelated dirty files
      elsewhere in the tree do NOT block this "nothing to do" path.
    - Runs a basic secret/sensitive-file guard on newly untracked
      allowlist candidates before staging.
    - Is safe to run manually at any time and is protected against
      concurrent execution via a named system Mutex.

.NOTES
    Exit codes:
      0 = OK (committed+pushed, OR nothing allowlisted changed, OR
          another instance already running the snapshot)
      2 = Wrong branch checked out - stopped safely, no action taken
      3 = Secret guard triggered - aborted, no staging/commit performed
      4 = Repository path invalid / not a git repository
      5 = Commit created locally but push failed (commit left intact)
      6 = SNAPSHOT_BLOCKED_UNRELATED_STAGED_CHANGES - index was already
          dirty (staged) before this run started; aborted, nothing touched
      7 = SNAPSHOT_BLOCKED_GITLINK_DETECTED - unexpected mode 160000
          entry found in the index; aborted, nothing touched
      8 = SNAPSHOT_BLOCKED_STAGED_PATH_NOT_ALLOWLISTED - post-stage
          verification found a staged path outside the allowlist
          (defense in depth; should not normally trigger); aborted
          before commit
      1 = Unexpected/other error
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
# NOTE: intentionally NOT 'Stop'. Native git commands routinely emit
# informational lines on stderr (e.g. "warning: adding embedded git
# repository: ..."). With ErrorActionPreference='Stop', PowerShell turns
# every such stderr line captured via 2>&1 into a terminating exception,
# aborting the script mid-command and leaving a stale .git/index.lock
# behind. All git outcomes in this script are checked explicitly via
# $LASTEXITCODE, so a non-terminating default is correct here. The few
# PowerShell cmdlets that truly must fail fast (New-Item, Add-Content)
# are given their own -ErrorAction Stop below.

# ---------------------------------------------------------------------------
# Fixed configuration - intentionally hardcoded, not parameterized, so the
# script can only ever operate on this one repository/branch/remote.
# ---------------------------------------------------------------------------
$RepoPath        = 'E:\KI\Hermes\hermes-agent'
$ExpectedBranch  = 'rmk/integration-current-upstream'
$RemoteName      = 'fork'   # writable RMK fork remote (origin is upstream, read-only for us)
$LogDir          = 'E:\KI\Hermes\logs\daily-git-snapshot'
$MutexName       = 'Global\RMK-Hermes-Daily-Git-Snapshot'

# ---------------------------------------------------------------------------
# ALLOWLIST - the ONLY paths this unattended job may ever stage/commit.
# Exact repo-relative paths (forward slashes, matching `git status`
# porcelain output), never directory globs for anything code-shaped.
# Extend this list deliberately and narrowly when a new snapshot-owned
# state/doc file is introduced. Application/source code must never be
# added here as a directory rule.
# ---------------------------------------------------------------------------
$AllowlistExactPaths = @(
    '.gitignore'
    'docs/operations/DAILY_GIT_SNAPSHOT.md'
    'tools/rmk-daily-git-snapshot.ps1'
    'tools/install-rmk-daily-git-snapshot.ps1'
    '.github/workflows/rmk-daily-snapshot-health-check.yml'
)

# Protected Smart Model Routing surface - if anything here is dirty, the
# script must report it and MUST NOT stage/reset/touch it. Smart Routing
# stays production-frozen unless separately authorized. These are path
# PREFIXES matched against porcelain paths (forward-slash form).
$ProtectedRoutingPrefixes = @(
    'agent/routing/'
    'tests/agent/routing/'
)

# Secret / sensitive-file guard patterns (matched against the file's
# basename only, case-insensitive). Applied only to allowlist candidates
# that are newly untracked (defense-in-depth; the allowlist is already
# an exact, known set of filenames, so this should never actually fire).
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

function Get-PorcelainEntries {
    # Returns an array of @{ Code = 'XY'; Path = 'a/b/c' } from
    # `git status --porcelain -z`, robust to spaces/special chars.
    $statusZ = & git -C $RepoPath status --porcelain -z 2>&1
    $raw = ($statusZ -join "`n")
    $parts = $raw -split "`0" | Where-Object { $_ -ne '' }
    $entries = @()
    foreach ($entry in $parts) {
        if ($entry.Length -lt 3) { continue }
        $entries += [PSCustomObject]@{
            Code = $entry.Substring(0, 2)
            Path = $entry.Substring(3)
        }
    }
    return $entries
}

function Test-IsProtectedRoutingPath {
    param([string]$Path)
    foreach ($prefix in $ProtectedRoutingPrefixes) {
        if ($Path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
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
    $entries       = Get-PorcelainEntries

    Write-Log "Timestamp: $timestamp"
    Write-Log "Branch (current): $currentBranch"
    Write-Log "Previous HEAD: $previousHead"
    if ($entries.Count -gt 0) {
        Write-Log "Git status before commit:"
        foreach ($e in $entries) { Write-Log "  $($e.Code) $($e.Path)" }
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
    # 4. No changes at all -> exit successfully, no empty commit.
    # -----------------------------------------------------------------
    if ($entries.Count -eq 0) {
        Write-Log "No changes detected (git status --porcelain empty). Nothing to snapshot."
        Write-Log "Commit result: SKIPPED (no changes)"
        Write-Log "END (no changes)"
        exit 0
    }

    # -----------------------------------------------------------------
    # 5. FAIL CLOSED if the index already has staged changes. We never
    #    build our commit on top of a pre-dirty index - too ambiguous,
    #    could sweep unrelated staged work into our snapshot commit.
    # -----------------------------------------------------------------
    $alreadyStaged = $entries | Where-Object { $_.Code[0] -ne ' ' -and $_.Code[0] -ne '?' }
    if ($alreadyStaged.Count -gt 0) {
        Write-Log "SNAPSHOT_BLOCKED_UNRELATED_STAGED_CHANGES"
        Write-Log "The Git index already contains staged changes before this run started. Aborting without staging/committing anything."
        Write-Log "Pre-existing staged paths:"
        foreach ($e in $alreadyStaged) { Write-Log "  $($e.Path)" }
        Write-Log "END (blocked: unrelated staged changes)"
        exit 6
    }

    # -----------------------------------------------------------------
    # 6. Report (never touch) dirty files under the protected Smart
    #    Model Routing surface. Purely informational - they are simply
    #    never candidates for staging since they are not in the
    #    allowlist, but we log them explicitly per the safety mandate.
    # -----------------------------------------------------------------
    $protectedDirty = $entries | Where-Object { Test-IsProtectedRoutingPath $_.Path }
    if ($protectedDirty.Count -gt 0) {
        Write-Log "PROTECTED_ROUTING_FILES_DIRTY (reported only, NOT staged, NOT touched, NOT reset):"
        foreach ($e in $protectedDirty) { Write-Log "  $($e.Code) $($e.Path)" }
    }

    # -----------------------------------------------------------------
    # 7. Determine allowlist candidates: entries whose path is an exact
    #    match in $AllowlistExactPaths and that are actually dirty.
    # -----------------------------------------------------------------
    $candidates = $entries | Where-Object { $AllowlistExactPaths -contains $_.Path }

    if ($candidates.Count -eq 0) {
        Write-Log "No allowlisted path changed (other dirty files exist but are outside the allowlist and are correctly left untouched)."
        Write-Log "Commit result: SKIPPED (nothing allowlisted to snapshot)"
        Write-Log "END (no allowlisted changes)"
        exit 0
    }

    Write-Log "Allowlisted candidates for staging:"
    foreach ($e in $candidates) { Write-Log "  $($e.Code) $($e.Path)" }

    # -----------------------------------------------------------------
    # 8. Secret / sensitive-file guard on newly untracked allowlist
    #    candidates, BEFORE staging anything.
    # -----------------------------------------------------------------
    $suspicious = @()
    foreach ($e in $candidates) {
        if ($e.Code -eq '??') {
            $baseName = Split-Path -Path $e.Path -Leaf
            foreach ($pattern in $SecretPatterns) {
                if ($baseName -imatch $pattern) {
                    $suspicious += $e.Path
                    break
                }
            }
        }
    }

    if ($suspicious.Count -gt 0) {
        Write-Log "STOP: secret/sensitive-file guard triggered on an allowlist candidate. Aborting BEFORE staging. No files were deleted or modified."
        Write-Log "Suspicious files:"
        foreach ($f in $suspicious) { Write-Log "  $f" }
        Write-Log "Commit result: ABORTED (secret guard)"
        Write-Log "END (secret guard block)"
        exit 3
    }

    # -----------------------------------------------------------------
    # 9. Gitlink / embedded-repository guard. Never commit through an
    #    unexpected mode 160000 entry in the index.
    # -----------------------------------------------------------------
    $lsFiles = & git -C $RepoPath ls-files -s 2>&1
    $gitlinks = $lsFiles | Where-Object { $_ -match '^160000\s' }
    if ($gitlinks.Count -gt 0) {
        Write-Log "SNAPSHOT_BLOCKED_GITLINK_DETECTED"
        Write-Log "Unexpected embedded-repository (mode 160000) entries found in the index. Aborting without staging/committing. Physical files are left untouched."
        foreach ($g in $gitlinks) { Write-Log "  $g" }
        Write-Log "END (blocked: gitlink detected)"
        exit 7
    }

    # -----------------------------------------------------------------
    # 10. Stage ONLY the exact allowlisted candidate paths. Never `git
    #     add -A` / `git add .`.
    # -----------------------------------------------------------------
    foreach ($e in $candidates) {
        Invoke-GitExit0 @('add', '--', $e.Path) | Out-Null
    }

    # -----------------------------------------------------------------
    # 11. Defense-in-depth: verify every staged path is in the
    #     allowlist. Abort (do not commit, do not auto-unstage) if not.
    # -----------------------------------------------------------------
    $stagedPaths = & git -C $RepoPath diff --cached --name-only 2>&1
    $notAllowed = $stagedPaths | Where-Object { $AllowlistExactPaths -notcontains $_ }
    if ($notAllowed.Count -gt 0) {
        Write-Log "SNAPSHOT_BLOCKED_STAGED_PATH_NOT_ALLOWLISTED"
        Write-Log "A staged path outside the allowlist was detected after staging. Aborting without committing. Not auto-unstaging (manual review required)."
        foreach ($p in $notAllowed) { Write-Log "  $p" }
        Write-Log "END (blocked: staged path not allowlisted)"
        exit 8
    }

    Write-Log "Staged files (verified: all within allowlist):"
    foreach ($p in $stagedPaths) { Write-Log "  $p" }

    # -----------------------------------------------------------------
    # 12. Commit.
    # -----------------------------------------------------------------
    $dateStamp   = (Get-Date).ToString('yyyy-MM-dd')
    $subject     = "chore(snapshot): daily Hermes state $dateStamp"
    $body        = @(
        "Automated RMK Hermes daily snapshot."
        "Branch: $currentBranch"
        "Previous HEAD: $previousHead"
        "Created: $timestamp"
        "Allowlisted files only (see tools/rmk-daily-git-snapshot.ps1)."
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
    # 13. Push ONLY the currently checked-out branch to its remote.
    #     Uses existing configured Git/GitHub credentials (credential
    #     manager) - never stores or reads tokens/passwords itself.
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
