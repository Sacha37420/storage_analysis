"""Persistance d'un scan.

Format : archive .npz (tableaux NumPy bruts + métadonnées JSON). Choisi plutôt
que SQLite ou Parquet parce que le modèle est déjà une pile de tableaux alignés :
l'écriture et la relecture sont directes, sans dépendance et sans conversion.
Comptez 20-40 Mo compressés pour un million d'entrées.

Sert à trois choses : rouvrir un scan sans repasser sur le disque, comparer deux
dates (lot 4), et rejouer un cas réel en développement.
"""

from __future__ import annotations

import json
import os
from typing import Any

import numpy as np

from .tree import FileTree

SNAPSHOT_FORMAT = 1


def save(tree: FileTree, path: str | os.PathLike[str], compress: bool = True) -> str:
    """Écrit l'arbre sur disque. Renvoie le chemin effectif."""
    target = os.fspath(path)
    if not target.endswith(".npz"):
        target += ".npz"

    parent = os.path.dirname(os.path.abspath(target))
    if parent:
        os.makedirs(parent, exist_ok=True)

    meta = dict(tree.meta)
    meta["snapshot_format"] = SNAPSHOT_FORMAT

    payload = {
        "parent": tree.parent,
        "size": tree.size,
        "mtime": tree.mtime,
        "depth": tree.depth,
        "flags": tree.flags,
        "name_off": tree.name_off,
        "name_blob": np.frombuffer(tree.name_blob, dtype=np.uint8),
        "meta": np.array(json.dumps(meta, ensure_ascii=False)),
    }

    writer = np.savez_compressed if compress else np.savez
    with open(target, "wb") as handle:
        writer(handle, **payload)
    return target


def load(path: str | os.PathLike[str]) -> FileTree:
    """Relit un snapshot écrit par `save`."""
    with np.load(os.fspath(path), allow_pickle=False) as data:
        meta: dict[str, Any] = json.loads(str(data["meta"].item()))
        fmt = meta.get("snapshot_format")
        if fmt != SNAPSHOT_FORMAT:
            raise ValueError(f"format de snapshot non supporté : {fmt!r}")
        return FileTree(
            parent=data["parent"],
            size=data["size"],
            mtime=data["mtime"],
            depth=data["depth"],
            flags=data["flags"],
            name_off=data["name_off"],
            name_blob=data["name_blob"].tobytes(),
            meta=meta,
        )
