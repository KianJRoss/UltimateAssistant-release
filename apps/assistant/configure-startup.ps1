param([switch]$Disable)
$ErrorActionPreference = 'Stop'
$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup 'Ultimate Assistant.lnk'
$preferences = Join-Path $env:LOCALAPPDATA 'UltimateAssistant\startup-disabled'
if ($Disable) {
    Remove-Item -LiteralPath $shortcutPath -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path (Split-Path $preferences) -Force | Out-Null
    Set-Content -LiteralPath $preferences -Value 'disabled'
    Write-Host 'Ultimate Assistant launch at sign-in disabled.'
    return
}
Remove-Item -LiteralPath $preferences -Force -ErrorAction SilentlyContinue
$launcher = Join-Path $PSScriptRoot 'run-local.ps1'
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
$shortcut.TargetPath = 'powershell.exe'
$shortcut.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $launcher + '"'
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.WindowStyle = 7
$shortcut.Save()
Write-Host 'Ultimate Assistant will open when this Windows user signs in.'
