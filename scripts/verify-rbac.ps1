<#
.SYNOPSIS
    T-026-E: End-to-end RBAC smoke test for all four app roles.

.DESCRIPTION
    Reads apps/backend/tests/fixtures/rbac_expected_matrix.json and for every
    (role, endpoint) cell in the matrix, issues an HTTP request to the running
    backend and asserts the permission outcome matches expectation.

    This is a PERMISSION smoke test, not a functional test:
      - Expected 403 must equal actual 403 exactly.
      - Expected 200/201 (permission granted) must NOT be 401 or 403.
        Downstream status codes (404 for missing IDs, 422 for empty bodies,
        200/201 for successful handlers) all count as "permission was granted".

    The backend must be running at $BaseUrl before invoking this script.

.PARAMETER BaseUrl
    Backend base URL. Default: http://127.0.0.1:8000

.PARAMETER FixturePath
    Relative or absolute path to the RBAC matrix JSON.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\verify-rbac.ps1
#>

[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$FixturePath = "apps/backend/tests/fixtures/rbac_expected_matrix.json"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not [System.IO.Path]::IsPathRooted($FixturePath)) {
    $FixturePath = Join-Path $RepoRoot $FixturePath
}

if (-not (Test-Path $FixturePath)) {
    Write-Error "Fixture not found: $FixturePath"
    exit 2
}

# Pre-flight: backend reachable?
try {
    $null = Invoke-WebRequest -Uri "$BaseUrl/health" -Method GET -UseBasicParsing -TimeoutSec 3
} catch {
    Write-Error "Backend not reachable at $BaseUrl/health. Start it with scripts\start-backend.ps1 first."
    exit 3
}

$matrix = Get-Content -Raw -Path $FixturePath | ConvertFrom-Json

# Minimal request bodies for endpoints that would otherwise fail validation
# before the permission check. Keep to the schema's required fields only.
function Get-RequestBody {
    param([string]$Method, [string]$Path)
    if ($Method -ne "POST" -and $Method -ne "PATCH") { return $null }
    switch -Regex ($Path) {
        "^/api/tasks$"                { return @{ title = "rbac-smoke"; description = "rbac smoke probe"; risk_level = "low" } }
        "^/api/memory/items$"         { return @{ key = "rbac-smoke"; value = "probe" } }
        "^/api/memory/items/"         { return @{ value = "probe" } }
        "^/api/memory/settings$"      { return @{} }
        "^/api/model-config/selected$" { return @{ provider = "mock"; model = "mock" } }
        "^/api/knowledge/sync$"       { return @{} }
        "^/api/approvals/.*/grant$"   { return @{ reason = "rbac smoke" } }
        "^/api/approvals/.*/reject$"  { return @{ reason = "rbac smoke" } }
        default                        { return @{} }
    }
}

# Substitute fixture path templates with harmless placeholders.
# 404 is fine — the permission check fires before lookup.
function Resolve-Path2 {
    param([string]$Path)
    return ($Path -replace "\{id\}", "rbac-smoke-id") `
                 -replace "\{name\}", "rbac-smoke-name"
}

function Invoke-Probe {
    param(
        [string]$Method,
        [string]$Url,
        [string]$AppRole,
        [object]$Body
    )
    $headers = @{
        "X-Actor-Role"     = "admin"        # any valid ActorRole enum
        "X-Actor-App-Role" = $AppRole       # the role under test
        "X-Actor-Name"     = "rbac-smoke"
    }
    $params = @{
        Uri             = $Url
        Method          = $Method
        Headers         = $headers
        UseBasicParsing = $true
        TimeoutSec      = 10
    }
    if ($null -ne $Body -and ($Method -eq "POST" -or $Method -eq "PATCH")) {
        $params["ContentType"] = "application/json"
        $params["Body"] = ($Body | ConvertTo-Json -Depth 4 -Compress)
    } elseif ($Method -eq "POST" -or $Method -eq "PATCH") {
        $params["ContentType"] = "application/json"
        $params["Body"] = "{}"
    }
    try {
        $resp = Invoke-WebRequest @params -ErrorAction Stop
        return [int]$resp.StatusCode
    } catch [System.Net.WebException] {
        if ($_.Exception.Response) {
            return [int]$_.Exception.Response.StatusCode
        }
        return -1
    } catch {
        # PowerShell 7+ throws HttpResponseException
        if ($_.Exception.Response) {
            return [int]$_.Exception.Response.StatusCode.value__
        }
        return -1
    }
}

$total = 0
$passed = 0
$failed = @()

foreach ($endpoint in $matrix.endpoints) {
    $method   = $endpoint.method
    $pathTpl  = $endpoint.path
    $resolved = Resolve-Path2 -Path $pathTpl
    $url      = "$BaseUrl$resolved"
    $body     = Get-RequestBody -Method $method -Path $pathTpl

    foreach ($role in $matrix.roles) {
        $total++
        $expected = [int]$endpoint.expected.$role
        $actual   = Invoke-Probe -Method $method -Url $url -AppRole $role -Body $body

        $ok = $false
        if ($expected -eq 403) {
            $ok = ($actual -eq 403)
        } elseif ($expected -eq 200 -or $expected -eq 201) {
            $ok = ($actual -ne 401 -and $actual -ne 403 -and $actual -ne -1)
        } else {
            $ok = ($actual -eq $expected)
        }

        if ($ok) {
            $passed++
        } else {
            $failed += [pscustomobject]@{
                Role     = $role
                Method   = $method
                Path     = $pathTpl
                Expected = $expected
                Actual   = $actual
            }
        }
    }
}

Write-Host ""
Write-Host "RBAC smoke: $passed / $total passed" -ForegroundColor Cyan

if ($failed.Count -gt 0) {
    Write-Host ""
    Write-Host "Failures:" -ForegroundColor Red
    $failed | Format-Table -AutoSize | Out-String | Write-Host
    exit 1
}

Write-Host "All role x endpoint cells match expectations." -ForegroundColor Green
exit 0
