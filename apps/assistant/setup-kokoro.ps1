$ErrorActionPreference = "Stop"
$workspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$venv = Join-Path $workspaceRoot ".venvs\kokoro"
$python = Join-Path $venv "Scripts\python.exe"
$modelDirectory = Join-Path $PSScriptRoot "models\kokoro"
$modelPath = Join-Path $modelDirectory "kokoro-v1.0.int8.onnx"
$voicesPath = Join-Path $modelDirectory "voices-v1.0.bin"
$licensePath = Join-Path $modelDirectory "LICENSE-Apache-2.0.txt"
$modelSha256 = "ae315a79b623f244700e4afb9246c46a26066782e049ba174bf3ba433970ee9c"
$voicesSha256 = "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"

if (-not (Test-Path $python)) {
    py -3.13 -m venv $venv
}
& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Could not update pip in the Kokoro environment." }
& $python -m pip install "kokoro-onnx>=0.6.1" "soundfile>=0.13"
if ($LASTEXITCODE -ne 0) { throw "Kokoro ONNX dependency installation failed." }

New-Item -ItemType Directory -Path $modelDirectory -Force | Out-Null
$ProgressPreference = "SilentlyContinue"
if (-not (Test-Path $modelPath)) {
    Invoke-WebRequest -Uri "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/kokoro-v1.0.int8.onnx" -OutFile $modelPath
}
if (-not (Test-Path $voicesPath)) {
    Invoke-WebRequest -Uri "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/voices-v1.0.bin" -OutFile $voicesPath
}
if (-not (Test-Path $licensePath)) {
    Invoke-WebRequest -Uri "https://www.apache.org/licenses/LICENSE-2.0.txt" -OutFile $licensePath
}
if ((Get-FileHash -LiteralPath $modelPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $modelSha256) { throw "Kokoro ONNX model failed SHA-256 verification." }
if ((Get-FileHash -LiteralPath $voicesPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $voicesSha256) { throw "Kokoro voice pack failed SHA-256 verification." }
Write-Host "Kokoro ONNX installed. Quantized model and voices are bundled under apps\assistant\models\kokoro."
