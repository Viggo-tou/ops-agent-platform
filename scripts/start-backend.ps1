param(
    [switch]$PrintOnly,
    [switch]$Reload,
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$repoRoot = Split-Path $PSScriptRoot -Parent
$backendDir = Join-Path $repoRoot "apps\backend"

Assert-PathExists -Path $backendDir -Message "Backend directory not found."

$python = Resolve-OpsAgentPython
$args = @("-m", "uvicorn", "app.main:app", "--host", $BindHost, "--port", "$Port")

if ($Reload) {
    $args = @("-m", "uvicorn", "app.main:app", "--reload", "--host", $BindHost, "--port", "$Port")
}

if ($PrintOnly) {
    Write-Host "Working directory: $backendDir"
    Write-Host "$python $($args -join ' ')"
    return
}

Push-Location $backendDir
try {
    & $python @args
}
finally {
    Pop-Location
}
