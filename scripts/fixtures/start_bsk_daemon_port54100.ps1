# Independent-terminal BSK daemon start.
# Default port 52800 is inside Windows Hyper-V excluded range 52733-52832
# (bind fails with os error 10013). Use 54100 instead.

$ErrorActionPreference = "Stop"
$bsk = Join-Path $env:USERPROFILE ".local\bin\bsk.exe"
if (-not (Test-Path $bsk)) {
    Write-Error "bsk not found at $bsk"
}
$port = 54100
Write-Host "Using $bsk"
Write-Host "WS port: $port (avoid Hyper-V exclusion around 52800)"
Write-Host "BSK_HOME: $(if ($env:BSK_HOME) { $env:BSK_HOME } else { Join-Path $env:USERPROFILE '.bsk' })"
Write-Host "Keep this window open. Then in Agent: `$env:BSK_AUTO_START='0'; bsk status --json"
& $bsk daemon start --foreground --daemon-idle 2h --port $port
Write-Host "Daemon exited with code $LASTEXITCODE"
Read-Host "Press Enter to close"
