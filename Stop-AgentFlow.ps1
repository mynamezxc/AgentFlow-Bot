[CmdletBinding(SupportsShouldProcess = $true)]
param([ValidateRange(1, 65535)][int]$Port = 8765)

# Stops the AgentFlow runtime started from this folder, including its child processes
# (Codex/Claude CLI turns, managed Chromium, MCP servers). Work is not cancelled: unfinished
# runs keep their checkpoints and resume on the next Start-AgentFlow.ps1.
$ErrorActionPreference = 'Stop'
$projectPath = [System.IO.Path]::GetFullPath($PSScriptRoot)
$serverPath = Join-Path $projectPath 'server.py'

$targets = @{}
foreach ($process in @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe' OR Name = 'py.exe'")) {
    if ($process.CommandLine -and $process.CommandLine.Contains($serverPath)) { $targets[[int]$process.ProcessId] = $process }
}
# Also catch a runtime started with a relative path: whoever listens on the port and runs server.py.
$listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
foreach ($listener in $listeners) {
    $owner = Get-CimInstance Win32_Process -Filter ("ProcessId = " + [int]$listener.OwningProcess) -ErrorAction SilentlyContinue
    if ($owner -and $owner.CommandLine -and $owner.CommandLine -match 'server\.py') { $targets[[int]$owner.ProcessId] = $owner }
}

if ($targets.Count -eq 0) {
    Write-Output 'AgentFlow is not running.'
    return
}

foreach ($id in $targets.Keys) {
    if (-not $PSCmdlet.ShouldProcess("PID $id ($serverPath)", 'Stop AgentFlow runtime and its child processes')) { continue }
    # /T stops the whole process tree so no CLI, browser or MCP child is left behind.
    & taskkill.exe /PID $id /T /F | Out-Null
    Wait-Process -Id $id -Timeout 15 -ErrorAction SilentlyContinue
    if (Get-Process -Id $id -ErrorAction SilentlyContinue) {
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    }
    Write-Output "Stopped AgentFlow runtime (PID $id)."
}

for ($attempt = 0; $attempt -lt 20; $attempt++) {
    if (-not @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue).Count) { break }
    Start-Sleep -Milliseconds 250
}
if (@(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue).Count) {
    Write-Warning "Port $Port is still in use by another program."
} else {
    Write-Output "Port $Port is free. Unfinished work resumes from its checkpoint on the next start."
}
