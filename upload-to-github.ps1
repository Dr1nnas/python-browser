# Upload / sync this project to GitHub (commit + push).
# Token: put your PAT in github-token.txt (one line). That file is gitignored.
param(
    [string]$Message = "Update"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$GitHubUser = "Dr1nnas"
$RepoName   = "python-browser"
$TokenPath  = Join-Path $PSScriptRoot "github-token.txt"

if (-not (Test-Path $TokenPath)) {
    Write-Error "Create github-token.txt in this folder with your GitHub personal access token (one line, no quotes)."
}

$token = (Get-Content -LiteralPath $TokenPath -Raw).Trim()
if (-not $token) {
    Write-Error "github-token.txt is empty."
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "Git is not installed or not on PATH. Install Git for Windows: https://git-scm.com/download/win"
}

$apiHeaders = @{
    Authorization = "Bearer $token"
    Accept        = "application/vnd.github+json"
}

# --- Ensure repo exists on GitHub (404 = create; 200 = ok; network = skip create) ---
$repoExists = $false
try {
    $null = Invoke-RestMethod -Uri "https://api.github.com/repos/$GitHubUser/$RepoName" -Headers $apiHeaders -Method Get -ErrorAction Stop
    $repoExists = $true
    Write-Host "Remote repo $GitHubUser/$RepoName found."
} catch {
    $code = $null
    if ($_.Exception.Response) {
        $code = [int]$_.Exception.Response.StatusCode
    }
    if ($code -eq 404) {
        $repoExists = $false
    } else {
        Write-Warning "Could not verify repo via API (HTTP $code). Assuming it already exists; skipping create."
        $repoExists = $true
    }
}

if (-not $repoExists) {
    Write-Host "Creating GitHub repository $RepoName ..."
    $body = @{
        name        = $RepoName
        private     = $false
        description = "PyQt6 desktop browser (Secret Browser)"
    } | ConvertTo-Json
    try {
        Invoke-RestMethod -Uri "https://api.github.com/user/repos" -Headers $apiHeaders -Method Post -Body $body -ContentType "application/json" -ErrorAction Stop
        Write-Host "Repository created: https://github.com/$GitHubUser/$RepoName"
    } catch {
        $err = $_.ErrorDetails.Message
        if ($err -match "name already exists") {
            Write-Host "Repository already exists on GitHub."
        } else {
            throw
        }
    }
}

# --- Git: init, remote, commit, push ---
if (-not (Test-Path -LiteralPath ".git")) {
    git init
}

# Local commit identity (only if unset - avoids "Please tell me who you are")
$gn = (& git config user.name 2>$null)
if ([string]::IsNullOrWhiteSpace($gn)) {
    git config user.name $GitHubUser
}
$ge = (& git config user.email 2>$null)
if ([string]::IsNullOrWhiteSpace($ge)) {
    git config user.email "$GitHubUser@users.noreply.github.com"
}

$remoteUrl = "https://github.com/$GitHubUser/$RepoName.git"
$hasOrigin = $true
try {
    $null = git remote get-url origin 2>$null
    if ($LASTEXITCODE -ne 0) { $hasOrigin = $false }
} catch {
    $hasOrigin = $false
}
if (-not $hasOrigin) {
    git remote add origin $remoteUrl
} else {
    git remote set-url origin $remoteUrl
}

git add -A
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host "Nothing to commit (no staged changes)."
} else {
    git commit -m $Message
}

# Ensure local branch is main when we have at least one commit
$hasHead = $false
try {
    git rev-parse --verify HEAD 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $hasHead = $true }
} catch { }
if ($hasHead) {
    git branch -M main 2>$null
}

$pushUrl = "https://x-access-token:$token@github.com/$GitHubUser/$RepoName.git"
git push $pushUrl refs/heads/main:refs/heads/main
if ($LASTEXITCODE -eq 0) {
    git fetch --quiet origin 2>$null
    git branch --set-upstream-to=origin/main main 2>$null
    Write-Host "Done. https://github.com/$GitHubUser/$RepoName"
} else {
    exit $LASTEXITCODE
}
