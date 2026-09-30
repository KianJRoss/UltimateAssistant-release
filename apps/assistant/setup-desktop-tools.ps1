$ErrorActionPreference = "Stop"
$workspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$venv = Join-Path $workspaceRoot ".venvs\desktop-tools"
$python = Join-Path $venv "Scripts\python.exe"
$perceptionRequirements = Join-Path $workspaceRoot "components\perception-mcp\requirements.txt"
$controlRequirements = Join-Path $workspaceRoot "components\ai-workspace\win-ui-mcp\requirements.txt"

if (-not (Test-Path -LiteralPath $python)) {
    py -3.13 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Python 3.13 is required for the Windows desktop MCP tools." }
}

& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Could not update pip for the desktop MCP environment." }
& $python -m pip install -r $perceptionRequirements
if ($LASTEXITCODE -ne 0) { throw "Could not install the perception MCP dependencies." }
& $python -m pip install -r $controlRequirements
if ($LASTEXITCODE -ne 0) { throw "Could not install the Windows UI MCP dependencies." }

Write-Host "Desktop vision and control MCP environments are ready. The assistant binds each capability only when enabled in its UI."
