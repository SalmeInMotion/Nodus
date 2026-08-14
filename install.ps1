# Installer for Claude Houdini.
# Writes the package descriptor into every detected Houdini user pref dir.
#
# The project layout is version-agnostic (code lives in <root>/python and is
# bootstrapped by scripts/123.py), so one descriptor works for H21, H22 and
# whatever ships next.
#
#   .\install.ps1                 # all detected versions
#   .\install.ps1 -Versions 22.0  # only H22

param(
    [string[]]$Versions,
    [string]$ProjectRoot = $PSScriptRoot
)

$ErrorActionPreference = "Stop"

# Houdini follows the OneDrive redirection of Documents; resolve the real one.
$docs = [Environment]::GetFolderPath('MyDocuments')
if (-not (Test-Path $docs)) {
    Write-Error "No pude resolver la carpeta Documents ($docs)."
    exit 1
}

$root = (Resolve-Path $ProjectRoot).Path -replace '\\', '/'

if ($Versions) {
    $prefDirs = $Versions | ForEach-Object { Join-Path $docs "houdini$_" } | Where-Object { Test-Path $_ }
} else {
    # Only real pref dirs: houdini<major>.<minor>. Skips things like "houdini22.0 - Copy".
    $prefDirs = Get-ChildItem -Path $docs -Directory -Filter "houdini*" |
                Where-Object { $_.Name -match '^houdini\d+\.\d+$' } |
                Select-Object -ExpandProperty FullName
}

if (-not $prefDirs) {
    Write-Error "No encontre ninguna carpeta houdini<ver> en $docs."
    exit 1
}

$json = @"
{
    "enable": true,
    "load_package_once": true,
    "version": "0.3.0",
    "env":
    [
        {"CLAUDE_HOUDINI_ROOT": "$root"},
        {"HOUDINI_PATH":
            [
                {
                    "value": "$root",
                    "method": "prepend"
                }
            ]
        },
        {"PYTHONPATH":
            [
                {
                    "value": "$root/python",
                    "method": "prepend"
                }
            ]
        }
    ]
}
"@

foreach ($prefDir in $prefDirs) {
    $packagesDir = Join-Path $prefDir "packages"
    if (-not (Test-Path $packagesDir)) {
        New-Item -ItemType Directory -Path $packagesDir | Out-Null
    }
    $pkgFile = Join-Path $packagesDir "claude_houdini.json"
    # UTF-8 *without* BOM: Set-Content -Encoding utf8 emits a BOM on Windows
    # PowerShell 5.1 and a leading BOM can trip JSON parsers.
    [System.IO.File]::WriteAllText($pkgFile, $json, (New-Object System.Text.UTF8Encoding($false)))
    Write-Output "OK  $pkgFile"
}

Write-Output ""
Write-Output "Project root: $root"
Write-Output ""
Write-Output "Siguiente paso: reinicia Houdini y abre el panel"
Write-Output "  - Shelf: pestana 'Claude' -> boton 'Claude Chat'"
Write-Output "  - O menu del '+' de cualquier pane tab -> Claude Chat"
Write-Output ""
Write-Output "Para desinstalar, borra los claude_houdini.json listados arriba."
