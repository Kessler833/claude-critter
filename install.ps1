# Claude Critter installer (Windows)
# Usage:  irm https://raw.githubusercontent.com/Kessler833/claude-critter/main/install.ps1 | iex
$ErrorActionPreference = "Stop"
Write-Host "Claude Critter installer" -ForegroundColor DarkYellow

# 1) Python 3 (the overlay is a small Python/tkinter program)
$py = Get-Command pythonw -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "Python 3 not found. Installing it with winget..." -ForegroundColor Yellow
    winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    $py = Get-Command pythonw -ErrorAction SilentlyContinue
    if (-not $py) { throw "Python is installed but not on PATH yet. Open a new terminal and run this installer again." }
}
$python = Join-Path (Split-Path $py.Source) "python.exe"

# 2) comtypes (reads the Claude window through Windows UI Automation; no screenshots)
Write-Host "Installing the Python package 'comtypes'..."
& $python -m pip install --user --quiet --upgrade comtypes

# 3) the Claude Code plugin
$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) {
    $local = Join-Path $env:USERPROFILE ".local\bin\claude.exe"
    if (Test-Path $local) { $claude = Get-Item $local }
}
if (-not $claude) { throw "Claude Code CLI not found. Install Claude Code first: https://claude.com/claude-code" }
$cli = if ($claude.Source) { $claude.Source } else { $claude.FullName }
& $cli plugin marketplace add Kessler833/claude-critter
& $cli plugin install critter@claude-critter

Write-Host ""
Write-Host "Done! Restart the Claude desktop app. The critter shows up on its own." -ForegroundColor Green
Write-Host "If it ever goes missing, type /critter in Claude Code."
