param([switch]$Disable)
$ErrorActionPreference = 'Stop'
$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup 'Ultimate Assistant.lnk'
$preferences = Join-Path $env:LOCALAPPDATA 'UltimateAssistant\startup-disabled'
$launcher = Join-Path $PSScriptRoot 'run-local.ps1'
$desktopShortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Ultimate Assistant.lnk'
if (Test-Path -LiteralPath $desktopShortcutPath) {
    $desktopShortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($desktopShortcutPath)
    if ($desktopShortcut.TargetPath -match 'powershell\.exe$' -and $desktopShortcut.Arguments -match 'run-local\.ps1') {
        $desktopShortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $launcher + '"'
        $desktopShortcut.WorkingDirectory = $PSScriptRoot
        $desktopShortcut.Save()
    }
}
if ($Disable) {
    Remove-Item -LiteralPath $shortcutPath -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path (Split-Path $preferences) -Force | Out-Null
    Set-Content -LiteralPath $preferences -Value 'disabled'
    Write-Host 'Ultimate Assistant launch at sign-in disabled.'
    return
}
Remove-Item -LiteralPath $preferences -Force -ErrorAction SilentlyContinue
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
$shortcut.TargetPath = 'powershell.exe'
$shortcut.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $launcher + '"'
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.WindowStyle = 7
$shortcut.Save()
Write-Host 'Ultimate Assistant will open when this Windows user signs in.'
