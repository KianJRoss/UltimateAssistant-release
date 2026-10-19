# The assistant's own monitor: a virtual display (VirtualDrivers' Virtual Display Driver, MIT)
# the assistant opens and drives windows on, off the user's screens, before handing results over.
#
# Same method as the project's official silent installer (Community Scripts/silent-install.ps1):
# nefarius' signed nefcon creates the Root\MttVDD device and installs the SignPath-signed driver.
# Both downloads are pinned by SHA-256. Needs administrator rights (re-launches itself elevated).
#
#   setup-virtual-display.ps1             install, one 1920x1080 monitor
#   setup-virtual-display.ps1 -Uninstall  remove it
#
# Driver's own guidance: uninstall it before major GPU/chipset driver updates; if a display ever
# goes black, boot into Safe Mode and uninstall "Virtual Display Driver" in Device Manager.
param([switch]$Uninstall, [int]$Width = 1920, [int]$Height = 1080)
$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $argsList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    if ($Uninstall) { $argsList += "-Uninstall" }
    Start-Process powershell -Verb RunAs -ArgumentList $argsList -Wait
    exit
}

$Driver = @{ Url = "https://github.com/VirtualDrivers/Virtual-Display-Driver/releases/download/25.7.23/VirtualDisplayDriver-x86.Driver.Only.zip"
             Sha256 = "E24210692B442B39AF763536330CE78B423F19342B7A7792C26DE3944E418B3A" }
$NefCon = @{ Url = "https://github.com/nefarius/nefcon/releases/download/v1.14.0/nefcon_v1.14.0.zip"
             Sha256 = "A15557DA24A9EFCA203158DE3B43B0EAF982DB231F0194031F1ED428BC13E669" }
$work = Join-Path $env:TEMP "ua-virtual-display"
New-Item -ItemType Directory -Force $work | Out-Null

function Get-Pinned($item, $name) {
    $path = Join-Path $work $name
    Invoke-WebRequest -Uri $item.Url -OutFile $path -UseBasicParsing
    $hash = (Get-FileHash -Algorithm SHA256 $path).Hash
    if ($hash -ne $item.Sha256) { throw "$name failed its checksum ($hash); not installing." }
    Expand-Archive -Path $path -DestinationPath $work -Force
}

Get-Pinned $NefCon "nefcon.zip"
$nefcon = Join-Path $work "x64\nefconc.exe"
$displayClass = "4D36E968-E325-11CE-BFC1-08002BE10318"
$utf8 = New-Object System.Text.UTF8Encoding($false)  # the assistant reads settings.json without a BOM

if ($Uninstall) {
    & $nefcon --remove-device-node --hardware-id "Root\MttVDD" --class-guid $displayClass
    Get-Pinned $Driver "driver.zip"
    & $nefcon --uninstall-driver --inf-path (Join-Path $work "VirtualDisplayDriver\MttVDD.inf")
    foreach ($folder in "UltimateAssistantPersonal", "UltimateAssistant") {
        $settings = Join-Path $env:LOCALAPPDATA "$folder\settings.json"
        if (Test-Path $settings) {
            $values = Get-Content $settings -Raw | ConvertFrom-Json
            $values.PSObject.Properties.Remove("operator_display")
            [System.IO.File]::WriteAllText($settings, ($values | ConvertTo-Json -Depth 20), $utf8)
        }
    }
    Write-Host "Virtual display removed." -ForegroundColor Green
    Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
    Read-Host "Press Enter to close"
    exit
}

Get-Pinned $Driver "driver.zip"

# One monitor at a normal resolution (the driver reads C:\VirtualDisplayDriver\vdd_settings.xml).
$settingsDir = "C:\VirtualDisplayDriver"
New-Item -ItemType Directory -Force $settingsDir | Out-Null
$xml = [xml](Get-Content (Join-Path $work "VirtualDisplayDriver\vdd_settings.xml") -Raw)
$xml.vdd_settings.monitors.count = "1"
$xml.Save((Join-Path $settingsDir "vdd_settings.xml"))

# Trust the driver's publisher (SignPath Foundation) so Windows installs it without a prompt.
$certs = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2Collection
$certs.Import([System.IO.File]::ReadAllBytes((Join-Path $work "VirtualDisplayDriver\mttvdd.cat")))
foreach ($cert in $certs) {
    $file = Join-Path $work "$($cert.Thumbprint).cer"
    [System.IO.File]::WriteAllBytes($file, $cert.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert))
    Import-Certificate -FilePath $file -CertStoreLocation "Cert:\LocalMachine\TrustedPublisher" | Out-Null
}

& $nefcon --create-device-node --hardware-id "Root\MttVDD" --class-name Display --class-guid $displayClass
if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual display device (nefcon exit $LASTEXITCODE)." }
& $nefcon --install-driver --inf-path (Join-Path $work "VirtualDisplayDriver\MttVDD.inf")
if ($LASTEXITCODE -ne 0) { throw "Could not install the virtual display driver (nefcon exit $LASTEXITCODE)." }
Start-Sleep -Seconds 8

# Tell the assistant which monitor is its own.
foreach ($folder in "UltimateAssistantPersonal", "UltimateAssistant") {
    $settings = Join-Path $env:LOCALAPPDATA "$folder\settings.json"
    if (Test-Path $settings) {
        $values = Get-Content $settings -Raw | ConvertFrom-Json
        $values | Add-Member -NotePropertyName operator_display -NotePropertyValue "Virtual Display Driver" -Force
        [System.IO.File]::WriteAllText($settings, ($values | ConvertTo-Json -Depth 20), $utf8)
    }
}
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "Virtual display installed. It appears as an extra monitor in Display settings; the assistant uses it as its workspace." -ForegroundColor Green
Read-Host "Press Enter to close"
