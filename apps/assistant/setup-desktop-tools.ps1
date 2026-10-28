$ErrorActionPreference = "Stop"
$workspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
# PySide6's Windows package carries files ~170 characters deep, past the 260-character path limit inside an
# installation folder (an update's releases\<version> folder especially). The environment lives in a short folder
# instead, one per installation, and the installation records where (.venvs\desktop-tools.path).
$sha = [System.Security.Cryptography.SHA256]::Create()
$id = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($workspaceRoot.ToLowerInvariant())) | Select-Object -First 4 | ForEach-Object { $_.ToString("x2") })
$venv = Join-Path $env:LOCALAPPDATA "UA\dt\$id"
New-Item -ItemType Directory -Path (Join-Path $workspaceRoot ".venvs") -Force | Out-Null
Set-Content -LiteralPath (Join-Path $workspaceRoot ".venvs\desktop-tools.path") -Value $venv -Encoding utf8
$python = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    py -3.13 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Python 3.13 is required for the Windows desktop MCP tools." }
}

& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Could not update pip for the desktop MCP environment." }
$canvasRequirements = Join-Path $workspaceRoot "components\canvas-overlay\requirements.txt"
& $python -m pip install -r $canvasRequirements
if ($LASTEXITCODE -ne 0) { throw "Could not install the canvas overlay dependencies." }

Write-Host "Desktop overlay environment is ready. Screen vision and Windows UI control install as modules when turned on or needed."
