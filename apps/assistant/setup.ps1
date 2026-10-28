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
    # Herald is its own product. A development checkout (HERALD_SOURCE_ROOT) installs
    # editable; otherwise the release pinned in herald.json, checked against its SHA-256. HERALD_WHEEL_DIR
    # (release testing) supplies those exact files locally; they are checked against the same SHA-256.
    if ($env:HERALD_SOURCE_ROOT -and (Test-Path (Join-Path $env:HERALD_SOURCE_ROOT "pyproject.toml"))) {
        & $python -m pip install -e $env:HERALD_SOURCE_ROOT
    } else {
        $pin = Get-Content -Raw (Join-Path $appRoot "herald.json") | ConvertFrom-Json
        if (-not $pin.parts) { throw "No Herald release is pinned in herald.json. Set HERALD_SOURCE_ROOT to a Herald checkout." }
        $wheels = foreach ($part in $pin.parts) {
            $wheel = Join-Path ([System.IO.Path]::GetTempPath()) $part.file
            $local = if ($env:HERALD_WHEEL_DIR) { Join-Path $env:HERALD_WHEEL_DIR $part.file }
            if ($local -and (Test-Path -LiteralPath $local)) { Copy-Item -LiteralPath $local -Destination $wheel -Force }
            else { Invoke-WebRequest -Uri $part.url -OutFile $wheel -UseBasicParsing }
            if ((Get-FileHash -Algorithm SHA256 -LiteralPath $wheel).Hash.ToLowerInvariant() -ne $part.sha256) {
                Remove-Item -LiteralPath $wheel -Force
                throw "The downloaded $($part.name) $($part.version) does not match its published checksum."
            }
            $wheel
        }
        & $python -m pip install @wheels
        $wheels | Remove-Item -Force -ErrorAction SilentlyContinue
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not install Herald." }
    & $python -m pip install -r (Join-Path $appRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Could not install assistant runtime dependencies." }
    & $python -c "import matplotlib, numpy, sympy; import herald.math_tools, ultimate_assistant.life_tools; print('Math and life features are ready.')"
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
