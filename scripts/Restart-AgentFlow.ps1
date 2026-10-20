param([int]$ProcessId = 0, [ValidateRange(1, 65535)][int]$Port = 8765)

# Restarts the AgentFlow runtime (double-click Restart-AgentFlow.cmd). It shares one code path with Telegram /restart and
# Settings > Restart runtime: scripts/restart_helper.py waits for the old process, starts the runtime again with the same
# Python and arguments, checks that it answers and logs every step to .agentflow\restart.log.
#  - -ProcessId is optional: the runtime is found by its listener on -Port, else by a python process running this folder's server.py.
#  - When no runtime is running this only starts it (Start-AgentFlow.ps1).
$ErrorActionPreference = 'Stop'
$projectPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
. (Join-Path $PSScriptRoot 'AgentFlow-Common.ps1')
$startScript = Join-Path $projectPath 'Start-AgentFlow.ps1'
$stopScript = Join-Path $projectPath 'Stop-AgentFlow.ps1'
$serverPath = Join-Path $projectPath 'server.py'
$helperPath = Join-Path $PSScriptRoot 'restart_helper.py'

if ($ProcessId -le 0) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) { $ProcessId = [int]$listener.OwningProcess }
    if ($ProcessId -le 0) {
        foreach ($process in @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe' OR Name = 'py.exe'")) {
            if ($process.CommandLine -and $process.CommandLine.Contains($serverPath)) { $ProcessId = [int]$process.ProcessId; break }
        }
    }
}
if ($ProcessId -le 0) {
    & $startScript -NoBrowser -Port $Port   # nothing is running: just start AgentFlow
    return
}

$running = Get-CimInstance Win32_Process -Filter ("ProcessId = " + $ProcessId) -ErrorAction SilentlyContinue
$preferred = ''
if ($running -and $running.ExecutablePath) { $preferred = $running.ExecutablePath }
$pythonPath = Resolve-AgentFlowPython -ProjectPath $projectPath -Preferred $preferred
if (-not $pythonPath) { throw 'Python 3.12 or newer was not found; the runtime was left running.' }

# The new runtime gets the same --port and --data-dir as the running one.
$dataPath = Join-Path $projectPath '.agentflow'
$serverArguments = @($serverPath, '--port', [string]$Port)
if ($running -and $running.CommandLine -match '--data-dir\s+("([^"]+)"|(\S+))') {
    $dataPath = if ($Matches[2]) { $Matches[2] } else { $Matches[3] }
    $serverArguments += @('--data-dir', $dataPath)
}
New-Item -ItemType Directory -Force -Path $dataPath | Out-Null

# Non-secret environment for the helper (names that look like credentials are never written).
$secretNames = 'TOKEN|SECRET|PASSW|API_?KEY|CREDENTIAL|BEARER|AUTH|COOKIE|PRIVATE|ACCESS_KEY|LICENSE'
$environment = [ordered]@{}
foreach ($item in Get-ChildItem Env:) {
    if ($item.Name -notmatch $secretNames -and $item.Value.Length -lt 8000) { $environment[$item.Name] = $item.Value }
}
$environmentFile = Join-Path $dataPath 'restart-env.json'
Set-Content -LiteralPath $environmentFile -Value ($environment | ConvertTo-Json -Compress) -Encoding UTF8

$parts = @($pythonPath, $helperPath, '--pid', [string]$ProcessId, '--port', [string]$Port, '--cwd', $projectPath, '--data-dir', $dataPath, '--env', $environmentFile, '--', $pythonPath) + $serverArguments
$commandLine = ($parts | ForEach-Object { ConvertTo-AgentFlowArgument $_ }) -join ' '
$startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
$created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $commandLine; CurrentDirectory = $projectPath; ProcessStartupInformation = $startup }
if ($created.ReturnValue -ne 0) { throw "Could not start the restart helper (code $($created.ReturnValue))." }

# This path was not asked to exit by the runtime itself, so stop it; the helper waits for the process, starts it again and logs to restart.log.
& $stopScript -Port $Port
