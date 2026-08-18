[CmdletBinding()]
param(
    [switch]$Check,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$MineruArgs
)

$ErrorActionPreference = 'Stop'
$skillRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$packageRoot = Join-Path $skillRoot 'mineru_textbook_to_md'

if (-not (Test-Path -LiteralPath $packageRoot -PathType Container)) {
    throw "Local MinerU package is missing: $packageRoot"
}

if ($Check) {
    [pscustomobject]@{
        LocalSkillRoot = $skillRoot
        PackageRoot = $packageRoot
        IsLocal = $true
    } | Format-List
    exit 0
}

$previousPythonPath = $env:PYTHONPATH
$previousPythonUtf8 = $env:PYTHONUTF8
try {
    $separator = [IO.Path]::PathSeparator
    $env:PYTHONPATH = if ($previousPythonPath) { "$skillRoot$separator$previousPythonPath" } else { $skillRoot }
    $env:PYTHONUTF8 = "1"
    & python -m mineru_textbook_to_md @MineruArgs
    exit $LASTEXITCODE
}
finally {
    $env:PYTHONPATH = $previousPythonPath
    $env:PYTHONUTF8 = $previousPythonUtf8
}
