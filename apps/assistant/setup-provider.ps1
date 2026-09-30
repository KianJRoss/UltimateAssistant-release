param([ValidateSet('antigravity','codex','claude')][string]$Provider)
$ErrorActionPreference = 'Stop'
if ($Provider -eq 'antigravity') {
    $download = Join-Path $env:TEMP ('antigravity-install-' + [guid]::NewGuid().ToString('N') + '.ps1')
    try {
        Invoke-WebRequest -UseBasicParsing -Uri 'https://antigravity.google/cli/install.ps1' -OutFile $download
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $download --skip-aliases
        if ($LASTEXITCODE -ne 0) { throw 'Antigravity installer failed.' }
    } finally { Remove-Item -LiteralPath $download -Force -ErrorAction SilentlyContinue }
} elseif ($Provider -eq 'claude') {
    $download = Join-Path $env:TEMP ('claude-install-' + [guid]::NewGuid().ToString('N') + '.ps1')
    try {
        Invoke-WebRequest -UseBasicParsing -Uri 'https://claude.ai/install.ps1' -OutFile $download
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $download
        if ($LASTEXITCODE -ne 0) { throw 'Claude Code installer failed.' }
    } finally { Remove-Item -LiteralPath $download -Force -ErrorAction SilentlyContinue }
} else {
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npm) {
        & winget install --id OpenJS.NodeJS.LTS --exact --scope user --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { throw 'Node.js installation failed. Install Node.js LTS, then retry.' }
        $env:PATH = [Environment]::GetEnvironmentVariable('PATH','Machine') + ';' + [Environment]::GetEnvironmentVariable('PATH','User')
        $npm = Get-Command npm.cmd -ErrorAction Stop
    }
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $npm.Source install -g '@openai/codex' --no-update-notifier
        $installExitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    if ($installExitCode -ne 0) { throw 'Codex installation failed.' }
}
