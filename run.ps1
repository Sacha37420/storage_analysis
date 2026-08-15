#Requires -Version 5.1
<#
    Lanceur unique de l'application.

    Lit VENV_PYTHON dans .env et exécute l'application avec CET interpréteur,
    quel que soit le Python présent dans le PATH. Tous les arguments sont
    transmis tels quels.

        .\run.ps1 scan D:\ --top 20
        .\run.ps1 info snapshots\d-2026-08-15.npz
#>

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$EnvFile     = Join-Path $ProjectRoot '.env'

if (-not (Test-Path -LiteralPath $EnvFile)) {
    Write-Host "  .env introuvable — lancez d'abord :" -ForegroundColor Yellow
    Write-Host '      .\install.ps1' -ForegroundColor Gray
    exit 1
}

$venvPython = $null
foreach ($line in [System.IO.File]::ReadAllLines($EnvFile)) {
    $t = $line.Trim()
    if (-not $t -or $t.StartsWith('#')) { continue }
    $i = $t.IndexOf('=')
    if ($i -lt 1) { continue }
    if ($t.Substring(0, $i).Trim() -eq 'VENV_PYTHON') { $venvPython = $t.Substring($i + 1).Trim() }
}

if (-not $venvPython -or -not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "  VENV_PYTHON absent ou invalide dans .env — relancez :" -ForegroundColor Yellow
    Write-Host '      .\install.ps1 -Force' -ForegroundColor Gray
    exit 1
}

# Le paquet est trouvé quel que soit le répertoire courant de l'appelant.
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$ProjectRoot;$env:PYTHONPATH"
} else {
    $env:PYTHONPATH = $ProjectRoot
}

& $venvPython -m storage_analysis @args
exit $LASTEXITCODE
