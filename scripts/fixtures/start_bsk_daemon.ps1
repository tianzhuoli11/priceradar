# Start BrowserSkill daemon outside Cursor's Job Object (official independent-terminal path).
# Run this in a normal PowerShell window (Win+R → powershell), keep it open.

$ErrorActionPreference = "Stop"
$bsk = Join-Path $env:USERPROFILE ".local\bin\bsk.exe"
if (-not (Test-Path $bsk)) {
    Write-Error "bsk not found at $bsk — install with: irm https://raw.githubusercontent.com/Tencent/BrowserSkill/main/install.ps1 | iex"
}
Write-Host "Using $bsk"
Write-Host "BSK_HOME default: $env:USERPROFILE\.bsk"
# Windows Hyper-V often excludes TCP 52733-52832; default 52800 then fails with
# os error 10013. Prefer an unexcluded port.
$port = 54100
Write-Host "WS port: $port (avoid Hyper-V exclusion around 52800)"
Write-Host "Keep this window open while the Agent runs browser commands."
& $bsk daemon start --foreground --daemon-idle 2h --port $port
