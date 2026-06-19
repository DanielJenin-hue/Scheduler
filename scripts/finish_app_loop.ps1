$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$log = Join-Path $root 'logs\finish_app_loop.log'
$pidFile = Join-Path $root 'logs\finish_app_loop.pid'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
Set-Content -Path $pidFile -Value $PID -Encoding utf8

$prompt = 'FINISH_APP: goal-coordinator orchestrates all 11 subagents toward unanimous 100% production-ready. Fix ONE blocker per iteration. pytest + RSI gate. $2000 CAD MRR.'
Add-Content -Path $log -Value "$(Get-Date -Format o) FINISH_APP loop started pid=$PID"

while ($true) {
  Start-Sleep -Seconds 86400
  $json = @{ prompt = $prompt } | ConvertTo-Json -Compress
  $line = "AGENT_LOOP_TICK_FINISH_APP $json"
  Add-Content -Path $log -Value "$(Get-Date -Format o) $line"
  Write-Output $line
}
