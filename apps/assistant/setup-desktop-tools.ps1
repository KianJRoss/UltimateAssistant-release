$ErrorActionPreference = "Stop"
$workspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$venv = Join-Path $workspaceRoot ".venvs\desktop-tools"
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
