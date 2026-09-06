# install.ps1 -- Windows side. Run from a PowerShell window in the repo directory:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
# Idempotent. Installs uv (winget), a uv-managed Python 3.12, the venv, the packages,
# the Kokoro model files, runs the tests, starts the service and speaks one line.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Find-Uv {
  $c = Get-Command uv.exe -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  $g = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\astral-sh.uv_*\uv.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($g) { return $g.FullName }
  return $null
}

$uv = Find-Uv
if (-not $uv) {
  Write-Host "installing uv via winget"
  winget install --id astral-sh.uv -e --silent --accept-source-agreements --accept-package-agreements | Out-Null
  $uv = Find-Uv
  if (-not $uv) { throw "uv not found after winget install; install it manually (https://docs.astral.sh/uv/)" }
}
Write-Host "uv: $uv"

& $uv python install 3.12 | Out-Null
if (-not (Test-Path .venv)) { & $uv venv --python 3.12 .venv | Out-Null }
& $uv pip install --python .venv\Scripts\python.exe pocket-tts kokoro-onnx sounddevice soundfile pycaw pytest | Out-Null
Write-Host "venv ready"

New-Item -ItemType Directory -Force models, spool | Out-Null
$base = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
foreach ($f in "kokoro-v1.0.onnx", "voices-v1.0.bin") {
  if (-not (Test-Path "models\$f")) { Write-Host "downloading $f"; curl.exe -sL -o "models\$f" "$base/$f" }
}
$sz = (Get-Item models\kokoro-v1.0.onnx).Length
if ($sz -lt 300MB) { throw "kokoro-v1.0.onnx is $sz bytes; download failed" }

& .venv\Scripts\python.exe -m pytest -q test_speakability.py
if ($LASTEXITCODE -ne 0) { throw "speakability golden tests failed" }

# Pocket-TTS cloning weights are gated. Without them the service falls back to Kokoro (still Bella).
$tok = "$env:USERPROFILE\.cache\huggingface\token"
if (-not (Test-Path $tok)) {
  Write-Host "NOTE: no Hugging Face token at $tok -> service will run Kokoro (slower first audio)."
  Write-Host "      For the fast engine: accept terms at https://huggingface.co/kyutai/pocket-tts, then"
  Write-Host "      .venv\Scripts\hf.exe auth login   (token needs 'read gated repos' scope), then restart the service."
}

& .venv\Scripts\python.exe speaker.py stop | Out-Null
& .venv\Scripts\python.exe speaker.py start
Start-Sleep 12
& .venv\Scripts\python.exe speaker.py status
& .venv\Scripts\python.exe speaker.py say "Install complete. If you can hear this, the Windows side works."
Start-Sleep 8
Get-Content speaker.log -Tail 3
Write-Host ""
Write-Host "Windows side done. Now in WSL: bash install.sh   (wires the Claude Code hooks)"
