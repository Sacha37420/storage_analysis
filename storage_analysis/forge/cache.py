"""Cache du catalogue.

Mesurer la taille de plusieurs Go de clones sur un disque rotatif froid prend
des dizaines de secondes ; interroger deux forges ajoute des appels réseau.
Refaire tout cela à chaque commande serait inutilisable, d'où ce cache JSON,
volontairement lisible pour rester inspectable à la main.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from ..env import PROJECT_ROOT
from .catalog import Catalog, build_catalog
from .models import LocalRepo, RemoteRepo, RepoRef, SubmoduleDecl

CACHE_PATH = PROJECT_ROOT / "snapshots" / "repos-cache.json"
FORMAT = 1


def _ref_from(data: dict | None) -> RepoRef | None:
    return RepoRef(**data) if data else None


def _subs_from(items: list[dict]) -> list[SubmoduleDecl]:
    return [
        SubmoduleDecl(
            name=item["name"], rel_path=item["rel_path"], url=item["url"],
            ref=_ref_from(item.get("ref")),
        )
        for item in items
    ]


def save(catalog: Catalog, roots: list[str], path: Path = CACHE_PATH) -> Path:
    payload = {
        "format": FORMAT,
        "built_at": time.time(),
        "roots": roots,
        "queried_hosts": sorted(catalog.queried_hosts),
        "locals": [asdict(e.local) for e in catalog.entries.values() if e.local],
        "remotes": [asdict(e.remote) for e in catalog.entries.values() if e.remote],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load(max_age: float | None = None, path: Path = CACHE_PATH) -> tuple[Catalog, float] | None:
    """Renvoie (catalogue, âge en secondes), ou None si absent, périmé ou illisible."""
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("format") != FORMAT:
        return None

    age = time.time() - float(payload.get("built_at", 0))
    if max_age is not None and age > max_age:
        return None

    locals_: list[LocalRepo] = []
    for item in payload.get("locals", []):
        item = dict(item)
        item["ref"] = _ref_from(item.get("ref"))
        item["submodules"] = _subs_from(item.get("submodules", []))
        locals_.append(LocalRepo(**item))

    remotes: list[RemoteRepo] = []
    for item in payload.get("remotes", []):
        item = dict(item)
        item["ref"] = _ref_from(item.get("ref"))
        item["submodules"] = _subs_from(item.get("submodules", []))
        remotes.append(RemoteRepo(**item))

    catalog = build_catalog(locals_, remotes, set(payload.get("queried_hosts", [])))
    return catalog, age
