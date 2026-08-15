"""Lecture du .env produit par install.ps1 / install.sh.

L'application doit tourner avec l'interpréteur du .venv enregistré dans .env.
Les lanceurs (run.ps1 / run.sh) s'en chargent ; `check_interpreter()` sert de
filet quand quelqu'un appelle le module directement avec un autre Python.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

_cache: dict[str, str] | None = None
_cache_stamp: float | None = None


def load_env(refresh: bool = False) -> dict[str, str]:
    """Renvoie le contenu du .env sous forme de dictionnaire (vide s'il manque).

    Le cache est invalidé dès que le fichier change sur le disque. Sans cela,
    une application déjà lancée continuerait de voir l'ancienne configuration
    après une édition du .env — et annoncerait par exemple « aucune forge
    configurée » alors que l'adresse vient d'y être écrite.
    """
    global _cache, _cache_stamp

    try:
        stamp = ENV_FILE.stat().st_mtime
    except OSError:
        stamp = None

    if _cache is not None and not refresh and stamp == _cache_stamp:
        return _cache

    _cache_stamp = stamp
    values: dict[str, str] = {}
    if ENV_FILE.is_file():
        # utf-8-sig : tolère un éventuel BOM écrit par un éditeur Windows.
        for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()

    _cache = values
    return values


def set_env_values(values: dict[str, str], comment: str | None = None) -> Path:
    """Écrit des clés dans le .env sans toucher au reste du fichier.

    Une clé déjà présente est remplacée sur place ; une clé nouvelle est
    ajoutée en fin de fichier. Commentaires, ordre et clés inconnues sont
    préservés à l'identique — le .env contient aussi bien la configuration
    gérée par install.ps1 que des jetons, on ne réécrit donc jamais en bloc.
    """
    lines = ENV_FILE.read_text(encoding="utf-8-sig").splitlines() if ENV_FILE.is_file() else []
    remaining = dict(values)
    output: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)

    if remaining:
        if output and output[-1].strip():
            output.append("")
        if comment:
            output.append(f"# {comment}")
        for key, value in remaining.items():
            output.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(output) + "\n", encoding="utf-8")
    load_env(refresh=True)
    return ENV_FILE


def venv_python() -> Path | None:
    """Chemin de l'interpréteur du venv, s'il est déclaré et existe."""
    raw = load_env().get("VENV_PYTHON")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_file() else None


def in_project_venv() -> bool:
    """Vrai si l'interpréteur courant est bien celui du .venv du projet."""
    expected = venv_python()
    if expected is None:
        # Pas de .env : on se contente de vérifier qu'on est dans un venv local.
        return Path(sys.prefix).resolve() == (PROJECT_ROOT / ".venv").resolve()
    try:
        return os.path.samefile(sys.executable, expected)
    except OSError:
        return Path(sys.executable).resolve() == expected.resolve()


def check_interpreter(strict: bool = False) -> None:
    """Avertit (ou interrompt) si l'application ne tourne pas dans son venv."""
    if in_project_venv():
        return

    expected = venv_python()
    message = [
        "Attention : cet interpréteur n'est pas celui du projet.",
        f"  en cours  : {sys.executable}",
        f"  attendu   : {expected if expected else '(.env absent — lancez install.ps1)'}",
        "  utilisez  : .\\run.ps1 <commande>   (Windows)  |  ./run.sh <commande>",
    ]
    print("\n".join(message), file=sys.stderr)
    if strict:
        raise SystemExit(2)
