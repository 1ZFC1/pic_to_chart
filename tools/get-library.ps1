# Fetches the chart I/O library this tool depends on (rizChartFunction_Library).
#
# The library is NOT part of this repository (it is a separate project, see its upstream URL below);
# this script only clones it. The tool finds it automatically when it sits next to the tool folder:
#
#   <parent of pic_to_chart>\
#       pic_to_chart\
#       rizChartFunction_Library\        <- cloned here by default
#           src\rizChartFunction\
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\get-library.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\get-library.ps1 -Destination D:\libs\rizChartFunction_Library
#
# Alternative: clone it anywhere and point the environment variable RIZCHARTFUNCTION_SRC at its `src`
# folder (the tool checks that variable first).

[CmdletBinding()]
param(
    [string]$Destination,
    [string]$Upstream = 'https://github.com/xp00000002/rizChartFunction_Library.git',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$toolRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Destination)) {
    $Destination = Join-Path (Split-Path -Parent $toolRoot) 'rizChartFunction_Library'
}
$Destination = [IO.Path]::GetFullPath($Destination)

$src = Join-Path $Destination 'src\rizChartFunction\__init__.py'
if (Test-Path -LiteralPath $src) {
    Write-Host "library already present: $Destination"
    Write-Host "src folder: $(Join-Path $Destination 'src')"
    exit 0
}

if ((Test-Path -LiteralPath $Destination) -and -not $Force) {
    throw "destination exists but does not look like the library: $Destination (use -Force to overwrite)"
}

Write-Host "cloning $Upstream"
Write-Host "        -> $Destination"
& git clone --depth 1 $Upstream $Destination 2>&1 | Out-String | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw 'git clone failed: check network access and the upstream URL.'
}

if (-not (Test-Path -LiteralPath $src)) {
    throw "clone finished but $src is missing: unexpected upstream layout."
}

Write-Host "done. src folder: $(Join-Path $Destination 'src')"
Write-Host "the tool finds it automatically (or set RIZCHARTFUNCTION_SRC to that src folder)."
