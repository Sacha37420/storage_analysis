"""Normalisation des URL de remote.

Un même dépôt s'écrit de plusieurs façons selon le protocole :

    git@gitlab.com:mon-groupe/ai_wrapper.git
    ssh://git@gitlab.com:2222/mon-groupe/ai_wrapper.git
    https://gitlab.com/mon-groupe/ai_wrapper.git

Toutes désignent le même projet. Sans cette normalisation, impossible de
rapprocher un clone local de son projet distant, ni de résoudre l'URL déclarée
par un submodule vers une entrée du catalogue.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .models import RepoRef

# git@hote:chemin/projet.git  — la forme SCP, qui n'est pas une URL valide et
# que urlsplit interprète donc de travers.
_SCP_LIKE = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/]+):(?P<path>[^:].*)$")


def _clean_path(path: str) -> str:
    path = path.strip().strip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    return path.strip("/")


def normalize_remote(url: str | None) -> RepoRef | None:
    """Convertit une URL de remote en (hôte, chemin), ou None si non reconnue.

    Renvoie None pour un chemin local (`/srv/depots/x`, `D:\\miroir`) ou une URL
    de protocole non géré : ce sont des dépôts réels, mais sans forge derrière.
    """
    if not url:
        return None

    raw = url.strip()
    if not raw:
        return None

    # Chemins locaux et file:// : pas de forge.
    if raw.startswith("file://") or re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith(("/", ".", "\\\\")):
        return None

    if "://" in raw:
        parts = urlsplit(raw)
        if parts.scheme not in ("ssh", "git", "http", "https"):
            return None
        host = (parts.hostname or "").lower()
        path = _clean_path(parts.path)
    else:
        match = _SCP_LIKE.match(raw)
        if not match:
            return None
        host = match.group("host").lower()
        path = _clean_path(match.group("path"))

    if not host or not path or "/" not in path:
        return None

    return RepoRef(host=host, path=path)


def forge_kind(host: str) -> str:
    """Devine la forge à partir de l'hôte. « gitlab » couvre l'auto-hébergé."""
    host = host.lower()
    if host == "github.com" or host.endswith(".github.com"):
        return "github"
    if "gitlab" in host:
        return "gitlab"
    return "inconnu"
