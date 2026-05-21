param(
  [string]$InstallDir = $(if ($env:SUB2CLI_INSTALL_DIR) { $env:SUB2CLI_INSTALL_DIR } else { Join-Path $env:USERPROFILE ".local\bin" }),
  [string]$Python = $(if ($env:PYTHON) { $env:PYTHON } else { "python" })
)

$ErrorActionPreference = "Stop"
$bins = @("sub2cli", "sub2cli-inject")
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

foreach ($bin in $bins) {
  Copy-Item -LiteralPath (Join-Path $scriptDir $bin) -Destination (Join-Path $InstallDir $bin) -Force
  $cmd = @(
    "@echo off",
    "`"$Python`" `"%~dp0$bin`" %*"
  )
  Set-Content -LiteralPath (Join-Path $InstallDir "$bin.cmd") -Encoding UTF8 -Value $cmd
  Write-Host "Installed: $(Join-Path $InstallDir "$bin.cmd")"
}

Write-Host ""
Write-Host "Python dependencies:"
Write-Host "  python -m pip install --user requests websocket-client"
Write-Host ""
if (($env:PATH -split ';') -notcontains $InstallDir) {
  Write-Host "$InstallDir is not in PATH."
  Write-Host "Add it for the current user:"
  Write-Host "  [Environment]::SetEnvironmentVariable('Path', `$env:Path + ';$InstallDir', 'User')"
}
Write-Host ""
Write-Host "Run: sub2cli"
