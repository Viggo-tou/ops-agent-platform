param(
    [switch]$PrintOnly
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$repoRoot = Split-Path $PSScriptRoot -Parent
$backendDir = Join-Path $repoRoot "apps\backend"
$webDir = Join-Path $repoRoot "apps\web"

$python = Resolve-OpsAgentPython
$npm = Resolve-OpsAgentNpm

$backendArgs = @("-m", "pip", "install", "-r", "requirements.txt")
$frontendArgs = @("install")

if ($PrintOnly) {
    Write-Host "Backend:"
    Write-Host "$python $($backendArgs -join ' ')"
    Write-Host ""
    Write-Host "Frontend:"
    Write-Host "$npm $($frontendArgs -join ' ')"
    return
}

Push-Location $backendDir
try {
    & $python @backendArgs
}
finally {
    Pop-Location
}

Push-Location $webDir
try {
    & $npm @frontendArgs
}
finally {
    Pop-Location
}
