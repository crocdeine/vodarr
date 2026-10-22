# ADR-0005 : Authentification, protections HTTP et secrets

## Statut
Accepté (2026-10-22)

## Date
2026-10-22

## Contexte
- En conteneur, VODarr est joignable par tout le réseau Docker : l'exemption « adresses locales » des *arr reviendrait à n'avoir aucune auth.
- Secrets manipulés : identifiants Xtream (aussi dans les URL de téléchargement), URL M3U, clé Prowlarr (aussi dans `downloadUrl`), identifiants qBittorrent, passkeys dans les `.torrent`.
- Failles trouvées et corrigées pendant la revue de la démo : fuite de la clé Prowlarr vers le navigateur, absence d'auth, restauration d'un secret masqué vers un hôte changé.

## Décision

### Authentification
- Un admin unique (`admin.id = 1`), créé au premier lancement ; mot de passe ≥ 12 caractères, haché par `hashlib.scrypt` (n=2¹⁴, r=8, p=1, sel aléatoire de 16 octets), comparaison en temps constant (`hmac.compare_digest`).
- Session : jeton aléatoire de 32 octets dans un cookie `vodarr_session` (`HttpOnly`, `SameSite=Strict`, `Secure` si la requête arrive en HTTPS ou via `X-Forwarded-Proto: https`) ; seul son SHA-256 est stocké ; durée 30 jours glissants ; déconnexion = suppression en base.
- Clé API : 32 octets aléatoires, en-tête `X-Api-Key`, comparée en temps constant. Stockée **en clair** dans la base (`600`) pour pouvoir être réaffichée : masquée dans les réglages, affichée en clair seulement après saisie du mot de passe admin (`POST /api/auth/api-key/reveal`, soumis au même anti-force brute que la connexion). Régénérable à tout moment.
- Anti-force brute (révisé le 2026-10-22, décision de l'auteur) : compteur **par adresse client** — 5 échecs en 5 min → cette adresse est refusée 5 min — et plafond **global** de 50 échecs en 5 min (toutes adresses) contre un attaquant distribué. Un client ne peut donc plus bloquer l'admin à lui seul. Derrière un reverse proxy, tous les clients partagent l'adresse du proxy (`X-Forwarded-For` n'est pas cru, il se falsifie). Vérifications de mot de passe sérialisées. limite connue : compteur en mémoire, remis à zéro au redémarrage ; le persister si des attaques réelles le contournent.
- Premier lancement (décision du 2026-10-22) : la création de l'admin exige un **code à usage unique** affiché dans le journal du conteneur au démarrage (`docker logs vodarr`), ou fixé par `VODARR_SETUP_TOKEN`. Sans lui, n'importe qui sur le réseau Docker pourrait s'approprier une instance neuve. Le code est soumis au même anti-force brute et disparaît une fois l'admin créé.
- Aucune exemption par IP.

### Protections HTTP (middleware unique dans `app.py`)
- `Host` hors liste autorisée → 421 (anti DNS-rebinding). Liste : `localhost`, `127.0.0.1`, nom d'hôte du conteneur, plus `VODARR_ALLOWED_HOSTS` (virgules).
- Requête modifiante authentifiée par cookie : `Content-Type: application/json` obligatoire et `Origin` égale à l'hôte, sinon 403 (anti-CSRF). Les appels par clé API n'ont pas de cookie, donc pas de CSRF.
- Corps > 1 Mio → 413.
- En-têtes : `Content-Security-Policy: default-src 'self'; img-src 'self' https: data:` (affiches des catalogues), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`.
- `/docs`, `/redoc`, `/openapi.json` désactivés, sauf `VODARR_DEV=1`.

### Secrets
- Stockés dans `vodarr.db` (`600`, dossier `/config` `700`). Pas de chiffrement applicatif : la clé serait sur le même disque.
- Jamais renvoyés par l'API : remplacés par `"••••"`. À l'enregistrement, `"••••"` conserve l'ancien secret **seulement si** le serveur/l'URL du service est inchangé ; sinon 422 `settings.secret_required` (empêche de rediriger un secret vers un serveur choisi par un attaquant).
- URL contenant des identifiants (Xtream, M3U, `downloadUrl`) construites et utilisées côté serveur uniquement.
- Logs : filtre `logging` qui remplace toute valeur de secret connue (lue dans les réglages) par `••••` ; les URL sont journalisées sans requête ni identifiants (schéma + hôte + chemin masqué).
- Les `.torrent` de référence (passkey) restent dans `/config/torrents/` (`600`), jamais servis par l'API.

### Entrées non fiables
- Réponses Xtream, M3U, Prowlarr, qBittorrent : validation de forme avant usage ; une réponse invalide n'est pas mise en cache.
- Noms de fichiers issus des sources : caractères interdits remplacés, extension en liste blanche, chemin final vérifié sous le dossier de téléchargement (`Path.resolve()` + `is_relative_to`).
- M3U : schémas `http`/`https`, ou fichier sous `/config/playlists` ; taille ≤ 50 Mio lue en flux.
- `config.js` de cross-seed : analyse des littéraux uniquement, jamais exécuté (ADR-0006).

## Alternatives envisagées

### Exemption « adresses locales » (comme les *arr)
- Rejeté : tout le réseau Docker est « local ».

### Argon2 / bcrypt
- Contre : dépendance compilée (multi-arch) pour un seul compte.
- Rejeté : scrypt de la stdlib est un KDF résistant aux GPU.

### JWT
- Contre : révocation compliquée, aucun besoin de jeton sans état.
- Rejeté : sessions en base.

### Chiffrement des secrets au repos
- Contre : la clé serait dans `/config` à côté de la base ; sécurité illusoire.
- Rejeté : permissions `600`/`700`, documenté dans le README.

## Conséquences
- Accès distant : derrière le reverse proxy ou le VPN de l'utilisateur (documenté) ; VODarr ne gère pas TLS.
- Test transversal (AC-CORE-04, AC-UI-09) : parcours complet sur faux serveurs avec des secrets connus, recherche de ces secrets dans les logs et toutes les réponses.
- Revue `security-reviewer` obligatoire sur `auth.py`, le middleware et les sérialiseurs.
