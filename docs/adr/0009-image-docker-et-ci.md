# ADR-0009 : Image Docker, compose et CI

## Statut
Accepté (2026-10-22)

## Date
2026-10-22

## Contexte
- Cible : stack *arr en conteneurs (Mac mini M4 sous OrbStack, serveurs x86) → amd64 + arm64.
- Conventions de la stack (linuxserver) : `PUID`/`PGID`, `TZ`, volumes `/config` et `/data`.
- Besoin de `ffprobe`. Hardlinks : même volume `/data` que qBittorrent.
- Publication uniquement avec l'accord de l'auteur.

## Décision

### Image
- Base `python:3.12-slim` (Debian), multi-arch native ; `apt-get install --no-install-recommends ffmpeg` (fournit `ffprobe`).
- Build en deux étapes : `pip install` dans un venv à l'étape *builder*, copie du venv et de `src/` + `web/` dans l'image finale ; pas de compilateur dans l'image finale.
- Entrée : script `entrypoint.sh` qui crée l'utilisateur `vodarr` avec `PUID`/`PGID` (défaut 1000/1000), règle les droits de `/config`, puis lance Uvicorn **sous cet utilisateur** via `setpriv` (util-linux, présent dans l'image de base). Jamais exécuté en root.
- Commande : `uvicorn vodarr.app:create_app --factory --host 0.0.0.0 --port ${VODARR_PORT} --workers 1` (l'application est construite par une fabrique, ce qui permet aux tests d'en créer une par dossier de configuration). Pas de `--proxy-headers` : l'adresse du client sert à l'anti-force brute et `X-Forwarded-For` se falsifie (ADR-0005).
- Le build multi-arch local demande un builder `docker-container` temporaire (`docker buildx create --driver docker-container`), supprimé après usage.
- `HEALTHCHECK` : `python -c` + `urllib` sur `http://127.0.0.1:8863/api/health`.
- Volumes : `/config` (base, cache, `.torrent`, playlists), `/data` (médias partagés). `EXPOSE 8863`.
- Labels OCI (source, licence MIT, version). Aucun secret, aucun chemin personnel.

### Compose d'exemple
`docker-compose.example.yml` : service `vodarr` sur le réseau de la stack, même montage `/data` que qBittorrent, montage facultatif `./cross-seed/config.js:/crossseed/config.js:ro`, variables `PUID`, `PGID`, `TZ`, `VODARR_ALLOWED_HOSTS`. Valeurs génériques uniquement.

### CI (`.github/workflows/ci.yml`)
- À chaque push et PR : `ruff`, `pytest`, `gitleaks`.
- Sur tag `v*` uniquement : `docker buildx` amd64 + arm64 (QEMU) et push sur `ghcr.io/<propriétaire>/vodarr` avec le `GITHUB_TOKEN`.
- Le workflow est écrit et vérifié localement (build multi-arch sans push) ; il ne tourne qu'une fois le dépôt publié par l'auteur.

## Alternatives envisagées

### Base linuxserver (`ghcr.io/linuxserver/baseimage-*`, s6-overlay)
- Pour : conventions natives, PUID/PGID intégrés.
- Contre : s6 et une couche d'init pour un seul processus ; dépendance à leur rythme de mise à jour.
- Rejeté : un `entrypoint.sh` de quelques lignes suffit.

### Alpine
- Contre : musl, roues Python parfois à compiler, ffmpeg différent ; gain de taille marginal face à ffmpeg.
- Rejeté.

### Binaire `ffprobe` statique téléchargé au build
- Contre : source tierce à vérifier, build moins reproductible.
- Rejeté : paquet Debian signé.

### `gosu`
- Rejeté : `setpriv` est déjà dans l'image.

## Conséquences
- Image plus lourde à cause de ffmpeg (~ quelques centaines de Mo) : accepté. limite connue : ffmpeg complet pour ffprobe seul ; passer à un ffprobe minimal si la taille de l'image pose problème.
- Docker Hub ajoutable plus tard (quelques lignes dans le workflow).
