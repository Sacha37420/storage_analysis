# storage_analysis

Une application de bureau pour comprendre ce qui remplit un disque, et le nettoyer sans rien perdre.

Elle répond à deux questions que les outils habituels traitent séparément :

- **où est passée la place ?** — treemap, icicle et répartition par type, sur la même arborescence ;
- **qu'est-ce que je peux supprimer ?** — pour un dépôt git, la réponse ne se lit pas sur le disque mais
  sur GitHub ou GitLab : est-il poussé, archivé, inclus comme submodule ailleurs ?

Les choix de conception — pourquoi un treemap squarifié plutôt qu'un sunburst, pourquoi `scandir` et
pas la MFT, pourquoi un seul fil sur disque rotatif — sont détaillés dans
[docs/CONCEPTION.md](docs/CONCEPTION.md).

---

## Installation

```powershell
.\install.ps1        # PowerShell
install.bat          # cmd.exe, ou double-clic depuis l'Explorateur
./install.sh         # Linux / macOS
```

Le script se débrouille seul :

1. il **cherche un Python ≥ 3.10** dans cet ordre — système (lanceur `py`, PATH, registre,
   emplacements standards), puis Miniconda / Anaconda / Miniforge, puis les Python **embarqués**
   d'applications (Spyder, QGIS, OSGeo4W, ArcGIS Pro, Blender, FME) ;
2. il crée le `.venv` local et y installe les dépendances ;
3. il écrit les chemins retenus dans un `.env` **gitignoré**, que tous les lancements réutilisent.

| Option | Effet |
|---|---|
| `-ListOnly` | liste les interpréteurs détectés sans rien installer |
| `-Rescan` | oublie le Python mémorisé et relance la détection |
| `-Force` | supprime et recrée le `.venv` |
| `-Python <chemin>` | impose un interpréteur précis |

Les clés que vous ajoutez vous-même dans `.env` (jetons, racines de recherche) sont **préservées**
lors d'une réinstallation.

## Lancement

```powershell
.\run.ps1            # ouvre la fenêtre
run.bat              # idem — et un double-clic depuis l'Explorateur fonctionne aussi
./run.sh             # Linux / macOS
```

Une fenêtre native s'ouvre (WebView2 via pywebview) ; à défaut, l'application bascule dans le
navigateur plutôt que d'échouer. `--browser` force ce mode.

Les lanceurs lisent le `.env` et utilisent **l'interpréteur du venv**, quel que soit le Python
présent dans le PATH et quel que soit le répertoire courant.

---

## Ce qu'on peut faire

L'application a deux sections, choisies par l'onglet **Disque / Dépôts** en haut à gauche.

### Analyser un disque

Saisissez un chemin — un dossier, une lettre de lecteur, ou un instantané `.npz` déjà calculé — puis
**Analyser**. Le scan tourne en tâche de fond avec sa progression : sur un disque rotatif il dure des
minutes, et l'interface reste utilisable pendant ce temps.

Trois vues du **même dossier courant**, à gauche :

| Vue | Ce qu'elle montre |
|---|---|
| **Treemap** | où est la masse — surface proportionnelle à la taille, pavage squarifié |
| **Icicle** | la structure et la profondeur des chemins, libellés horizontaux |
| **Extensions** | la répartition par type de fichier |

À droite, la table du dossier courant : taille, part du parent, nombre de fichiers, date. **Cliquer
dans le treemap déplace la table**, le fil d'Ariane suit, « ↑ Remonter » revient au parent. C'est le
principe de l'outil : une vue pour repérer, une vue pour agir.

Deux réglages changent la lecture :

- **couleur par profondeur** pour comprendre la structure, **par ancienneté** pour repérer les
  données froides — souvent le meilleur critère de suppression ;
- **taille logique** ou **taille allouée**, cette dernière arrondissant au cluster du volume, ce qui
  corrige le biais des arborescences à millions de petits fichiers (`node_modules`, dépôts git).

### Nettoyer

Cochez dans la table, vérifiez l'espace annoncé, puis **Envoyer à la corbeille**. Une confirmation
liste ce qui va partir. Rien n'est effacé définitivement, et ce qui disparaît est retiré des vues
immédiatement, sans relancer de scan.

### Suivre ses dépôts

La section **Dépôts** met côte à côte vos clones locaux et vos projets GitHub / GitLab, corrélés par
une clé normalisée : les trois écritures d'un même remote (`git@…`, `ssh://…`, `https://…`) désignent
bien le même dépôt.

Les barres classent les dépôts par taille et **mettent en avant ce qui est supprimable sans perte**.
La table ajoute l'état, la part d'historique `.git`, l'activité, et surtout la colonne **« Ce qui
retient »** : commits non poussés, fichiers modifiés, aucun remote, ou inclusion comme submodule par
un autre dépôt. L'onglet **Submodules** montre ces liens en pavage hiérarchique.

Deux filtres se croisent : par **forge** (`github.com`, votre GitLab, ou les dépôts sans remote) et
par **état** (clonés, distants non clonés, archivés, libérables).

Actions disponibles :

| Action | Portée | Réversible |
|---|---|---|
| Supprimer le clone local | le disque uniquement | oui, via la corbeille |
| Archiver / Désarchiver | le projet distant passe en lecture seule | oui |
| Supprimer le dépôt distant | destruction sur la forge | **non** |

La suppression distante ne s'active que sur **une** entrée à la fois et exige de **saisir le chemin
complet à l'identique**. Les namespaces listés dans `FORGE_PROTECTED` sont refusés d'office, l'absence
de clone local est signalée comme « dernière copie connue », et toute tentative est journalisée dans
`logs/forge-actions.log`.

### Se connecter à une forge

Sans jeton, l'application ne voit que vos clones locaux — et le dit, plutôt que d'afficher un `0`
ambigu. Deux voies :

**Via un courtier OAuth** (recommandé si vous en avez un). Renseignez le domaine dans le champ
**Lab** de la section Dépôts : l'issuer, l'API et l'URI de rappel s'en déduisent et sont affichés
pour vérification. Choisissez le site, cliquez **Connecter**, autorisez dans le navigateur. Aucun
identifiant ni secret n'est stocké ici — ils vivent dans le courtier. Le compte connecté est affiché
en permanence, et **Déconnecter** efface la session du poste.

**En direct sur GitHub**, sans courtier : créez une application OAuth sur
<https://github.com/settings/developers> avec `http://127.0.0.1:8765/callback` en callback, laissez
« Enable Device Flow » **décoché**, puis mettez son Client ID dans `GITHUB_CLIENT_ID`. Le flux utilise
le code d'autorisation avec PKCE — aucun *client secret* n'est nécessaire.

Un jeton personnel placé dans `GITHUB_TOKEN` reste accepté dans tous les cas.

---

## En ligne de commande

Tout ce que fait l'interface est disponible au clavier, et quelques opérations n'existent que là.

```powershell
.\run.ps1 scan D:\ --extensions              # analyse, résumé texte, instantané
.\run.ps1 info snapshots\mon-scan.npz        # relire sans retoucher au disque
.\run.ps1 repos list --sort size             # catalogue
.\run.ps1 repos graph                        # liens entre dépôts
.\run.ps1 repos clean                        # ce qui part sans rien perdre, et ce qui est retenu
.\run.ps1 repos check                        # état de la configuration et des jetons
.\run.ps1 repos login  /  logout
.\run.ps1 repos archive <dépôt> [--undo]
.\run.ps1 repos rm-local <dépôt> [--force]
.\run.ps1 repos rm-remote <dépôt>            # IRRÉVERSIBLE, confirmation par saisie
```

`--dry-run` existe sur les trois actions. `--help` détaille chaque commande.

## Configuration

Tout vit dans `.env`, gitignoré — voir [.env.example](.env.example) pour le détail commenté.

| Clé | Rôle |
|---|---|
| `REPO_ROOTS` | où chercher les clones locaux (séparateur `;`) |
| `LAB_DOMAIN` | domaine du courtier OAuth ; le reste s'en déduit |
| `GITHUB_CLIENT_ID` | application OAuth, pour la connexion directe |
| `GITHUB_TOKEN` / `GITLAB_URL` / `GITLAB_TOKEN` | jetons personnels, si vous préférez |
| `FORGE_PROTECTED` | namespaces refusés à la suppression distante (motifs glob) |

> Les jetons y sont **en clair**. Le fichier est gitignoré, mais ne le copiez pas ailleurs et ne le
> joignez à aucun rapport ; en cas de doute, révoquez le jeton côté forge.

## Vérification

```powershell
.\.venv\Scripts\python.exe tests\verify_lot0.py    # scanner et modèle
.\.venv\Scripts\python.exe tests\verify_forge.py   # vue dépôts
```

Les deux suites tournent **hors ligne**, sans jeton ni réseau. La première fabrique une arborescence
témoin — jonction Windows comprise — et vérifie les totaux, le non-suivi des liens, l'équivalence
mono/multi-thread et l'aller-retour instantané. La seconde fabrique de faux dépôts et contrôle la
normalisation des URL, la corrélation local/distant, le graphe de submodules, le flux OAuth jusqu'à
la boucle locale, et **chaque refus des garde-fous**.

## Structure

```
storage_analysis/
├── core/          scanner, modèle compact, élagage, instantanés, info disque
├── forge/         clones locaux, API GitHub/GitLab, catalogue, actions, OAuth
├── ui/            application Dash, figures, thèmes, fenêtre native
├── cli.py         commandes ui / scan / info
└── cli_repos.py   commandes repos
```

Le cœur ne dépend d'aucune interface : les mêmes instantanés alimentent la fenêtre et le terminal.

## Limites connues

- Les **hardlinks** sont comptés à chaque occurrence : obtenir l'inode sous Windows impose d'ouvrir
  chaque fichier, ce sera un mode « précis » explicite.
- La taille allouée arrondit au cluster mais ignore la compression NTFS et les fichiers sparse (VHD,
  bases de données), qui demandent un appel par fichier.
- Les liens et jonctions sont affichés mais jamais parcourus : leur cible est comptée là où elle
  réside, ce qui évite boucles et double comptage.
- Le scan passe par `os.scandir`. La lecture directe de la MFT, qui rendrait un volume NTFS
  analysable en quelques secondes, demande les droits administrateur et reste à faire.
