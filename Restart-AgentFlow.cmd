@echo off
setlocal
rem Double-click to restart AgentFlow (stops the runtime if it is running, then starts it again).
set PORT=8765
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Restart-AgentFlow.ps1" -Port %PORT%
if errorlevel 1 goto failed
echo Waiting for AgentFlow on http://127.0.0.1:%PORT% ...
set TRIES=0
:wait
set /a TRIES+=1
powershell -NoProfile -Command "try { $null = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%PORT%/api/state' -TimeoutSec 2; exit 0 } catch { exit 1 }"
if not errorlevel 1 goto ok
if %TRIES% geq 45 goto failed
timeout /t 1 /nobreak >nul
goto wait
:ok
echo AgentFlow restarted
exit /b 0
:failed
echo ERROR: AgentFlow did not come back on http://127.0.0.1:%PORT%. See .agentflow\restart.log and .agentflow\server-start.log, or run Start-AgentFlow.ps1 -Foreground.
pause
exit /b 1
