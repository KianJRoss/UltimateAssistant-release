param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot "..\..\dist"),
    [string]$Version = "0.2.1"
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
        "apps\assistant\ultimate_assistant\g4f_setup.py",
        "apps\assistant\ultimate_assistant\g4f_worker.py",
        "apps\assistant\ultimate_assistant\g4f_gateway.py",
        "components\herald\herald\router\provider_discovery.py",
        "apps\assistant\ultimate_assistant\study_store.py",
        "apps\assistant\ultimate_assistant\study_tools.py",
        "apps\assistant\ultimate_assistant\flashcard_store.py",
        "components\herald\pyproject.toml",
        "components\herald\herald\math_tools.py",
        "components\herald\herald\mcp_gateway.py",
        "components\herald\herald\router\math_tools.py",
        "components\perception-mcp\requirements.txt",
        "components\ai-workspace\win-ui-mcp\requirements.txt"
    )
    foreach ($relative in $required) {
        if (-not (Test-Path (Join-Path $workspaceRoot $relative))) {
            throw "Required installer component is missing: $relative"
        }
    }
    py -3.13 --version *> $null
    if ($LASTEXITCODE -ne 0) { throw "Build requires Python 3.13 and the Windows py launcher." }

    New-Item -ItemType Directory -Path $bundleRoot -Force | Out-Null
    $appsTarget = Join-Path $bundleRoot "apps"
    New-Item -ItemType Directory -Path $appsTarget -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $workspaceRoot "apps\assistant") -Destination (Join-Path $appsTarget "assistant") -Recurse
    $heraldTarget = Join-Path $bundleRoot "components\herald"
    New-Item -ItemType Directory -Path $heraldTarget -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $workspaceRoot "components\herald\herald") -Destination $heraldTarget -Recurse
    foreach ($file in @("pyproject.toml", "setup.py", "README.md", "LICENSE")) {
        Copy-Item -LiteralPath (Join-Path $workspaceRoot "components\herald\$file") -Destination $heraldTarget
    }
    Set-Content -LiteralPath (Join-Path $appsTarget "assistant\VERSION") -Value $Version -Encoding ascii
    $maintainerOnlyHeraldFiles = @(
        "herald\admin_gui.py",
        "herald\router\admin_control.py",
        "herald\router\admin_loop.py",
        "herald\router\admin_state.py",
        "herald\router\admin_workspace.py",
        "herald\router\static\admin_review.html",
        "herald\router\static\admin_review.js"
    )
    foreach ($relative in $maintainerOnlyHeraldFiles) {
        Remove-Item -LiteralPath (Join-Path $heraldTarget $relative) -Force -ErrorAction SilentlyContinue
    }
    $perceptionTarget = Join-Path $bundleRoot "components\perception-mcp"
    $controlTarget = Join-Path $bundleRoot "components\ai-workspace\win-ui-mcp"
    New-Item -ItemType Directory -Path $perceptionTarget -Force | Out-Null
    New-Item -ItemType Directory -Path $controlTarget -Force | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $workspaceRoot "components\perception-mcp") -Force |
        Copy-Item -Destination $perceptionTarget -Recurse -Force
    Get-ChildItem -LiteralPath (Join-Path $workspaceRoot "components\ai-workspace\win-ui-mcp") -Force |
        Copy-Item -Destination $controlTarget -Recurse -Force

    Get-ChildItem -LiteralPath $bundleRoot -Directory -Recurse -Force |
        Where-Object { $_.Name -in @(".git", "__pycache__", ".venv", "venv", "node_modules", ".playwright-mcp", ".runtime") } |
        Sort-Object FullName -Descending |
        Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $bundleRoot -File -Recurse -Force |
        Where-Object { $_.Extension -in @(".pyc", ".pyo") -or $_.Name -match '\.(db|sqlite|sqlite3)(-wal|-shm)?$' } |
        Remove-Item -Force

    $readme = @"
Ultimate Assistant for Windows

1. Extract this entire ZIP to a writable folder.
2. Double-click apps\assistant\Install-UltimateAssistant.cmd.
   Or run apps\assistant\Install-UltimateAssistant.ps1 from PowerShell.
3. The app starts its bundled Herald Router on this device. Select Antigravity or Codex, install it if needed, and sign in in the app.

Requirements: Windows PowerShell 5.1 or newer, Python 3.13 with the py launcher,
and internet access for Python packages. Node.js/npm is recommended for the
official filesystem MCP. The Herald Router is bundled. Provider accounts and credentials are never bundled.
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
