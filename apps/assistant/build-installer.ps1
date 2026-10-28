param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot "..\..\dist"),
    [string]$Version = "0.4.0"
)

$ErrorActionPreference = "Stop"
$workspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("UltimateAssistant-bundle-" + [guid]::NewGuid().ToString("N"))
$bundleRoot = Join-Path $stage "UltimateAssistant"

try {
    $required = @(
        "apps\assistant\setup.ps1",
        "apps\assistant\requirements.txt",
        "apps\assistant\run-local.ps1",
        "apps\assistant\setup-provider.ps1",
        "apps\assistant\setup-browser-tools.ps1",
        "apps\assistant\ultimate_assistant\local_runtime.py",
        "apps\assistant\ultimate_assistant\native_workspace.py",
        "apps\assistant\ultimate_assistant\onboarding.py",
        "apps\assistant\ultimate_assistant\browser_setup.py",
        "apps\assistant\ultimate_assistant\updater.py",
        "apps\assistant\ultimate_assistant\release_experience.py",
        "apps\assistant\ultimate_assistant\release-highlights.json",
        "apps\assistant\ultimate_assistant\g4f_setup.py",
        "apps\assistant\ultimate_assistant\g4f_worker.py",
        "apps\assistant\ultimate_assistant\g4f_gateway.py",
        "components\canvas-overlay\requirements.txt",
        "components\canvas-overlay\overlay.py"
    )
    foreach ($relative in $required) {
        if (-not (Test-Path (Join-Path $workspaceRoot $relative))) {
            throw "Required installer component is missing: $relative"
        }
    }
    py -3.13 --version *> $null
    if ($LASTEXITCODE -ne 0) { throw "Build requires Python 3.13 and the Windows py launcher." }
    $env:UA_BUILD_ROOT = $workspaceRoot
    # Modules aren't bundled: each downloads on demand. The built-in catalog must list them with URL + SHA-256.
    py -3.13 -c "import json,os,pathlib,re; c=json.loads((pathlib.Path(os.environ['UA_BUILD_ROOT'])/'apps/assistant/ultimate_assistant/module_catalog.json').read_text('utf-8')); assert all(m.get('url','').startswith('https://') and re.fullmatch(r'[a-f0-9]{64}', m.get('sha256','')) for m in c['modules']), 'Module catalog entries need an HTTPS url and SHA-256; run optional-modules/build.py --app'"
    if ($LASTEXITCODE -ne 0) { throw "Module catalog validation failed." }

    New-Item -ItemType Directory -Path $bundleRoot -Force | Out-Null
    $appsTarget = Join-Path $bundleRoot "apps"
    New-Item -ItemType Directory -Path $appsTarget -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $workspaceRoot "apps\assistant") -Destination (Join-Path $appsTarget "assistant") -Recurse
    Set-Content -LiteralPath (Join-Path $appsTarget "assistant\VERSION") -Value $Version -Encoding ascii
    $canvasTarget = Join-Path $bundleRoot "components\canvas-overlay"
    New-Item -ItemType Directory -Path $canvasTarget -Force | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $workspaceRoot "components\canvas-overlay") -Force |
        Copy-Item -Destination $canvasTarget -Recurse -Force

    Get-ChildItem -LiteralPath $bundleRoot -Directory -Recurse -Force |
        Where-Object { $_.Name -in @(".git", "__pycache__", ".venv", "venv", "node_modules", ".playwright-mcp", ".runtime") } |
        Sort-Object FullName -Descending |
        Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $bundleRoot -File -Recurse -Force |
        Where-Object { $_.Name -like "test_*.py" -or $_.Name -like "*_test.py" -or $_.Extension -in @(".pyc", ".pyo") -or $_.Name -match '\.(db|sqlite|sqlite3)(-wal|-shm)?$' } |
        Remove-Item -Force
    # Every core file's SHA-256: the installed app checks itself against it (layers.py), so a
    # changed core is noticed before an update replaces it.
    $env:UA_BUNDLE_ROOT = $bundleRoot
    $env:UA_BUNDLE_VERSION = $Version
    py -3.13 -c "import os,sys,pathlib; sys.path.insert(0, os.path.join(os.environ['UA_BUILD_ROOT'], 'apps', 'assistant')); from ultimate_assistant.layers import write_manifest; print(write_manifest(pathlib.Path(os.environ['UA_BUNDLE_ROOT']), os.environ['UA_BUNDLE_VERSION']))"
    if ($LASTEXITCODE -ne 0) { throw "Could not write the core manifest." }

    $readme = @"
Ultimate Assistant for Windows

1. Extract this entire ZIP to a writable folder.
2. Double-click apps\assistant\Install-UltimateAssistant.cmd.
   Or run apps\assistant\Install-UltimateAssistant.ps1 from PowerShell.
3. Setup downloads the Herald Router (the release pinned in herald.json, checksum-verified) and the app starts it on this device. In the app, connect an AI subscription (install its CLI if needed and sign in) or paste an API key; keys are stored encrypted on this device.

Requirements: Windows PowerShell 5.1 or newer, Python 3.13 with the py launcher,
and internet access for Python packages. Node.js/npm is recommended for the
official filesystem MCP. Optional modules download when needed. Provider accounts and credentials are never bundled.
Public HTTPS updates need no GitHub account or Git.

Build version: $Version
"@
    Set-Content -LiteralPath (Join-Path $bundleRoot "INSTALL.txt") -Value $readme -Encoding utf8

    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    $output = Join-Path ([System.IO.Path]::GetFullPath($OutputDirectory)) "UltimateAssistant-$Version-windows-x64.zip"
    Compress-Archive -Path (Join-Path $bundleRoot "*") -DestinationPath $output -CompressionLevel Optimal -Force
    Write-Host "Installer bundle created: $output"
    Write-Host "Bundle size: $([math]::Round((Get-Item $output).Length / 1MB, 1)) MB"
} finally {
    if (Test-Path -LiteralPath $stage) {
        $resolvedStage = [System.IO.Path]::GetFullPath($stage)
        $expectedTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\') + '\'
        if (-not $resolvedStage.StartsWith($expectedTemp, [System.StringComparison]::OrdinalIgnoreCase) -or
            [System.IO.Path]::GetFileName($resolvedStage) -notmatch '^UltimateAssistant-bundle-[0-9a-f]{32}$') {
            throw "Refusing to remove unexpected staging path: $resolvedStage"
        }
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}
