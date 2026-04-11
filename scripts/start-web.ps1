param(
    [switch]$PrintOnly,
    [switch]$Dev,
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 5173
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$repoRoot = Split-Path $PSScriptRoot -Parent
$webDir = Join-Path $repoRoot "apps\web"
$distDir = Join-Path $webDir "dist"
$serveScript = Join-Path $PSScriptRoot "serve-web.py"

Assert-PathExists -Path $webDir -Message "Frontend directory not found."
Assert-PathExists -Path $serveScript -Message "Frontend static server script not found."

$python = Resolve-OpsAgentPython
$npm = Resolve-OpsAgentNpm
$args = @($serveScript, "--host", $BindHost, "--port", "$Port", "--dir", $distDir)

if ($Dev) {
    $args = @("run", "dev", "--", "--host", $BindHost, "--port", "$Port")
}

if ($PrintOnly) {
    Write-Host "Working directory: $webDir"
    if (-not $Dev -and -not (Test-Path -LiteralPath $distDir)) {
        Write-Host "$npm run build"
    }
    if ($Dev) {
        Write-Host "$npm $($args -join ' ')"
    }
    else {
        Write-Host "$python $($args -join ' ')"
    }
    return
}

Push-Location $webDir
try {
    if (-not $Dev -and -not (Test-Path -LiteralPath $distDir)) {
        & $npm "run" "build"
    }

    if ($Dev) {
        & $npm @args
    }
    else {
        & $python @args
    }
}
finally {
    Pop-Location
}
