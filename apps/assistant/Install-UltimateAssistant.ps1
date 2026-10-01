param(
    [switch]$SkipConfiguration,
    [switch]$SkipLaunch,
    [switch]$SkipStartup
)

$ErrorActionPreference = "Stop"
$bundleRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$assistantSource = Join-Path $bundleRoot "apps\assistant"
$heraldSource = Join-Path $bundleRoot "components\herald"
$installRoot = if ($env:ULTIMATE_ASSISTANT_INSTALL_DIR) {
    $env:ULTIMATE_ASSISTANT_INSTALL_DIR
} else {
    Join-Path $env:LOCALAPPDATA "Programs\UltimateAssistant"
}
$installRoot = [System.IO.Path]::GetFullPath($installRoot)

$pythonReady = $false
if (Get-Command py -ErrorAction SilentlyContinue) {
    py -3.13 --version *> $null
    $pythonReady = $LASTEXITCODE -eq 0
}
if (-not $pythonReady) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw 'Install Python 3.13 with its py launcher from python.org, then run this installer again.' }
    & winget install --id Python.Python.3.13 --exact --scope user --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed. Install Python 3.13 with its launcher and retry.' }
    $env:PATH = (Join-Path $env:LOCALAPPDATA 'Programs\Python\Launcher') + ';' + [Environment]::GetEnvironmentVariable('PATH','Machine') + ';' + [Environment]::GetEnvironmentVariable('PATH','User')
    py -3.13 --version *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.13 was installed. Restart the installer so it can discover the launcher.' }
}
if (-not (Test-Path (Join-Path $assistantSource "setup.ps1")) -or
    -not (Test-Path (Join-Path $heraldSource "pyproject.toml"))) {
    throw "The installation bundle is incomplete. Extract the full ZIP before running this installer."
}

New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $installRoot "apps") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $installRoot "components\herald") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $installRoot "components\perception-mcp") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $installRoot "components\ai-workspace\win-ui-mcp") -Force | Out-Null

Copy-Item -LiteralPath $assistantSource -Destination (Join-Path $installRoot "apps") -Recurse -Force
Copy-Item -LiteralPath (Join-Path $heraldSource "herald") -Destination (Join-Path $installRoot "components\herald") -Recurse -Force
foreach ($file in @("pyproject.toml", "setup.py", "README.md", "LICENSE")) {
    Copy-Item -LiteralPath (Join-Path $heraldSource $file) -Destination (Join-Path $installRoot "components\herald") -Force
}
Get-ChildItem -LiteralPath (Join-Path $bundleRoot "components\perception-mcp") -Force |
    Copy-Item -Destination (Join-Path $installRoot "components\perception-mcp") -Recurse -Force
Get-ChildItem -LiteralPath (Join-Path $bundleRoot "components\ai-workspace\win-ui-mcp") -Force |
    Copy-Item -Destination (Join-Path $installRoot "components\ai-workspace\win-ui-mcp") -Recurse -Force

$setup = Join-Path $installRoot "apps\assistant\setup.ps1"
& $setup
if ($LASTEXITCODE -ne 0) { throw "Assistant dependency setup did not complete successfully." }

Write-Host "Installation files and dependencies are ready at $installRoot"
$launchScript = Join-Path $installRoot "apps\assistant\run-local.ps1"
$desktop = [Environment]::GetFolderPath('Desktop')
if (-not $env:ULTIMATE_ASSISTANT_NO_SHORTCUT) {
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop 'Ultimate Assistant.lnk'))
    $shortcut.TargetPath = 'powershell.exe'
    $shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $launchScript + '"'
    $shortcut.WorkingDirectory = Join-Path $installRoot 'apps\assistant'
    $shortcut.Save()
}
Write-Host 'Your bundled local Router starts with the assistant. Connect Antigravity or Codex in the app.'
if (-not $env:ULTIMATE_ASSISTANT_NO_SHORTCUT -and -not $env:ULTIMATE_ASSISTANT_NO_STARTUP) {
    $startupDisabled = Test-Path (Join-Path $env:LOCALAPPDATA 'UltimateAssistant\startup-disabled')
    & (Join-Path $installRoot 'apps\assistant\configure-startup.ps1') -Disable:($SkipStartup -or $startupDisabled)
}
if (-not $SkipConfiguration -and -not $SkipLaunch) { & $launchScript }
