$ErrorActionPreference = 'Stop'

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Command failed with exit code $LASTEXITCODE"
    }
}

Push-Location $PSScriptRoot
try {
    Invoke-Checked python -m venv .venv

    . (Join-Path $PSScriptRoot '.venv\Scripts\Activate.ps1')
    Invoke-Checked python -m pip install --upgrade pip pre-commit
    Invoke-Checked pre-commit install
    Invoke-Checked pre-commit install-hooks

    Write-Host 'Development setup complete. Activate the venv in a new shell with: . .\.venv\Scripts\Activate.ps1'
} finally {
    Pop-Location
}
