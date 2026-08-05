[CmdletBinding()]
param(
    [string]$BundleDirectory = "runs\portable_readonly_preflight_bundle",
    [string]$ZipPath = "runs\readonly_hpc_preflight_portable_windows_x64_20260804.zip"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

function Resolve-RepoPath {
    param([Parameter(Mandatory = $true)][string]$Value)
    if ([System.IO.Path]::IsPathRooted($Value)) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $repoRoot $Value))
}

$bundle = Resolve-RepoPath -Value $BundleDirectory
$zip = Resolve-RepoPath -Value $ZipPath
if (-not (Test-Path -LiteralPath (Join-Path $bundle "readonly_hpc_preflight.exe") -PathType Leaf)) {
    throw "Portable executable is missing from $bundle"
}
if (Test-Path -LiteralPath $zip) {
    throw "Refusing to overwrite existing ZIP: $zip"
}

Copy-Item -LiteralPath (Join-Path $PSScriptRoot "RUN_PREFLIGHT.bat") -Destination $bundle
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "SELF_TEST_ONLY.bat") -Destination $bundle
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "PORTABLE_PREFLIGHT_README.txt") -Destination (Join-Path $bundle "README_FIRST.txt")
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "PORTABLE_BUILD_INFO.txt") -Destination (Join-Path $bundle "BUILD_INFO.txt")
Copy-Item -LiteralPath (Join-Path $repoRoot "scripts\offline_preflight_requirements-win-py312.txt") -Destination $bundle

$licenseDir = New-Item -ItemType Directory -Path (Join-Path $bundle "licenses") -Force
$licenseCopies = @(
    @((Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\LICENSE.txt"), "Python-3.12-LICENSE.txt"),
    @("runs\.offline_preflight_deps\bcrypt-5.0.0.dist-info\LICENSE", "bcrypt-5.0.0-LICENSE.txt"),
    @("runs\.offline_preflight_deps\cffi-2.1.0.dist-info\licenses\LICENSE", "cffi-2.1.0-LICENSE.txt"),
    @("runs\.offline_preflight_deps\cryptography-50.0.0.dist-info\licenses\LICENSE", "cryptography-50.0.0-LICENSE.txt"),
    @("runs\.offline_preflight_deps\cryptography-50.0.0.dist-info\licenses\LICENSE.APACHE", "cryptography-50.0.0-LICENSE.APACHE.txt"),
    @("runs\.offline_preflight_deps\cryptography-50.0.0.dist-info\licenses\LICENSE.BSD", "cryptography-50.0.0-LICENSE.BSD.txt"),
    @("runs\.offline_preflight_deps\invoke-3.0.3.dist-info\licenses\LICENSE", "invoke-3.0.3-LICENSE.txt"),
    @("runs\.offline_preflight_deps\paramiko-4.0.0.dist-info\licenses\LICENSE", "paramiko-4.0.0-LICENSE.txt"),
    @("runs\.offline_preflight_deps\pycparser-3.0.dist-info\licenses\LICENSE", "pycparser-3.0-LICENSE.txt"),
    @("runs\.offline_preflight_deps\pynacl-1.6.2.dist-info\licenses\LICENSE", "PyNaCl-1.6.2-LICENSE.txt"),
    @("runs\.offline_preflight_deps\pynacl-1.6.2.dist-info\licenses\licenses\LICENSE.libsodium.txt", "libsodium-LICENSE.txt"),
    @("runs\.offline_builder_deps\pyinstaller-6.16.0.dist-info\licenses\COPYING.txt", "PyInstaller-6.16.0-COPYING.txt")
)
foreach ($copy in $licenseCopies) {
    $source = if ([System.IO.Path]::IsPathRooted($copy[0])) { $copy[0] } else { Join-Path $repoRoot $copy[0] }
    Copy-Item -LiteralPath $source -Destination (Join-Path $licenseDir $copy[1])
}

$sourceDir = New-Item -ItemType Directory -Path (Join-Path $bundle "source_code") -Force
foreach ($source in @(
    "packaging\portable_preflight_entry.py",
    "packaging\export_preflight_config.py",
    "src\rnaseq_agent\container.py",
    "src\rnaseq_agent\portable_preflight.py",
    "src\rnaseq_agent\preflight.py",
    "src\rnaseq_agent\remote_transport.py"
)) {
    Copy-Item -LiteralPath (Join-Path $repoRoot $source) -Destination $sourceDir
}

$manifestPath = Join-Path $bundle "SHA256SUMS.txt"
$manifestLines = Get-ChildItem -LiteralPath $bundle -Recurse -File |
    Where-Object { $_.FullName -ne $manifestPath } |
    Sort-Object FullName |
    ForEach-Object {
        $relative = $_.FullName.Substring($bundle.Length + 1).Replace("\", "/")
        $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash *$relative"
    }
[System.IO.File]::WriteAllLines(
    $manifestPath,
    $manifestLines,
    [System.Text.UTF8Encoding]::new($false)
)

Compress-Archive -LiteralPath $bundle -DestinationPath $zip -CompressionLevel Optimal
$zipHash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
$zipHashPath = "$zip.sha256.txt"
[System.IO.File]::WriteAllText(
    $zipHashPath,
    "$zipHash *$([System.IO.Path]::GetFileName($zip))`r`n",
    [System.Text.UTF8Encoding]::new($false)
)

Get-Item -LiteralPath $zip, $zipHashPath | Select-Object FullName, Length
Write-Output "ZIP_SHA256=$zipHash"
