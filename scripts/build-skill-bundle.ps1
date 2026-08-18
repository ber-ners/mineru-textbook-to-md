[CmdletBinding()]
param(
    [string]$Version = "0.1.1",
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$output = if ($OutputDirectory) { (Resolve-Path -LiteralPath $OutputDirectory -ErrorAction SilentlyContinue).Path } else { Join-Path $root "dist" }
if (-not $output) { $output = Join-Path $root "dist" }
New-Item -ItemType Directory -Force -Path $output | Out-Null

$staging = Join-Path ([IO.Path]::GetTempPath()) ("mineru-textbook-to-md-skill-" + [guid]::NewGuid().ToString("N"))
$bundle = Join-Path $staging "mineru-textbook-to-md"
New-Item -ItemType Directory -Force -Path (Join-Path $bundle "agents"), (Join-Path $bundle "references"), (Join-Path $bundle "scripts"), (Join-Path $bundle "mineru_textbook_to_md") | Out-Null

try {
    Copy-Item (Join-Path $root "SKILL.md") $bundle
    Copy-Item (Join-Path $root "LICENSE") $bundle
    Copy-Item (Join-Path $root "requirements.txt") $bundle
    Copy-Item (Join-Path $root "run-local-mineru.ps1") $bundle
    Copy-Item (Join-Path $root "agents\openai.yaml") (Join-Path $bundle "agents")
    Copy-Item (Join-Path $root "references\workflow.md") (Join-Path $bundle "references")
    Copy-Item (Join-Path $root "scripts\run.py") (Join-Path $bundle "scripts")
    Get-ChildItem (Join-Path $root "mineru_textbook_to_md") -Filter "*.py" | Copy-Item -Destination (Join-Path $bundle "mineru_textbook_to_md")

    $archive = Join-Path $output ("mineru-textbook-to-md-skill-v$Version.zip")
    if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }
    Compress-Archive -Path (Join-Path $bundle "*") -DestinationPath $archive -CompressionLevel Optimal
    Write-Output $archive
}
finally {
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
}
