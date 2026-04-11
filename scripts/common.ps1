function Resolve-OpsAgentPython {
    if ($env:OPS_AGENT_PYTHON -and (Test-Path -LiteralPath $env:OPS_AGENT_PYTHON)) {
        return $env:OPS_AGENT_PYTHON
    }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Python\bin\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python314\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe")
    )

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }

    $pythonCommands = Get-Command python.exe -ErrorAction SilentlyContinue -All | Where-Object {
        $_.Source -and $_.Source -notmatch "WindowsApps"
    }

    if ($pythonCommands) {
        return $pythonCommands[0].Source
    }

    throw "Python executable not found. Set OPS_AGENT_PYTHON or install Python locally."
}

function Resolve-OpsAgentNpm {
    $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($null -ne $npmCommand) {
        return $npmCommand.Source
    }

    throw "npm.cmd not found in PATH. Install Node.js or add npm.cmd to PATH."
}

function Assert-PathExists {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw $Message
    }
}
