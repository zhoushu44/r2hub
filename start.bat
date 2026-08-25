@echo off
cd /d %~dp0
set ADMIN_TOKEN=zs1236547
set PORT=8100
set DB_PATH=%~dp0data\r2hub.db
if exist "%~dp0.venv\Scripts\python.exe" (
  .venv\Scripts\python.exe main.py
) else (
  python main.py
)
pause
