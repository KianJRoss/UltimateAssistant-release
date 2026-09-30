$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $root '.venvs\assistant\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run Install-UltimateAssistant.cmd first.' }
Push-Location $PSScriptRoot
try { & $python -m ultimate_assistant.local_runtime } finally { Pop-Location }
