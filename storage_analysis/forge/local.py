"""Découverte des clones locaux et lecture de leur état git.

Deux sources d'information, délibérément séparées :

  * les **fichiers** `.git/config` et `.gitmodules`, lus directement — toujours
    disponibles, même sans git installé, et sans coût de processus ;
  * la **commande git**, pour l'état dynamique (dernier commit, fichiers
    modifiés, commits non poussés) qu'aucun fichier ne donne simplement.

Si git est absent ou échoue, on garde tout ce que les fichiers ont donné et on
note l'erreur : le catalogue reste utilisable, seulement moins renseigné.
"""

from __future__ import annotations

import configparser
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, Iterator

from .models import LocalRepo, SubmoduleDecl
from .urls import normalize_remote

# Dossiers dans lesquels un dépôt ne se cache jamais, et qui coûtent cher à
# parcourir. Les submodules ne sont pas concernés : ils sont résolus par leur
# chemin déclaré dans .gitmodules, pas par exploration à l'aveugle.
_SKIP_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages",
})

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


# ------------------------------------------------------------- découverte --

def discover(roots: Iterable[str], max_depth: int = 6) -> list[str]:
    """Chemins des dépôts trouvés sous `roots`, sans descendre dans un dépôt.

    Une fois un `.git` rencontré, inutile de continuer plus bas : les éventuels
    submodules seront atteints par leur chemin déclaré, ce qui est à la fois
    plus rapide et plus fiable qu'une exploration exhaustive.
    """
    found: list[str] = []
    seen: set[str] = set()

    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            real = os.path.normcase(current)
            if real in seen:
                continue
            seen.add(real)

            if os.path.exists(os.path.join(current, ".git")):
                found.append(current)
                continue  # on ne descend pas dans un dépôt
            if depth >= max_depth:
                continue

            try:
                with os.scandir(current) as iterator:
                    for entry in iterator:
                        try:
                            if not entry.is_dir(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        if entry.name in _SKIP_DIRS or entry.name.startswith("$"):
                            continue
                        stack.append((entry.path, depth + 1))
            except OSError:
                continue

    return sorted(found)


def directory_size(path: str) -> tuple[int, int]:
    """(taille totale, part occupée par .git) en octets.

    Isoler `.git` est utile : un dépôt de 2 Gio dont 1,8 Gio d'historique
    n'appelle pas la même décision qu'un dépôt de 2 Gio de données.
    """
    total = 0
    git_part = 0
    git_root = os.path.normcase(os.path.join(path, ".git"))

    stack = [path]
    while stack:
        current = stack.pop()
        in_git = os.path.normcase(current).startswith(git_root)
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        size = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
                    total += size
                    if in_git:
                        git_part += size
        except OSError:
            continue

    return total, git_part


# ------------------------------------------------------ lecture des fichiers --

def _read_ini(path: str) -> configparser.ConfigParser | None:
    """Lit un fichier au format git-config. Renvoie None si illisible."""
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            parser.read_string(handle.read())
    except (OSError, configparser.Error):
        return None
    return parser


def _remote_url(repo_path: str) -> str | None:
    """URL du remote « origin », à défaut le premier remote déclaré."""
    parser = _read_ini(os.path.join(repo_path, ".git", "config"))
    if parser is None:
        # Cas d'un submodule ou d'un worktree : .git est un fichier.
        gitdir = _resolve_gitfile(repo_path)
        parser = _read_ini(os.path.join(gitdir, "config")) if gitdir else None
    if parser is None:
        return None

    remotes: dict[str, str] = {}
    for section in parser.sections():
        if section.startswith('remote "') and parser.has_option(section, "url"):
            remotes[section[8:-1]] = parser.get(section, "url").strip()
    if not remotes:
        return None
    return remotes.get("origin") or next(iter(remotes.values()))


def _resolve_gitfile(repo_path: str) -> str | None:
    """Cible d'un `.git` qui est un fichier (submodule, worktree)."""
    git_path = os.path.join(repo_path, ".git")
    if not os.path.isfile(git_path):
        return None
    try:
        with open(git_path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read().strip()
    except OSError:
        return None
    if not content.startswith("gitdir:"):
        return None
    target = content[7:].strip()
    if not os.path.isabs(target):
        target = os.path.normpath(os.path.join(repo_path, target))
    return target


def read_submodules(repo_path: str) -> list[SubmoduleDecl]:
    """Déclarations du .gitmodules à la racine du dépôt."""
    parser = _read_ini(os.path.join(repo_path, ".gitmodules"))
    if parser is None:
        return []

    declarations: list[SubmoduleDecl] = []
    for section in parser.sections():
        if not section.startswith('submodule "'):
            continue
        url = parser.get(section, "url", fallback="").strip()
        rel_path = parser.get(section, "path", fallback="").strip()
        declarations.append(
            SubmoduleDecl(
                name=section[11:-1],
                rel_path=rel_path,
                url=url,
                ref=normalize_remote(url),
            )
        )
    return declarations


# ------------------------------------------------------------ appels à git --

def _run_git(repo_path: str, *args: str, timeout: float = 15.0) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _read_git_state(repo: LocalRepo) -> None:
    """Complète branche, propreté, avance/retard et date du dernier commit."""
    status = _run_git(repo.path, "status", "--porcelain=v2", "--branch")
    if status is None:
        repo.error = "git indisponible ou dépôt illisible"
    else:
        dirty = 0
        for line in status.splitlines():
            if line.startswith("# branch.head "):
                head = line[14:].strip()
                repo.branch = None if head == "(detached)" else head
            elif line.startswith("# branch.ab "):
                for token in line[12:].split():
                    if token.startswith("+"):
                        repo.ahead = int(token[1:])
                    elif token.startswith("-"):
                        repo.behind = int(token[1:])
            elif line and not line.startswith("#"):
                dirty += 1
        repo.dirty = dirty

    last = _run_git(repo.path, "log", "-1", "--format=%ct")
    if last:
        try:
            repo.last_commit = int(last.strip().splitlines()[0])
        except (ValueError, IndexError):
            repo.last_commit = None


# ----------------------------------------------------------------- lecture --

def read_repo(path: str, *, with_git: bool = True, with_size: bool = True) -> LocalRepo:
    """Construit un LocalRepo à partir d'un chemin de dépôt."""
    url = _remote_url(path)
    repo = LocalRepo(
        path=os.path.abspath(path),
        name=os.path.basename(os.path.abspath(path)),
        remote_url=url,
        ref=normalize_remote(url),
        submodules=read_submodules(path),
        is_submodule_checkout="modules" in (_resolve_gitfile(path) or "").replace("\\", "/").split("/"),
    )
    if with_size:
        repo.size_bytes, repo.git_dir_bytes = directory_size(path)
    if with_git:
        _read_git_state(repo)
    return repo


def read_repos(
    paths: Iterable[str],
    *,
    workers: int | None = None,
    with_git: bool = True,
    with_size: bool = True,
    on_progress: "Callable[[int, int, str], None] | None" = None,
) -> list[LocalRepo]:
    """Lit plusieurs dépôts en parallèle (git et le disque sont I/O bound).

    Même politique que le scanner : sur disque rotatif on reste à 1 thread,
    les seeks concurrents coûtant plus qu'ils ne rapportent. À froid sur un
    HDD, mesurer la taille de plusieurs Go de clones prend de toute façon des
    dizaines de secondes — d'où le rappel de progression.
    """
    paths = list(paths)
    if not paths:
        return []

    if workers is None:
        from ..core.sysinfo import default_workers
        workers = default_workers(paths[0])

    results: list[LocalRepo] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = pool.map(
            lambda p: read_repo(p, with_git=with_git, with_size=with_size), paths
        )
        for done, repo in enumerate(futures, start=1):
            results.append(repo)
            if on_progress is not None:
                on_progress(done, len(paths), repo.name)
    return results


def iter_submodule_checkouts(repo: LocalRepo) -> Iterator[str]:
    """Chemins des submodules réellement présents sur le disque."""
    for declaration in repo.submodules:
        if not declaration.rel_path:
            continue
        candidate = os.path.normpath(os.path.join(repo.path, declaration.rel_path))
        if os.path.exists(os.path.join(candidate, ".git")):
            yield candidate
