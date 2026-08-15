r"""Parcours du système de fichiers.

Backend par défaut : os.scandir(). L'entrée de répertoire renvoyée par
FindFirstFile/FindNextFile (Windows) ou readdir (POSIX) porte déjà le type et,
sous Windows, la taille et les dates — un stat() par fichier est donc évité,
ce qui vaut un facteur 7 à 50 par rapport à un os.walk naïf (PEP 471).

Choix structurants :
  * parcours itératif (pile explicite) : pas de RecursionError sur chemins profonds ;
  * liens symboliques, jonctions et points de reparse jamais suivis : sinon
    boucles infinies et double comptage ;
  * préfixe \\?\ sous Windows pour franchir la limite MAX_PATH de 260 caractères ;
  * une erreur d'accès est capturée par nœud, comptée, et n'interrompt rien ;
  * les threads ne font que lister ; l'insertion dans les tableaux reste sur le
    thread principal, donc aucun verrou et un résultat déterministe.
"""

from __future__ import annotations

import os
import time
from array import array
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from threading import Event
from typing import Any, Callable

import numpy as np

from .sysinfo import cluster_size, default_workers, has_seek_penalty
from .tree import FLAG_DIR, FLAG_ERROR, FLAG_LINK, FileTree

_WINDOWS = os.name == "nt"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_MAX_RECORDED_ERRORS = 200

ProgressCallback = Callable[[int, int, int], None]  # (fichiers, dossiers, octets)


# --------------------------------------------------------------- utilitaires --

def _long_path(path: str) -> str:
    r"""Préfixe \\?\ sous Windows pour dépasser MAX_PATH (260 caractères)."""
    if not _WINDOWS or path.startswith("\\\\?\\"):
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\"):  # partage réseau \\serveur\partage
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _strip_long_path(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _list_dir(path: str) -> tuple[list[tuple], OSError | None]:
    """Liste un répertoire. Renvoie (entrées, erreur d'ouverture éventuelle).

    Chaque entrée : (nom, est_dossier, est_lien, taille, mtime, chemin, en_erreur).
    Exécuté dans les threads de travail : ne touche à aucun état partagé.
    """
    entries: list[tuple] = []
    try:
        with os.scandir(path) as iterator:
            for entry in iterator:
                try:
                    stat = entry.stat(follow_symlinks=False)
                    is_link = entry.is_symlink()
                    if not is_link and _WINDOWS:
                        attributes = getattr(stat, "st_file_attributes", 0)
                        is_link = bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
                    is_dir = entry.is_dir(follow_symlinks=False)
                    entries.append(
                        (entry.name, is_dir, is_link, stat.st_size, stat.st_mtime, entry.path, False)
                    )
                except OSError:
                    # Entrée illisible (droits, fichier volatil) : on la garde
                    # dans l'arbre, marquée, plutôt que de la faire disparaître.
                    entries.append((entry.name, False, False, 0, 0.0, "", True))
    except OSError as error:
        return entries, error
    return entries, None


class _InlineExecutor:
    """Exécuteur synchrone, pour le mode mono-thread (disques rotatifs)."""

    def submit(self, fn: Callable, *args: Any) -> Future:
        future: Future = Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:  # noqa: BLE001 - propagé via le Future
            future.set_exception(exc)
        return future

    def __enter__(self) -> "_InlineExecutor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


# ------------------------------------------------------------- construction --

class _Builder:
    """Accumule les entrées dans des tableaux compacts pendant le scan."""

    def __init__(self) -> None:
        self.parent = array("i")
        self.size = array("q")
        self.mtime = array("q")
        self.depth = array("h")
        self.flags = bytearray()
        self.name_off = array("q", [0])
        self.name_blob = bytearray()

    def add(self, parent: int, name: str, size: int, mtime: int, flags: int, depth: int) -> int:
        self.parent.append(parent)
        self.size.append(size)
        self.mtime.append(mtime)
        self.depth.append(depth)
        self.flags.append(flags)
        self.name_blob += name.encode("utf-8", "surrogatepass")
        self.name_off.append(len(self.name_blob))
        return len(self.parent) - 1

    def to_tree(self, meta: dict[str, Any]) -> FileTree:
        return FileTree(
            parent=np.frombuffer(self.parent, dtype=np.int32).copy(),
            size=np.frombuffer(self.size, dtype=np.int64).copy(),
            mtime=np.frombuffer(self.mtime, dtype=np.int64).copy(),
            depth=np.frombuffer(self.depth, dtype=np.int16).copy(),
            flags=np.frombuffer(bytes(self.flags), dtype=np.uint8).copy(),
            name_off=np.frombuffer(self.name_off, dtype=np.int64).copy(),
            name_blob=bytes(self.name_blob),
            meta=meta,
        )


# -------------------------------------------------------------------- scan --

def scan(
    root: str | os.PathLike[str],
    *,
    workers: int | None = None,
    size_mode: str = "logical",
    max_depth: int | None = None,
    on_progress: ProgressCallback | None = None,
    cancel: Event | None = None,
) -> FileTree:
    """Scanne `root` et renvoie l'arbre construit.

    size_mode :
        "logical"   taille déclarée du fichier (st_size), gratuite ;
        "allocated" arrondie au cluster du volume — corrige le biais des
                    arborescences à millions de petits fichiers (node_modules,
                    dépôts git), pour un coût nul.

    Les statistiques du scan sont déposées dans `tree.meta`.
    """
    started = time.perf_counter()
    root_path = os.path.abspath(os.fspath(root))
    if not os.path.isdir(root_path):
        raise NotADirectoryError(root_path)

    if size_mode not in ("logical", "allocated"):
        raise ValueError(f"size_mode inconnu : {size_mode!r}")

    if workers is None:
        workers = default_workers(root_path)
    workers = max(1, int(workers))

    cluster = cluster_size(root_path) if size_mode == "allocated" else None

    builder = _Builder()
    try:
        root_mtime = int(os.stat(root_path).st_mtime)
    except OSError:
        root_mtime = 0
    builder.add(0, root_path, 0, root_mtime, FLAG_DIR, 0)

    todo: deque[tuple[int, str, int]] = deque([(0, _long_path(root_path), 0)])
    in_flight: dict[Future, tuple[int, str]] = {}
    max_in_flight = max(4, workers * 4)

    n_files = n_dirs = n_bytes = n_errors = 0
    error_paths: list[str] = []
    last_report = started
    cancelled = False

    executor = ThreadPoolExecutor(max_workers=workers) if workers > 1 else _InlineExecutor()

    with executor as pool:
        while todo or in_flight:
            if cancel is not None and cancel.is_set():
                cancelled = True
                break

            while todo and len(in_flight) < max_in_flight:
                index, path, depth = todo.popleft()
                in_flight[pool.submit(_list_dir, path)] = (index, depth)

            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)

            for future in done:
                index, depth = in_flight.pop(future)
                entries, error = future.result()

                if error is not None:
                    n_errors += 1
                    builder.flags[index] |= FLAG_ERROR
                    if len(error_paths) < _MAX_RECORDED_ERRORS:
                        error_paths.append(f"{_strip_long_path(str(error.filename or ''))}: {error.strerror}")

                child_depth = depth + 1
                for name, is_dir, is_link, size, mtime, child_path, failed in entries:
                    flags = 0
                    if is_dir:
                        flags |= FLAG_DIR
                    if is_link:
                        flags |= FLAG_LINK
                    if failed:
                        flags |= FLAG_ERROR
                        n_errors += 1

                    if is_dir:
                        own_size = 0  # l'entrée de répertoire elle-même ne compte pas
                        n_dirs += 1
                    else:
                        own_size = int(size)
                        if cluster and own_size:
                            own_size = -(-own_size // cluster) * cluster
                        n_files += 1
                        n_bytes += own_size

                    child = builder.add(index, name, own_size, int(mtime), flags, child_depth)

                    # Les liens ne sont jamais suivis : boucles et double comptage.
                    if is_dir and not is_link and not failed:
                        if max_depth is None or child_depth < max_depth:
                            todo.append((child, child_path, child_depth))

            if on_progress is not None:
                now = time.perf_counter()
                if now - last_report >= 0.15:
                    last_report = now
                    on_progress(n_files, n_dirs, n_bytes)

    if on_progress is not None:
        on_progress(n_files, n_dirs, n_bytes)

    elapsed = time.perf_counter() - started
    meta: dict[str, Any] = {
        "root": root_path,
        "scanned_at": time.time(),
        "elapsed": elapsed,
        "backend": "scandir",
        "size_mode": size_mode,
        "cluster_size": cluster,
        "workers": workers,
        "rotational": has_seek_penalty(root_path),
        "files": n_files,
        "dirs": n_dirs,
        "bytes": n_bytes,
        "errors": n_errors,
        "error_samples": error_paths,
        "cancelled": cancelled,
        "max_depth": max_depth,
    }
    return builder.to_tree(meta)
