param([int]$Port = 8765, [switch]$NoBrowser, [switch]$Foreground)
$ErrorActionPreference = 'Stop'
$projectPath = $PSScriptRoot
. (Join-Path $projectPath 'scripts\AgentFlow-Common.ps1')
$pythonPath = Resolve-AgentFlowPython -ProjectPath $projectPath
if (-not $pythonPath) { throw 'Python 3.12 or newer was not found. Install it (py -3.12, pyenv-win or python.org) or create .venv in this folder.' }
$serverPath = Join-Path $projectPath 'server.py'
$dataPath = Join-Path $projectPath '.agentflow'
New-Item -ItemType Directory -Force -Path $dataPath | Out-Null
$outputLog = Join-Path $dataPath 'server-start.log'
$errorLog = Join-Path $dataPath 'server-start.err.log'
if ($Foreground) {
    & $pythonPath $serverPath --port $Port
} else {
    Set-Content -LiteralPath $outputLog -Value ('{0} start: {1} {2} --port {3}' -f (Get-Date -Format 's'), $pythonPath, $serverPath, $Port) -Encoding ASCII
    $quotedServerPath = [char]34 + $serverPath + [char]34
    Start-Process -FilePath $pythonPath -ArgumentList @($quotedServerPath, '--port', $Port) -WorkingDirectory $projectPath -WindowStyle Hidden `
        -RedirectStandardOutput $outputLog -RedirectStandardError $errorLog
    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try { $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port" -UseBasicParsing; $ready = $response.StatusCode -eq 200; if ($ready) { break } } catch {}
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) {
        $tail = ''
        if (Test-Path -LiteralPath $errorLog) { $tail = (Get-Content -LiteralPath $errorLog -Tail 15 -ErrorAction SilentlyContinue) -join [Environment]::NewLine }
        throw ('Runtime is not ready. Python: {0}. Logs: {1} and {2}. {3}' -f $pythonPath, $outputLog, $errorLog, $tail)
    }
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port" }
    Write-Output "AgentFlow: http://127.0.0.1:$Port"
}
