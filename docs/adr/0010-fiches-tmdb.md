# ADR-0010 : Fiches des titres via TMDB et métadonnées techniques

## Statut
Accepté (2026-10-24), version 0.1.1.

## Date
2026-10-24

## Contexte
- Au test réel, les fiches des films et séries étaient pauvres : les fournisseurs IPTV donnent peu de descriptifs.
- Une fois un fichier téléchargé, l'auteur veut voir ses caractéristiques techniques dans la fiche.
- Règle du projet : aucun secret dans le dépôt.

## Décision
- **TMDB avec la clé de l'utilisateur** : Réglages › Services › TMDB, clé API v3 ou jeton de lecture v4, masquée comme les autres secrets (le serveur étant fixe, `••••` signifie toujours « garder »). Sans clé, la fonction est absente et rien ne change.
- **Correspondance** : l'identifiant TMDB fourni par la source Xtream (`tmdb` / `tmdb_id`) quand il existe, sinon recherche par titre et année. L'identifiant est gardé dans `releases.tmdb_id`.
- **Route** `GET /api/metadata?kind=&tmdbId=&title=&year=&lang=` : synopsis, affiche, genres, note, durée ou nombre de saisons, distribution principale, dans la langue de l'interface. Réponses mises en cache 7 jours dans `/config/cache/tmdb/`.
- **Confidentialité** : les appels à l'API TMDB partent du serveur ; les affiches sont chargées par le navigateur depuis `image.tmdb.org` (documenté dans le README).
- **Métadonnées techniques** : le même appel `ffprobe` que pour le nom de release lit résolution, codec et profil, HDR, images/s, pistes audio (codec, canaux, langue), sous-titres, durée, conteneur ; stockées dans `files.media_info` et affichées dans la fiche de la release.
- Schéma : migration 2 (`files.media_info`, `releases.tmdb_id`) appliquée aux bases 0.1.0.

## Alternatives envisagées
- Clé TMDB intégrée au projet : visible de tous dans un dépôt public, quotas partagés ; rejetée.
- Interroger Sonarr/Radarr pour les fiches : dépendance à des services non requis en V1 ; rejetée.

## Conséquences
- Une fiche peut rester vide (pas de clé, titre introuvable) : l'interface affiche alors ce que fournit la source.
- `VODARR_TMDB_URL` permet de pointer un faux TMDB pour les tests et la démo.
