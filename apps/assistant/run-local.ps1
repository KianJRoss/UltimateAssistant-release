$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $root '.venvs\assistant\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run Install-UltimateAssistant.cmd first.' }
$mutex = New-Object System.Threading.Mutex($false, 'Local\UltimateAssistant-LocalRuntime')
$owned = $false
try {
    try { $owned = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned) { Start-Process 'http://127.0.0.1:8765'; return }
    Push-Location $PSScriptRoot
    try { & $python -m ultimate_assistant.local_runtime } finally { Pop-Location }
} finally {
    if ($owned) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
