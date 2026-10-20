[CmdletBinding(SupportsShouldProcess = $true)]
param([ValidateRange(1, 65535)][int]$Port = 8765)

$ErrorActionPreference = 'Stop'
$projectPath = [System.IO.Path]::GetFullPath($PSScriptRoot)
$projectPrefix = $projectPath.TrimEnd('\') + '\'
$backupPath = Join-Path $projectPath ('artifacts\reset-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
$sources = @(
    @{ Source = (Join-Path $projectPath '.agentflow'); Name = 'runtime' },
    @{ Source = (Join-Path $projectPath 'workspaces\company'); Name = 'company' }
)

# Check every source/destination before stopping or moving anything.
foreach ($candidate in @($backupPath) + @($sources | ForEach-Object { $_.Source })) {
    $absolutePath = [System.IO.Path]::GetFullPath($candidate)
    if (-not $absolutePath.StartsWith($projectPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Reset target is outside the project: $absolutePath"
    }
    $checkPath = $absolutePath
    while ($checkPath -and $checkPath.StartsWith($projectPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        if (Test-Path -LiteralPath $checkPath) {
            $entry = Get-Item -LiteralPath $checkPath -Force
            if ($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                throw "Reset does not move linked folders: $checkPath"
            }
        }
        if ($checkPath -eq $projectPath) { break }
        $checkPath = Split-Path -Parent $checkPath
    }
}

if (-not $PSCmdlet.ShouldProcess($projectPath, "Stop AgentFlow, back up runtime/company to $backupPath, and start a fresh workspace")) {
    return
}

# Stop the runtime and its whole process tree (CLI turns, browser, MCP servers) before moving data.
& (Join-Path $projectPath 'Stop-AgentFlow.ps1') -Port $Port

New-Item -ItemType Directory -Path $backupPath -ErrorAction Stop | Out-Null
foreach ($source in $sources) {
    if (Test-Path -LiteralPath $source.Source) {
        Move-Item -LiteralPath $source.Source -Destination (Join-Path $backupPath $source.Name) -ErrorAction Stop
    }
}
New-Item -ItemType Directory -Path (Join-Path $projectPath 'workspaces\company') -Force | Out-Null
Write-Output "Backup: $backupPath"
Write-Output 'Fresh workspace: Atlas only. Native CLI logins and .env are unchanged.'
& (Join-Path $projectPath 'Start-AgentFlow.ps1') -Port $Port
