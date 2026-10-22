# ADR-0001 : Stack technique et structure du dépôt

## Statut
Accepté (2026-10-22)

## Date
2026-10-22

## Contexte
- La logique métier de la démo (`iptv-restore.py`, `iptv-sources.py`, `iptv-dl.py`, étapes de `iptv-pipeline.py`) est en Python stdlib, testée en réel ; on veut la reprendre sans la réécrire.
- La démo tenait sur un `http.server` monolithique et des fichiers JSON écrits par plusieurs threads (écritures concurrentes fragiles).
- Contraintes : image légère multi-arch, dépendances minimales, un seul administrateur, entrées à valider aux frontières (formulaires, API, réponses des services tiers).

## Décision
- **Python 3.12**, **FastAPI** + **Uvicorn** pour le serveur web.
- **stdlib partout ailleurs** : `sqlite3` (base), `urllib.request` (HTTP sortant), `hashlib.scrypt` (mots de passe), `http.server` (faux serveurs de test), `subprocess` (`ffprobe`).
- Dépendances d'exécution : `fastapi`, `uvicorn` (versions figées dans `pyproject.toml`). Dev : `pytest`, `httpx2` (requis par `TestClient`, successeur de `httpx` depuis 2026-10-22), `ruff`. Toute autre dépendance : demander d'abord.
- **Frontend sans build** : HTML, CSS natif, JavaScript en modules ES, servis par FastAPI (`StaticFiles`).
- Structure :

```
src/vodarr/
  app.py        → application FastAPI : routes, protections HTTP, fichiers statiques
  core.py       → base SQLite, migrations, réglages, écritures atomiques, logs sans secret, codes de message
  auth.py       → admin, sessions, clé API
  sources.py    → clients Xtream / M3U, catalogue, cache
  downloads.py  → file globale, téléchargement avec reprise
  pipeline.py   → étapes analyse → recherche → vérification → restauration → seed
  torrent.py    → bdecode, infohash, association, padding, vérification des pièces
  services.py   → clients Prowlarr et qBittorrent
  crossseed.py  → lecture stricte de config.js (sans exécution)
web/            → index.html, app.js (+ modules), styles/tokens.css, styles/components.css, i18n/en.json, i18n/fr.json
tests/          → pytest ; tests/fakes/ : faux Xtream, M3U, fichiers, Prowlarr, qBittorrent
docs/adr/       → décisions d'architecture
```

Un module = un fichier tant qu'il reste sous 500 lignes ; on ne découpe qu'au dépassement.

## Alternatives envisagées

### Garder `http.server` (comme la démo)
- Pour : zéro dépendance.
- Contre : routage, validation, sessions, fichiers statiques, réponses d'erreur à réécrire à la main ; c'est là qu'étaient les failles corrigées pendant la revue de la démo.
- Rejeté : FastAPI apporte la validation Pydantic aux frontières pour deux dépendances.

### Flask / Django
- Flask : pas de validation intégrée (il faudrait une lib en plus). Django : ORM, admin, templates inutiles ici.
- Rejeté : plus lourd pour moins de valeur.

### `httpx` / `requests` pour les appels sortants
- Pour : API plus agréable.
- Contre : dépendance d'exécution en plus ; `urllib` a fait ses preuves dans la démo (reprise `Range`, redirections, 416).
- Rejeté. `httpx2` (successeur de `httpx`) reste en dépendance de dev uniquement.

### Frontend React/Vue + build
- Contre : chaîne de build, `node_modules`, image plus complexe, pour une interface de 4 écrans.
- Rejeté (voir ADR-0008).

## Conséquences
- Le code de la démo se reprend presque tel quel dans `torrent.py`, `sources.py`, `downloads.py`, `pipeline.py`.
- Pydantic valide chaque entrée de l'API ; les réponses des services tiers (Xtream, Prowlarr, qBittorrent) sont validées explicitement dans `sources.py` / `services.py`.
- OpenAPI généré gratuitement (`/docs` désactivé en production, voir ADR-0005).
