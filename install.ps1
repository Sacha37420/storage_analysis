#Requires -Version 5.1
<#
.SYNOPSIS
    Prépare l'environnement Python du projet.

.DESCRIPTION
    1. Recherche un interpréteur Python hôte, dans cet ordre de priorité :
         a. Python système (lanceur py, PATH, registre, emplacements standards)
         b. Miniconda / Anaconda / Miniforge
         c. Python embarqués d'applications (Spyder, QGIS, OSGeo4W, ArcGIS Pro, Blender)
    2. Crée le .venv local et y installe requirements.txt.
    3. Écrit les chemins retenus dans .env (gitignoré) pour tous les lancements ultérieurs.

.PARAMETER Force
    Supprime et recrée le .venv existant.

.PARAMETER Rescan
    Ignore le BASE_PYTHON déjà mémorisé dans .env et relance la détection.

.PARAMETER Python
    Impose un interpréteur précis (chemin vers python.exe), court-circuite la détection.

.PARAMETER ListOnly
    Affiche les interpréteurs détectés et s'arrête sans rien installer.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -Force -Rescan
    .\install.ps1 -Python "C:\Python313\python.exe"
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$Rescan,
    [string]$Python,
    [switch]$ListOnly
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$EnvFile     = Join-Path $ProjectRoot '.env'
$VenvDir     = Join-Path $ProjectRoot '.venv'
$Requirements = Join-Path $ProjectRoot 'requirements.txt'
$MinVersion  = [version]'3.10'

# ---------------------------------------------------------------- affichage --

function Write-Step  ([string]$m) { Write-Host ''; Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Info  ([string]$m) { Write-Host "    $m" -ForegroundColor Gray }
function Write-Ok    ([string]$m) { Write-Host "    $m" -ForegroundColor Green }
function Write-Warn2 ([string]$m) { Write-Host "    $m" -ForegroundColor Yellow }
function Write-Err2  ([string]$m) { Write-Host "    $m" -ForegroundColor Red }

# ------------------------------------------------------------- utilitaires --

function Expand-Glob {
    # Get-ChildItem tolérant : renvoie un tableau vide si le motif ne matche rien.
    param([string]$Pattern)
    try {
        @(Get-ChildItem -Path $Pattern -File -ErrorAction Stop | ForEach-Object { $_.FullName })
    } catch {
        @()
    }
}

function Get-PythonInfo {
    <#
        Interroge un candidat et renvoie ses caractéristiques, ou $null s'il est
        inutilisable (absent, trop ancien, incapable de créer un venv).
    #>
    param([string]$Exe, [string]$Source)

    if ([string]::IsNullOrWhiteSpace($Exe)) { return $null }
    if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) { return $null }

    # Sonde volontairement sans guillemets ni JSON : PowerShell supprime les
    # guillemets d'un argument transmis à un exécutable natif, ce qui casserait
    # le code Python. Une valeur par ligne, donc.
    $probe = 'import sys;print(sys.version_info[0]);print(sys.version_info[1]);print(sys.version_info[2]);print(sys.executable);print(sys.maxsize.bit_length()+1)'
    try {
        $raw = @(& $Exe -c $probe)
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0 -or $raw.Count -lt 5) { return $null }

    try {
        $version = [version]("{0}.{1}.{2}" -f $raw[0].Trim(), $raw[1].Trim(), $raw[2].Trim())
        $realPath = $raw[3].Trim()
        $bits = [int]$raw[4].Trim()
    } catch {
        return $null
    }
    if (-not $realPath) { return $null }

    $hasVenv = $false
    try {
        & $Exe -c 'import venv, ensurepip' | Out-Null
        if ($LASTEXITCODE -eq 0) { $hasVenv = $true }
    } catch {
        $hasVenv = $false
    }

    [pscustomobject]@{
        Path    = $realPath
        Version = $version
        Bits    = $bits
        Source  = $Source
        HasVenv = $hasVenv
        Usable  = ($version -ge $MinVersion -and $hasVenv)
    }
}

# ------------------------------------------------------ détection : système --

function Find-SystemCandidates {
    $found = New-Object System.Collections.Generic.List[string]

    # Lanceur officiel « py » : source la plus fiable sur Windows.
    try {
        $lines = & py -0p
        if ($LASTEXITCODE -eq 0) {
            foreach ($line in $lines) {
                $m = [regex]::Match([string]$line, '([A-Za-z]:\\[^\r\n]*?python\.exe)')
                if ($m.Success) { $found.Add($m.Groups[1].Value) }
            }
        }
    } catch { }

    # PATH — en excluant l'alias Microsoft Store (stub de 0 octet).
    try {
        foreach ($cmd in @(Get-Command python, python3 -All -ErrorAction Stop)) {
            if ($cmd.Source -and $cmd.Source -notmatch 'WindowsApps') { $found.Add($cmd.Source) }
        }
    } catch { }

    # Registre : installations officielles python.org.
    foreach ($hive in @('HKLM:\SOFTWARE\Python\PythonCore', 'HKCU:\SOFTWARE\Python\PythonCore')) {
        try {
            foreach ($key in Get-ChildItem $hive -ErrorAction Stop) {
                try {
                    $ip = (Get-ItemProperty (Join-Path $key.PSPath 'InstallPath') -ErrorAction Stop)
                    if ($ip.ExecutablePath) { $found.Add($ip.ExecutablePath) }
                    elseif ($ip.'(default)') { $found.Add((Join-Path $ip.'(default)' 'python.exe')) }
                } catch { }
            }
        } catch { }
    }

    # Emplacements d'installation standards.
    foreach ($pattern in @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe'),
        'C:\Python3*\python.exe',
        (Join-Path $env:ProgramFiles 'Python3*\python.exe')
    )) {
        foreach ($p in (Expand-Glob $pattern)) { $found.Add($p) }
    }

    $found
}

# ------------------------------------------------------- détection : conda --

function Find-CondaCandidates {
    $found = New-Object System.Collections.Generic.List[string]

    try {
        $base = & conda info --base
        if ($LASTEXITCODE -eq 0 -and $base) {
            $found.Add((Join-Path ([string]($base | Select-Object -First 1)).Trim() 'python.exe'))
        }
    } catch { }

    foreach ($dir in @(
        (Join-Path $env:USERPROFILE 'miniconda3'),
        (Join-Path $env:USERPROFILE 'anaconda3'),
        (Join-Path $env:USERPROFILE 'miniforge3'),
        (Join-Path $env:USERPROFILE 'mambaforge'),
        (Join-Path $env:LOCALAPPDATA 'miniconda3'),
        (Join-Path $env:LOCALAPPDATA 'anaconda3'),
        (Join-Path $env:LOCALAPPDATA 'Continuum\anaconda3'),
        'C:\ProgramData\miniconda3',
        'C:\ProgramData\Anaconda3',
        'C:\miniconda3',
        'C:\miniforge3'
    )) {
        $exe = Join-Path $dir 'python.exe'
        if (Test-Path -LiteralPath $exe -PathType Leaf) { $found.Add($exe) }
    }

    $found
}

# ---------------------------------------------------- détection : embarqués --

function Find-EmbeddedCandidates {
    $found = New-Object System.Collections.Generic.List[string]

    $patterns = @(
        # Spyder (installeur autonome)
        (Join-Path $env:LOCALAPPDATA 'spyder-*\Python\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Spyder\Python\python.exe'),
        (Join-Path $env:ProgramFiles 'Spyder\Python\python.exe'),
        # QGIS / OSGeo4W
        (Join-Path $env:ProgramFiles 'QGIS *\apps\Python3*\python.exe'),
        'C:\OSGeo4W\apps\Python3*\python.exe',
        'C:\OSGeo4W64\apps\Python3*\python.exe',
        # ArcGIS Pro
        (Join-Path $env:ProgramFiles 'ArcGIS\Pro\bin\Python\envs\*\python.exe'),
        # Blender
        (Join-Path $env:ProgramFiles 'Blender Foundation\Blender*\*\python\bin\python.exe'),
        # FME / autres SIG
        (Join-Path $env:ProgramFiles 'FME\python\python3*\python.exe')
    )

    foreach ($pattern in $patterns) {
        foreach ($p in (Expand-Glob $pattern)) { $found.Add($p) }
    }

    $found
}

# ------------------------------------------------------------- sélection --

function Select-BasePython {
    $categories = @(
        @{ Name = 'system';   Finder = { Find-SystemCandidates } },
        @{ Name = 'conda';    Finder = { Find-CondaCandidates } },
        @{ Name = 'embedded'; Finder = { Find-EmbeddedCandidates } }
    )

    $seen = New-Object System.Collections.Generic.HashSet[string]
    $all  = New-Object System.Collections.Generic.List[object]
    $best = $null

    foreach ($cat in $categories) {
        Write-Info ("recherche « {0} »..." -f $cat.Name)
        $candidates = & $cat.Finder
        $valid = New-Object System.Collections.Generic.List[object]

        foreach ($exe in $candidates) {
            $key = $exe.ToLowerInvariant()
            if (-not $seen.Add($key)) { continue }

            $info = Get-PythonInfo -Exe $exe -Source $cat.Name
            if (-not $info) { continue }

            # sys.executable peut différer du chemin sondé (lien, lanceur py) :
            # on ne dédoublonne à nouveau que dans ce cas.
            $resolved = $info.Path.ToLowerInvariant()
            if ($resolved -ne $key -and -not $seen.Add($resolved)) { continue }

            $all.Add($info)
            $flag = if ($info.Usable) { 'ok' } elseif (-not $info.HasVenv) { 'sans venv' } else { 'trop ancien' }
            Write-Info ("  [{0,-11}] {1,-8} {2}" -f $flag, $info.Version, $info.Path)
            if ($info.Usable) { $valid.Add($info) }
        }

        if ($valid.Count -gt 0 -and -not $best) {
            # Dans une catégorie donnée, on prend la version la plus récente.
            $best = $valid | Sort-Object -Property Version -Descending | Select-Object -First 1
            if (-not $ListOnly) { break }
        }
    }

    [pscustomobject]@{ Best = $best; All = $all }
}

# ------------------------------------------------------------------ .env --

function Read-EnvFile {
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith('#')) { continue }
        $i = $t.IndexOf('=')
        if ($i -lt 1) { continue }
        $map[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim()
    }
    $map
}

function Write-EnvFile {
    param([string]$Path, [hashtable]$Values)

    $managed = @('PROJECT_ROOT', 'BASE_PYTHON', 'BASE_PYTHON_VERSION', 'BASE_PYTHON_SOURCE', 'VENV_DIR', 'VENV_PYTHON')
    $existing = Read-EnvFile -Path $Path

    $sb = New-Object System.Text.StringBuilder
    # Contenu volontairement sans accent : certains outils relisent .env en ANSI.
    [void]$sb.AppendLine('# Section geree par install.ps1 - ne pas editer a la main.')
    [void]$sb.AppendLine(('# ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')))
    [void]$sb.AppendLine('')
    foreach ($k in $managed) {
        if ($Values.ContainsKey($k)) { [void]$sb.AppendLine("$k=$($Values[$k])") }
    }

    # Les cles ajoutees a la main - jetons GITHUB_TOKEN / GITLAB_TOKEN, racines
    # REPO_ROOTS - doivent survivre a une reinstallation. Sans cette reprise,
    # un simple .\install.ps1 effacerait les jetons de l'utilisateur.
    $extra = @($existing.Keys | Where-Object { $managed -notcontains $_ } | Sort-Object)
    if ($extra.Count -gt 0) {
        [void]$sb.AppendLine('')
        [void]$sb.AppendLine('# Cles ajoutees a la main, preservees par install.ps1.')
        foreach ($k in $extra) { [void]$sb.AppendLine("$k=$($existing[$k])") }
    }

    # UTF-8 sans BOM : lisible tel quel par Python, PowerShell et les outils tiers.
    [System.IO.File]::WriteAllText($Path, $sb.ToString(), (New-Object System.Text.UTF8Encoding($false)))
}

# =============================================================== programme ==

Write-Host ''
Write-Host '  Analyseur d''espace disque — installation' -ForegroundColor White
Write-Host "  $ProjectRoot" -ForegroundColor DarkGray

$existing = Read-EnvFile -Path $EnvFile

# --- 1. interpréteur hôte ----------------------------------------------------

Write-Step '1/3  Recherche d''un interpréteur Python'

$base = $null

if ($Python) {
    $base = Get-PythonInfo -Exe $Python -Source 'imposé'
    if (-not $base) { Write-Err2 "Interpréteur inutilisable : $Python"; exit 1 }
    if (-not $base.Usable) {
        Write-Err2 ("{0} : version {1} (minimum {2}) ou module venv absent." -f $base.Path, $base.Version, $MinVersion)
        exit 1
    }
    Write-Ok ("imposé : {0} ({1})" -f $base.Path, $base.Version)
}
elseif (-not $Rescan -and -not $ListOnly -and $existing['BASE_PYTHON']) {
    # -ListOnly doit toujours balayer : réutiliser le .env ne listerait rien.
    $base = Get-PythonInfo -Exe $existing['BASE_PYTHON'] -Source $existing['BASE_PYTHON_SOURCE']
    if ($base -and $base.Usable) {
        Write-Ok ("réutilisé depuis .env : {0} ({1})" -f $base.Path, $base.Version)
    } else {
        Write-Warn2 'BASE_PYTHON du .env invalide, nouvelle détection.'
        $base = $null
    }
}

if (-not $base) {
    $scan = Select-BasePython
    $base = $scan.Best

    if ($ListOnly) {
        Write-Step 'Interpréteurs détectés'
        $scan.All | Sort-Object Source, Version | Format-Table Source, Version, Bits, HasVenv, Path -AutoSize
        exit 0
    }
    if (-not $base) {
        Write-Err2 "Aucun Python >= $MinVersion utilisable n'a été trouvé."
        Write-Info 'Installez Python depuis https://www.python.org/downloads/ puis relancez,'
        Write-Info 'ou désignez-en un explicitement : .\install.ps1 -Python "C:\chemin\python.exe"'
        exit 1
    }
    Write-Ok ("retenu [{0}] : {1} ({2}, {3} bits)" -f $base.Source, $base.Path, $base.Version, $base.Bits)
}

if ($ListOnly) { exit 0 }

# --- 2. venv -----------------------------------------------------------------

Write-Step '2/3  Environnement virtuel'

$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'

if ($Force -and (Test-Path -LiteralPath $VenvDir)) {
    Write-Info 'suppression du .venv existant (-Force)'
    Remove-Item -LiteralPath $VenvDir -Recurse -Force -Confirm:$false
}

if (Test-Path -LiteralPath $VenvPython) {
    Write-Ok "déjà présent : $VenvDir"
} else {
    Write-Info "création : $VenvDir"
    & $base.Path -m venv $VenvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        Write-Err2 'Échec de la création du venv.'
        exit 1
    }
    Write-Ok 'venv créé'
}

Write-Info 'mise à jour de pip'
& $VenvPython -m pip install --upgrade pip --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Write-Warn2 'mise à jour de pip échouée, on continue.' }

if (Test-Path -LiteralPath $Requirements) {
    Write-Info "installation de $(Split-Path -Leaf $Requirements)"
    & $VenvPython -m pip install -r $Requirements --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { Write-Err2 'Échec de l''installation des dépendances.'; exit 1 }
    Write-Ok 'dépendances installées'
} else {
    Write-Warn2 "requirements.txt introuvable, étape ignorée."
}

# --- 3. .env -----------------------------------------------------------------

Write-Step '3/3  Enregistrement dans .env'

Write-EnvFile -Path $EnvFile -Values @{
    PROJECT_ROOT        = $ProjectRoot
    BASE_PYTHON         = $base.Path
    BASE_PYTHON_VERSION = $base.Version.ToString()
    BASE_PYTHON_SOURCE  = $base.Source
    VENV_DIR            = $VenvDir
    VENV_PYTHON         = $VenvPython
}
Write-Ok $EnvFile

# --- vérification ------------------------------------------------------------

$check = 'import sys,numpy,plotly,dash;print(sys.version.split()[0],numpy.__version__,plotly.__version__,dash.__version__)'
try {
    $out = @(& $VenvPython -c $check)
    if ($LASTEXITCODE -eq 0) { Write-Ok ("venv ok : python {0} · numpy {1} · plotly {2} · dash {3}" -f $out[0].Split()) }
    else { Write-Warn2 'Vérification des imports échouée.' }
} catch {
    Write-Warn2 'Vérification des imports échouée.'
}

Write-Host ''
Write-Host '  Prêt.' -ForegroundColor Green
Write-Host '    .\run.ps1 scan D:\           analyse un disque' -ForegroundColor DarkGray
Write-Host '    .\run.ps1 scan . --top 20    analyse le dossier courant' -ForegroundColor DarkGray
Write-Host ''
