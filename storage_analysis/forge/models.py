"""Structures de données de la vue « dépôts ». Aucune logique ici."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RepoRef:
    """Identité normalisée d'un dépôt : hôte + chemin avec namespace."""

    host: str
    path: str

    @property
    def key(self) -> str:
        return f"{self.host}/{self.path}"

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def namespace(self) -> str:
        return self.path.rsplit("/", 1)[0] if "/" in self.path else ""

    def __str__(self) -> str:
        return self.key


@dataclass(slots=True)
class SubmoduleDecl:
    """Une entrée de .gitmodules."""

    name: str
    rel_path: str
    url: str
    ref: RepoRef | None = None


@dataclass(slots=True)
class LocalRepo:
    """Un clone présent sur le disque."""

    path: str
    name: str
    remote_url: str | None = None
    ref: RepoRef | None = None
    branch: str | None = None
    dirty: int = 0                 # nombre de fichiers modifiés/non suivis
    ahead: int = 0                 # commits locaux non poussés
    behind: int = 0
    last_commit: int | None = None  # epoch
    size_bytes: int = 0
    git_dir_bytes: int = 0
    submodules: list[SubmoduleDecl] = field(default_factory=list)
    is_submodule_checkout: bool = False
    error: str | None = None

    @property
    def has_remote(self) -> bool:
        return self.ref is not None

    @property
    def unsaved(self) -> bool:
        """Vrai si supprimer ce clone perdrait du travail."""
        return self.ref is None or self.ahead > 0 or self.dirty > 0


@dataclass(slots=True)
class RemoteRepo:
    """Un projet vu par l'API d'une forge."""

    ref: RepoRef
    forge: str                      # "github" | "gitlab"
    project_id: str                 # identifiant natif, pour les appels d'action
    archived: bool = False
    private: bool = True
    fork: bool = False
    size_bytes: int | None = None
    last_activity: int | None = None  # epoch
    web_url: str = ""
    default_branch: str | None = None
    submodules: list[SubmoduleDecl] = field(default_factory=list)


@dataclass(slots=True)
class CatalogEntry:
    """Un dépôt vu des deux côtés."""

    ref: RepoRef | None
    local: LocalRepo | None = None
    remote: RemoteRepo | None = None

    @property
    def key(self) -> str:
        if self.ref is not None:
            return self.ref.key
        return f"(local){self.local.path}" if self.local else "(inconnu)"

    @property
    def name(self) -> str:
        if self.ref is not None:
            return self.ref.name
        return self.local.name if self.local else "?"

    @property
    def status(self) -> str:
        """orphelin : aucun remote du tout, donc rien ailleurs pour le sauver."""
        if self.local is not None and self.ref is None:
            return "orphelin"
        if self.local is not None and self.remote is not None:
            return "cloné"
        if self.local is not None:
            return "local seul"
        return "distant seul"

    @property
    def size_bytes(self) -> int:
        if self.local is not None:
            return self.local.size_bytes
        if self.remote is not None and self.remote.size_bytes is not None:
            return self.remote.size_bytes
        return 0

    @property
    def archived(self) -> bool:
        return self.remote.archived if self.remote is not None else False

    @property
    def last_activity(self) -> int | None:
        candidates = [
            value
            for value in (
                self.local.last_commit if self.local else None,
                self.remote.last_activity if self.remote else None,
            )
            if value
        ]
        return max(candidates) if candidates else None

    @property
    def submodules(self) -> list[SubmoduleDecl]:
        """Déclarations locales en priorité : elles reflètent l'état réel du disque."""
        if self.local is not None and self.local.submodules:
            return self.local.submodules
        return self.remote.submodules if self.remote is not None else []
