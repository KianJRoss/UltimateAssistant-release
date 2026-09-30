$ErrorActionPreference = "Stop"
$workspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $workspaceRoot ".venvs\assistant\Scripts\python.exe"
$settingsPath = Join-Path $env:LOCALAPPDATA "UltimateAssistant\settings.json"
$credentialPath = Join-Path $env:LOCALAPPDATA "UltimateAssistant\herald-api-key.txt"
if (-not (Test-Path $python)) {
    throw "Run apps\assistant\setup.ps1 first."
}
if (-not (Test-Path -LiteralPath $credentialPath)) {
    throw "Herald key file not found. Run apps\assistant\configure.ps1 to connect your own Router."
}
$userSettings = @{}
if (Test-Path -LiteralPath $settingsPath) {
    $settingsObject = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
    foreach ($property in $settingsObject.PSObject.Properties) {
        $userSettings[$property.Name] = $property.Value
    }
}
$previousHeraldUrl = $env:HERALD_URL
$previousHeraldKey = $env:HERALD_API_KEY
$env:HERALD_URL = if ($previousHeraldUrl) { $previousHeraldUrl } elseif ($userSettings.herald_url) { $userSettings.herald_url } else { "http://127.0.0.1:8790" }
$env:HERALD_API_KEY = [System.IO.File]::ReadAllText($credentialPath).Trim()
Push-Location $PSScriptRoot
try {
    & $python -m ultimate_assistant @args
    exit $LASTEXITCODE
} finally {
    Pop-Location
    $env:HERALD_URL = $previousHeraldUrl
    $env:HERALD_API_KEY = $previousHeraldKey
}
