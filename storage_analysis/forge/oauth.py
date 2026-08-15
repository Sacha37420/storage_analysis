"""Authentification GitHub par OAuth 2.0, sans secret client.

Deux flux, et l'ordre de préférence n'est pas anodin.

**Code d'autorisation + PKCE (défaut).** Le navigateur revient sur un port de
la boucle locale ; le code reçu ne vaut rien sans le `code_verifier` détenu par
le seul processus qui a lancé le flux. C'est ce que GitHub recommande pour un
client public depuis qu'il supporte PKCE (juillet 2025).

**Device flow (`--device`, à réserver au sans-tête).** Pas de redirection du
tout — ce qui est précisément le problème : n'importe qui peut reprendre un
`client_id` public, déclencher le flux et faire saisir le code à une victime
qui verra le nom de VOTRE application sur l'écran de consentement. GitHub le
dit sans détour : « an attacker can use the device flow to remotely impersonate
your app as part of a phishing attack ». À n'activer que pour une session SSH
ou une machine sans navigateur.

Dans les deux cas le `client_id` est public par construction : il peut être
distribué et figurer dans le .env. Le jeton obtenu, lui, ne le peut pas.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Sequence

import requests

from .loopback import LoopbackError, LoopbackServer

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

# Port de boucle locale. GitHub exige que l'URI de rappel corresponde
# exactement à celle enregistrée dans l'application, port compris : il est donc
# fixe, et configurable seulement si le port est déjà pris sur le poste.
DEFAULT_CALLBACK_PORT = 8765
CALLBACK_PATH = "/callback"

# Portées : « repo » couvre le listage des dépôts privés et l'archivage.
# « delete_repo » est délibérément hors du jeu par défaut — c'est la seule
# portée irréversible, elle ne s'obtient qu'avec « repos login --with-delete ».
DEFAULT_SCOPES = ("repo", "read:user")
DELETE_SCOPE = "delete_repo"

_TIMEOUT = 30

# Messages en clair pour chaque code d'erreur documenté par GitHub : un
# « device_flow_disabled » brut n'aide personne.
_ERRORS = {
    "device_flow_disabled":
        "Le device flow n'est pas activé sur l'application OAuth. Ouvrez ses paramètres "
        "sur GitHub et cochez « Enable Device Flow ».",
    "incorrect_client_credentials":
        "GITHUB_CLIENT_ID inconnu de GitHub. Vérifiez l'identifiant de l'application OAuth.",
    "incorrect_device_code":
        "Code d'appareil invalide. Relancez « repos login ».",
    "unsupported_grant_type":
        "Type d'autorisation refusé par GitHub (anomalie interne).",
    "access_denied":
        "Autorisation refusée dans le navigateur.",
    "expired_token":
        "Le code a expiré avant d'être saisi. Relancez « repos login ».",
    # Spécifiques au code d'autorisation + PKCE
    "bad_verification_code":
        "Code d'autorisation invalide ou déjà consommé. Relancez « repos login ».",
    "redirect_uri_mismatch":
        "L'URI de rappel ne correspond pas à celle enregistrée dans l'application OAuth. "
        "Elle doit être identique au caractère près, port compris.",
    "application_suspended":
        "L'application OAuth est suspendue par GitHub.",
    "unverified_user_email":
        "Votre adresse e-mail GitHub doit être vérifiée pour autoriser cette application.",
}


class OAuthError(RuntimeError):
    """Échec du flux, avec un message affichable tel quel."""


@dataclass(slots=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int = 900
    interval: int = 5


@dataclass(slots=True)
class TokenResult:
    token: str
    scopes: list[str] = field(default_factory=list)
    token_type: str = "bearer"
    refresh_token: str | None = None
    expires_in: int | None = None


def build_scopes(with_delete: bool = False, extra: Sequence[str] = ()) -> list[str]:
    scopes = list(DEFAULT_SCOPES)
    if with_delete:
        scopes.append(DELETE_SCOPE)
    for scope in extra:
        if scope and scope not in scopes:
            scopes.append(scope)
    return scopes


# ------------------------------------------------- code d'autorisation + PKCE --

def callback_url(port: int = DEFAULT_CALLBACK_PORT) -> str:
    """URI de rappel à enregistrer à l'identique dans l'application OAuth."""
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


def make_pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge) selon la RFC 7636, méthode S256.

    Le verifier ne quitte jamais le processus ; seul son condensé part dans
    l'URL d'autorisation. Un code d'autorisation intercepté est donc inutile
    à qui ne détient pas le verifier.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_authorize_url(
    client_id: str, scopes: Sequence[str], state: str, challenge: str,
    port: int = DEFAULT_CALLBACK_PORT,
) -> str:
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": callback_url(port),
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"{AUTHORIZE_URL}?{query}"


def parse_callback(path: str, expected_state: str) -> str:
    """Extrait le code d'autorisation du rappel, en validant l'état anti-CSRF."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)

    error = (query.get("error") or [None])[0]
    if error:
        description = (query.get("error_description") or [""])[0]
        raise OAuthError(_ERRORS.get(error, f"GitHub a refusé : {error} {description}".strip()))

    state = (query.get("state") or [None])[0]
    if not state or not secrets.compare_digest(state, expected_state):
        raise OAuthError("État OAuth invalide : rappel rejeté (tentative de CSRF ?).")

    code = (query.get("code") or [None])[0]
    if not code:
        raise OAuthError("Rappel sans code d'autorisation.")
    return code


def pkce_login(
    client_id: str,
    scopes: Sequence[str],
    *,
    port: int = DEFAULT_CALLBACK_PORT,
    timeout: float = 300.0,
    on_prompt: Callable[[str], None] | None = None,
) -> TokenResult:
    """Flux complet contre GitHub : navigateur, rappel local, echange du code.

    Mode direct, sans le lab : utile hors connexion au cadriciel. Le montage
    oauth-hub lui est preferable des qu'il est joignable, puisqu'il evite de
    distribuer le moindre identifiant d'application.
    """
    verifier, challenge = make_pkce_pair()
    state = secrets.token_urlsafe(24)

    try:
        with LoopbackServer(port) as loopback:
            url = build_authorize_url(client_id, scopes, state, challenge, port)
            if on_prompt is not None:
                on_prompt(url)
            answer = loopback.wait_for(CALLBACK_PATH, timeout=timeout)
    except LoopbackError as exc:
        raise OAuthError(str(exc)) from exc

    code = parse_callback("?" + urllib.parse.urlencode(answer), state)

    payload = _post(ACCESS_TOKEN_URL, {
        "client_id": client_id,
        "code": code,
        "redirect_uri": callback_url(port),
        "code_verifier": verifier,
    })
    error = payload.get("error")
    if error:
        raise OAuthError(_ERRORS.get(error, f"Echange du code refuse : {error}"))

    token = payload.get("access_token")
    if not token:
        raise OAuthError("GitHub n'a pas renvoye de jeton.")

    return TokenResult(
        token=token,
        scopes=[s for s in (payload.get("scope") or "").replace(",", " ").split() if s],
        token_type=payload.get("token_type", "bearer"),
        refresh_token=payload.get("refresh_token"),
        expires_in=payload.get("expires_in"),
    )


# ------------------------------------------------------------- device flow --

def _post(url: str, data: dict) -> dict:
    """POST vers GitHub en demandant du JSON (sinon la réponse est form-encodée)."""
    try:
        response = requests.post(
            url, data=data, headers={"Accept": "application/json"}, timeout=_TIMEOUT
        )
    except requests.RequestException as exc:
        raise OAuthError(f"GitHub injoignable : {exc}") from exc

    try:
        payload = response.json()
    except ValueError:
        raise OAuthError(f"Réponse inattendue de GitHub (HTTP {response.status_code}).") from None

    if not isinstance(payload, dict):
        raise OAuthError("Réponse inattendue de GitHub.")
    return payload


def request_device_code(client_id: str, scopes: Sequence[str]) -> DeviceCode:
    """Étape 1 : obtenir le code à saisir par l'utilisateur."""
    payload = _post(DEVICE_CODE_URL, {"client_id": client_id, "scope": " ".join(scopes)})

    error = payload.get("error")
    if error:
        raise OAuthError(_ERRORS.get(error, f"GitHub a refusé la demande : {error}"))

    try:
        return DeviceCode(
            device_code=payload["device_code"],
            user_code=payload["user_code"],
            verification_uri=payload.get("verification_uri", "https://github.com/login/device"),
            expires_in=int(payload.get("expires_in", 900)),
            interval=int(payload.get("interval", 5)),
        )
    except KeyError as exc:
        raise OAuthError(f"Réponse incomplète de GitHub (champ {exc} manquant).") from exc


def poll_for_token(
    client_id: str,
    device: DeviceCode,
    *,
    on_wait: Callable[[int], None] | None = None,
) -> TokenResult:
    """Étape 3 : interroger GitHub jusqu'à autorisation, refus ou expiration.

    GitHub répond HTTP 200 même en cas d'erreur : c'est le champ `error` du
    corps qui fait foi, pas le code de statut.
    """
    interval = max(1, device.interval)
    deadline = time.monotonic() + device.expires_in

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OAuthError(_ERRORS["expired_token"])

        if on_wait is not None:
            on_wait(int(remaining))
        time.sleep(min(interval, max(1, remaining)))

        payload = _post(ACCESS_TOKEN_URL, {
            "client_id": client_id,
            "device_code": device.device_code,
            "grant_type": GRANT_TYPE,
        })

        error = payload.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            # GitHub impose 5 secondes de plus à chaque fois qu'on l'a bousculé.
            interval = int(payload.get("interval", interval + 5)) or interval + 5
            continue
        if error:
            raise OAuthError(_ERRORS.get(error, f"GitHub a refusé l'autorisation : {error}"))

        token = payload.get("access_token")
        if not token:
            raise OAuthError("GitHub n'a pas renvoyé de jeton.")

        return TokenResult(
            token=token,
            scopes=[s for s in (payload.get("scope") or "").replace(",", " ").split() if s],
            token_type=payload.get("token_type", "bearer"),
            refresh_token=payload.get("refresh_token"),
            expires_in=payload.get("expires_in"),
        )
