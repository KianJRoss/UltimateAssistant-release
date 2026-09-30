$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$runtime = Join-Path $root '.runtime'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
if (-not (Get-Command node.exe -ErrorAction SilentlyContinue)) {
    $releases = Invoke-RestMethod -Uri 'https://nodejs.org/dist/index.json'
    $release = $releases | Where-Object { $_.lts -and $_.files -contains 'win-x64-zip' } | Select-Object -First 1
    if (-not $release) { throw 'No supported Node.js LTS Windows release was found.' }
    $asset = "node-$($release.version)-win-x64.zip"
    $zip = Join-Path $runtime $asset
    $base = "https://nodejs.org/dist/$($release.version)"
    Invoke-WebRequest -UseBasicParsing -Uri "$base/$asset" -OutFile $zip
    $checksums = (Invoke-WebRequest -UseBasicParsing -Uri "$base/SHASUMS256.txt").Content
    $line = ($checksums -split "`n" | Where-Object { $_.Trim().EndsWith(" $asset") } | Select-Object -First 1)
    if (-not $line -or (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLower() -ne ($line -split '\s+')[0]) { throw 'Node.js checksum verification failed.' }
    Expand-Archive -LiteralPath $zip -DestinationPath $runtime -Force
    $nodeDir = Join-Path $runtime "node-$($release.version)-win-x64"
    Set-Content -LiteralPath (Join-Path $runtime 'node-path.txt') -Value $nodeDir
    $env:PATH = $nodeDir + ';' + $env:PATH
}
$prefix = Join-Path $runtime 'browser-mcp'
$nativePreference = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    & npm.cmd install --prefix $prefix --no-audit --no-fund --no-update-notifier 'kapture-mcp@2.6.1' '@playwright/mcp@0.0.83'
    $nativeExit = $LASTEXITCODE
} finally { $ErrorActionPreference = $nativePreference }
if ($nativeExit -ne 0) { throw 'Browser MCP package installation failed.' }
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $runtime 'browsers'
$playwrightCLI = Join-Path $prefix 'node_modules\playwright\cli.js'
if (-not (Test-Path $playwrightCLI)) { $playwrightCLI = Join-Path $prefix 'node_modules\playwright-core\cli.js' }
try {
    $ErrorActionPreference = 'Continue'
    & node.exe $playwrightCLI install chromium
    $nativeExit = $LASTEXITCODE
} finally { $ErrorActionPreference = $nativePreference }
if ($nativeExit -ne 0) { throw 'Playwright Chromium installation failed.' }
Write-Host 'Kapture and Playwright MCP packages installed. Kapture needs the browser extension and a connected tab.'
