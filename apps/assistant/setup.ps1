$ErrorActionPreference = "Stop"
$appRoot = $PSScriptRoot
$workspaceRoot = (Resolve-Path (Join-Path $appRoot "..\..")).Path
$python = Join-Path $workspaceRoot ".venvs\assistant\Scripts\python.exe"

if (-not (Test-Path $python)) {
    py -3.13 -m venv (Join-Path $workspaceRoot ".venvs\assistant")
    if ($LASTEXITCODE -ne 0) { throw "Python 3.13 is required for the assistant runtime." }
}

Push-Location $appRoot
try {
    & $python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Could not update pip in the assistant environment." }
    & $python -m pip install -r (Join-Path $appRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Could not install assistant runtime dependencies." }
    & $python -c "import matplotlib, numpy, sympy; import herald.math_tools, ultimate_assistant.flashcard_store; print('Math and study features are ready.')"
    if ($LASTEXITCODE -ne 0) { throw "Math MCP dependencies failed their import check." }
} finally {
    Pop-Location
}

& (Join-Path $appRoot "setup-kokoro.ps1")
if ($LASTEXITCODE -ne 0) { throw "Kokoro local speech setup failed." }

& (Join-Path $appRoot "setup-desktop-tools.ps1")
if ($LASTEXITCODE -ne 0) { throw "Desktop MCP setup failed." }

& (Join-Path $appRoot "setup-browser-tools.ps1")
if ($LASTEXITCODE -ne 0) { throw "Browser MCP setup failed." }

Write-Host "Environment ready: $python"
