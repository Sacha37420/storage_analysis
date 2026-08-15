"""Configuration de la vue « dépôts », lue dans le .env du projet.

Les jetons vivent dans le .env, à la demande explicite de l'utilisateur.
Conséquences assumées, rappelées par `warn_token_hygiene()` :
le fichier est en clair, il est gitignoré, et il ne doit jamais être copié
ailleurs ni joint à un rapport.

Une variable d'environnement du même nom l'emporte sur le .env : pratique pour
un jeton temporaire à portée élargie, sans toucher au fichier.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from ..env import ENV_FILE, PROJECT_ROOT, load_env
from .clients import ForgeClient, GitHubClient, GitLabClient

if TYPE_CHECKING:
    from .hub import HubConfig


@dataclass(slots=True)
class ForgeConfig:
    github_token: str | None = None
    github_client_id: str | None = None   # public par construction en mode direct
    github_token_source: str | None = None
    github_token_scopes: str | None = None
    gitlab_url: str | None = None
    gitlab_token: str | None = None
    repo_roots: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    # Courtier du lab (oauth-hub)
    lab_domain: str | None = None
    hub_issuer: str | None = None
    hub_api: str | None = None
    hub_client_id: str = "storage-analysis"
    hub_port: int = 8765
    lab_refresh_token: str | None = None

    @property
    def has_any_token(self) -> bool:
        return bool(self.github_token or self.gitlab_token)

    @property
    def hub_configured(self) -> bool:
        return bool(self.hub_issuer and self.hub_api)

    def hub(self) -> "HubConfig | None":
        """Configuration du courtier, ou None s'il n'est pas renseigné."""
        if not self.hub_configured:
            return None
        from .hub import HubConfig
        return HubConfig(
            issuer=self.hub_issuer,       # type: ignore[arg-type]
            api_base=self.hub_api,        # type: ignore[arg-type]
            client_id=self.hub_client_id,
            port=self.hub_port,
        )


def _get(key: str) -> str | None:
    value = os.environ.get(key) or load_env().get(key)
    value = (value or "").strip()
    return value or None


def _int(key: str, default: int) -> int:
    raw = _get(key)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def load_config() -> ForgeConfig:
    roots_raw = _get("REPO_ROOTS") or ""
    protected_raw = _get("FORGE_PROTECTED") or ""

    # Un seul LAB_DOMAIN suffit : les deux URLs s'en déduisent selon le routage
    # documenté par oauth-hub. Les surcharges explicites restent prioritaires,
    # pour un lab dont le préfixe d'URL différerait.
    domain = (_get("LAB_DOMAIN") or "").strip().rstrip("/")
    issuer = _get("HUB_ISSUER") or (f"https://{domain}/auth/realms/ssolab" if domain else None)
    api = _get("HUB_API") or (f"https://{domain}/oauth-hub-api" if domain else None)

    return ForgeConfig(
        lab_domain=domain or None,
        hub_issuer=issuer,
        hub_api=api,
        hub_client_id=_get("HUB_CLIENT_ID") or "storage-analysis",
        hub_port=_int("HUB_PORT", 8765),
        lab_refresh_token=_get("LAB_REFRESH_TOKEN"),
        github_token=_get("GITHUB_TOKEN"),
        github_client_id=_get("GITHUB_CLIENT_ID"),
        github_token_source=_get("GITHUB_TOKEN_SOURCE"),
        github_token_scopes=_get("GITHUB_TOKEN_SCOPES"),
        gitlab_url=_get("GITLAB_URL"),
        gitlab_token=_get("GITLAB_TOKEN"),
        repo_roots=[p.strip() for p in roots_raw.replace(";", os.pathsep).split(os.pathsep) if p.strip()],
        protected=[p.strip() for p in protected_raw.split(",") if p.strip()],
    )


_resolved_github_token: str | None = None


def resolve_github_token(
    config: ForgeConfig | None = None, *, on_note: "Callable[[str], None] | None" = None
) -> str | None:
    """Jeton GitHub à utiliser, courtier d'abord, jeton local ensuite.

    Le courtier est interrogé à chaque fois plutôt que mis en cache sur disque :
    c'est lui qui renouvelle le jeton amont, le figer ici reviendrait à se priver
    de ce service. Un cache mémoire évite seulement de le redemander deux fois
    dans une même commande.
    """
    global _resolved_github_token
    if _resolved_github_token is not None:
        return _resolved_github_token

    config = config or load_config()
    hub_config = config.hub()

    if hub_config is not None and config.lab_refresh_token:
        from .hub import HubError, refresh, site_token
        try:
            tokens = refresh(hub_config, config.lab_refresh_token)
            payload = site_token(hub_config, tokens.access_token, "github", interactive=False)
            _resolved_github_token = payload["access_token"]
            if tokens.refresh_token and tokens.refresh_token != config.lab_refresh_token:
                # Keycloak fait tourner le refresh_token à chaque usage.
                from ..env import set_env_values
                set_env_values({"LAB_REFRESH_TOKEN": tokens.refresh_token})
            return _resolved_github_token
        except HubError as exc:
            if on_note is not None:
                on_note(f"courtier indisponible ({exc}) — repli sur le jeton local")

    _resolved_github_token = config.github_token
    return _resolved_github_token


def reset_token_cache() -> None:
    """À appeler après une connexion : le jeton mémorisé n'est plus le bon."""
    global _resolved_github_token
    _resolved_github_token = None


def build_clients(
    config: ForgeConfig | None = None, *, on_note: "Callable[[str], None] | None" = None
) -> list[ForgeClient]:
    """Instancie un client par forge configurée.

    Peut déclencher un appel réseau vers le courtier pour obtenir le jeton
    GitHub — jamais d'interaction avec l'utilisateur : une commande de listage
    ne doit pas ouvrir un navigateur de sa propre initiative.
    """
    config = config or load_config()
    clients: list[ForgeClient] = []

    github_token = resolve_github_token(config, on_note=on_note)
    if github_token:
        clients.append(GitHubClient(github_token))
    if config.gitlab_token:
        if not config.gitlab_url:
            raise ValueError("GITLAB_TOKEN est défini mais GITLAB_URL manque dans le .env.")
        clients.append(GitLabClient(config.gitlab_token, config.gitlab_url))

    return clients


def warn_token_hygiene() -> list[str]:
    """Contrôles rapides sur le stockage des jetons. Renvoie les alertes."""
    warnings: list[str] = []
    config = load_config()
    if not config.has_any_token:
        return warnings

    gitignore = PROJECT_ROOT / ".gitignore"
    ignored = gitignore.is_file() and ".env" in gitignore.read_text(encoding="utf-8", errors="replace")
    if not ignored:
        warnings.append(".env n'est PAS dans .gitignore : vos jetons risquent d'être poussés.")

    if ENV_FILE.is_file():
        warnings.append(
            f"jetons en clair dans {ENV_FILE} — ne pas copier ce fichier, "
            "ne pas le joindre à un rapport, le révoquer en cas de doute."
        )
    return warnings
