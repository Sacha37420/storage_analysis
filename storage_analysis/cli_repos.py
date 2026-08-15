"""Commandes « repos » : catalogue des dépôts, graphe de submodules, actions."""

from __future__ import annotations

import argparse
import sys

from .fmt import count, duration, human
from .forge import actions, cache, local, render
from .forge.catalog import build_catalog, match_entries
from .forge.clients import ForgeError, parse_gitmodules
from .forge.config import build_clients, load_config, warn_token_hygiene


def _err(message: str) -> None:
    print(f"  {message}", file=sys.stderr)


# ------------------------------------------------------- construction du catalogue --

def _roots(args: argparse.Namespace) -> list[str]:
    if getattr(args, "root", None):
        return list(args.root)
    roots = load_config().repo_roots
    if not roots:
        _err("Aucune racine de recherche : passez --root, ou renseignez REPO_ROOTS dans .env")
        _err(r"    REPO_ROOTS=D:\github;D:\gitlab")
    return roots


def _load_catalog(args: argparse.Namespace):
    """Catalogue depuis le cache, ou reconstruit. Renvoie None en cas d'échec."""
    refresh = getattr(args, "refresh", False)
    if not refresh:
        cached = cache.load(max_age=getattr(args, "max_age", 3600))
        if cached is not None:
            catalog, age = cached
            _err(f"catalogue en cache ({duration(age)} d'ancienneté, --refresh pour recharger)")
            return catalog

    roots = _roots(args)
    if not roots:
        return None

    _err(f"recherche des dépôts sous {', '.join(roots)}")
    paths = local.discover(roots)
    if not paths:
        _err("aucun dépôt git trouvé")
        return None

    quiet = getattr(args, "quiet", False)

    def progress(done: int, total: int, name: str) -> None:
        if not quiet:
            sys.stderr.write(f"\r  lecture {done}/{total} — {name[:40]:<40}")
            sys.stderr.flush()

    repos = local.read_repos(
        paths, with_size=not getattr(args, "no_size", False), on_progress=progress
    )
    if not quiet:
        sys.stderr.write("\r" + " " * 70 + "\r")

    remotes = []
    queried: set[str] = set()
    if not getattr(args, "offline", False):
        remotes, queried = _fetch_remotes(args)

    catalog = build_catalog(repos, remotes, queried)
    cache.save(catalog, roots)
    return catalog


def _fetch_remotes(args: argparse.Namespace):
    """Interroge les forges configurées. Une panne d'une forge n'empêche pas l'autre."""
    try:
        clients = build_clients(on_note=_err)
    except ValueError as exc:
        _err(str(exc))
        return [], set()

    if not clients:
        return [], set()

    remotes = []
    queried: set[str] = set()
    want_submodules = getattr(args, "submodules", False)

    for client in clients:
        try:
            _err(f"interrogation de {client.host}...")
            projects = client.list_repos()
        except ForgeError as exc:
            _err(f"{exc}")
            continue

        queried.add(client.host)

        if want_submodules:
            for index, project in enumerate(projects, start=1):
                sys.stderr.write(f"\r  .gitmodules {index}/{len(projects)}   ")
                content = client.fetch_gitmodules(project)
                if content:
                    project.submodules = parse_gitmodules(content)
            sys.stderr.write("\r" + " " * 40 + "\r")

        remotes.extend(projects)
        _err(f"{client.host} : {count(len(projects))} projet(s)")

    return remotes, queried


def _select_one(catalog, pattern: str):
    """Résout un motif vers une entrée unique, ou explique l'ambiguïté."""
    matches = match_entries(catalog, pattern)
    if not matches:
        _err(f"aucun dépôt ne correspond à « {pattern} »")
        return None
    if len(matches) > 1:
        _err(f"« {pattern} » correspond à {len(matches)} dépôts — précisez :")
        for entry in sorted(matches, key=lambda e: e.key)[:10]:
            _err(f"    {entry.key}")
        return None
    return matches[0]


def _client_for(entry):
    """Client de la forge hébergeant cette entrée."""
    if entry.remote is None or entry.ref is None:
        _err("aucun projet distant connu pour cette entrée (lancez « repos list --refresh »)")
        return None
    for client in build_clients():
        if client.host == entry.ref.host:
            return client
    _err(f"aucun jeton configuré pour {entry.ref.host}")
    return None


# ------------------------------------------------------------------ commandes --

_OAUTH_APP_HOWTO = """
  Aucun GITHUB_CLIENT_ID configuré. Créez une application OAuth, une fois pour toutes :

    1. https://github.com/settings/developers  >  OAuth Apps  >  New OAuth App
    2. Application name  : storage_analysis (ce que vous voulez)
       Homepage URL      : https://github.com/Sacha37420/storage_analysis
       Callback URL      : http://127.0.0.1:8765/callback
                           <- exactement cette valeur, port compris
    3. NE cochez PAS « Enable Device Flow ».
       Sans redirection, n'importe qui peut se servir de votre Client ID pour
       faire autoriser VOTRE application par une victime. Laissée décochée,
       cette attaque est impossible.
    4. Copiez le Client ID (public, ce n'est pas un secret) :

         .\\run.ps1 repos login --client-id Ov23li...

  Le Client Secret n'est jamais nécessaire : PKCE le remplace.
"""

_NO_AUTH_HOWTO = """
  Aucune authentification configurée. Deux voies, la première est préférable :

  1. Via oauth-hub, le courtier du lab — aucun identifiant d'application à créer,
     et le même montage servira pour GitLab, Google, etc. Dans .env :

         LAB_DOMAIN=mon-lab.exemple.fr
         HUB_CLIENT_ID=storage-analysis      (défaut, à ajuster si besoin)
         HUB_PORT=8765                       (défaut, doit correspondre à Keycloak)

     Prérequis côté lab : le client Keycloak « storage-analysis » existe, et son
     client_id figure dans KEYCLOAK_TRUSTED_CLIENTS du .env d'oauth-hub.
     Puis :  .\\run.ps1 repos login

  2. En direct sur GitHub, sans le lab — utile hors connexion au cadriciel :

         .\\run.ps1 repos login --direct --client-id Ov23li...

  Un jeton personnel (PAT) placé dans GITHUB_TOKEN reste également accepté.
"""


def cmd_login(args: argparse.Namespace) -> int:
    config = load_config()
    hub_config = config.hub()

    if hub_config is not None and not args.direct:
        return _login_hub(args, config, hub_config)
    if args.direct or config.github_client_id:
        return _login_direct(args, config)

    print(_NO_AUTH_HOWTO, file=sys.stdout)
    return 1


def _open(url: str, no_browser: bool) -> None:
    print(file=sys.stdout)
    print("    Ouvrez cette page si elle ne s'ouvre pas seule :", file=sys.stdout)
    print(f"    {url}", file=sys.stdout)
    print(file=sys.stdout)
    sys.stdout.flush()
    if not no_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass


def _login_hub(args: argparse.Namespace, config, hub_config) -> int:
    """Connexion via oauth-hub : jeton de lab, puis jeton du site."""
    from . import env as env_module
    from .forge import hub

    print(f"\n  Connexion au lab — {hub_config.issuer}", file=sys.stdout)
    print(f"  Client « {hub_config.client_id} », rappel {hub_config.redirect_uri}", file=sys.stdout)

    try:
        tokens = hub.login(hub_config, on_prompt=lambda u: _open(u, args.no_browser))
    except hub.HubError as exc:
        _err(str(exc))
        return 1
    except KeyboardInterrupt:
        _err("annulé.")
        return 130

    if tokens.refresh_token:
        env_module.set_env_values(
            {"LAB_REFRESH_TOKEN": tokens.refresh_token},
            comment="Session lab - ecrite par « repos login », ne pas partager.",
        )
    else:
        _err("Keycloak n'a pas fourni de refresh_token : la session ne survivra pas au processus.")

    print("  Authentifié sur le lab.", file=sys.stdout)
    print(f"\n  Liaison du compte {args.provider} via oauth-hub", file=sys.stdout)

    try:
        payload = hub.site_token(
            hub_config, tokens.access_token, args.provider,
            on_prompt=lambda u: _open(u, args.no_browser),
        )
    except hub.HubError as exc:
        _err(str(exc))
        return 1
    except KeyboardInterrupt:
        _err("annulé.")
        return 130

    scopes = payload.get("scope") or ""
    print(f"  Jeton {args.provider} obtenu — portées : {scopes or '(non déclarées)'}", file=sys.stdout)
    if "delete_repo" not in scopes:
        print("  « repos rm-remote » sera refusé : la portée delete_repo n'est pas accordée.",
              file=sys.stdout)
        print("  Elle se demande côté oauth-hub, dans les portées par défaut du site.",
              file=sys.stdout)

    if args.provider == "github":
        from .forge.clients import GitHubClient
        try:
            print(f"  Connecté en tant que {GitHubClient(payload['access_token']).check()}",
                  file=sys.stdout)
        except ForgeError as exc:
            _err(str(exc))
            return 1

    print(f"\n  Aucun jeton de site n'est stocké localement : il est redemandé au courtier,",
          file=sys.stdout)
    print("  qui se charge de son renouvellement.\n", file=sys.stdout)
    return 0


def _login_direct(args: argparse.Namespace, config) -> int:
    """Connexion directe à GitHub, sans le lab. Repli hors connexion au cadriciel."""
    from . import env as env_module
    from .forge import oauth

    client_id = args.client_id or config.github_client_id
    if not client_id:
        print(_OAUTH_APP_HOWTO, file=sys.stdout)
        return 1

    extra = [s.strip() for s in (args.scopes or "").replace(",", " ").split() if s.strip()]
    scopes = oauth.build_scopes(with_delete=args.with_delete, extra=extra)

    mode = "device flow" if args.device else "code d'autorisation + PKCE"
    print(f"\n  Connexion directe à GitHub — {mode}", file=sys.stdout)
    print(f"  Portées demandées : {' '.join(scopes)}", file=sys.stdout)

    try:
        if args.device:
            print("\n  ! Le device flow est phishable : quiconque connaît ce Client ID peut",
                  file=sys.stdout)
            print("    faire autoriser cette application par un tiers. À réserver aux machines",
                  file=sys.stdout)
            print("    sans navigateur.", file=sys.stdout)
            device = oauth.request_device_code(client_id, scopes)
            print(file=sys.stdout)
            print(f"    1. ouvrez  {device.verification_uri}", file=sys.stdout)
            print(f"    2. saisissez le code   {device.user_code}", file=sys.stdout)
            print(f"       (valable {device.expires_in // 60} minutes)", file=sys.stdout)
            print(file=sys.stdout)
            sys.stdout.flush()
            if not args.no_browser:
                try:
                    import webbrowser
                    webbrowser.open(device.verification_uri)
                except Exception:
                    pass

            def waiting(remaining: int) -> None:
                sys.stderr.write(
                    f"\r  en attente de votre autorisation... {remaining // 60}:{remaining % 60:02d}  ")
                sys.stderr.flush()

            result = oauth.poll_for_token(client_id, device, on_wait=waiting)
        else:
            result = oauth.pkce_login(
                client_id, scopes, port=args.port,
                on_prompt=lambda u: _open(u, args.no_browser))
    except oauth.OAuthError as exc:
        sys.stderr.write("\r" + " " * 60 + "\r")
        _err(str(exc))
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("\r" + " " * 60 + "\r")
        _err("annulé.")
        return 130
    sys.stderr.write("\r" + " " * 60 + "\r")

    env_module.set_env_values(
        {
            "GITHUB_CLIENT_ID": client_id,
            "GITHUB_TOKEN": result.token,
            "GITHUB_TOKEN_SOURCE": "oauth-device" if args.device else "oauth-pkce",
            "GITHUB_TOKEN_SCOPES": " ".join(result.scopes or scopes),
        },
        comment="Connexion GitHub directe - ecrit par « repos login --direct ».",
    )

    print(f"  Jeton enregistré dans {env_module.ENV_FILE}", file=sys.stdout)
    if result.scopes:
        print(f"  Portées accordées : {' '.join(result.scopes)}", file=sys.stdout)
    if oauth.DELETE_SCOPE not in (result.scopes or scopes):
        print("  « repos rm-remote » restera refusé : relancez avec --with-delete si besoin.",
              file=sys.stdout)

    try:
        for client in build_clients():
            if client.host == "github.com":
                print(f"  Connecté en tant que {client.check()}", file=sys.stdout)
    except ForgeError as exc:
        _err(str(exc))
        return 1

    print(file=sys.stdout)
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    from . import env as env_module

    config = load_config()
    if not config.github_token:
        print("\n  Aucun jeton GitHub enregistré.\n", file=sys.stdout)
        return 0

    env_module.set_env_values({"GITHUB_TOKEN": "", "GITHUB_TOKEN_SCOPES": ""})
    print(f"\n  Jeton retiré de {env_module.ENV_FILE}", file=sys.stdout)
    print("  Le jeton reste valide côté GitHub tant qu'il n'est pas révoqué :", file=sys.stdout)
    print("    https://github.com/settings/applications\n", file=sys.stdout)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    config = load_config()
    print(file=sys.stdout)
    print("  Configuration", file=sys.stdout)
    print(f"    racines      : {', '.join(config.repo_roots) or '(non défini)'}", file=sys.stdout)

    if config.hub_configured:
        session = "session active" if config.lab_refresh_token else "non connecté"
        print(f"    oauth-hub    : {config.hub_api}", file=sys.stdout)
        print(f"    lab (OIDC)   : {config.hub_issuer}", file=sys.stdout)
        print(f"    client       : {config.hub_client_id} — {session}", file=sys.stdout)
        print(f"    rappel local : http://127.0.0.1:{config.hub_port}/callback", file=sys.stdout)
    else:
        print("    oauth-hub    : non configuré (LAB_DOMAIN absent du .env)", file=sys.stdout)

    print(f"    GitHub       : {'jeton local présent' if config.github_token else 'aucun jeton local'}", file=sys.stdout)
    print(f"    GitLab       : {config.gitlab_url or '(URL non définie)'} — "
          f"{'jeton présent' if config.gitlab_token else 'aucun jeton'}", file=sys.stdout)
    print(f"    protégés     : {', '.join(config.protected) or '(aucun motif)'}", file=sys.stdout)

    for warning in warn_token_hygiene():
        print(f"    ! {warning}", file=sys.stdout)

    try:
        clients = build_clients(config)
    except ValueError as exc:
        _err(str(exc))
        return 1

    if not clients:
        print("\n  Aucune forge configurée : ajoutez GITHUB_TOKEN et/ou GITLAB_TOKEN au .env.",
              file=sys.stdout)
        return 1

    print(file=sys.stdout)
    print("  Connexions", file=sys.stdout)
    failed = 0
    for client in clients:
        try:
            identity = client.check()
            print(f"    {client.host:<24} ok — connecté en tant que {identity}", file=sys.stdout)
        except ForgeError as exc:
            print(f"    {client.host:<24} ECHEC — {exc}", file=sys.stdout)
            failed += 1
    print(file=sys.stdout)
    return 1 if failed else 0


def cmd_list(args: argparse.Namespace) -> int:
    catalog = _load_catalog(args)
    if catalog is None:
        return 1
    render.render_summary(catalog)
    render.render_table(catalog, sort=args.sort, limit=args.limit)
    print(file=sys.stdout)
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    catalog = _load_catalog(args)
    if catalog is None:
        return 1
    render.render_graph(catalog)
    print(file=sys.stdout)
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    catalog = _load_catalog(args)
    if catalog is None:
        return 1
    render.render_summary(catalog)
    render.render_reclaimable(catalog, limit=args.limit or 20)
    print(file=sys.stdout)
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    catalog = _load_catalog(args)
    if catalog is None:
        return 1
    entry = _select_one(catalog, args.repo)
    if entry is None:
        return 1
    client = _client_for(entry)
    if client is None:
        return 1

    outcome = actions.set_archived(entry, client, not args.undo, dry_run=args.dry_run)
    print(f"\n  {'[simulation] ' if args.dry_run else ''}{outcome.message}\n")
    return 0 if outcome.ok else 1


def cmd_rm_local(args: argparse.Namespace) -> int:
    catalog = _load_catalog(args)
    if catalog is None:
        return 1
    entry = _select_one(catalog, args.repo)
    if entry is None:
        return 1

    outcome = actions.delete_local(
        entry, catalog, force=args.force, dry_run=args.dry_run, to_trash=not args.permanent
    )
    prefix = "[simulation] " if args.dry_run else ""
    print(f"\n  {prefix}{outcome.message}")
    if outcome.ok and outcome.freed_bytes:
        print(f"  {human(outcome.freed_bytes)} récupérés")
    if outcome.blocked_by:
        print("  --force passe outre, après vérification de votre côté.")
    print()
    if outcome.ok and not args.dry_run:
        cache.CACHE_PATH.unlink(missing_ok=True)  # le catalogue n'est plus à jour
    return 0 if outcome.ok else 1


def cmd_rm_remote(args: argparse.Namespace) -> int:
    catalog = _load_catalog(args)
    if catalog is None:
        return 1
    entry = _select_one(catalog, args.repo)
    if entry is None:
        return 1
    client = _client_for(entry)
    if client is None:
        return 1

    expected = entry.ref.path if entry.ref else entry.key
    confirmation = args.confirm
    if confirmation is None and not args.dry_run and sys.stdin.isatty():
        print(f"\n  Suppression IRRÉVERSIBLE de {entry.key} sur {entry.remote.forge}.")
        if entry.local is not None:
            print(f"  Un clone local subsiste : {entry.local.path}")
        print(f"  Saisissez exactement « {expected} » pour confirmer, ou rien pour annuler.")
        try:
            confirmation = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Annulé.\n")
            return 1

    outcome = actions.delete_remote(
        entry, client,
        confirmation=confirmation,
        protected=load_config().protected,
        dry_run=args.dry_run,
        allow_last_copy=args.allow_last_copy,
    )
    print(f"\n  {'[simulation] ' if args.dry_run else ''}{outcome.message}\n")
    if outcome.ok and not args.dry_run:
        cache.CACHE_PATH.unlink(missing_ok=True)
    return 0 if outcome.ok else 1


# ------------------------------------------------------------------ parseur --

def register(subparsers: argparse._SubParsersAction) -> None:
    repos = subparsers.add_parser(
        "repos", help="catalogue des dépôts GitHub / GitLab et de leurs clones"
    )
    actions_parser = repos.add_subparsers(dest="repos_command", required=True)

    def add_catalog_options(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--root", action="append", help="racine de recherche (répétable)")
        sub.add_argument("--refresh", action="store_true", help="ignorer le cache")
        sub.add_argument("--max-age", type=float, default=3600,
                         help="âge maximal du cache en secondes (défaut : 3600)")
        sub.add_argument("--offline", action="store_true", help="ne pas interroger les forges")
        sub.add_argument("--no-size", action="store_true",
                         help="ne pas mesurer la taille des clones (bien plus rapide)")
        sub.add_argument("--submodules", action="store_true",
                         help="lire aussi les .gitmodules distants (un appel API par projet)")
        sub.add_argument("-q", "--quiet", action="store_true", help="pas de progression")

    login = actions_parser.add_parser(
        "login", help="connexion GitHub par OAuth (code d'autorisation + PKCE)"
    )
    login.add_argument("--direct", action="store_true",
                       help="ignorer oauth-hub et s'authentifier directement sur GitHub")
    login.add_argument("--provider", default="github",
                       help="slug du site à relier via oauth-hub (défaut : github)")
    login.add_argument("--client-id", help="Client ID de l'application OAuth (mode --direct)")
    login.add_argument("--with-delete", action="store_true",
                       help="demander aussi la portée delete_repo, requise par rm-remote")
    login.add_argument("--scopes", help="portées supplémentaires, séparées par des virgules")
    login.add_argument("--no-browser", action="store_true",
                       help="ne pas ouvrir le navigateur automatiquement")
    login.add_argument("--port", type=int, default=8765,
                       help="port de rappel local, à enregistrer aussi côté GitHub (défaut : 8765)")
    login.add_argument("--device", action="store_true",
                       help="utiliser le device flow au lieu de PKCE — machines sans navigateur "
                            "uniquement, ce flux est phishable")
    login.set_defaults(func=cmd_login)

    logout = actions_parser.add_parser("logout", help="retirer le jeton GitHub du .env")
    logout.set_defaults(func=cmd_logout)

    check = actions_parser.add_parser("check", help="vérifier la configuration et les jetons")
    check.set_defaults(func=cmd_check)

    listing = actions_parser.add_parser("list", help="lister les dépôts")
    add_catalog_options(listing)
    listing.add_argument("--sort", choices=("size", "activity", "name"), default="size")
    listing.add_argument("--limit", type=int, default=0, help="nombre de lignes (0 = tout)")
    listing.set_defaults(func=cmd_list)

    graph = actions_parser.add_parser("graph", help="graphe des submodules")
    add_catalog_options(graph)
    graph.set_defaults(func=cmd_graph)

    clean = actions_parser.add_parser("clean", help="ce qui peut être supprimé sans perte")
    add_catalog_options(clean)
    clean.add_argument("--limit", type=int, default=20)
    clean.set_defaults(func=cmd_clean)

    archive = actions_parser.add_parser("archive", help="archiver un projet distant")
    add_catalog_options(archive)
    archive.add_argument("repo", help="nom, chemin ou motif du dépôt")
    archive.add_argument("--undo", action="store_true", help="désarchiver au lieu d'archiver")
    archive.add_argument("--dry-run", action="store_true")
    archive.set_defaults(func=cmd_archive)

    rm_local = actions_parser.add_parser("rm-local", help="supprimer le clone local")
    add_catalog_options(rm_local)
    rm_local.add_argument("repo", help="nom, chemin ou motif du dépôt")
    rm_local.add_argument("--force", action="store_true",
                          help="passer outre les garde-fous (travail non poussé, submodule utilisé)")
    rm_local.add_argument("--permanent", action="store_true",
                          help="supprimer définitivement au lieu d'envoyer à la corbeille")
    rm_local.add_argument("--dry-run", action="store_true")
    rm_local.set_defaults(func=cmd_rm_local)

    rm_remote = actions_parser.add_parser(
        "rm-remote", help="supprimer le projet sur la forge (IRRÉVERSIBLE)"
    )
    add_catalog_options(rm_remote)
    rm_remote.add_argument("repo", help="nom, chemin ou motif du dépôt")
    rm_remote.add_argument("--confirm", help="chemin complet, à saisir à l'identique")
    rm_remote.add_argument("--allow-last-copy", action="store_true",
                           help="autoriser même sans clone local de sauvegarde")
    rm_remote.add_argument("--dry-run", action="store_true")
    rm_remote.set_defaults(func=cmd_rm_remote)
