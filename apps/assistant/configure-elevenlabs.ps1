$ErrorActionPreference = "Stop"
$appData = Join-Path $env:LOCALAPPDATA "UltimateAssistant"
$credentialPath = Join-Path $appData "elevenlabs-api-key.txt"
New-Item -ItemType Directory -Path $appData -Force | Out-Null
$key = Read-Host "ElevenLabs API key"
if (-not $key) { throw "An ElevenLabs API key is required." }
[System.IO.File]::WriteAllText($credentialPath, $key.Trim())
$key = $null
Write-Host "ElevenLabs key saved for this Windows account. Restart run-ui.ps1 to use it."
