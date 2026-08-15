"""Client de `oauth-hub` — le courtier de jetons OAuth du lab.

Montage à deux jetons, qui ne se renouvellent pas au même endroit :

* le **jeton Keycloak** prouve qui nous sommes auprès du lab. Nous le récupérons
  par authorization code + PKCE S256 (client public, sans secret) et nous le
  renouvelons nous-mêmes avec son `refresh_token` ;
* le **jeton amont** (GitHub, GitLab…) est détenu par `oauth-hub`, qui le
  renouvelle tout seul. Nous nous contentons de le redemander : rien à
  implémenter de ce côté.

Le `client_secret` du site n'existe que sur le serveur du lab. C'est toute la
raison d'être de ce détour : sans lui, chaque poste devrait détenir le secret
pour échanger son code d'autorisation.

Seul le `refresh_token` Keycloak est conservé localement. Le jeton amont, lui,
n'est jamais stocké : il se redemande, ce qui laisse `oauth-hub` faire son
travail de renouvellement.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable

import requests

from .loopback import LoopbackServer

_TIMEOUT = 20
CALLBACK_PATH = "/callback"        # URI n°2 — enregistrée dans Keycloak
RETURN_PATH = "/oauth-hub-done"    # URI n°3 — passée en paramètre

# Codes stables renvoyés par oauth-hub. On teste le code, jamais le message :
# le texte est destiné à l'affichage et peut être reformulé.
FATAL_ERRORS = {"exchange_failed"}
USER_ERRORS = {"provider_refused"}


class HubError(RuntimeError):
    """Erreur d'accès au lab ou au courtier, avec un message affichable."""


@dataclass(slots=True)
class HubConfig:
    issuer: str          # https://<DOMAIN>/auth/realms/ssolab
    api_base: str        # https://<DOMAIN>/oauth-hub-api
    client_id: str = "storage-analysis"
    port: int = 8765

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{CALLBACK_PATH}"

    @property
    def token_endpoint(self) -> str:
        return f"{self.issuer.rstrip('/')}/protocol/openid-connect/token"

    @property
    def auth_endpoint(self) -> str:
        return f"{self.issuer.rstrip('/')}/protocol/openid-connect/auth"

    def provider_token_url(self, slug: str) -> str:
        return f"{self.api_base.rstrip('/')}/api/providers/{slug}/token/"

    def provider_status_url(self, slug: str) -> str:
        return f"{self.api_base.rstrip('/')}/api/providers/{slug}/status/"


@dataclass(slots=True)
class LabTokens:
    access_token: str
    refresh_token: str | None = None
    expires_at: float = 0.0

    @property
    def stale(self) -> bool:
        # 60 s de marge : un jeton qui expire pendant le trajet réseau ne doit
        # jamais nous être servi.
        return time.time() >= self.expires_at - 60


# --------------------------------------------------------- jeton Keycloak --

def _token_request(config: HubConfig, data: dict) -> LabTokens:
    try:
        response = requests.post(config.token_endpoint, data=data, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise HubError(f"Lab injoignable ({config.issuer}) : {exc}") from exc

    if response.status_code >= 400:
        detail = ""
        try:
            payload = response.json()
            detail = payload.get("error_description") or payload.get("error") or ""
        except ValueError:
            detail = response.text[:200]
        raise HubError(f"Keycloak a refusé la demande (HTTP {response.status_code}) : {detail}")

    payload = response.json()
    return LabTokens(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        expires_at=time.time() + float(payload.get("expires_in", 300)),
    )


def login(
    config: HubConfig,
    *,
    on_prompt: Callable[[str], None] | None = None,
    timeout: float = 300.0,
) -> LabTokens:
    """Authentification sur le lab : authorization code + PKCE S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    state = secrets.token_urlsafe(24)

    query = urllib.parse.urlencode({
        "client_id": config.client_id,
        "response_type": "code",
        "redirect_uri": config.redirect_uri,
        "scope": "openid profile email",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })

    with LoopbackServer(config.port) as loopback:
        if on_prompt is not None:
            on_prompt(f"{config.auth_endpoint}?{query}")
        answer = loopback.wait_for(CALLBACK_PATH, timeout=timeout)

    if answer.get("error"):
        raise HubError(
            f"Keycloak a refusé : {answer.get('error_description') or answer['error']}")
    if not secrets.compare_digest(answer.get("state", ""), state):
        raise HubError("État OAuth inattendu — connexion abandonnée (tentative de CSRF ?).")
    if not answer.get("code"):
        raise HubError("Retour de Keycloak sans code d'autorisation.")

    return _token_request(config, {
        "grant_type": "authorization_code",
        "client_id": config.client_id,
        "code": answer["code"],
        "redirect_uri": config.redirect_uri,
        "code_verifier": verifier,   # ce qui remplace le client_secret
    })


def refresh(config: HubConfig, refresh_token: str) -> LabTokens:
    """Renouvelle le jeton de lab sans interaction. Échoue si la session SSO est morte."""
    tokens = _token_request(config, {
        "grant_type": "refresh_token",
        "client_id": config.client_id,
        "refresh_token": refresh_token,
    })
    # Keycloak fait tourner le refresh_token : conserver l'ancien serait fatal.
    if tokens.refresh_token is None:
        tokens.refresh_token = refresh_token
    return tokens


# ------------------------------------------------------------ jeton amont --

def _authorized_get(url: str, access_token: str, params: dict | None = None):
    try:
        return requests.get(
            url, params=params,
            headers={"Authorization": f"Bearer {access_token}"}, timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise HubError(f"oauth-hub injoignable : {exc}") from exc


def list_providers(config: HubConfig, access_token: str) -> list[dict]:
    """Sites déclarés dans le courtier, avec leur état de configuration.

    C'est le courtier qui fait autorité sur la liste : un site ajouté depuis son
    interface doit apparaître ici sans qu'on touche au client.
    """
    response = _authorized_get(f"{config.api_base.rstrip('/')}/api/providers/", access_token)
    if response.status_code == 403:
        raise HubError(
            f"oauth-hub refuse ce client (403). Ajoutez « {config.client_id} » à "
            f"KEYCLOAK_TRUSTED_CLIENTS dans oauth-hub/.env, puis redémarrez son backend."
        )
    if response.status_code >= 400:
        raise HubError(f"oauth-hub : HTTP {response.status_code} — {response.text[:200]}")

    payload = response.json()
    if isinstance(payload, dict):          # tolère une réponse paginée
        payload = payload.get("results", [])
    return payload if isinstance(payload, list) else []


def status(config: HubConfig, access_token: str, slug: str) -> dict:
    """État de la liaison, sans faire transiter de jeton."""
    response = _authorized_get(config.provider_status_url(slug), access_token)
    if response.status_code == 403:
        raise HubError(
            f"oauth-hub refuse ce client (403). Ajoutez « {config.client_id} » à "
            f"KEYCLOAK_TRUSTED_CLIENTS dans oauth-hub/.env, puis redémarrez son backend."
        )
    if response.status_code >= 400:
        raise HubError(f"oauth-hub : HTTP {response.status_code} — {response.text[:200]}")
    return response.json()


def check_return(params: dict[str, str]) -> dict[str, str]:
    """Analyse le retour d'oauth-hub sur la boucle locale, ou lève."""
    if "oauth_error" in params:
        code = params.get("oauth_error_code", "")
        message = params["oauth_error"]
        if code in FATAL_ERRORS:
            raise HubError(
                f"Configuration du site cassée côté lab : {message} "
                f"Réessayer n'y changera rien — prévenez un dev."
            )
        if code in USER_ERRORS:
            raise HubError(f"{message} (décision de l'utilisateur, pas de nouvelle tentative)")
        raise HubError(message)
    return params


def site_token(
    config: HubConfig,
    access_token: str,
    slug: str = "github",
    *,
    interactive: bool = True,
    on_prompt: Callable[[str], None] | None = None,
    timeout: float = 300.0,
) -> dict:
    """Rend le jeton du site, en guidant l'utilisateur s'il n'a rien relié.

    `interactive=False` remonte l'URL de liaison au lieu d'ouvrir un navigateur :
    une commande de listage ne doit pas décider seule d'interrompre l'utilisateur.
    """
    url = config.provider_token_url(slug)

    if not interactive:
        response = _authorized_get(url, access_token)
        if response.status_code == 409:
            payload = response.json()
            raise HubError(
                f"Compte {slug} non relié. Lancez « repos login » pour l'autoriser."
                f"\n    {payload.get('connect_url', '')}"
            )
        return _decode_token_response(response, config)

    with LoopbackServer(config.port) as loopback:
        return_url = loopback.url(RETURN_PATH)
        response = _authorized_get(url, access_token, {"return_url": return_url})

        if response.status_code == 409:
            payload = response.json()
            connect_url = payload.get("connect_url")
            if not connect_url:
                raise HubError(f"oauth-hub n'a pas fourni d'URL de liaison pour « {slug} ».")
            if on_prompt is not None:
                on_prompt(connect_url)
            check_return(loopback.wait_for(RETURN_PATH, timeout=timeout))
            response = _authorized_get(url, access_token)

    return _decode_token_response(response, config)


def _decode_token_response(response, config: HubConfig) -> dict:
    if response.status_code == 403:
        raise HubError(
            f"oauth-hub refuse ce client (403). Ajoutez « {config.client_id} » à "
            f"KEYCLOAK_TRUSTED_CLIENTS dans oauth-hub/.env, puis redémarrez son backend."
        )
    if response.status_code == 409:
        raise HubError("Compte non relié — relancez « repos login ».")
    if response.status_code >= 400:
        raise HubError(f"oauth-hub : HTTP {response.status_code} — {response.text[:200]}")

    payload = response.json()
    if not payload.get("access_token"):
        raise HubError("oauth-hub n'a pas renvoyé de jeton.")
    return payload
