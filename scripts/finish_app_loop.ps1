$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$log = Join-Path $root 'logs\finish_app_loop.log'
$pidFile = Join-Path $root 'logs\finish_app_loop_wake.pid'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
Set-Content -Path $pidFile -Value $PID -Encoding utf8

$prompt = 'FINISH_APP: Run iteration — all 11 subagents production-ready sign-off. Run pytest, RSI gate, update FINISH_APP_ITERATIONS.md. Fix clear code blockers. Log mailto/publish bundle human blockers.'
$intervalSeconds = 86400

Add-Content -Path $log -Value "$(Get-Date -Format o) FINISH_APP dynamic wake started pid=$PID interval=${intervalSeconds}s"

Start-Sleep -Seconds $intervalSeconds
$json = @{ prompt = $prompt } | ConvertTo-Json -Compress
$line = "AGENT_LOOP_WAKE_FINISH_APP $json"
Add-Content -Path $log -Value "$(Get-Date -Format o) $line"
Write-Output $line
