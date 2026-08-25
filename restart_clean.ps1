$ErrorActionPreference = "SilentlyContinue"
for ($round = 1; $round -le 6; $round++) {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'main\.py' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep 2
    $l = Get-NetTCPConnection -LocalPort 8100 -State Listen
    if (-not $l) { Write-Output "port free after round $round"; break }
}
if (Get-NetTCPConnection -LocalPort 8100 -State Listen) { Write-Output "STILL BUSY"; exit 1 }

$wd = "E:\360MoveData\Users\Administrator\Desktop\cf\r2hub"
$cmd = "cmd /c cd /d `"$wd`" && set ADMIN_TOKEN=zs1236547&& set PORT=8100&& set WORKERS=4&& set THREADPOOL_TOKENS=200&& set DB_PATH=$wd\data\r2hub.db&& .venv\Scripts\python.exe main.py 1>>server.log 2>>server.err.log"
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd } | Out-Null
Start-Sleep 6
$h = Invoke-WebRequest "http://127.0.0.1:8100/health" -UseBasicParsing -TimeoutSec 5
Write-Output "health: $($h.Content)"
& "$wd\.venv\Scripts\python.exe" probe_now.py
