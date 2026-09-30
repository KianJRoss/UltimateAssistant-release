$ErrorActionPreference = "Stop"
$appData = Join-Path $env:LOCALAPPDATA "UltimateAssistant"
$settingsPath = Join-Path $appData "settings.json"
$credentialPath = Join-Path $appData "herald-api-key.txt"
$elevenLabsCredentialPath = Join-Path $appData "elevenlabs-api-key.txt"
New-Item -ItemType Directory -Path $appData -Force | Out-Null

$existing = @{}
if (Test-Path -LiteralPath $settingsPath) {
    $settingsObject = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
    foreach ($property in $settingsObject.PSObject.Properties) {
        $existing[$property.Name] = $property.Value
    }
}
$defaultUrl = if ($existing.herald_url) { $existing.herald_url } else { "http://127.0.0.1:8790" }
$url = Read-Host "Herald Router URL [$defaultUrl]"
if (-not $url) { $url = $defaultUrl }
if ($url -notmatch '^https?://[^\s]+$') { throw "Enter a valid HTTP(S) URL." }
$defaultFilesRoot = if ($existing.files_root) { $existing.files_root } else { Join-Path ([Environment]::GetFolderPath("MyDocuments")) "UltimateAssistant" }
$filesRoot = Read-Host "Assistant file and command workspace [$defaultFilesRoot]"
if (-not $filesRoot) { $filesRoot = $defaultFilesRoot }
if (-not [System.IO.Path]::IsPathRooted($filesRoot)) { throw "The assistant workspace must be an absolute path." }
New-Item -ItemType Directory -Path $filesRoot -Force | Out-Null
$filesRoot = (Resolve-Path -LiteralPath $filesRoot).Path
$key = Read-Host "Herald Router API key"
if (-not $key) { throw "An API key is required. Get it from your own Herald Router setup." }
$elevenLabsKey = Read-Host "ElevenLabs API key (optional; blank keeps existing key)"

$routerUrl = $url.TrimEnd('/')
try {
    $null = Invoke-RestMethod -Uri "$routerUrl/v1/models" -Method Get `
        -Headers @{ Authorization = "Bearer $($key.Trim())" } -TimeoutSec 15
} catch {
    $key = $null
    throw "Could not authenticate to the Herald Router at $routerUrl. Check the address, key, and Router availability. Nothing was saved. Details: $($_.Exception.Message)"
}

$existing.herald_url = $routerUrl
$existing.files_root = $filesRoot
[System.IO.File]::WriteAllText($settingsPath, ($existing | ConvertTo-Json -Depth 5) + "`n")
[System.IO.File]::WriteAllText($credentialPath, $key.Trim())
$key = $null
if ($elevenLabsKey) {
    [System.IO.File]::WriteAllText($elevenLabsCredentialPath, $elevenLabsKey.Trim())
}
$elevenLabsKey = $null
Write-Host "Router connection saved for this Windows account."
