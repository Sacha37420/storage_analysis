@echo off
rem ===========================================================================
rem  Lanceur cmd.exe de l'application.
rem
rem  Contrairement a install.bat, celui-ci ne passe PAS par PowerShell : lire
rem  une ligne du .env est trivial en batch, et on evite ainsi le demarrage de
rem  powershell.exe a chaque commande.
rem
rem  Fichier volontairement sans accent (page de codes OEM de la console).
rem
rem    run.bat scan D:\ --top 20
rem    run.bat info snapshots\c-users-20260815-112359.npz
rem ===========================================================================
setlocal EnableExtensions

rem Voir install.bat : guillemet double apres /c = lance par l'Explorateur.
set "DOUBLECLICK="
setlocal EnableDelayedExpansion
set "CL=!cmdcmdline!"
set "CL=!CL:"=@!"
if not "!CL!"=="!CL:/c @@=!" (endlocal & set "DOUBLECLICK=1") else (endlocal)

set "PROJECT_ROOT=%~dp0"
if "%PROJECT_ROOT:~-1%"=="\" set "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"
set "ENV_FILE=%PROJECT_ROOT%\.env"
set "RC=1"

if not exist "%ENV_FILE%" (
    echo   .env introuvable - lancez d'abord :
    echo       install.bat
    goto :end
)

rem eol=# ignore les commentaires, delims== coupe sur le premier signe egal.
set "VENV_PYTHON="
for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ENV_FILE%") do (
    if /i "%%~A"=="VENV_PYTHON" set "VENV_PYTHON=%%~B"
)

if not defined VENV_PYTHON goto :bad_env
if not exist "%VENV_PYTHON%" goto :bad_env

rem Le paquet est trouve quel que soit le repertoire courant de l'appelant.
if defined PYTHONPATH (
    set "PYTHONPATH=%PROJECT_ROOT%;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%PROJECT_ROOT%"
)

"%VENV_PYTHON%" -m storage_analysis %*
set "RC=%ERRORLEVEL%"
goto :end

:bad_env
echo   VENV_PYTHON absent ou invalide dans .env - relancez :
echo       install.bat -Force

:end
if defined DOUBLECLICK (
    echo.
    pause
)
exit /b %RC%
