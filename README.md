# storage_analysis

Analyseur d'occupation disque : scan rapide, modèle compact, visualisations lisibles.

L'état de l'art des représentations, les choix de scan et l'architecture sont détaillés dans
[docs/CONCEPTION.md](docs/CONCEPTION.md).

---

## Installation

```powershell
.\install.ps1        # PowerShell
install.bat          # cmd.exe, ou double-clic depuis l'Explorateur
./install.sh         # Linux / macOS
```

`install.bat` est un relais vers `install.ps1` avec `-ExecutionPolicy Bypass` — utile quand la stratégie
d'exécution bloque les `.ps1`, et quand on lance depuis l'Explorateur. Il ne marque une pause en fin de
course que s'il a été **double-cliqué**, jamais sur un appel `cmd /c` automatisé.

Le script :

1. cherche un interpréteur Python ≥ 3.10, dans l'ordre **système** (lanceur `py`, PATH, registre,
   emplacements standards) → **Miniconda/Anaconda/Miniforge** → **Python embarqués** d'applications
   (Spyder, QGIS, OSGeo4W, ArcGIS Pro, Blender, FME) ;
2. crée le `.venv` local et y installe `requirements.txt` ;
3. écrit `BASE_PYTHON` et `VENV_PYTHON` dans `.env` (gitignoré) pour tous les lancements suivants.

| Option | Effet |
|---|---|
| `-ListOnly` | liste tous les interpréteurs détectés sans rien installer |
| `-Rescan` | ignore le `BASE_PYTHON` mémorisé et relance la détection |
| `-Force` | supprime et recrée le `.venv` |
| `-Python <chemin>` | impose un interpréteur précis |

Sous Linux/macOS, `./install.sh --force` recrée le venv.

## Utilisation

`run.ps1` (PowerShell), `run.bat` (cmd.exe) et `run.sh` (Linux/macOS) lisent `VENV_PYTHON` dans `.env` et
lancent l'application **avec cet interpréteur**, quel que soit le Python présent dans le PATH et quel que
soit le répertoire courant. `run.bat` attaque directement le Python du venv, sans passer par PowerShell.

```powershell
.\run.ps1 scan D:\                      # analyse un disque entier
.\run.ps1 scan . --tree-depth 3 --top 15 --extensions
.\run.ps1 scan C:\Users --size-mode allocated   # tailles arrondies au cluster
.\run.ps1 info snapshots\c-users-20260815-112359.npz
```

Sur Linux/macOS : `./run.sh scan ~`.

### Options principales

| Option | Défaut | Rôle |
|---|---|---|
| `-o, --out` | `snapshots/<racine>-<horodatage>.npz` | destination du snapshot |
| `--no-save` | — | ne pas écrire de snapshot |
| `-w, --workers` | auto | threads de scan (**1** est forcé sur disque rotatif) |
| `--size-mode` | `logical` | `allocated` arrondit au cluster du volume |
| `--max-depth` | — | limite la descente |
| `--top` / `--tree-depth` / `--min-share` | 10 / 2 / 0.01 | densité de l'arborescence affichée |
| `--files` | 15 | nombre de fichiers listés |
| `--extensions` | — | ajoute la répartition par type |

Un scan émet un snapshot réutilisable : `info` le relit sans retoucher au disque.

## Vérification

```powershell
.\.venv\Scripts\python.exe tests\verify_lot0.py    # scanner
.\.venv\Scripts\python.exe tests\verify_forge.py   # vue depots
```

`verify_lot0` fabrique une arborescence témoin (avec une jonction Windows) et contrôle les totaux, le non-suivi des liens,
l'équivalence mono/multi-thread, les tailles allouées, les cas limites et l'aller-retour snapshot.

`verify_forge` fabrique de faux dépôts et contrôle, **entièrement hors ligne**, la normalisation des URL,
la corrélation local/distant, le graphe de submodules, le cache, et chaque refus des garde-fous.

## Vue dépôts (GitHub / GitLab)

Un clone local occupe de la place ; savoir s'il est *supprimable* dépend de ce qui existe ailleurs — est-il
poussé, archivé, référencé comme submodule par un autre dépôt ? C'est l'objet de la commande `repos`.

```powershell
.
\run.ps1 repos check                 # configuration et validité des jetons
.
\run.ps1 repos list --sort size      # catalogue : clones locaux + projets distants
.
\run.ps1 repos graph                 # liens entre dépôts via submodules
.
\run.ps1 repos clean                 # ce qui part sans rien perdre, et ce qui est retenu
```

### Connexion GitHub

Deux voies. La première évite de créer le moindre identifiant d'application côté client.

**Via `oauth-hub`, le courtier du lab** (recommandé) — dans `.env` : `LAB_DOMAIN=mon-lab.exemple.fr`,
puis :

```powershell
.\run.ps1 repos login                    # authentifie sur le lab, puis relie le compte GitHub
.\run.ps1 repos login --provider gitlab   # même chose pour un autre site
```

Le montage met en jeu **trois URI de rappel** qu'il ne faut pas confondre : celle du fournisseur
pointe sur le serveur du lab (`https://<DOMAIN>/oauth-hub-api/api/callback/<slug>/`) et **jamais**
sur une boucle locale — sans quoi il faudrait distribuer le `client_secret` sur chaque poste. Les
deux autres sont locales et à nous : `/callback` pour le retour Keycloak, `/oauth-hub-done` pour le
retour du courtier. Elles partagent le même port et le même serveur éphémère.

Deux jetons circulent, et ils ne se renouvellent pas au même endroit : le **jeton de lab** (1 h) est
renouvelé ici, avec le `refresh_token` stocké dans `LAB_REFRESH_TOKEN` ; le **jeton GitHub** est
renouvelé par `oauth-hub`, et n'est donc **jamais stocké localement** — il est redemandé à chaque
besoin, ce qui est précisément le service rendu.

Prérequis côté lab : le client Keycloak `storage-analysis` existe (public, PKCE S256, rappel
`http://127.0.0.1:8765/callback`) et son `client_id` figure dans `KEYCLOAK_TRUSTED_CLIENTS` du
`.env` d'`oauth-hub`.

**En direct sur GitHub** — utile hors connexion au cadriciel :

```powershell
.\run.ps1 repos login --direct --client-id Ov23li...
.\run.ps1 repos login --direct --with-delete
.\run.ps1 repos logout
```

Code d'autorisation + PKCE contre `github.com`, sans *client secret*. Créez l'application sur
<https://github.com/settings/developers> avec `http://127.0.0.1:8765/callback` en Callback URL et
**« Enable Device Flow » décoché** — voir [la note de sécurité](docs/CONCEPTION.md). `--device`
existe pour les machines sans navigateur, avec avertissement : ce flux est *phishable*.

Un jeton personnel (PAT) placé dans `GITHUB_TOKEN` reste accepté dans tous les cas.

### Autres clés

Configuration dans `.env` (voir [.env.example](.env.example)) : `REPO_ROOTS`, `GITHUB_CLIENT_ID`,
`GITLAB_URL` + `GITLAB_TOKEN`, `FORGE_PROTECTED`. Ces clés sont **préservées** lors d'une réinstallation.

Le catalogue est mis en cache dans `snapshots/repos-cache.json` : mesurer plusieurs Go de clones sur un
disque rotatif froid prend des dizaines de secondes. `--refresh` force la reconstruction, `--offline` saute
les appels API, `--no-size` saute la mesure des tailles.

### Actions

| Commande | Portée | Réversible |
|---|---|---|
| `repos rm-local <dépôt>` | supprime le clone local vers la corbeille | oui, tant que la corbeille n'est pas vidée |
| `repos archive <dépôt>` | passe le projet distant en lecture seule | oui (`--undo`) |
| `repos rm-remote <dépôt>` | détruit le projet sur la forge | **non** |

`rm-local` refuse par défaut si le dépôt a des commits non poussés, des fichiers modifiés, aucun remote, ou
s'il est inclus comme submodule par un autre dépôt présent ; `--force` passe outre.

`rm-remote` cumule trois barrières : saisie du **chemin complet** à l'identique, refus si le namespace figure
dans `FORGE_PROTECTED`, et refus si aucun clone local ne subsiste (`--allow-last-copy` pour déroger).
Toute tentative, réussie ou non, est journalisée dans `logs/forge-actions.log`. `--dry-run` sur les trois.

## Structure

```
storage_analysis/
├── core/
│   ├── scanner.py     parcours scandir, threads, progression, erreurs par nœud
│   ├── tree.py        tableaux parallèles, agrégation, navigation, requêtes
│   ├── prune.py       élagage adaptatif — jamais plus de ~2 000 marques
│   ├── snapshot.py    persistance .npz
│   └── sysinfo.py     taille de cluster, HDD vs SSD
├── forge/
│   ├── local.py       découverte des clones, lecture de .git/config et .gitmodules
│   ├── clients.py     API GitHub et GitLab (pagination, quotas, erreurs)
│   ├── catalog.py     union local/distant, graphe de submodules, garde-fous
│   ├── actions.py     suppression locale, archivage, suppression distante
│   ├── cache.py       cache JSON du catalogue
│   └── render.py      vues texte du catalogue
├── ui/
│   ├── app.py         application Dash : mise en page, callbacks, nettoyage
│   ├── figures.py     treemap, icicle, barres — formes et encodages
│   ├── theme.py       palettes clair/sombre, gabarits Plotly
│   ├── window.py      fenêtre native (pywebview), repli navigateur
│   └── assets/app.css surfaces, chrome, table
├── cli.py             commandes ui / scan / info
├── cli_repos.py       commandes repos
├── fmt.py             formatage partagé
└── env.py             lecture du .env, garde-fou d'interpréteur
```

Le cœur ne dépend d'aucune interface : l'UI (Dash au lot 1) consomme le même modèle.

## Avancement

- [x] **Lot 0** — scanner, modèle compact, snapshots, CLI `scan` / `info`
- [ ] **Lot 1** — treemap squarifiée + table triée liées (Dash)
- [ ] **Lot 2** — icicle, top fichiers, extensions, carte d'âge
- [ ] **Lot 3** — export HTML autonome
- [ ] **Lot 4** — backend MFT (NTFS), doublons, diff de snapshots, suppression corbeille
- [x] **Lot 5** — vue dépôts : catalogue GitHub/GitLab, graphe de submodules, actions

## Limites connues

- Les **hardlinks** sont comptés à chaque occurrence : obtenir `st_ino` sous Windows impose d'ouvrir chaque
  fichier, ce sera un mode « précis » explicite au lot 4.
- `--size-mode allocated` arrondit au cluster mais ne tient pas compte de la compression NTFS ni des fichiers
  sparse (VHD, bases de données), qui demandent `GetCompressedFileSize` par fichier.
- Les liens et jonctions sont affichés mais jamais parcourus : leur cible est comptée là où elle réside.
- Les jetons sont stockés **en clair** dans `.env`, par choix explicite. Le fichier est gitignoré, mais il ne
  doit être ni copié ni joint à un rapport ; en cas de doute, révoquer le jeton côté forge.
- Les appels d'API en écriture (archivage, suppression distante) et la pagination des listages n'ont pas pu
  être exercés contre une instance réelle faute de jeton valide : seuls le chemin d'authentification et la
  remontée d'erreur l'ont été. À valider par un `repos archive --dry-run` puis un archivage réel sur un
  dépôt sans enjeu.
