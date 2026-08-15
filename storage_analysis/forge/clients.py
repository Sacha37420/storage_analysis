"""Accès aux API GitHub et GitLab.

Un seul contrat (`ForgeClient`) pour deux forges qui ne se ressemblent pas :
GitHub identifie un projet par `owner/repo`, GitLab par un identifiant numérique
et un `path_with_namespace`. Le reste du code ne manipule que des RepoRef.

Périmètre retenu : les projets dont l'utilisateur est **membre**. Sur une
instance partagée, lister tout ce qui est visible représenterait des milliers
de projets pour aucun bénéfice.
"""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Iterator

import requests

from .models import RemoteRepo, RepoRef, SubmoduleDecl
from .urls import normalize_remote

_USER_AGENT = "storage_analysis/0.2"
_TIMEOUT = 30
_MAX_PAGES = 100  # garde-fou : 10 000 projets, largement au-delà du réaliste


class ForgeError(RuntimeError):
    """Erreur d'accès à une forge, avec un message affichable tel quel."""


def _parse_iso(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00"))
                   .astimezone(timezone.utc).timestamp())
    except ValueError:
        return None


def _next_link(header: str | None) -> str | None:
    """URL de la page suivante, extraite de l'en-tête Link (RFC 5988)."""
    if not header:
        return None
    for part in header.split(","):
        match = re.match(r'\s*<([^>]+)>\s*;\s*rel="next"', part)
        if match:
            return match.group(1)
    return None


class ForgeClient(ABC):
    """Contrat commun aux deux forges."""

    kind: str = "?"

    def __init__(self, host: str, token: str, api_root: str) -> None:
        self.host = host
        self.api_root = api_root.rstrip("/")
        self.granted_scopes: list[str] = []
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _USER_AGENT, **self._auth_headers(token)})

    @abstractmethod
    def _auth_headers(self, token: str) -> dict[str, str]: ...

    @abstractmethod
    def check(self) -> str:
        """Vérifie le jeton et renvoie l'identité connectée."""

    @abstractmethod
    def list_repos(self) -> list[RemoteRepo]: ...

    @abstractmethod
    def fetch_gitmodules(self, repo: RemoteRepo) -> str | None: ...

    @abstractmethod
    def set_archived(self, repo: RemoteRepo, archived: bool) -> None: ...

    @abstractmethod
    def delete(self, repo: RemoteRepo) -> None: ...

    # ------------------------------------------------------------ requêtes --

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        if url.startswith("/"):
            url = self.api_root + url
        for attempt in range(4):
            try:
                response = self.session.request(method, url, timeout=_TIMEOUT, **kwargs)
            except requests.RequestException as exc:
                raise ForgeError(f"{self.host} injoignable : {exc}") from exc

            # 429 / 403 de quota : on respecte Retry-After plutôt que de marteler.
            if response.status_code in (429, 502, 503) and attempt < 3:
                delay = float(response.headers.get("Retry-After", 2 ** attempt))
                time.sleep(min(delay, 30))
                continue

            if response.status_code == 401:
                raise ForgeError(f"{self.host} : jeton refusé (401). Vérifiez sa validité.")
            if response.status_code == 403:
                hint = ""
                if self.kind == "github" and method == "DELETE":
                    hint = " Il manque la portée « delete_repo » : relancez « repos login --with-delete »."
                elif self.kind == "github":
                    hint = " Vérifiez les portées avec « repos check »."
                raise ForgeError(
                    f"{self.host} : accès refusé (403) sur {method} {url}."
                    f" Portée du jeton insuffisante.{hint}"
                )
            if response.status_code == 404:
                raise ForgeError(f"{self.host} : introuvable (404) — {url}")
            if not response.ok:
                raise ForgeError(f"{self.host} : HTTP {response.status_code} — {response.text[:200]}")
            return response

        raise ForgeError(f"{self.host} : trop de tentatives infructueuses.")

    def _paginate(self, url: str, params: dict[str, Any]) -> Iterator[dict]:
        page_url: str | None = url
        first = True
        pages = 0
        while page_url and pages < _MAX_PAGES:
            response = self._request("GET", page_url, params=params if first else None)
            payload = response.json()
            if not isinstance(payload, list):
                raise ForgeError(f"{self.host} : réponse inattendue pour {page_url}")
            yield from payload
            page_url = _next_link(response.headers.get("Link"))
            first = False
            pages += 1


# ------------------------------------------------------------------ GitHub --

class GitHubClient(ForgeClient):
    kind = "github"

    def __init__(self, token: str, host: str = "github.com",
                 api_root: str = "https://api.github.com") -> None:
        super().__init__(host, token, api_root)

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def check(self) -> str:
        response = self._request("GET", "/user")
        data = response.json()
        # GitHub annonce les portées réellement accordées dans un en-tête : c'est
        # la seule façon fiable de diagnostiquer un 403 à venir sur rm-remote.
        granted = response.headers.get("X-OAuth-Scopes", "").strip()
        self.granted_scopes = [s.strip() for s in granted.split(",") if s.strip()]
        suffix = f" — portées : {granted}" if granted else ""
        return f"{data.get('login', '?')} ({data.get('name') or 'sans nom'}){suffix}"

    def list_repos(self) -> list[RemoteRepo]:
        repos: list[RemoteRepo] = []
        params = {
            "affiliation": "owner,collaborator,organization_member",
            "per_page": 100,
            "sort": "pushed",
        }
        for item in self._paginate("/user/repos", params):
            ref = RepoRef(host=self.host, path=item["full_name"])
            repos.append(RemoteRepo(
                ref=ref,
                forge=self.kind,
                project_id=item["full_name"],
                archived=bool(item.get("archived")),
                private=bool(item.get("private")),
                fork=bool(item.get("fork")),
                # GitHub annonce la taille en Kio.
                size_bytes=int(item["size"]) * 1024 if item.get("size") is not None else None,
                last_activity=_parse_iso(item.get("pushed_at") or item.get("updated_at")),
                web_url=item.get("html_url", ""),
                default_branch=item.get("default_branch"),
            ))
        return repos

    def fetch_gitmodules(self, repo: RemoteRepo) -> str | None:
        try:
            response = self._request(
                "GET", f"/repos/{repo.project_id}/contents/.gitmodules",
                headers={"Accept": "application/vnd.github.raw+json"},
            )
        except ForgeError:
            return None  # absence de .gitmodules = 404, cas normal
        return response.text

    def set_archived(self, repo: RemoteRepo, archived: bool) -> None:
        self._request("PATCH", f"/repos/{repo.project_id}", json={"archived": archived})

    def delete(self, repo: RemoteRepo) -> None:
        self._request("DELETE", f"/repos/{repo.project_id}")


# ------------------------------------------------------------------ GitLab --

class GitLabClient(ForgeClient):
    kind = "gitlab"

    def __init__(self, token: str, base_url: str) -> None:
        base = base_url.rstrip("/")
        host = re.sub(r"^https?://", "", base).split("/")[0].lower()
        super().__init__(host, token, f"{base}/api/v4")

    def _auth_headers(self, token: str) -> dict[str, str]:
        # PRIVATE-TOKEN accepte aussi bien un PAT qu'un jeton de groupe.
        return {"PRIVATE-TOKEN": token}

    def check(self) -> str:
        data = self._request("GET", "/user").json()
        return f"{data.get('username', '?')} ({data.get('name') or 'sans nom'})"

    def list_repos(self) -> list[RemoteRepo]:
        repos: list[RemoteRepo] = []
        params = {
            "membership": "true",
            "per_page": 100,
            "order_by": "last_activity_at",
            "statistics": "true",   # ignoré si le jeton n'a pas le droit
            "archived": None,       # archivés inclus : ce sont eux qu'on cherche
        }
        for item in self._paginate("/projects", {k: v for k, v in params.items() if v is not None}):
            ref = RepoRef(host=self.host, path=item["path_with_namespace"])
            statistics = item.get("statistics") or {}
            size = statistics.get("repository_size")
            repos.append(RemoteRepo(
                ref=ref,
                forge=self.kind,
                project_id=str(item["id"]),
                archived=bool(item.get("archived")),
                private=item.get("visibility") != "public",
                fork="forked_from_project" in item,
                size_bytes=int(size) if size is not None else None,
                last_activity=_parse_iso(item.get("last_activity_at")),
                web_url=item.get("web_url", ""),
                default_branch=item.get("default_branch"),
            ))
        return repos

    def fetch_gitmodules(self, repo: RemoteRepo) -> str | None:
        branch = repo.default_branch or "main"
        try:
            response = self._request(
                "GET", f"/projects/{repo.project_id}/repository/files/.gitmodules/raw",
                params={"ref": branch},
            )
        except ForgeError:
            return None
        return response.text

    def set_archived(self, repo: RemoteRepo, archived: bool) -> None:
        verb = "archive" if archived else "unarchive"
        self._request("POST", f"/projects/{repo.project_id}/{verb}")

    def delete(self, repo: RemoteRepo) -> None:
        # Selon la configuration de l'instance, GitLab peut appliquer une
        # suppression différée : le projet part en corbeille avant destruction.
        self._request("DELETE", f"/projects/{repo.project_id}")


# ------------------------------------------------------------- submodules --

def parse_gitmodules(content: str) -> list[SubmoduleDecl]:
    """Analyse un .gitmodules récupéré par API (même format que le fichier local)."""
    import configparser

    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read_string(content)
    except configparser.Error:
        return []

    declarations: list[SubmoduleDecl] = []
    for section in parser.sections():
        if not section.startswith('submodule "'):
            continue
        url = parser.get(section, "url", fallback="").strip()
        declarations.append(SubmoduleDecl(
            name=section[11:-1],
            rel_path=parser.get(section, "path", fallback="").strip(),
            url=url,
            ref=normalize_remote(url),
        ))
    return declarations
