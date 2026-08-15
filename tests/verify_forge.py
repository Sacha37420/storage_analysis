r"""Vérifications de la vue « dépôts », entièrement hors ligne.

Aucun appel réseau, aucun jeton : on fabrique une arborescence de faux dépôts
et on contrôle la normalisation des URL, la corrélation local/distant, le
graphe de submodules et surtout les garde-fous des actions destructives.

    .\.venv\Scripts\python.exe tests\verify_forge.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage_analysis.forge import actions, cache, local
from storage_analysis.forge.catalog import build_catalog, match_entries
from storage_analysis.forge.clients import _next_link, parse_gitmodules
from storage_analysis.forge.models import RemoteRepo, RepoRef
from storage_analysis.forge.urls import forge_kind, normalize_remote

failures = []


def check(label, got, expected):
    ok = got == expected
    print(f"  {'OK ' if ok else 'ECHEC'}  {label}: {got!r}" + ("" if ok else f"  (attendu {expected!r})"))
    if not ok:
        failures.append(label)


# --------------------------------------------------------- normalisation --

print("\n--- normalisation des URL ---")
same = "gitlab.com/mon-groupe/ai_wrapper"
for label, url in [
    ("SCP", "git@gitlab.com:mon-groupe/ai_wrapper.git"),
    ("ssh", "ssh://git@gitlab.com:2222/mon-groupe/ai_wrapper.git"),
    ("https", "https://gitlab.com/mon-groupe/ai_wrapper.git"),
    ("https sans .git", "https://gitlab.com/mon-groupe/ai_wrapper"),
    ("avec utilisateur", "https://sacha@gitlab.com/mon-groupe/ai_wrapper.git"),
]:
    ref = normalize_remote(url)
    check(f"{label} -> clé commune", ref.key if ref else None, same)

check("casse de l'hôte", normalize_remote("git@GitHub.com:A/B.git").key, "github.com/A/B")
check("namespace profond",
      normalize_remote("git@h:a/b/c/d.git").path, "a/b/c/d")
for label, url in [("chemin Windows", r"D:\miroirs\projet"), ("posix", "/srv/git/projet"),
                   ("file://", "file:///srv/git/projet"), ("vide", ""), ("None", None),
                   ("sans namespace", "https://host/projet")]:
    check(f"non-forge : {label}", normalize_remote(url), None)

check("forge github", forge_kind("github.com"), "github")
check("forge gitlab.com", forge_kind("gitlab.com"), "gitlab")
check("forge gitlab auto-hébergé", forge_kind("gitlab.exemple.fr"), "gitlab")
check("forge inconnue", forge_kind("git.example.org"), "inconnu")

check("Link rel=next",
      _next_link('<https://api/x?page=2>; rel="next", <https://api/x?page=9>; rel="last"'),
      "https://api/x?page=2")
check("Link sans next", _next_link('<https://api/x?page=9>; rel="last"'), None)
check("Link absent", _next_link(None), None)


# ------------------------------------------------- arborescence fabriquée --

root = Path(tempfile.mkdtemp(prefix="sa-forge-"))


def make_repo(name: str, url: str | None, submodules: list[tuple[str, str, str]] = ()) -> Path:
    """Faux dépôt : un .git/config suffit pour tout ce qui est testé ici."""
    path = root / name
    (path / ".git").mkdir(parents=True)
    config = "[core]\n\tbare = false\n"
    if url:
        config += f'[remote "origin"]\n\turl = {url}\n\tfetch = +refs/heads/*\n'
    (path / ".git" / "config").write_text(config, encoding="utf-8")
    (path / "contenu.bin").write_bytes(b"x" * 4096)
    if submodules:
        text = ""
        for sub_name, rel, sub_url in submodules:
            text += f'[submodule "{sub_name}"]\n\tpath = {rel}\n\turl = {sub_url}\n'
        (path / ".gitmodules").write_text(text, encoding="utf-8")
    return path


parent = make_repo(
    "parent", "git@gitlab.com:grp/parent.git",
    submodules=[("libs/geo", "libs/geo", "git@gitlab.com:grp/geo.git"),
                ("vendor/x", "vendor/x", "https://github.com/tiers/x.git")],
)
make_repo("geo", "git@gitlab.com:grp/geo.git")
make_repo("solo", "git@gitlab.com:grp/solo.git")
make_repo("orphelin", None)

# Submodule réellement déployé sous le parent.
(parent / "libs" / "geo" / ".git").mkdir(parents=True)
(parent / "libs" / "geo" / ".git" / "config").write_text(
    '[remote "origin"]\n\turl = git@gitlab.com:grp/geo.git\n', encoding="utf-8")

print("\n--- découverte ---")
paths = local.discover([str(root)])
check("dépôts trouvés", len(paths), 4)
check("on ne descend pas dans un dépôt",
      any("libs" in p for p in paths), False)

repos = local.read_repos(paths, with_git=False, workers=1)
by_name = {r.name: r for r in repos}
check("remote lu depuis .git/config", by_name["geo"].ref.key, "gitlab.com/grp/geo")
check("dépôt sans remote", by_name["orphelin"].ref, None)
check("submodules déclarés", len(by_name["parent"].submodules), 2)
check("taille non nulle", by_name["solo"].size_bytes >= 4096, True)
check("submodule déployé détecté",
      list(local.iter_submodule_checkouts(by_name["parent"])) != [], True)

check("parse_gitmodules distant",
      [d.ref.key for d in parse_gitmodules(
          '[submodule "a"]\n\tpath = a\n\turl = git@h.fr:n/a.git\n') if d.ref],
      ["h.fr/n/a"])


# ------------------------------------------------------------- catalogue --

print("\n--- catalogue et graphe ---")
remotes = [
    RemoteRepo(ref=RepoRef("gitlab.com", "grp/parent"), forge="gitlab", project_id="1"),
    RemoteRepo(ref=RepoRef("gitlab.com", "grp/geo"), forge="gitlab", project_id="2"),
    RemoteRepo(ref=RepoRef("gitlab.com", "grp/solo"), forge="gitlab", project_id="3",
               archived=True),
    RemoteRepo(ref=RepoRef("gitlab.com", "grp/jamais-clone"), forge="gitlab", project_id="4"),
]
catalog = build_catalog(repos, remotes, {"gitlab.com"})

check("entrées totales", len(catalog), 5)  # 3 clonés + orphelin + 1 distant seul
check("statut cloné", catalog.entries["gitlab.com/grp/geo"].status, "cloné")
check("statut distant seul",
      catalog.entries["gitlab.com/grp/jamais-clone"].status, "distant seul")
check("statut orphelin",
      [e.status for e in catalog.entries.values() if e.ref is None], ["orphelin"])
check("archivé remonté", catalog.entries["gitlab.com/grp/solo"].archived, True)

check("arêtes de submodule", len(catalog.edges), 2)
geo_edge = [e for e in catalog.edges if e.child_key == "gitlab.com/grp/geo"][0]
check("enfant connu", geo_edge.child_known, True)
check("submodule déployé", geo_edge.checked_out, True)
tiers_edge = [e for e in catalog.edges if e.child_key == "github.com/tiers/x"][0]
check("enfant hors catalogue", tiers_edge.child_known, False)
check("racine du graphe", catalog.roots(), ["gitlab.com/grp/parent"])
check("parents de geo",
      [e.parent_key for e in catalog.parents_of("gitlab.com/grp/geo")],
      ["gitlab.com/grp/parent"])

print("\n--- garde-fous ---")
solo = catalog.entries["gitlab.com/grp/solo"]
geo = catalog.entries["gitlab.com/grp/geo"]
orphan = [e for e in catalog.entries.values() if e.ref is None][0]

check("solo supprimable", catalog.blockers(solo), [])
check("geo retenu car submodule",
      any("submodule" in r for r in catalog.blockers(geo)), True)
check("orphelin retenu car sans remote",
      any("aucun remote" in r for r in catalog.blockers(orphan)), True)
# « parent » est supprimable bien qu'il embarque geo : personne ne l'inclut, lui.
# geo reste retenu tant que parent existe — la règle se réévalue après chaque
# suppression, d'où l'invalidation du cache par la commande rm-local.
check("liste des supprimables", sorted(e.name for e in catalog.reclaimable()),
      ["parent", "solo"])
check("geo exclu des supprimables",
      "geo" in [e.name for e in catalog.reclaimable()], False)

local_repo = solo.local
local_repo.ahead = 3
check("commits non poussés bloquent",
      any("non poussé" in r for r in catalog.blockers(solo)), True)
local_repo.ahead = 0

# Forge non interrogée : ne doit PAS être lue comme « projet disparu ».
partial = build_catalog(repos, [], set())
partial_solo = [e for e in partial.entries.values() if e.name == "solo"][0]
check("forge non interrogée signalée comme telle",
      any("non interrogée" in r for r in partial.blockers(partial_solo)), True)
check("pas d'accusation de disparition",
      any("introuvable" in r for r in partial.blockers(partial_solo)), False)

print("\n--- actions : refus attendus ---")
res = actions.delete_local(geo, catalog, dry_run=True)
check("rm-local refusé sur submodule utilisé", res.ok, False)
res = actions.delete_local(geo, catalog, dry_run=True, force=True)
check("--force passe outre", res.ok, True)
res = actions.delete_local(solo, catalog, dry_run=True)
check("rm-local accepté sur dépôt sûr", res.ok, True)
check("place annoncée", res.freed_bytes >= 4096, True)

check("protection par motif exact",
      actions.is_protected(solo, ["grp/solo"]), "grp/solo")
check("protection par glob de namespace",
      actions.is_protected(solo, ["grp/*"]), "grp/*")
check("motif non concerné", actions.is_protected(solo, ["autre/*"]), None)


class _FakeClient:
    """Ne doit jamais être appelé : tous les cas testés sont des refus."""
    host = "gitlab.com"

    def delete(self, repo):
        raise AssertionError("suppression distante déclenchée malgré un garde-fou")

    def set_archived(self, repo, archived):
        raise AssertionError("archivage déclenché en simulation")


fake = _FakeClient()
distant = catalog.entries["gitlab.com/grp/jamais-clone"]

res = actions.delete_remote(solo, fake, confirmation=None, protected=[])
check("rm-remote sans confirmation", res.ok, False)
res = actions.delete_remote(solo, fake, confirmation="solo", protected=[])
check("rm-remote confirmation partielle refusée", res.ok, False)
res = actions.delete_remote(solo, fake, confirmation="grp/solo", protected=["grp/*"])
check("rm-remote namespace protégé", res.ok, False)
res = actions.delete_remote(distant, fake, confirmation="grp/jamais-clone", protected=[])
check("rm-remote refusé si dernière copie", res.ok, False)
res = actions.delete_remote(solo, fake, confirmation="grp/solo", protected=[], dry_run=True)
check("rm-remote simulation acceptée", res.ok, True)
res = actions.set_archived(solo, fake, True, dry_run=True)
check("archivage simulation acceptée", res.ok, True)

print("\n--- sélection par motif ---")
check("par nom", [e.name for e in match_entries(catalog, "solo")], ["solo"])
check("par chemin complet",
      [e.name for e in match_entries(catalog, "gitlab.com/grp/geo")], ["geo"])
check("par glob", sorted(e.name for e in match_entries(catalog, "*grp/*")),
      ["geo", "jamais-clone", "parent", "solo"])
check("sans correspondance", match_entries(catalog, "inexistant"), [])

print("\n--- OAuth : PKCE ---")
import base64 as _b64
import hashlib as _hashlib
import threading as _threading
import urllib.parse as _urlparse
import urllib.request as _urlrequest

from storage_analysis.forge import oauth

verifier, challenge = oauth.make_pkce_pair()
check("verifier dans les bornes RFC 7636", 43 <= len(verifier) <= 128, True)
check("verifier sans remplissage base64", verifier.endswith("="), False)
expected_challenge = _b64.urlsafe_b64encode(
    _hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
check("challenge = S256(verifier)", challenge, expected_challenge)
check("deux appels donnent deux verifiers", oauth.make_pkce_pair()[0] == verifier, False)

url = oauth.build_authorize_url("Ov23liTEST", ["repo", "read:user"], "etat123", challenge)
params = _urlparse.parse_qs(_urlparse.urlparse(url).query)
check("méthode de challenge", params["code_challenge_method"], ["S256"])
check("URI de rappel", params["redirect_uri"], ["http://127.0.0.1:8765/callback"])
check("portées transmises", params["scope"], ["repo read:user"])
check("le verifier ne part JAMAIS dans l'URL", verifier in url, False)

check("rappel valide", oauth.parse_callback("/callback?code=abc&state=etat123", "etat123"), "abc")
for label, path, state in [
    ("état falsifié", "/callback?code=abc&state=pirate", "etat123"),
    ("état absent", "/callback?code=abc", "etat123"),
    ("code absent", "/callback?state=etat123", "etat123"),
    ("refus utilisateur", "/callback?error=access_denied&state=etat123", "etat123"),
]:
    try:
        oauth.parse_callback(path, state)
        check(f"rappel rejeté : {label}", "accepté", "OAuthError")
    except oauth.OAuthError:
        check(f"rappel rejeté : {label}", True, True)

print("\n--- OAuth : boucle locale de bout en bout ---")
# On remplace le seul appel réseau : tout le reste (serveur local, validation
# d'état, échange du code contre le verifier) est exercé pour de vrai.
exchanged = {}


def _fake_post(url, data):
    exchanged.update(data)
    return {"access_token": "jeton-de-test", "scope": "repo,read:user", "token_type": "bearer"}


real_post = oauth._post
oauth._post = _fake_post
captured = {}


def _run():
    try:
        captured["result"] = oauth.pkce_login(
            "Ov23liTEST", ["repo", "read:user"], port=8799, timeout=20,
            on_prompt=lambda u: captured.setdefault("url", u))
    except oauth.OAuthError as exc:
        captured["error"] = str(exc)


thread = _threading.Thread(target=_run, daemon=True)
thread.start()
for _ in range(100):
    if "url" in captured:
        break
    __import__("time").sleep(0.05)

state = _urlparse.parse_qs(_urlparse.urlparse(captured["url"]).query)["state"][0]
with _urlrequest.urlopen(
        f"http://127.0.0.1:8799/callback?code=code-recu&state={state}", timeout=5) as response:
    page = response.read().decode("utf-8")
thread.join(timeout=10)

check("navigateur reçoit une page de succès", "C'est bon." in page, True)
check("jeton obtenu", captured.get("result").token if captured.get("result") else None, "jeton-de-test")
check("portées analysées", captured["result"].scopes, ["repo", "read:user"])
check("le verifier est bien envoyé à l'échange", "code_verifier" in exchanged, True)
check("aucun secret client envoyé", "client_secret" in exchanged, False)
check("code d'autorisation transmis", exchanged.get("code"), "code-recu")

oauth._post = real_post

check("portées par défaut sans suppression",
      oauth.build_scopes(), ["repo", "read:user"])
check("delete_repo sur demande explicite",
      oauth.DELETE_SCOPE in oauth.build_scopes(with_delete=True), True)

print("\n--- cache aller-retour ---")
cache_file = root / "cache.json"
cache.save(catalog, [str(root)], path=cache_file)
loaded = cache.load(path=cache_file)
check("cache relu", loaded is not None, True)
restored, age = loaded
check("entrées conservées", len(restored), len(catalog))
check("arêtes reconstruites", len(restored.edges), len(catalog.edges))
check("hôtes interrogés conservés", restored.queried_hosts, {"gitlab.com"})
check("garde-fous identiques",
      catalog.blockers(catalog.entries["gitlab.com/grp/geo"]),
      restored.blockers(restored.entries["gitlab.com/grp/geo"]))
check("cache périmé ignoré", cache.load(max_age=-1, path=cache_file), None)

print("\n--- boucle locale mutualisée ---")
from storage_analysis.forge import hub
from storage_analysis.forge.loopback import LoopbackError, LoopbackServer

# Les deux retours (Keycloak puis oauth-hub) arrivent sur le MÊME port, à la
# suite : c'est la propriété que ce test protège.
with LoopbackServer(8801) as loop:
    check("URL construite", loop.url("/callback"), "http://127.0.0.1:8801/callback")

    def _hit(url):
        try:
            with _urlrequest.urlopen(url, timeout=5) as r:
                return r.status, r.read().decode("utf-8")
        except Exception as exc:
            return getattr(exc, "code", 0), ""

    for path, query, label in [
        ("/callback", "code=c1&state=s1", "premier retour (Keycloak)"),
        ("/oauth-hub-done", "connected=github&scope=repo", "second retour (oauth-hub)"),
    ]:
        got = {}
        t = _threading.Thread(target=lambda: got.update(loop.wait_for(path, timeout=10)),
                              daemon=True)
        t.start()
        __import__("time").sleep(0.15)
        status_code, page = _hit(f"http://127.0.0.1:8801{path}?{query}")
        t.join(timeout=5)
        check(f"{label} capté", got, dict(_urlparse.parse_qsl(query)))
        check(f"{label} : page rendue", status_code, 200)

    check("chemin inattendu rejeté", _hit("http://127.0.0.1:8801/autre")[0], 404)

try:
    LoopbackServer(8801)  # le serveur precedent est ferme, on peut relier
    check("port libéré après fermeture", True, True)
except LoopbackError:
    check("port libéré après fermeture", False, True)

print("\n--- oauth-hub : configuration ---")
cfg = hub.HubConfig(issuer="https://lab.test/auth/realms/ssolab",
                    api_base="https://lab.test/oauth-hub-api")
check("URI de rappel (n°2)", cfg.redirect_uri, "http://127.0.0.1:8765/callback")
check("endpoint d'autorisation", cfg.auth_endpoint,
      "https://lab.test/auth/realms/ssolab/protocol/openid-connect/auth")
check("URL du jeton amont", cfg.provider_token_url("github"),
      "https://lab.test/oauth-hub-api/api/providers/github/token/")
check("URL d'état", cfg.provider_status_url("github"),
      "https://lab.test/oauth-hub-api/api/providers/github/status/")

import os as _os
_os.environ["LAB_DOMAIN"] = "lab.test"
from storage_analysis.forge import config as _config
derived = _config.load_config()
check("issuer déduit de LAB_DOMAIN", derived.hub_issuer, "https://lab.test/auth/realms/ssolab")
check("API déduite de LAB_DOMAIN", derived.hub_api, "https://lab.test/oauth-hub-api")
check("client par défaut", derived.hub_client_id, "storage-analysis")
del _os.environ["LAB_DOMAIN"]

print("\n--- oauth-hub : retours du courtier ---")
check("succès laissé passer",
      hub.check_return({"connected": "github", "scope": "repo"})["connected"], "github")
for label, params, expect in [
    ("erreur de config = fatale", {"oauth_error": "boum", "oauth_error_code": "exchange_failed"},
     "prévenez un dev"),
    ("refus utilisateur", {"oauth_error": "refusé", "oauth_error_code": "provider_refused"},
     "pas de nouvelle tentative"),
    ("état expiré", {"oauth_error": "expiré", "oauth_error_code": "expired_state"}, "expiré"),
]:
    try:
        hub.check_return(params)
        check(label, "accepté", "HubError")
    except hub.HubError as exc:
        check(label, expect in str(exc), True)

print("\n--- oauth-hub : 409 puis liaison ---")


class _Resp:
    def __init__(self, code, payload):
        self.status_code, self._payload, self.text = code, payload, str(payload)

    def json(self):
        return self._payload


calls = []
real_get = hub._authorized_get


def _fake_get(url, token, params=None):
    calls.append((url, params))
    if len(calls) == 1:                      # premier appel : pas encore relié
        return _Resp(409, {"detail": "non relié", "provider": "github",
                           "connect_url": "https://lab.test/connect?x=1"})
    return _Resp(200, {"access_token": "jeton-github", "scope": "read:user repo"})


hub._authorized_get = _fake_get
captured = {}


def _run_site_token():
    try:
        captured["payload"] = hub.site_token(
            hub.HubConfig(issuer="https://lab.test/auth/realms/ssolab",
                          api_base="https://lab.test/oauth-hub-api", port=8802),
            "jeton-keycloak", "github", timeout=15,
            on_prompt=lambda u: captured.setdefault("connect_url", u))
    except hub.HubError as exc:
        captured["error"] = str(exc)


t = _threading.Thread(target=_run_site_token, daemon=True)
t.start()
for _ in range(100):
    if "connect_url" in captured:
        break
    __import__("time").sleep(0.05)

check("connect_url remontée", captured.get("connect_url"), "https://lab.test/connect?x=1")
with _urlrequest.urlopen(
        "http://127.0.0.1:8802/oauth-hub-done?connected=github&scope=read:user+repo",
        timeout=5) as r:
    r.read()
t.join(timeout=10)

check("jeton obtenu après liaison",
      (captured.get("payload") or {}).get("access_token"), "jeton-github")
check("return_url passée au premier appel", calls[0][1], {"return_url":
      "http://127.0.0.1:8802/oauth-hub-done"})
check("second appel sans return_url", calls[1][1], None)

# Mode non interactif : jamais de navigateur ouvert dans une commande de listage.
calls.clear()
try:
    hub.site_token(cfg, "jeton", "github", interactive=False)
    check("409 non interactif -> erreur explicite", "accepté", "HubError")
except hub.HubError as exc:
    check("409 non interactif -> erreur explicite", "repos login" in str(exc), True)

hub._authorized_get = real_get

print("\n--- section « Dépôts » de l'interface ---")
# Le catalogue est une union : un dépôt jamais cloné doit apparaître au même
# titre qu'un clone local. C'est ce que ce bloc protège, sans aucun jeton.
from storage_analysis.ui import figures as ui_figures
from storage_analysis.ui import repos_view

rows = repos_view._rows(catalog, "all")
by_key = {r["cle"]: r for r in rows}
check("union local + distant", len(rows), len(catalog))
check("dépôt jamais cloné présent",
      "gitlab.com/grp/jamais-clone" in by_key, True)
check("son état", by_key["gitlab.com/grp/jamais-clone"]["etat"], "distant seul")
check("pas de chemin local", by_key["gitlab.com/grp/jamais-clone"]["chemin"], "")
check("état de la forge lu", by_key["gitlab.com/grp/solo"]["distant"], "archivé · privé")
check("actif et privé", by_key["gitlab.com/grp/geo"]["distant"], "actif · privé")

counts = {kind: len(repos_view._rows(catalog, kind)) for kind in repos_view.FILTERS}
check("filtre : tous", counts["all"], 5)
check("filtre : clonés", counts["local"], 4)
check("filtre : distants non clonés", counts["remote"], 1)
check("filtre : archivés", counts["archived"], 1)
check("filtre : libérables", counts["free"], 2)

# Sans forge interrogée, l'absence de distant ne doit pas se lire « disparu ».
muet = build_catalog(repos, [], set())
check("forge non interrogée signalée",
      repos_view._rows(muet, "all")[0]["distant"], "non interrogé")

figure = ui_figures.repos_bar(rows, "light")
names = [t.name for t in figure.data]
check("deux classes tracées", sorted(names), ["Retenu", "Supprimable sans perte"])
check("légende présente", figure.layout.legend is not None, True)
plotted = sum(len(t.x) for t in figure.data)
check("dépôt distant tracé aussi", plotted, len([r for r in rows if r["octets"]]))

graph = repos_view._graph_nodes(catalog)
check("graphe : racine + enfants", len(graph), 3)
check("racine du graphe", graph[0]["label"], "parent")
check("submodule déployé annoté", "déployé" in graph[1]["note"] or "déployé" in graph[2]["note"], True)

shutil.rmtree(root, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} vérification(s) en échec : {failures}")
    sys.exit(1)
print("Toutes les vérifications passent.")
