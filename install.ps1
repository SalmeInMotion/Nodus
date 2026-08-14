# Installer for claude-houdini.
# Writes the package descriptor into every detected Houdini user pref dir.
#
# The project layout is version-agnostic (code lives in <root>/python and is
# bootstrapped by scripts/123.py), so one descriptor works for H21, H22 and
# whatever ships next.
#
#   .\install.ps1                                  # all detected versions
#   .\install.ps1 -Versions 22.0                   # only H22
#   .\install.ps1 -Language "Spanish (es-ES)"      # always answer in Spanish
#   .\install.ps1 -UserName "Alex"                 # the assistant knows your name
#
# Language and UserName are optional. Without them the assistant simply replies
# in whatever language you write to it in.

param(
    [string[]]$Versions,
    [string]$Language,
    [string]$UserName,
    [string]$ProjectRoot = $PSScriptRoot
)

$ErrorActionPreference = "Stop"

# Houdini follows the OneDrive redirection of Documents; resolve the real one.
$docs = [Environment]::GetFolderPath('MyDocuments')
if (-not (Test-Path $docs)) {
    Write-Error "Could not resolve the Documents folder ($docs)."
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
    Write-Error "Found no houdini<ver> folder in $docs. Has Houdini been launched at least once?"
    exit 1
}

# Optional preference entries, only emitted when the flags are given.
$extraEnv = ""
if ($Language) {
    $escaped = $Language -replace '"', '\"'
    $extraEnv += ",`n        {""CLAUDE_HOUDINI_LANGUAGE"": ""$escaped""}"
}
if ($UserName) {
    $escaped = $UserName -replace '"', '\"'
    $extraEnv += ",`n        {""CLAUDE_HOUDINI_USER_NAME"": ""$escaped""}"
}

$json = @"
{
    "enable": true,
    "load_package_once": true,
    "version": "0.4.0",
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
        }$extraEnv
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
if ($Language) { Write-Output "Reply language: $Language" }
if ($UserName) { Write-Output "User name: $UserName" }
Write-Output ""
Write-Output "Next: restart Houdini and open the panel"
Write-Output "  - Shelf: 'Claude' tab -> 'Claude Chat' button"
Write-Output "  - Or the '+' menu of any pane tab -> Claude Chat"
Write-Output ""
Write-Output "To uninstall, delete the claude_houdini.json files listed above."
