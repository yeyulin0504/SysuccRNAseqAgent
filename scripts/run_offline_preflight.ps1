[CmdletBinding()]
param(
    [string]$Config = "runs\fastq_pair_test\project.json",
    [string]$Output = "",
    [switch]$PasswordPrompt
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

function Resolve-WorkspacePath {
    param([Parameter(Mandatory = $true)][string]$Value)
    if ([System.IO.Path]::IsPathRooted($Value)) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $repoRoot $Value))
}

$configPath = Resolve-WorkspacePath -Value $Config
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "Project config not found: $configPath"
}

if ([string]::IsNullOrWhiteSpace($Output)) {
    $outputPath = Join-Path (Split-Path -Parent $configPath) "server_preflight.json"
} else {
    $outputPath = Resolve-WorkspacePath -Value $Output
}

$pythonCandidates = @(
    $env:RNASEQ_AGENT_PYTHON,
    (Join-Path $repoRoot ".venv\Scripts\python.exe"),
    (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe")
) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }

$pythonExe = $pythonCandidates |
    Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
    Select-Object -First 1

if (-not $pythonExe) {
    throw "No Python runtime found. Set RNASEQ_AGENT_PYTHON to a Python 3.11+ executable."
}

$previousPythonPath = [Environment]::GetEnvironmentVariable("PYTHONPATH", "Process")
$preflightExitCode = 1
$attemptToken = "{0}-{1}" -f (Get-Date -Format "yyyyMMddTHHmmss"), $PID
$attemptOutputPath = "$outputPath.pending-$attemptToken"
try {
    $pythonPathEntries = @((Join-Path $repoRoot "src"))
    if ($PasswordPrompt) {
        $offlineDependencies = Join-Path $repoRoot "runs\.offline_preflight_deps"
        if (-not (Test-Path -LiteralPath $offlineDependencies -PathType Container)) {
            throw "Offline password dependencies are missing. Reconnect the company VPN and ask Codex to prepare them before switching VPNs."
        }
        $pythonPathEntries += $offlineDependencies
    }
    $env:PYTHONPATH = $pythonPathEntries -join [System.IO.Path]::PathSeparator
    if ($PasswordPrompt) {
        & $pythonExe -c "import bcrypt, cryptography, nacl, paramiko"
        if ($LASTEXITCODE -ne 0) {
            throw "Offline password dependencies failed their import check. Reconnect the company VPN before retrying."
        }
    }
    Write-Host "Running the fixed read-only HPC preflight..."
    Write-Host "The agent will not inspect local FASTQ, upload/download files, create project files remotely, submit a job, or execute init_commands."
    $preflightArguments = @(
        "-m", "rnaseq_agent", "preflight", $configPath,
        "--output", $attemptOutputPath
    )
    if ($PasswordPrompt) {
        $preflightArguments += "--prompt-password"
    }
    & $pythonExe @preflightArguments
    $preflightExitCode = $LASTEXITCODE
} finally {
    if ($null -eq $previousPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONPATH = $previousPythonPath
    }
}

if (Test-Path -LiteralPath $attemptOutputPath -PathType Leaf) {
    try {
        $freshReport = Get-Content -LiteralPath $attemptOutputPath -Raw |
            ConvertFrom-Json -ErrorAction Stop
        if ($freshReport.schema_version -ne 1 -or -not $freshReport.generated_at) {
            throw "The fresh preflight report is incomplete."
        }
        if (Test-Path -LiteralPath $outputPath -PathType Leaf) {
            $backupPath = "$outputPath.previous-$attemptToken"
            Move-Item -LiteralPath $outputPath -Destination $backupPath
            Write-Host "Previous report preserved as: $backupPath"
        }
        Move-Item -LiteralPath $attemptOutputPath -Destination $outputPath
    } catch {
        Write-Host "A fresh report candidate was produced but could not be validated; the previous report was not replaced."
        exit 1
    }
    Write-Host "Fresh sanitized report: $outputPath"
    Write-Host "Reconnect the company VPN, then tell Codex that the offline preflight is complete."
} else {
    Write-Host "No fresh report was written. Check hospital VPN access, the password, and host-key trust."
}

exit $preflightExitCode
