# Prompt à coller dans le dépôt `dev/` (sso-lab)

> À exécuter par un agent travaillant dans `~/dev`. Le résultat attendu est côté serveur ;
> le client Python correspondant vit dans `storage_analysis` et sera adapté ensuite.

---

## Contexte

Le realm Keycloak `ssolab` doit devenir le **courtier d'identité** (identity broker) du lab pour des
fournisseurs externes, en commençant par GitHub. Objectif : qu'une application — du lab ou non —
obtienne un jeton GitHub au nom de l'utilisateur **sans qu'aucun secret GitHub ne soit distribué**.

Le premier consommateur est `storage_analysis`, une application **Python de bureau** (hors lab,
hors Docker, exécutée sur le poste de l'utilisateur). C'est un type de client nouveau pour ce
realm : tous les clients existants sont des applications web avec des URIs LAN/WAN, alors que
celui-ci est un **client public natif à redirection loopback** (`http://127.0.0.1:8765/callback`).

## Ce qu'il ne faut PAS faire

- **Ne pas créer d'application Django/Angular « oauth » ni de sous-module.** Keycloak fait déjà le
  courtage ; une app maison ne ferait que le dupliquer moins bien. Aucun appel à `new-app.sh`.
- Ne pas toucher à `~/edge-router/`.
- Ne pas écrire de secret GitHub dans un fichier versionné, ni dans un exemple `.env.example`.
- Ne pas accorder `read-token` au niveau du realm ni à un client existant : uniquement, et
  explicitement, aux clients qui en ont besoin.

## Travail demandé

### 1. Nouveau script `scripts/create-idp.sh`

Même facture que `scripts/create-app-client.sh` : bash `set -euo pipefail`, `curl` + `jq`, en-tête
de commentaires, fonctions `info/success/warn/die` colorées, `usage()`, **idempotent** (crée ou met
à jour, jamais de doublon), jeton admin obtenu depuis `sso-lab/.env`.

```
Usage : ./create-idp.sh <provider> [options]
  <provider>            github (aujourd'hui) — gitlab, google prévus
  --scopes "<s1 s2>"    portées demandées au fournisseur
  --alias <a>           alias Keycloak (défaut : nom du provider)
  --no-store-tokens     ne pas stocker les jetons du fournisseur
```

Pour `github`, configurer l'identity provider avec :

- `providerId: github`, `alias: github`
- `clientId` / `clientSecret` lus dans `sso-lab/.env` (`GITHUB_IDP_CLIENT_ID`,
  `GITHUB_IDP_CLIENT_SECRET`) — ajouter ces deux clés à `init-secrets.sh` en tant que valeurs
  **saisies par l'humain**, jamais générées, jamais commitées
- `storeToken: true` et `addReadTokenRoleOnCreate: true` — ce sont les deux réglages sans lesquels
  le endpoint `/realms/ssolab/broker/github/token` renvoie 403
- `defaultScope` : `read:user repo` par défaut, surchargeable par `--scopes`
- `trustEmail: false`, `linkOnly: false`, `hideOnLoginPage: false`

Le script doit afficher, en fin d'exécution, l'URI de rappel exacte à enregistrer côté GitHub :

```
https://<DOMAIN>/realms/ssolab/broker/github/endpoint
```

en la construisant depuis la configuration existante (`KEYCLOAK_HOSTNAME_URL` / `DOMAIN`), et non
en la codant en dur.

### 2. Extensions de `scripts/create-app-client.sh`

Ajouter trois options, sans changer le comportement par défaut des apps existantes :

- `--native-redirect <uri>` (répétable) — ajoute une URI de rappel telle quelle, en plus des URIs
  LAN/WAN calculées. Nécessaire pour `http://127.0.0.1:8765/callback`, que la logique LAN/WAN
  actuelle ne sait pas produire.
- `--broker-read-token` — ajoute le rôle `read-token` du client `broker` à la portée du client.
- Sur tout client créé avec `--public`, **forcer PKCE S256** :
  `attributes["pkce.code.challenge.method"] = "S256"`. Un client public sans PKCE est vulnérable à
  l'interception du code d'autorisation ; ce doit être le défaut, pas une option.

Vérifier que les clients publics existants ne sont pas cassés par l'ajout de PKCE : les
frontends Angular du lab doivent continuer à fonctionner (le mettre dans le plan de test).

### 3. Client Keycloak `storage-analysis`

```bash
bash scripts/create-app-client.sh storage-analysis \
  --public \
  --native-redirect 'http://127.0.0.1:8765/callback' \
  --broker-read-token \
  --require-group developers \
  --no-wan
```

Pas de port, pas de docker-compose, pas d'entrée dans `.ports` : ce client n'a pas de service
hébergé. Si `create-app-client.sh` suppose aujourd'hui l'existence d'un dossier d'application ou
d'un `.keycloak-client-opts`, prévoir ce cas sans dégrader le chemin nominal.

Le cloisonnement reste obligatoire (`--require-group`) : le Verrou 1 s'applique aussi à un client
natif, l'utilisateur passe bien par le navigateur.

### 4. Documentation

- `README.md` : nouvelle sous-section sous « Créer une nouvelle application », intitulée
  « Fournisseurs externes (GitHub, GitLab…) », décrivant l'enregistrement de l'app OAuth côté
  fournisseur, l'usage de `create-idp.sh`, et le endpoint de récupération du jeton.
- `CLAUDE.md` : consigner la décision et son motif — *pourquoi Keycloak plutôt qu'un service OAuth
  maison*, et **pourquoi `read-token` se donne client par client** (le endpoint broker rend le jeton
  amont brut : tout client qui l'obtient agit sur GitHub au nom de l'utilisateur).
- Documenter la limite connue : un jeton d'application OAuth GitHub **n'expire pas** par défaut,
  Keycloak stocke donc un identifiant durable. Noter la piste GitHub App (jetons à 8 h + refresh)
  comme évolution possible, sans l'implémenter.

### 5. Vérification à produire

1. `create-idp.sh github` sur un realm où l'IdP existe déjà → aucune duplication, sortie explicite.
2. Connexion navigateur via GitHub sur un client du lab → l'utilisateur est fédéré dans `ssolab`.
3. `curl` sur `/realms/ssolab/broker/github/token` avec un jeton `storage-analysis` valide →
   renvoie le jeton GitHub. Le même appel avec un jeton d'un client **sans** `read-token` → 403.
4. Cloisonnement dans les deux sens : membre de `developers` accepté, non-membre refusé, **y
   compris avec une session SSO active** (voir le piège documenté du contournement par `Cookie`).
5. Les frontends Angular existants fonctionnent toujours après l'activation de PKCE.

Utiliser le runner Playwright pour les points 4 et 5 si un test de cloisonnement existant peut
servir de modèle.

## Contrat d'interface pour le client Python

À figer dans la documentation, car `storage_analysis` s'y branchera :

| Élément | Valeur |
|---|---|
| Issuer | `https://<DOMAIN>/realms/ssolab` |
| Client ID | `storage-analysis` (public, sans secret) |
| Flux | authorization code + PKCE S256 |
| Redirect URI | `http://127.0.0.1:8765/callback` |
| Jeton amont | `GET /realms/ssolab/broker/github/token`, en-tête `Authorization: Bearer <jeton Keycloak>` |
| Réponse | JSON de GitHub : `access_token`, `token_type`, `scope` |
