$ErrorActionPreference = "SilentlyContinue"
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'spawn_main|main\.py|r2hub' } |
    ForEach-Object { Write-Output ("KILL " + $_.ProcessId + " :: " + $_.CommandLine.Substring(0,[Math]::Min(80,$_.CommandLine.Length))); Stop-Process -Id $_.ProcessId -Force }
Start-Sleep 3
$l = Get-NetTCPConnection -LocalPort 8100 -State Listen
if ($l) { Write-Output "port STILL busy" } else { Write-Output "port free" }
