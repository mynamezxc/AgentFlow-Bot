# Shared by Start-AgentFlow.ps1 and Restart-AgentFlow.ps1 (ASCII only, dot-source it).

function Test-AgentFlowPython {
    param([string]$Path)
    if (-not $Path) { return $false }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    if ($Path -like '*\WindowsApps\*') { return $false }      # Microsoft Store alias stubs
    if ($Path -match '\.(bat|cmd|ps1)$') { return $false }    # pyenv-win shims
    try {
        $answer = & $Path -c 'import sys; print(1 if sys.version_info >= (3, 12) else 0)' 2>$null
        return ((@($answer) | Select-Object -First 1) -eq '1')
    } catch { return $false }
}

function Get-AgentFlowPythonCandidates {
    param([string]$ProjectPath, [string]$Preferred)
    $list = New-Object System.Collections.Generic.List[string]
    if ($Preferred) { $list.Add($Preferred) }
    $list.Add((Join-Path $ProjectPath '.venv\Scripts\python.exe'))
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($version in @('-3.12', '-3')) {
            try {
                $found = & $launcher.Source $version -c 'import sys; print(sys.executable)' 2>$null
                if ($found) { $list.Add([string](@($found) | Select-Object -First 1)) }
            } catch {}
        }
    }
    $pyenv = Get-Command pyenv -ErrorAction SilentlyContinue
    if ($pyenv) {
        try {
            $found = & $pyenv.Source which python 2>$null
            if ($found) { $list.Add([string](@($found) | Select-Object -First 1)) }
        } catch {}
    }
    if ($env:USERPROFILE) {
        $versions = Join-Path $env:USERPROFILE '.pyenv\pyenv-win\versions'
        if (Test-Path -LiteralPath $versions) {
            $folders = @(Get-ChildItem -LiteralPath $versions -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like '3.1[2-9]*' })
            $folders = $folders | Sort-Object { try { [version]($_.Name -replace '[^0-9.].*$', '') } catch { [version]'0.0' } } -Descending
            foreach ($folder in $folders) { $list.Add((Join-Path $folder.FullName 'python.exe')) }
        }
    }
    foreach ($command in @(Get-Command python -All -ErrorAction SilentlyContinue)) { if ($command.Source) { $list.Add($command.Source) } }
    try { foreach ($line in @(& where.exe python 2>$null)) { if ($line) { $list.Add([string]$line) } } } catch {}
    return $list
}

function Resolve-AgentFlowPython {
    param([string]$ProjectPath, [string]$Preferred)
    foreach ($candidate in (Get-AgentFlowPythonCandidates -ProjectPath $ProjectPath -Preferred $Preferred)) {
        if (Test-AgentFlowPython -Path $candidate) { return $candidate }
    }
    return $null
}

function ConvertTo-AgentFlowArgument {
    param([string]$Text)
    return '"' + ($Text -replace '"', '\"') + '"'
}
