$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $PSScriptRoot "CaseMailLauncher.cs"
$dist = Join-Path $repoRoot "dist"
$output = Join-Path $dist "CaseMailLauncher.exe"

$candidates = @(
    "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
    "$env:WINDIR\Microsoft.NET\FrameworkArm64\v4.0.30319\csc.exe",
    "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe"
)

$csc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $csc) {
    throw "Could not find csc.exe. Install the .NET Framework developer tools or use the source launcher directly."
}

New-Item -ItemType Directory -Force -Path $dist | Out-Null

& $csc `
    /nologo `
    /target:exe `
    /platform:anycpu `
    /out:$output `
    /reference:System.Windows.Forms.dll `
    /reference:System.Management.dll `
    $source

Write-Host "Built $output"
