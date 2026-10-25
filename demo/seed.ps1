<#
.SYNOPSIS
  Seed a Gatekeeper playground repository with the synthetic demo pack.

.DESCRIPTION
  You run this yourself; it uses only your existing git (and optionally gh) credentials
  and never reads .env. It clones the repository into a temporary folder, shows a plan
  and asks you to type "yes". It pushes demo/base to main ONLY if the repository has no
  commits, creates one branch per demo PR (skipping branches that already exist), and
  prints a compare URL for each. It never deletes branches and never force-pushes.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File demo\seed.ps1 your-name/gatekeeper-playground
#>
param(
    [Parameter(Mandatory = $true)][string]$Repo
)
# Errors are checked explicitly ($LASTEXITCODE) so native git stderr output is not fatal.

if ($Repo -notmatch '^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$') {
    throw "Expected the repository as owner/name, got '$Repo'."
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is required."
}

$demo = Split-Path -Parent $MyInvocation.MyCommand.Path
$prsDir = Join-Path $demo "prs"
$prs = Get-ChildItem -Directory $prsDir | Sort-Object Name
$work = Join-Path ([System.IO.Path]::GetTempPath()) ("gatekeeper-seed-" + [guid]::NewGuid().ToString("N").Substring(0, 8))

Write-Host "Cloning https://github.com/$Repo.git into $work"
git clone "https://github.com/$Repo.git" $work
if ($LASTEXITCODE -ne 0) { throw "Clone failed. Does the repository exist and can your git credentials read it?" }

Push-Location $work
try {
    git rev-parse --verify HEAD 2>$null | Out-Null
    $empty = ($LASTEXITCODE -ne 0)

    Write-Host ""
    Write-Host "Plan for $Repo :"
    if ($empty) {
        Write-Host "  - push demo/base as the first commit on main"
    } else {
        Write-Host "  - the repository already has commits: main will NOT be changed"
    }
    foreach ($pr in $prs) {
        $branch = (Get-Content (Join-Path $pr.FullName "branch.txt") -Raw).Trim()
        $title = (Get-Content (Join-Path $pr.FullName "title.txt") -Raw).Trim()
        Write-Host "  - create branch '$branch' ($title), unless it already exists"
    }
    Write-Host "  - never delete branches, never force-push"
    $answer = Read-Host "Type yes to continue"
    if ($answer -ne "yes") { Write-Host "Nothing was changed."; exit 1 }

    if ($empty) {
        Copy-Item -Path (Join-Path $demo "base\*") -Destination $work -Recurse -Force
        git checkout -b main
        git add -A
        git commit -m "Add textkit playground"
        git push -u origin main
        if ($LASTEXITCODE -ne 0) { throw "Push of main failed." }
    }
    git fetch origin

    $created = @()
    foreach ($pr in $prs) {
        $branch = (Get-Content (Join-Path $pr.FullName "branch.txt") -Raw).Trim()
        $title = (Get-Content (Join-Path $pr.FullName "title.txt") -Raw).Trim()
        $remote = git ls-remote --heads origin $branch
        if ($remote) { Write-Host "Branch '$branch' already exists; skipping."; continue }
        git checkout -B $branch origin/main
        Copy-Item -Path (Join-Path $pr.FullName "changes\*") -Destination $work -Recurse -Force
        git add -A
        git commit -m $title
        git push -u origin $branch
        if ($LASTEXITCODE -ne 0) { throw "Push of '$branch' failed." }
        $created += [pscustomobject]@{ Branch = $branch; Title = $title; Body = (Join-Path $pr.FullName "body.md") }
        Write-Host "Open the PR: https://github.com/$Repo/compare/main...$branch`?expand=1"
    }

    if ($created.Count -gt 0 -and (Get-Command gh -ErrorAction SilentlyContinue)) {
        gh auth status 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $open = Read-Host "GitHub CLI found. Type yes to open the $($created.Count) pull request(s) now"
            if ($open -eq "yes") {
                foreach ($item in $created) {
                    gh pr create --repo $Repo --base main --head $item.Branch --title $item.Title --body-file $item.Body
                }
            }
        }
    }
} finally {
    Pop-Location
}
Write-Host "Done. The temporary clone is at $work (you may delete it)."
