# Daily Git Snapshot — Hermes Repository

## Purpose

Once per day, automatically **commit and push** the current local working
tree state of the Hermes repository
(`E:\KI\Hermes\hermes-agent`, branch `rmk/integration-current-upstream`)
to the writable RMK fork on GitHub (`fork` remote →
`https://github.com/RMK-Studio-90/hermes-agent.git`), **but only if there
are actual changes**.

This exists because GitHub Actions cannot see unpushed local changes on
this Windows machine — the snapshot has to originate locally.

It is a safety net against local-only work-in-progress being lost (disk
failure, accidental `git reset --hard`, etc.), not a replacement for
normal, reviewed commits/PRs.

## Schedule

- Windows Scheduled Task: **`RMK-Hermes-Daily-Git-Snapshot`**
- Trigger: **Daily at 21:30** local Windows time
- Runs as: the current interactive Windows user (`RMK`), `LogonType=Interactive`,
  `RunLevel=Limited` — no elevation, no stored password.
- `MultipleInstances = IgnoreNew` at the Task Scheduler level, **plus** an
  independent named-Mutex concurrency guard inside the script itself.

## Paths

| What | Path |
|---|---|
| Snapshot script | `E:\KI\Hermes\hermes-agent\tools\rmk-daily-git-snapshot.ps1` |
| Task installer/updater | `E:\KI\Hermes\hermes-agent\tools\install-rmk-daily-git-snapshot.ps1` |
| Logs | `E:\KI\Hermes\logs\daily-git-snapshot\YYYY-MM-DD.log` |
| Repository (fixed, hardcoded) | `E:\KI\Hermes\hermes-agent` |
| Expected branch (fixed, hardcoded) | `rmk/integration-current-upstream` |
| Push remote (fixed, hardcoded) | `fork` → `https://github.com/RMK-Studio-90/hermes-agent.git` |

`origin` (`https://github.com/NousResearch/hermes-agent.git`, the
upstream Nous Research repo) is **read-only** for this account — the
script never pushes there.

## How to run manually

The script is safe to run at any time, from an elevated or normal
PowerShell prompt:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "E:\KI\Hermes\hermes-agent\tools\rmk-daily-git-snapshot.ps1"
```

Or trigger the installed scheduled task directly:

```powershell
Start-ScheduledTask -TaskName 'RMK-Hermes-Daily-Git-Snapshot'
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | OK — committed+pushed, OR nothing to commit, OR another instance already running |
| 2 | Wrong branch checked out — stopped safely, **no action taken** |
| 3 | Secret/sensitive-file guard triggered — aborted **before** staging |
| 4 | Repository path invalid / not a git repository |
| 5 | Commit created locally but **push failed** (commit is left intact) |
| 1 | Unexpected/other error |

## How to inspect logs

One log file per day:

```
E:\KI\Hermes\logs\daily-git-snapshot\2026-09-18.log
```

Each run appends a block containing: `START`, repository, branch,
previous HEAD, git status before commit, staged files, commit result,
new HEAD, push result, `END`. Secret file **contents** are never
logged — only file **paths** of guard hits, if any.

```powershell
Get-Content "E:\KI\Hermes\logs\daily-git-snapshot\$(Get-Date -Format yyyy-MM-dd).log" -Tail 50
```

## How to disable the task

```powershell
Disable-ScheduledTask -TaskName 'RMK-Hermes-Daily-Git-Snapshot'
```

## How to re-enable the task

```powershell
Enable-ScheduledTask -TaskName 'RMK-Hermes-Daily-Git-Snapshot'
```

## How to uninstall the scheduled task

```powershell
Unregister-ScheduledTask -TaskName 'RMK-Hermes-Daily-Git-Snapshot' -Confirm:$false
```

The script and installer files stay on disk; only the Task Scheduler
entry is removed. Re-run `install-rmk-daily-git-snapshot.ps1` to
recreate it later (idempotent — updates in place if it already exists).

## What happens on push failure

The local commit is **never rolled back**. `git commit` succeeds first;
only afterward does the script attempt `git push fork HEAD:refs/heads/<branch>`.
If the push fails (network, auth, rejected non-fast-forward, etc.):

- The commit remains intact locally (nothing is undone).
- The script logs `Push result: FAILED ...` with the git push output.
- The script exits with code `5` and writes a clear error via `Write-Error`.
- No further automatic retry is attempted; the **next scheduled run**
  will simply find that same commit (or newer, if more local changes
  accumulate) still unpushed and try again as part of its normal flow —
  since `git status --porcelain` on the working tree is what gates
  whether a *new* commit happens, but the push step always runs whenever
  a run reaches it, so a stuck local commit will keep attempting to push
  each day until it succeeds or is resolved manually.

## Secret guard behavior

Before staging anything (`git add -A`), the script inspects
`git status --porcelain -z` for entries marked `??` (**newly untracked**
files, i.e. files `git add -A` would start tracking for the first time)
and checks each file's **basename** (case-insensitive) against:

```
^\.env(\..*)?$      *.key      *.pem
^Credentials.*      ^Secrets.*  .*token.*
^Auth.*\.json$      ^Cookies.*\.json$
```

If **any** newly untracked file matches, the entire run **aborts**
before `git add` is ever called:

- Nothing is staged, nothing is committed, nothing is pushed.
- No files are deleted or modified.
- The suspicious file **paths** (never contents) are written to the log.
- Exit code `3`.

This is defense-in-depth on top of the repository's own `.gitignore`,
which already excludes most of these patterns (`.env*`, `*.pem`, etc.)
from ever appearing as untracked in the first place. Already-tracked
files that later start matching one of these names are **not** newly
caught by this guard (by design — the guard targets accidental new
secret files being added to tracking for the first time, not existing
tracked history).

## Concurrency protection

A system-wide named Mutex (`Global\RMK-Hermes-Daily-Git-Snapshot`)
ensures only one snapshot run proceeds at a time. If a second run
(manual or scheduled) starts while one is already in progress, it logs
`Another snapshot instance already holds the lock. Exiting safely
without action.` and exits `0` immediately — no waiting, no queuing, no
partial actions.

## Non-negotiable safety boundaries

The script **never** invokes: `git reset --hard`, `git clean`,
`git checkout`, `git restore`, `git push --force`, `git pull --rebase`,
or any destructive stash manipulation. If the checked-out branch isn't
exactly `rmk/integration-current-upstream`, the script logs `STOP:
expected branch ... but found ...` and exits `2` **without switching
branches, without resetting, without touching the working tree**.
test change
further test
