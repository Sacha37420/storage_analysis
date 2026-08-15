@echo off
rem ===========================================================================
rem  Point d'entree cmd.exe / double-clic pour install.ps1.
rem
rem  Volontairement un simple relais : toute la logique de detection reste dans
rem  install.ps1, un seul endroit a maintenir. -ExecutionPolicy Bypass evite
rem  le blocage des .ps1 sur un poste ou la strategie d'execution est
rem  restreinte, ce qui est le cas par defaut sur Windows.
rem
rem  Fichier volontairement sans accent : cmd.exe le lit dans la page de codes
rem  OEM de la console (850/437), pas en UTF-8.
rem
rem    install.bat                      installation standard
rem    install.bat -ListOnly            liste les interpreteurs detectes
rem    install.bat -Force -Rescan       recree le venv, redetecte le Python
rem ===========================================================================
setlocal EnableExtensions

rem --- Lance par double-clic ? ------------------------------------------------
rem  L'Explorateur invoque   cmd /c ""C:\...\install.bat" "   soit un guillemet
rem  DOUBLE apres /c, la ou  cmd /c "script args"  n'en a qu'un et ou un appel
rem  depuis une console ouverte ne mentionne meme pas le script. On normalise
rem  les guillemets en @ pour pouvoir tester la sous-chaine sans se battre avec
rem  l'echappement. Sans ce test, un appel automatise resterait bloque sur le
rem  pause final.
set "DOUBLECLICK="
setlocal EnableDelayedExpansion
set "CL=!cmdcmdline!"
set "CL=!CL:"=@!"
if not "!CL!"=="!CL:/c @@=!" (endlocal & set "DOUBLECLICK=1") else (endlocal)

set "SCRIPT_DIR=%~dp0"
set "PS_SCRIPT=%SCRIPT_DIR%install.ps1"
set "RC=0"

if not exist "%PS_SCRIPT%" (
    echo   install.ps1 introuvable a cote de install.bat.
    set "RC=1"
    goto :end
)

where powershell.exe >nul 2>nul
if errorlevel 1 (
    echo   powershell.exe est introuvable : impossible de lancer l'installation.
    set "RC=1"
    goto :end
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" %*
set "RC=%ERRORLEVEL%"

:end
if defined DOUBLECLICK (
    echo.
    pause
)
exit /b %RC%
